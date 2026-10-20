"""classify.py -- data/articles_raw.csv -> data/articles.csv with AI tags (window A).

One fixed prompt, taxonomy from docs/BRIEF.md, batches of 10 articles (title + summary
only), structured JSON output. Idempotent: ids already in data/articles.csv are skipped.
Spend guard: stops before cumulative USD in runs/log.csv would reach SPEND_STOP_USD.
collect.py owns `entity` (from the query group) and a fixed `topic` (queries.yaml `topics`
phrases): a non-blank value in articles_raw.csv is kept; the classifier fills only blanks.

Sentiment-only mode (--sentiment-only): re-tags only the `sentiment` column of every row in
data/articles.csv (title + summary, same batching and retries); every other column is left as
it is. Logged as step classify_sentiment; stops before this run reaches SENTIMENT_RUN_CAP_USD
(and before the cumulative SPEND_STOP_USD).

Run: python classify.py
     python classify.py --sentiment-only
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import sys
from pathlib import Path

import anthropic
import pandas as pd

ROOT = Path(__file__).resolve().parent
RAW_CSV = ROOT / "data" / "articles_raw.csv"
OUT_CSV = ROOT / "data" / "articles.csv"
LOG_CSV = ROOT / "runs" / "log.csv"
ENV_FILE = ROOT / ".env"

COLUMNS = [
    "id", "published", "source", "outlet", "outlet_tier", "title", "url", "summary",
    "language", "company", "topic", "entity", "sentiment", "confidence", "flag",
    "url_source", "url_resolved",   # set by collect.py, passed through
]
LOG_HEADER = ["date", "step", "rows", "tokens_in", "tokens_out", "usd"]

MODEL = "claude-sonnet-5-5"
USD_PER_MTOK_IN = 2.0
USD_PER_MTOK_OUT = 10.0
SPEND_STOP_USD = 8.00      # stop cleanly before cumulative spend reaches this
PROJECT_CAP_USD = 15.00    # hard project cap (never reached if the stop above holds)
SENTIMENT_RUN_CAP_USD = 2.00   # per-run stop for --sentiment-only
BATCH_SIZE = 10
MAX_TOKENS = 4000
JSON_RETRIES = 2           # extra attempts per batch on malformed / unusable output
CAPS = {"prosus": 250, "peer_deliveryhero": 150, "peer_zalando": 150}
DEFAULT_PEER_CAP = 150

LANGUAGES = ["en", "nl", "pt", "hi", "other"]
TOPICS = ["results", "tencent_stake", "ai", "jet_integration", "ventures_investments",
          "governance_regulation", "other"]
ENTITIES = ["company", "bloisi", "sgourdos", "kolek", "beinat", "portfolio_brand", "none"]
SENTIMENTS = ["positive", "neutral", "negative"]
FLAGS = ["issue", "opportunity", ""]
COMPANY_NAMES = {"prosus": "Prosus", "peer_deliveryhero": "Delivery Hero",
                 "peer_zalando": "Zalando"}

SENTIMENT_RULE = """sentiment: tone of the article toward the company named for that article \
(not toward the market in general): positive, neutral, negative. Judge English and Dutch \
articles the same way. Neutral is the default for routine coverage: funding announcements, \
investments made, analyst notes and ratings, results reporting, share price and market moves, executive appointments, and product \
launches reported factually. Use positive only when the article clearly frames the named \
company favourably (e.g. praise, a win or success attributed to the company); use negative only \
when it clearly frames the company unfavourably (e.g. criticism, a loss or failure attributed to \
the company, a scandal, a fine, a lawsuit, layoffs presented as trouble). If in doubt: neutral."""

SYSTEM_PROMPT = """You tag news headlines for a corporate communications team that monitors \
Prosus (the Amsterdam-listed technology investor, majority-owned by Naspers) and two peers: \
Delivery Hero and Zalando. Each article has an id, the company it was collected for, a title \
and a short feed summary. Articles may be in English, Dutch or another language; read them \
in their own language and judge them the same way. Use only the title and summary; do not \
assume facts that are not in them.

For every article return exactly one object with these fields:

language: language of the title/summary. en, nl, pt (Portuguese), hi (Hindi), or other.

topic (the main subject, as it relates to the company named):
- results: financial results, trading updates, guidance, earnings dates, share price moves, \
analyst ratings, buybacks, dividends, credit ratings.
- tencent_stake: Prosus/Naspers holding in Tencent, sales of Tencent shares, Tencent-linked \
valuation discount.
- ai: artificial intelligence products, AI strategy, AI agents, Toqan, AI House, Luminate, \
large language models, AI investments.
- jet_integration: Just Eat Takeaway.com (JET) acquisition by Prosus, its integration, \
regulatory approval or remedies of that deal.
- ventures_investments: investments, acquisitions, disposals, funding rounds and portfolio \
companies (e.g. iFood, Swiggy, OLX, PayU, Meesho, Stack Overflow, Prosus Ventures), India, \
Latin America. For peers: their acquisitions, disposals and stakes.
- governance_regulation: board and executive changes, governance, shareholder structure, \
regulation, antitrust, lawsuits, tax, labour disputes.
- other: anything else (including articles that barely concern the company).

entity (who the article is mainly about):
- company: the company named (Prosus, Delivery Hero or Zalando) as an organisation.
- bloisi: Fabricio Bloisi. sgourdos: Basil Sgourdos. kolek: Patrick Kolek. beinat: Euro Beinat.
- portfolio_brand: a Prosus portfolio company or brand (iFood, Swiggy, OLX, PayU, Just Eat \
Takeaway, Meesho, Stack Overflow, Tencent, etc.). For peers: one of the peer's own brands \
(e.g. Glovo, Talabat, Baemin/Woowa, foodpanda for Delivery Hero; About You, Lounge for Zalando).
- none: no clear main entity.

{SENTIMENT_RULE}

confidence: your confidence in the topic and sentiment together, a number from 0 to 1.

flag: should the company's communications team look at this?
- issue: reputational risk or criticism that may need a response (regulatory action, \
lawsuit, layoffs, strike, executive controversy, sharp negative coverage in a major outlet).
- opportunity: a positive story the team could amplify or a media opening (award, \
positive feature, milestone, favourable analyst move worth sharing).
- "" (empty string): nothing to escalate. Most routine articles get "".

Return JSON only: {"articles": [ ...one object per input article, same ids... ]}."""
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{SENTIMENT_RULE}", SENTIMENT_RULE)

SENTIMENT_SYSTEM_PROMPT = """You judge the tone of news headlines for a corporate communications \
team that monitors Prosus (the Amsterdam-listed technology investor, majority-owned by Naspers) \
and two peers: Delivery Hero and Zalando. Each article has an id, the company it was collected \
for, a title and a short feed summary. Articles may be in English, Dutch or another language; \
read them in their own language. Use only the title and summary; do not assume facts that are \
not in them.

For every article return exactly one object with its id and:

""" + SENTIMENT_RULE + """

Return JSON only: {"articles": [ ...one object per input article, same ids... ]}."""

ARTICLE_SCHEMA = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "language": {"type": "string", "enum": LANGUAGES},
                    "topic": {"type": "string", "enum": TOPICS},
                    "entity": {"type": "string", "enum": ENTITIES},
                    "sentiment": {"type": "string", "enum": SENTIMENTS},
                    "confidence": {"type": "number"},
                    "flag": {"type": "string", "enum": FLAGS},
                },
                "required": ["id", "language", "topic", "entity", "sentiment",
                             "confidence", "flag"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["articles"],
    "additionalProperties": False,
}


SENTIMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "articles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "sentiment": {"type": "string", "enum": SENTIMENTS},
                },
                "required": ["id", "sentiment"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["articles"],
    "additionalProperties": False,
}


def load_env() -> None:
    """Tiny .env parser: KEY=VALUE lines; does not override existing env vars."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def logged_usd() -> float:
    if not LOG_CSV.exists():
        return 0.0
    with LOG_CSV.open(newline="", encoding="utf-8") as f:
        return sum(float(r.get("usd") or 0) for r in csv.DictReader(f))


def append_log(step: str, rows: int, tin: int, tout: int, usd: float) -> None:
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not LOG_CSV.exists() or LOG_CSV.stat().st_size == 0
    with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(LOG_HEADER)
        w.writerow([dt.date.today().isoformat(), step, rows, tin, tout, f"{usd:.4f}"])


def cost(tin: int, tout: int) -> float:
    return tin / 1e6 * USD_PER_MTOK_IN + tout / 1e6 * USD_PER_MTOK_OUT


def validate(obj: dict) -> dict | None:
    """Return a cleaned tag dict or None if any field is missing/invalid."""
    try:
        conf = float(obj["confidence"])
        tags = {
            "language": obj["language"], "topic": obj["topic"], "entity": obj["entity"],
            "sentiment": obj["sentiment"], "flag": obj["flag"] if obj["flag"] is not None else "",
        }
    except (KeyError, TypeError, ValueError):
        return None
    if (tags["language"] not in LANGUAGES or tags["topic"] not in TOPICS
            or tags["entity"] not in ENTITIES or tags["sentiment"] not in SENTIMENTS
            or tags["flag"] not in FLAGS or not math.isfinite(conf) or not 0 <= conf <= 1):
        return None
    tags["confidence"] = round(conf, 2)
    return tags


def validate_sentiment(obj: dict) -> dict | None:
    s = obj.get("sentiment") if isinstance(obj, dict) else None
    return {"sentiment": s} if s in SENTIMENTS else None


class Classifier:
    def __init__(self, spent_before: float, system: str = SYSTEM_PROMPT,
                 schema: dict = ARTICLE_SCHEMA, validator=validate,
                 run_cap: float | None = None, instruction: str = "Tag these articles."):
        self.client = anthropic.Anthropic(max_retries=4, timeout=120)
        self.spent_before = spent_before
        self.system, self.schema, self.validator = system, schema, validator
        self.run_cap, self.instruction = run_cap, instruction
        self.tin = self.tout = 0
        self.structured = True     # flips to False if the API rejects output_config.format
        self.effort = True         # flips to False if the API rejects output_config.effort
        self.stopped = False

    @property
    def usd(self) -> float:
        return cost(self.tin, self.tout)

    def _guard(self, user_text: str) -> bool:
        # conservative: ~3 chars/token for input incl. system prompt, full max_tokens output
        est = cost((len(self.system) + len(user_text)) // 3 + 500, MAX_TOKENS)
        if self.run_cap is not None and self.usd + est >= self.run_cap:
            print(f"RUN CAP: this run ${self.usd:.4f} + next call est ${est:.4f} would reach "
                  f"${self.run_cap:.2f}; stopping cleanly.")
            self.stopped = True
            return False
        total = self.spent_before + self.usd + est
        if total >= SPEND_STOP_USD or total >= PROJECT_CAP_USD:
            print(f"SPEND GUARD: cumulative ${self.spent_before + self.usd:.4f} + next call "
                  f"est ${est:.4f} would reach ${SPEND_STOP_USD:.2f}; stopping cleanly.")
            self.stopped = True
            return False
        return True

    def _call(self, user_text: str):
        while True:
            oc: dict = {}
            if self.effort:
                oc["effort"] = "low"
            if self.structured:
                oc["format"] = {"type": "json_schema", "schema": self.schema}
            kwargs = dict(model=MODEL, max_tokens=MAX_TOKENS, system=self.system,
                          messages=[{"role": "user", "content": user_text}])
            if oc:
                kwargs["output_config"] = oc
            try:
                return self.client.messages.create(**kwargs)
            except anthropic.BadRequestError as e:
                msg = str(e).lower()
                if self.structured and ("format" in msg or "schema" in msg or "output_config" in msg):
                    print(f"Structured output rejected ({str(e)[:160]}); falling back to JSON instruction.")
                    self.structured = False
                    continue
                if self.effort and "effort" in msg:
                    print(f"effort rejected ({str(e)[:160]}); retrying without it.")
                    self.effort = False
                    continue
                raise

    def classify_batch(self, rows: list[dict]) -> dict[str, dict]:
        """Return {id: tags} for valid results. Retries malformed output up to JSON_RETRIES."""
        # short per-batch refs ("1".."10") instead of 40-char sha1 ids: fewer tokens, no copy errors
        ref_to_id = {str(i + 1): r["id"] for i, r in enumerate(rows)}
        payload = [{"id": str(i + 1), "company": COMPANY_NAMES.get(r["company"], r["company"]),
                    "title": r["title"], "summary": r["summary"]} for i, r in enumerate(rows)]
        user_text = (f"{self.instruction} Return one object per id.\n"
                     + json.dumps(payload, ensure_ascii=False))
        if not self.structured:
            user_text += ("\nRespond with a single JSON object and nothing else, exactly "
                          "matching this JSON schema:\n" + json.dumps(self.schema))
        for attempt in range(1 + JSON_RETRIES):
            if not self._guard(user_text):
                return {}
            resp = self._call(user_text)
            self.tin += resp.usage.input_tokens
            self.tout += resp.usage.output_tokens
            if resp.stop_reason in ("refusal", "max_tokens"):
                print(f"  batch attempt {attempt + 1}: stop_reason={resp.stop_reason}")
                continue
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
            if text.startswith("```"):
                text = text.strip("`").removeprefix("json").strip()
            try:
                items = json.loads(text)["articles"]
                assert isinstance(items, list)
            except (json.JSONDecodeError, KeyError, TypeError, AssertionError):
                print(f"  batch attempt {attempt + 1}: malformed JSON")
                continue
            out = {}
            for obj in items:
                if isinstance(obj, dict) and str(obj.get("id")) in ref_to_id:
                    tags = self.validator(obj)
                    if tags:
                        out[ref_to_id[str(obj["id"])]] = tags
            return out
        return {}


def run_passes(clf: Classifier, pending: list[dict], results: dict[str, dict]) -> None:
    for pass_no in (1, 2):   # pass 2 = one retry for rows that came back invalid/missing
        retry = []
        for i in range(0, len(pending), BATCH_SIZE):
            if clf.stopped:
                return
            batch = pending[i:i + BATCH_SIZE]
            got = clf.classify_batch(batch)
            results.update(got)
            retry.extend(r for r in batch if r["id"] not in got)
            print(f"pass {pass_no} batch {i // BATCH_SIZE + 1}: {len(got)}/{len(batch)} valid; "
                  f"spend so far ${clf.usd:.4f}")
        if not retry:
            return
        pending = retry


COLLECT_OWNED_IF_SET = ["entity", "topic"]


def keep_collect_owned(row: dict, raw_row: dict) -> None:
    """entity and topic from collect.py win when set; the classifier fills only blanks."""
    for c in COLLECT_OWNED_IF_SET:
        if raw_row.get(c, ""):
            row[c] = raw_row[c]


def select_rows(raw: pd.DataFrame, done: set[str]) -> pd.DataFrame:
    todo = raw[~raw["id"].isin(done)].sort_values(["published", "id"], ascending=False)
    # prosus first, so a spend-guard stop leaves the main company fully classified
    order = sorted(todo["company"].unique(), key=lambda c: (c != "prosus", c))
    parts = [todo[todo["company"] == c].head(CAPS.get(c, DEFAULT_PEER_CAP)) for c in order]
    return pd.concat(parts) if parts else todo.head(0)


def main() -> int:
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set (env or .env); cannot classify.")
        return 1
    if not RAW_CSV.exists():
        print(f"{RAW_CSV} missing; run collect.py first.")
        return 1

    raw = pd.read_csv(RAW_CSV, dtype=str, keep_default_na=False)
    existing = (pd.read_csv(OUT_CSV, dtype=str, keep_default_na=False)
                if OUT_CSV.exists() else pd.DataFrame(columns=COLUMNS))
    done = set(existing["id"])
    todo = select_rows(raw, done)
    print(f"raw rows: {len(raw)}; already classified: {int(raw['id'].isin(done).sum())}; "
          f"to classify this run (after caps): {len(todo)} "
          f"{todo['company'].value_counts().to_dict()}")

    spent_before = logged_usd()
    if spent_before >= SPEND_STOP_USD:
        print(f"Cumulative spend ${spent_before:.4f} already at/over ${SPEND_STOP_USD:.2f}; nothing done.")
        append_log("classify", 0, 0, 0, 0.0)
        return 0

    clf = Classifier(spent_before)
    records = todo.to_dict("records")
    results: dict[str, dict] = {}
    pending = records
    crashed = None
    try:
        run_passes(clf, pending, results)
    except Exception as e:  # noqa: BLE001 -- persist results and log spend, then re-raise
        crashed = e

    new_rows = []
    for r in records:
        if r["id"] in results:
            row = {c: r.get(c, "") for c in COLUMNS}
            row.update(results[r["id"]])
            keep_collect_owned(row, r)
            new_rows.append(row)
    failed = len(records) - len(new_rows) if not (clf.stopped or crashed) else None

    new_df = pd.DataFrame(new_rows, columns=COLUMNS)
    merged = (pd.concat([existing, new_df], ignore_index=True) if len(existing) else new_df)
    merged = merged.drop_duplicates("id", keep="first")
    # refresh collect-owned columns from the current raw file (classifier columns kept)
    owned = ["published", "source", "outlet", "outlet_tier", "title", "url", "summary", "company",
             "url_source", "url_resolved"]
    rawi = raw.drop_duplicates("id").set_index("id")
    # columns added downstream (e.g. stories.py) are kept, after the schema columns
    out_cols = COLUMNS + [c for c in existing.columns if c not in COLUMNS]
    merged = merged.reindex(columns=out_cols).fillna("")
    m = merged["id"].isin(rawi.index)
    for c in owned:
        if c in rawi.columns:
            merged.loc[m, c] = merged.loc[m, "id"].map(rawi[c]).values
    for c in COLLECT_OWNED_IF_SET:   # collect's entity / fixed topic win over earlier tags
        if c in rawi.columns:
            src = merged.loc[m, "id"].map(rawi[c])
            merged.loc[m, c] = src.where(src != "", merged.loc[m, c]).values
    # keep only ids present in the current raw file (filtered-out rows leave the output)
    before = len(merged)
    merged = merged[merged["id"].isin(set(raw["id"]))]
    removed_not_raw = before - len(merged)
    # flags are for the Prosus comms team: blank for every non-prosus row
    peer_flagged = int(((merged["company"] != "prosus") & (merged["flag"] != "")).sum())
    merged.loc[merged["company"] != "prosus", "flag"] = ""
    print(f"rows removed (id not in articles_raw.csv): {removed_not_raw}; "
          f"non-prosus flags blanked: {peer_flagged}")
    merged = merged.sort_values(["published", "id"], ascending=False)[out_cols]
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(OUT_CSV, index=False)

    append_log("classify", len(new_rows), clf.tin, clf.tout, clf.usd)
    print(f"\nclassified this run: {len(new_rows)}; "
          + (f"failed/invalid after retry: {failed}" if failed is not None
             else f"not classified (spend guard or crash): {len(records) - len(new_rows)}"))
    print(f"structured output: {'yes' if clf.structured else 'no (JSON-instruction fallback)'}; "
          f"effort low: {'yes' if clf.effort else 'no'}")
    print(f"tokens in {clf.tin}, out {clf.tout}, usd {clf.usd:.4f}; "
          f"cumulative usd {spent_before + clf.usd:.4f}")
    print(f"data/articles.csv rows: {len(merged)} {merged['company'].value_counts().to_dict()}")
    if crashed:
        raise crashed
    return 0


def sentiment_counts(df: pd.DataFrame) -> dict:
    return {c: {s: int(((df["company"] == c) & (df["sentiment"] == s)).sum()) for s in SENTIMENTS}
            for c in sorted(df["company"].unique())}


def sentiment_only() -> int:
    """Re-tag the sentiment column of every row in data/articles.csv; nothing else changes."""
    load_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY not set (env or .env); cannot classify.")
        return 1
    if not OUT_CSV.exists():
        print(f"{OUT_CSV} missing; run classify.py first.")
        return 1
    df = pd.read_csv(OUT_CSV, dtype=str, keep_default_na=False)
    before = sentiment_counts(df)
    spent_before = logged_usd()
    if spent_before >= SPEND_STOP_USD:
        print(f"Cumulative spend ${spent_before:.4f} already at/over ${SPEND_STOP_USD:.2f}; nothing done.")
        append_log("classify_sentiment", 0, 0, 0, 0.0)
        return 0
    clf = Classifier(spent_before, system=SENTIMENT_SYSTEM_PROMPT, schema=SENTIMENT_SCHEMA,
                     validator=validate_sentiment, run_cap=SENTIMENT_RUN_CAP_USD,
                     instruction="Judge the sentiment of these articles.")
    records = df.to_dict("records")
    results: dict[str, dict] = {}
    crashed = None
    try:
        run_passes(clf, records, results)
    except Exception as e:  # noqa: BLE001 -- persist results and log spend, then re-raise
        crashed = e
    new = df["id"].map(lambda i: results[i]["sentiment"] if i in results else None)
    changed = int((new.notna() & (new != df["sentiment"])).sum())
    df["sentiment"] = new.where(new.notna(), df["sentiment"])
    df.to_csv(OUT_CSV, index=False)
    append_log("classify_sentiment", len(results), clf.tin, clf.tout, clf.usd)
    print(f"\nre-tagged: {len(results)}/{len(df)}; sentiment changed: {changed}")
    print(f"before: {before}")
    print(f"after:  {sentiment_counts(df)}")
    print(f"tokens in {clf.tin}, out {clf.tout}, usd {clf.usd:.4f}; "
          f"cumulative usd {spent_before + clf.usd:.4f}")
    if crashed:
        raise crashed
    return 0


if __name__ == "__main__":
    sys.exit(sentiment_only() if "--sentiment-only" in sys.argv[1:] else main())

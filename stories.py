"""stories.py -- add on_target, story_id and is_canonical to data/articles.csv (window F).

on_target (prosus rows only; blank for peers): yes if title or summary names Prosus, Naspers,
Prosus Ventures, AI House, Toqan or Luminate as a whole word (case-insensitive), else no.

Story dedupe over on-target Prosus rows plus all peer rows, per company:
  - normalise each title: strip accents, lowercase, drop the outlet suffix (" - <outlet>" or
    " | <outlet>" when it matches the row's outlet), apply company aliases (Just Eat /
    Thuisbezorgd / Prosus Food Delivery -> jet), drop punctuation, EN+NL stopwords, amount units,
    the company's own name words and 1-char tokens; map Dutch/English event words to one token
    (SYNONYMS: groei/growth, aandeel/shares, benoemt/names, AI-team/leadership, ...).
  - similarity = IDF-weighted overlap coefficient of the two token sets:
        sum(idf of shared tokens) / min(sum(idf of A), sum(idf of B))
    IDF is computed within each company's clustered rows, so words common in that company's
    coverage weigh little and names like "cosmochute" weigh a lot.
  - a pair (same company, published <= 3 days apart) links when
      A  similarity >= 0.5 with >= 2 shared tokens, or
      B  similarity >= 0.3 with >= 2 shared rare tokens (in <= 10% of the company's rows), or
      C  it shares a name (a word never written lowercase and capitalised mid-title in a
         sentence-case title, in <= 15% of rows; e.g. Nalden, WeTransfer, Oolka) and the two
         rows are <= 1 day apart;
    never between a funding-round title and a non-funding title.
  - links are applied strongest first (union-find); a link is skipped when the merged story
    would have a row more than 3 days after the story's earliest row. Canonical row per
    story = best outlet_tier (tier1 > tier2 > tier3), then earliest published, then id asc.
story_id = id of the canonical row. Off-target rows and singletons are their own story
(is_canonical = yes). Idempotent: the output depends only on the input columns.

Run: python stories.py   (writes data/articles.csv, prints every multi-row cluster)
     python stories.py --dry-run [--threshold 0.5]   (print only, for tuning)
"""

from __future__ import annotations

import math
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
CSV = ROOT / "data" / "articles.csv"
NEW_COLS = ["on_target", "story_id", "is_canonical"]

SIM_THRESHOLD = 0.5      # rule A: similarity >= 0.5 and >= MIN_SHARED shared tokens
ANCHOR_SIM = 0.3         # rule B: similarity >= 0.3 and >= MIN_SHARED shared rare tokens
RARE_SHARE = 0.10        #   rare = in <= 10% of that company's clustered rows
NAME_SHARE = 0.15        # rule C: a shared name (see names()) in <= 15% of the company's rows,
NAME_DAYS = 1            #   published <= 1 day apart
MIN_SHARED = 2
MAX_DAYS = 3             # every row of a story is <= 3 days from the story's earliest row
TIER_RANK = {"tier1": 1, "tier2": 2, "tier3": 3}

ON_TARGET_RE = re.compile(r"\b(Prosus Ventures|Prosus|Naspers|AI House|Toqan|Luminate)\b", re.I)

COMPANY_WORDS = {
    "prosus": {"prosus", "naspers"},
    "peer_deliveryhero": {"delivery", "hero"},
    "peer_zalando": {"zalando"},
}

STOPWORDS = set("""
a an the and or but if of to in on at by for from with without into onto over under about after
before as is are was were be been being has have had do does did will would shall should can could
may might must it its it's this that these those there their they them he she his her we our you
your i me my not no than then so such too very via vs up down out off new more most also just
amid how what why who whom which when where while all any each both few other some own same s
de het een en of in op aan bij voor van met naar uit om te tot door over onder na als is zijn was
waren wordt worden werd heeft hebben had dat die deze dit er hun hij zij ze we wij je jij u ik
niet geen dan ook nog wel al maar want dus zo meer meest nu toe weer hoe wat waarom wie welke
waar wanneer terwijl alle elk elke ander andere eigen zelf zich bij tegen sinds per nieuw nieuwe
billion million thousand crore lakh rs inr usd eur us
""".split())   # last line: amount units and currencies (the number itself is kept)

# Event words: Dutch and English variants -> one token, so NL and EN titles of one event match.
SYNONYMS = {}
for canon, words in {
    "growth": "groei groeit grows grow growth",
    "returns": "keert return returns returned",
    "orders": "bestellingen orders order",
    "decline": "krimp decline declines",
    "months": "maanden months month",
    "stock": "aandeel aandelen share shares koers stock damrak aex beurs",
    "rise": "uitblinker omhoog stijgt hoger impuls schiet jumps surges soars rises rise gains "
            "climbs groen groene",
    "fall": "daalt lager verliest fall falls drops slides tumbles",
    "names": "benoemt names appoints appointed presenteert",
    "leadership": "team topteam leaders leadership chief chiefs officer executive executives",
    "founder": "oprichter founder cofounder",
    "technology": "tech technologie technology",
    "hires": "haalt hires hire hired",
    "joins": "start overstap joins joined",
    "invest": "investeert invest investment investing investering bet bets",
    "funding": "financiering funding",
}.items():
    SYNONYMS.update({w: canon for w in words.split()})

# Multi-word aliases, applied to the lowercased title before tokenising (per company).
PHRASES = {
    "prosus": [(r"just eat takeaway(\.com)?|just eat|thuisbezorgd|\bjet\b|prosus food delivery",
                " jet "),
               (r"\b(steps|dials|ramps) up\b|\bdoubles down\b", " increase "),
               (r"\bco[- ]founder", " founder ")],
}
ALIAS_NAMES = {"prosus": {"jet"}}   # alias tokens that count as names regardless of frequency

# Capitalised words that are not event-specific names.
NOT_NAMES = {"ai", "ceo", "cfo", "cto", "cpo", "coo", "ipo", "ventures", "series", "eu", "us",
             "uk", "news", "analysis", "opinion", "reg", "exclusive", "update", "lead", "key",
             "weekly", "recap", "se", "nv"}

# A funding-round title never links to a non-funding title (keeps different events apart).
FUNDING_WORDS = {"raises", "raise", "raising", "raised", "funding", "seed", "invest", "secures",
                 "bags", "lands", "backs", "series"}

UNITS = {"b": "billion", "bn": "billion", "m": "million", "mn": "million", "k": "thousand",
         "cr": "crore"}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", strip_accents(s).lower())).strip()


def drop_outlet_suffix(title: str, outlet: str) -> str:
    m = re.match(r"^(.*\S)\s+[-|]\s+([^-|]+)$", title)
    if m and outlet and norm_text(m.group(2)) == norm_text(outlet):
        return m.group(1)
    return title


def tokens(title: str, outlet: str, company: str) -> frozenset[str]:
    t = strip_accents(drop_outlet_suffix(title, outlet)).lower()
    for pat, rep in PHRASES.get(company, []):
        t = re.sub(pat, rep, t)
    t = re.sub(r"(\d)[.,](\d)", r"\1\2", t)            # 1.5 -> 15, 33,50 -> 3350
    t = re.sub(r"[’'`]s\b", "", t)                      # possessive
    t = re.sub(r"[^a-z0-9]+", " ", t)
    out = []
    for w in t.split():
        m = re.fullmatch(r"(\d+)(bn|b|mn|m|k|cr)", w)           # 560b -> 560 billion
        if m:
            out += [m.group(1), UNITS[m.group(2)]]
        else:
            out.append(UNITS[w] if w in ("bn", "mn", "cr") else w)
    drop = STOPWORDS | COMPANY_WORDS.get(company, set())
    return frozenset(SYNONYMS.get(w, w) for w in out if len(w) > 1 and w not in drop)


def names(titles: list[str]) -> set[str]:
    """Proper nouns such as Nalden, WeTransfer, Kiwi, Oolka, Sereact: never written lowercase
    in this company's titles, and capitalised mid-title in at least one sentence-case title
    (Title Case Headlines capitalise every word, so they are no evidence of a name)."""
    cap, low = set(), set()
    for title in titles:
        words = [w for w in re.findall(r"[A-Za-z0-9]+", strip_accents(title)) if not w.isdigit()]
        long_words = [w for w in words if len(w) >= 4]
        title_case = bool(long_words) and sum(w[0].isupper() for w in long_words) >= 0.6 * len(long_words)
        for pos, w in enumerate(words):
            if not any(c.isupper() for c in w):
                low.add(w.lower())
            elif pos > 0 and not title_case:
                cap.add(w.lower())
    return cap - low - NOT_NAMES


def on_target(row) -> str:
    if row["company"] != "prosus":
        return ""
    return "yes" if ON_TARGET_RE.search(f"{row['title']} {row['summary']}") else "no"


class UnionFind:
    def __init__(self, items):
        self.p = {i: i for i in items}

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def similarity(a: frozenset, b: frozenset, idf: dict) -> tuple[float, int]:
    shared = a & b
    wa, wb = sum(idf[w] for w in a), sum(idf[w] for w in b)
    if not shared or not wa or not wb:
        return 0.0, len(shared)
    return sum(idf[w] for w in shared) / min(wa, wb), len(shared)


def doc_freq(token_sets: list[frozenset]) -> dict[str, int]:
    df_count: dict[str, int] = {}
    for ts in token_sets:
        for w in ts:
            df_count[w] = df_count.get(w, 0) + 1
    return df_count


def cluster(pool: pd.DataFrame, threshold: float = SIM_THRESHOLD,
            all_titles: dict[str, list[str]] | None = None) -> dict[str, str]:
    """Return {id: story_id} for the rows in pool.

    Candidate links (same company, dates <= MAX_DAYS apart, not funding vs non-funding):
      A  similarity >= threshold and >= MIN_SHARED shared tokens
      B  similarity >= ANCHOR_SIM and >= MIN_SHARED shared rare tokens
      C  a shared name (capitalised everywhere, in <= NAME_SHARE of rows) and <= NAME_DAYS apart
    Links are applied strongest first; a union is skipped when the merged story would span
    more than MAX_DAYS (every row within MAX_DAYS of the story's earliest row).
    """
    toks = {r.id: tokens(r.title, r.outlet, r.company) for r in pool.itertuples()}
    dates = dict(zip(pool["id"], pd.to_datetime(pool["published"], errors="coerce")))
    links = []
    for company, grp in pool.groupby("company", sort=True):
        ids = sorted(grp["id"])
        n = len(ids)
        # IDF within one company's rows: words common in that company's coverage weigh little
        dfc = doc_freq([toks[i] for i in ids])
        idf = {w: math.log(n / c) + 1.0 for w, c in dfc.items()}
        rare = {w for w, c in dfc.items() if c <= RARE_SHARE * n}
        titles = (all_titles or {}).get(company, list(grp["title"]))
        nm = {w for w in names(titles) if dfc.get(w, 0) <= NAME_SHARE * n}
        nm |= ALIAS_NAMES.get(company, set())
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                if pd.isna(dates[a]) or pd.isna(dates[b]):
                    continue
                days = abs((dates[a] - dates[b]).days)
                if days > MAX_DAYS:
                    continue
                if bool(toks[a] & FUNDING_WORDS) != bool(toks[b] & FUNDING_WORDS):
                    continue
                shared = toks[a] & toks[b]
                sim, k = similarity(toks[a], toks[b], idf)
                if k >= MIN_SHARED and (sim >= threshold or (
                        sim >= ANCHOR_SIM and len(shared & rare) >= MIN_SHARED)):
                    links.append((0, -sim, a, b))
                elif days <= NAME_DAYS and shared & nm:
                    links.append((1, -sim, a, b))
    uf = UnionFind(sorted(toks))
    lo = {i: dates[i] for i in toks}
    hi = dict(lo)
    for _, _, a, b in sorted(links):
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            continue
        if (max(hi[ra], hi[rb]) - min(lo[ra], lo[rb])).days > MAX_DAYS:
            continue
        uf.union(a, b)
        r = uf.find(a)
        lo[r], hi[r] = min(lo[ra], lo[rb]), max(hi[ra], hi[rb])
    rows = pool.set_index("id")
    members: dict[str, list[str]] = {}
    for i in toks:
        members.setdefault(uf.find(i), []).append(i)
    story = {}
    for ids in members.values():
        canon = min(ids, key=lambda i: (TIER_RANK.get(rows.at[i, "outlet_tier"], 9),
                                        rows.at[i, "published"], i))
        for i in ids:
            story[i] = canon
    return story


def assign(df: pd.DataFrame, threshold: float = SIM_THRESHOLD) -> pd.DataFrame:
    df = df.copy()
    df["on_target"] = df.apply(on_target, axis=1)
    pool = df[(df["company"] != "prosus") | (df["on_target"] == "yes")]
    all_titles = {c: list(g["title"]) for c, g in df.groupby("company")}   # case evidence
    story = cluster(pool, threshold, all_titles)
    df["story_id"] = [story.get(i, i) for i in df["id"]]   # off-target rows: own singleton
    df["is_canonical"] = (df["story_id"] == df["id"]).map({True: "yes", False: "no"})
    return df


def print_clusters(df: pd.DataFrame) -> None:
    multi = df.groupby("story_id").filter(lambda g: len(g) > 1)
    for company in sorted(df["company"].unique()):
        sub = df[df["company"] == company]
        pool = sub if company != "prosus" else sub[sub["on_target"] == "yes"]
        m = multi[multi["company"] == company]
        print(f"\n== {company}: articles {len(pool)} -> stories {pool['story_id'].nunique()}; "
              f"multi-row clusters {m['story_id'].nunique()}")
        for sid, g in sorted(m.groupby("story_id"), key=lambda kv: (kv[1]["published"].min(), kv[0])):
            print(f"-- story {sid[:8]} ({len(g)} rows)")
            for r in g.sort_values(["published", "id"]).itertuples():
                mark = "*" if r.is_canonical == "yes" else " "
                print(f"   {mark} {r.published} {r.outlet_tier} {r.outlet[:22]:22} | {r.title[:90]}")


def main() -> int:
    threshold = float(sys.argv[sys.argv.index("--threshold") + 1]) if "--threshold" in sys.argv else SIM_THRESHOLD
    df = pd.read_csv(CSV, dtype=str, keep_default_na=False)
    base = [c for c in df.columns if c not in NEW_COLS]
    out = assign(df[base], threshold)[base + NEW_COLS]
    print_clusters(out)
    off = out[(out["company"] == "prosus") & (out["on_target"] == "no")]
    print(f"\nprosus off-target: {len(off)} of {int((out['company'] == 'prosus').sum())}")
    if "--dry-run" not in sys.argv:
        out.to_csv(CSV, index=False)
        print(f"wrote {CSV.relative_to(ROOT)}: {len(out)} rows, columns +{NEW_COLS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

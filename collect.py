"""collect.py -- feeds.yaml + queries.yaml -> data/articles_raw.csv (window A).

Reads Google News RSS search feeds (and plain RSS feeds, if listed) for each company
term, keeps the last 30 days, de-duplicates, assigns outlet_tier by a fixed name rule,
and writes the raw CSV in the final schema with the classifier columns left blank.

Run: python collect.py
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import html
import importlib
import re
import sys
import time
import urllib.parse
from collections import Counter
from pathlib import Path

import feedparser
import requests
import yaml

ROOT = Path(__file__).resolve().parent
RAW_CSV = ROOT / "data" / "articles_raw.csv"
LOG_CSV = ROOT / "runs" / "log.csv"

COLUMNS = [
    "id", "published", "source", "outlet", "outlet_tier", "title", "url", "summary",
    "language", "company", "topic", "entity", "sentiment", "confidence", "flag",
]
LOG_HEADER = ["date", "step", "rows", "tokens_in", "tokens_out", "usd"]
COMPANIES = ["prosus", "peer_deliveryhero", "peer_zalando"]

WINDOW_DAYS = 30
SUMMARY_MAX = 600
REQUEST_GAP_S = 1.0
USER_AGENT = "prosus-media-demo/0.1 (public RSS monitoring demo; python-requests)"

# Outlet tiers: deterministic rule on the publisher name (case-insensitive exact match
# after normalisation). Anything not listed is tier3. No other inference.
TIER1 = {
    # Financial Times
    "financial times", "ft", "ft.com", "the financial times",
    # Reuters
    "reuters", "reuters.com", "thomson reuters",
    # Bloomberg
    "bloomberg", "bloomberg.com", "bloomberg news", "bloomberg law",
    # Het Financieele Dagblad
    "fd", "fd.nl", "het financieele dagblad", "financieele dagblad", "fd persgroep",
    # NRC
    "nrc", "nrc.nl", "nrc handelsblad", "nrc next",
    # Economic Times
    "the economic times", "economic times", "economictimes", "et", "et now",
    "economictimes.com", "the economic times (et)", "etretail.com", "ettech",
}
TIER2 = {
    # Dutch national news and business/tech trade
    "nos", "nos.nl", "nu.nl", "nu", "de volkskrant", "volkskrant", "trouw", "ad", "ad.nl",
    "algemeen dagblad", "de telegraaf", "telegraaf", "telegraaf.nl", "bnr", "bnr nieuwsradio",
    "bnr.nl", "rtl nieuws", "rtl.nl", "het parool", "parool", "emerce", "emerce.nl",
    "silicon canals", "tweakers", "tweakers.net", "mt/sprout", "sprout", "management team",
    "het nieuwsblad", "de tijd",
    # International business / tech
    "sifted", "techcrunch", "the information", "wall street journal", "the wall street journal",
    "wsj", "cnbc", "business insider", "business insider nederland", "forbes", "fortune",
    "the guardian", "bbc", "bbc news", "associated press", "ap news", "afp", "the verge",
    "wired", "the new york times", "new york times", "the economist", "barron's",
    "the next web", "tech.eu", "ag connect", "the globe and mail",
    # Retail / fashion trade (Zalando)
    "the business of fashion", "retail week", "wwd",
    # Korean national (Delivery Hero / Woowa)
    "the korea herald", "the korea times",
    # German national business (peers are German)
    "handelsblatt", "frankfurter allgemeine zeitung", "faz", "faz.net",
    "süddeutsche zeitung", "sueddeutsche zeitung", "der spiegel", "spiegel", "der spiegel online",
    "manager magazin", "wirtschaftswoche", "die welt", "welt", "t3n", "gründerszene",
    # Indian national business (Prosus portfolio coverage)
    "mint", "livemint", "business standard", "moneycontrol", "moneycontrol.com", "the hindu businessline",
    "hindustan times", "the times of india", "times of india", "inc42", "entrackr", "the ken",
    # South African national business (Naspers/Prosus home market)
    "business day", "businesslive", "news24", "fin24", "daily maverick", "moneyweb",
    "techcentral",
}


# Relevance filter: a row is kept only if its title contains one of its company's terms
# (case-insensitive, word-boundary). Terms in queries.yaml are added to these at run time
# so window C's file extends them.
RELEVANCE_TERMS = {
    "prosus": ["Prosus", "Naspers", "Just Eat Takeaway", "Just Eat", "Takeaway.com", "Thuisbezorgd",
               "Lieferando", "iFood", "Swiggy", "PayU", "OLX", "Stack Overflow", "Toqan",
               "Bloisi", "Sgourdos"],
    "peer_deliveryhero": ["Delivery Hero", "Talabat", "foodpanda", "Glovo", "Baemin", "Foodora"],
    "peer_zalando": ["Zalando"],
}

# Automated price-ticker items: price-move phrasing in the title.
TICKER_RE = re.compile(
    r"(?:\d+(?:[.,]\d+)?\s?(?:%|percent|per cent)\s+(?:drop|gain|jump|surge|fall|rise|slump|plunge|decline|loss)"
    r"|(?:drop|gain|rise|fall|jump|surge|slump|plunge|decline|loss)s?\s+(?:of\s+)?\d+(?:[.,]\d+)?\s?(?:%|percent|per cent)"
    r"|\bshares?\s+(?:\w+\s+)?(?:rise|rises|rose|fall|falls|fell|jump|jumps|drop|drops|gain|gains|slip|slips|climb|climbs|sink|sinks|surge|surges|tumble|tumbles|slide|slides)\b"
    r"|\bstock\s+heads\s+into\s+the\s+open\b"
    r"|\bAktie\b|\bKurs(?:ziel|plus|minus|verlust)?\b)",
    re.IGNORECASE,
)
TICKER_OUTLETS = {"ad-hoc-news.de"}

# Outlet aliases (keys lower-case, after stripping a leading "www."): variants -> one name.
OUTLET_ALIASES = {
    "ad-hoc-news.de": "ad-hoc-news.de", "ad hoc news": "ad-hoc-news.de",
    "adhocnews.de": "ad-hoc-news.de", "ad-hoc-news": "ad-hoc-news.de",
    "yahoo finance": "Yahoo Finance", "yahoo! finance": "Yahoo Finance",
    "yahoo! finance canada": "Yahoo Finance", "yahoo finance canada": "Yahoo Finance",
    "yahoo finance uk": "Yahoo Finance", "yahoo! finance uk": "Yahoo Finance",
    "marketscreener": "MarketScreener", "marketscreener nederland": "MarketScreener",
    "marketscreener.com": "MarketScreener",
}


# Regional / spelling variants collapsed by prefix (checked after the exact alias map).
OUTLET_PREFIX_ALIASES = [
    (re.compile(r"^yahoo!?\s*finance\b", re.IGNORECASE), "Yahoo Finance"),
    (re.compile(r"^fashionnetwork\b", re.IGNORECASE), "FashionNetwork"),
    (re.compile(r"^investing\.com\b", re.IGNORECASE), "Investing.com"),
    (re.compile(r"^retaildetail\b", re.IGNORECASE), "RetailDetail"),
]

# Outlets dropped outright (compared on lower-case alphanumerics only, so all spellings match).
OUTLET_BLOCKLIST = ["Royal Belgian FA", "feeling.be", "Simply Wall St", "Simply Wall Street",
                    "simplywall.st", "Traders Union", "GuruFocus", "Tracxn", "Capital.com"]
OUTLET_BLOCK_KEYS = {re.sub(r"[^a-z0-9]", "", x.lower()) for x in OUTLET_BLOCKLIST}

# Title exclusions: quote/data pages, shareholding notices, giveaways/sponsor posts.
TITLE_EXCLUDE_TERMS = [
    "Stock Price, News, Quote", "Valuation Measures", "Income Statement", "Analyst Ratings",
    "Stock Forecast", "Price Target", "Earnings Call Transcript", "transcript", "Top Shareholders",
    "Institutional Holders", "Top Holders", "WpHG", "Stimmrechtsmitteilung", "voting rights", "Major holdings",
    "Directors' Dealings", "Arrival Moment", "giveaway", "winactie",
]


def build_title_exclude_re(extra: list[str] | None = None) -> re.Pattern:
    parts = [re.escape(t) for t in TITLE_EXCLUDE_TERMS + list(extra or [])]
    return re.compile("|".join(parts), re.IGNORECASE)


def normalise_outlet(outlet: str) -> str:
    o = re.sub(r"\s+", " ", (outlet or "").strip())
    if re.match(r"^[a-z][a-z0-9+.-]*://", o, re.IGNORECASE) or "/" in o:
        o = re.sub(r"^[a-z][a-z0-9+.-]*://", "", o, flags=re.IGNORECASE).split("/")[0]
    o = re.sub(r"^www\.", "", o, flags=re.IGNORECASE)
    if o.lower() in OUTLET_ALIASES:
        return OUTLET_ALIASES[o.lower()]
    for rx, name in OUTLET_PREFIX_ALIASES:
        if rx.match(o):
            return name
    return o


def term_regex(terms: list[str]) -> re.Pattern:
    return re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)",
                      re.IGNORECASE)


def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def outlet_tier(outlet: str) -> str:
    n = norm_name(outlet)
    if n in TIER1:
        return "tier1"
    if n in TIER2:
        return "tier2"
    return "tier3"


def strip_html(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def strip_outlet_suffix(text: str, outlet: str) -> str:
    if outlet:
        for sep in (" - ", " – ", " | "):
            suffix = f"{sep}{outlet}"
            if text.endswith(suffix):
                return text[: -len(suffix)].strip()
        if text.endswith(" " + outlet):
            return text[: -len(outlet) - 1].strip()
    return text


def norm_title(t: str) -> str:
    return re.sub(r"[^\w]+", " ", (t or "").lower()).strip()


# ---------------------------------------------------------------- config

def load_queries(path: Path) -> tuple[dict[str, dict], list[str]]:
    """Return ({company: {"terms": [...], "phrases": [...]}}, exclusions).

    Primary shape is `companies:` (see queries.yaml header). Tolerated fallback shapes
    for window C's file: top-level `prosus`/`terms`/`prosus_terms`, `topics` (dict of
    phrase lists, applied to prosus) and `peers` (dict name -> list/dict, or list of
    {name, phrases}). Unknown keys are ignored.
    """
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, dict] = {}

    def as_list(v) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        if isinstance(v, dict):
            res = []
            for x in v.values():
                res.extend(as_list(x))
            return res
        return [str(x) for x in v if x]

    def phrases_from_topics(topics) -> list[str]:
        if isinstance(topics, dict):
            res = []
            for v in topics.values():
                if isinstance(v, dict):
                    res.extend(as_list(v.get("phrases") or v.get("queries") or v.get("terms")))
                else:
                    res.extend(as_list(v))
            return res
        return as_list(topics)

    if isinstance(cfg.get("companies"), dict):
        for key, spec in cfg["companies"].items():
            spec = spec or {}
            if isinstance(spec, list):
                spec = {"terms": spec}
            out[str(key)] = {
                "terms": as_list(spec.get("terms")),
                "phrases": phrases_from_topics(spec.get("topics")),
            }
    else:
        p = cfg.get("prosus")
        terms = as_list(cfg.get("terms") or cfg.get("prosus_terms")
                        or (p.get("terms") if isinstance(p, dict) else p))
        out["prosus"] = {"terms": terms, "phrases": phrases_from_topics(cfg.get("topics"))}
        peers = cfg.get("peers") or {}
        items = (peers.items() if isinstance(peers, dict)
                 else [((x or {}).get("name"), x) for x in peers if isinstance(x, dict)])
        for name, spec in items:
            if not name:
                continue
            key = "peer_" + re.sub(r"[^a-z0-9]", "", str(name).lower())
            if isinstance(spec, dict):
                t = as_list(spec.get("terms")) or [str(name)]
                ph = as_list(spec.get("phrases") or spec.get("queries"))
            else:
                t, ph = [str(name)], as_list(spec)
            out[key] = {"terms": t, "phrases": [x for x in ph if x not in t]}

    excl = cfg.get("exclusions") or []
    if isinstance(excl, dict):
        excl = [x for v in excl.values() for x in (v if isinstance(v, list) else [v])]
    exclusions = [str(x).lower() for x in excl if isinstance(x, (str, int))]
    unknown = [c for c in out if c not in COMPANIES]
    if unknown:
        print(f"WARNING: company keys not in schema, skipped: {unknown}")
    return {c: out[c] for c in COMPANIES if c in out}, exclusions


def load_feeds(path: Path) -> list[dict]:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    feeds = cfg.get("feeds", cfg) if isinstance(cfg, dict) else cfg
    return [f for f in (feeds or []) if isinstance(f, dict) and f.get("url")]


# ---------------------------------------------------------------- fetch

_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_last_request = [0.0]


def fetch(url: str):
    wait = REQUEST_GAP_S - (time.monotonic() - _last_request[0])
    if wait > 0:
        time.sleep(wait)
    for attempt in range(3):
        _last_request[0] = time.monotonic()
        try:
            r = _session.get(url, timeout=20)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** (attempt + 1))
                continue
            r.raise_for_status()
            return feedparser.parse(r.content)
        except requests.RequestException as e:
            if attempt == 2:
                print(f"  fetch failed: {e.__class__.__name__}: {str(e)[:120]}")
                return None
            time.sleep(2 ** (attempt + 1))
    print("  fetch failed after retries")
    return None


def entry_to_row(e, source: str, company: str) -> dict | None:
    url = (e.get("link") or "").strip()
    pp = e.get("published_parsed") or e.get("updated_parsed")
    if not url or not pp:
        return None
    published = dt.date(pp.tm_year, pp.tm_mon, pp.tm_mday)
    src = e.get("source") or {}
    raw_outlet = strip_html(src.get("title", "") if hasattr(src, "get") else "")
    title = strip_outlet_suffix(strip_html(e.get("title", "")), raw_outlet)
    summary = strip_outlet_suffix(strip_html(e.get("summary", "")), raw_outlet)
    outlet = normalise_outlet(raw_outlet)
    if norm_title(summary) in (norm_title(title), norm_title(strip_html(e.get("title", "")))):
        summary = ""   # feed summary just repeats the title: leave blank
    return {
        "id": hashlib.sha1(url.encode("utf-8")).hexdigest(),
        "published": published,
        "source": source,
        "outlet": outlet,
        "outlet_tier": outlet_tier(outlet),
        "title": title,
        "url": url,
        "summary": summary[:SUMMARY_MAX],
        "language": "",
        "company": company,
    }


def contains_term(text: str, terms: list[str]) -> bool:
    t = text.lower()
    return any(term.lower() in t for term in terms)


def run_newsroom() -> list[dict]:
    """Integration point for window C. Expects collect_prosus_newsroom.py with a
    collect()/fetch_articles()/main() function returning a list of row dicts."""
    if not (ROOT / "collect_prosus_newsroom.py").exists():
        print("collect_prosus_newsroom.py not found: skipping Prosus newsroom")
        return []
    mod = importlib.import_module("collect_prosus_newsroom")
    fn = next((getattr(mod, n) for n in ("collect", "fetch_articles", "main")
               if callable(getattr(mod, n, None))), None)
    if fn is None:
        print("collect_prosus_newsroom.py has no collect()/fetch_articles()/main(): skipping")
        return []
    rows = []
    for r in fn() or []:
        if not isinstance(r, dict) or not r.get("url") or not r.get("published"):
            continue
        outlet = normalise_outlet(r.get("outlet") or "Prosus")
        try:
            pub = dt.date.fromisoformat(str(r["published"])[:10])
        except ValueError:
            continue
        rows.append({
            "id": hashlib.sha1(r["url"].encode("utf-8")).hexdigest(),
            "published": pub, "source": r.get("source") or "prosus_newsroom",
            "outlet": outlet, "outlet_tier": outlet_tier(outlet),
            "title": strip_html(r.get("title", "")), "url": r["url"],
            "summary": strip_html(r.get("summary", ""))[:SUMMARY_MAX],
            "language": "", "company": "prosus",
        })
    print(f"collect_prosus_newsroom: {len(rows)} rows")
    return rows


# ---------------------------------------------------------------- main

def append_log(step: str, rows: int, tin: int = 0, tout: int = 0, usd: float = 0.0) -> None:
    LOG_CSV.parent.mkdir(parents=True, exist_ok=True)
    new = not LOG_CSV.exists() or LOG_CSV.stat().st_size == 0
    with LOG_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(LOG_HEADER)
        w.writerow([dt.date.today().isoformat(), step, rows, tin, tout, f"{usd:.4f}"])


def main() -> int:
    companies, exclusions = load_queries(ROOT / "queries.yaml")
    exclusions_raw = list(exclusions)
    feeds = load_feeds(ROOT / "feeds.yaml")
    today = dt.date.today()
    cutoff = today - dt.timedelta(days=WINDOW_DAYS)

    collected: list[dict] = []   # in priority order: prosus first
    feed_counts: Counter = Counter()
    empty_feeds: list[str] = []

    for company in COMPANIES:
        if company not in companies:
            continue
        spec = companies[company]
        queries = list(dict.fromkeys(spec["terms"] + spec["phrases"]))
        for feed in feeds:
            ftype = str(feed.get("type", "")).lower()
            if ftype != "googlenews":
                continue
            source = f"{feed['name']}_{company}"
            for q in queries:
                qq = f'"{q}"' if " " in q and not q.startswith('"') else q
                url = feed["url"].replace("{query}", urllib.parse.quote_plus(f"{qq} when:{WINDOW_DAYS}d"))
                d = fetch(url)
                n = len(d.entries) if d else 0
                feed_counts[f"{source} [{q}]"] += n
                if n == 0:
                    empty_feeds.append(f"{source} [{q}]")
                for e in (d.entries if d else []):
                    row = entry_to_row(e, source, company)
                    if row:
                        collected.append(row)

    # plain RSS feeds: keep items mentioning a company term (prosus checked first)
    for feed in feeds:
        if str(feed.get("type", "")).lower() != "rss":
            continue
        d = fetch(feed["url"])
        n = len(d.entries) if d else 0
        feed_counts[feed["name"]] += n
        if n == 0:
            empty_feeds.append(feed["name"])
        for e in (d.entries if d else []):
            text = f"{e.get('title', '')} {e.get('summary', '')}"
            for company in COMPANIES:
                if company in companies and contains_term(text, companies[company]["terms"]):
                    row = entry_to_row(e, feed["name"], company)
                    if row:
                        if not row["outlet"]:
                            row["outlet"] = feed["name"]
                            row["outlet_tier"] = outlet_tier(row["outlet"])
                        collected.append(row)
                    break

    collected = run_newsroom() + collected

    # filter window, exclusions, de-duplicate (url or normalised title; first wins = prosus)
    seen_urls, seen_titles, rows = set(), set(), []
    dropped = Counter()
    rel_re = {}
    for c in COMPANIES:
        extra = companies.get(c, {}).get("terms", [])
        rel_re[c] = term_regex(list(dict.fromkeys(RELEVANCE_TERMS[c] + extra)))
    irrelevant: Counter = Counter()
    title_ex_re = build_title_exclude_re(exclusions_raw)
    for r in collected:
        if r["published"] < cutoff or r["published"] > today + dt.timedelta(days=1):
            dropped["outside_30d"] += 1
            continue
        if exclusions and any(x in r["title"].lower() for x in exclusions):
            dropped["exclusion"] += 1
            continue
        if re.sub(r"[^a-z0-9]", "", r["outlet"].lower()) in OUTLET_BLOCK_KEYS:
            dropped["blocked_outlet"] += 1
            continue
        if title_ex_re.search(r["title"]):
            dropped["title_exclusion"] += 1
            continue
        if norm_name(r["outlet"]) in TICKER_OUTLETS or TICKER_RE.search(r["title"]):
            dropped["ticker"] += 1
            continue
        if not rel_re[r["company"]].search(r["title"]):
            dropped["irrelevant"] += 1
            irrelevant[r["company"]] += 1
            continue
        nt = norm_title(r["title"])
        if r["url"] in seen_urls or (nt and nt in seen_titles):
            dropped["duplicate"] += 1
            continue
        seen_urls.add(r["url"])
        seen_titles.add(nt)
        rows.append(r)

    rows.sort(key=lambda r: (r["published"], r["id"]), reverse=True)
    RAW_CSV.parent.mkdir(parents=True, exist_ok=True)
    with RAW_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            out = {c: "" for c in COLUMNS}
            out.update(r)
            out["published"] = r["published"].isoformat()
            w.writerow(out)

    print(f"\nWrote {len(rows)} rows to {RAW_CSV.relative_to(ROOT)} "
          f"(window {cutoff} to {today}); dropped: {dict(dropped)}")
    print(f"Dropped by relevance filter per company: {dict(irrelevant)}")
    print(f"Dropped by ticker exclusion: {dropped['ticker']}")
    print("Rows per company:")
    for c, n in Counter(r["company"] for r in rows).most_common():
        print(f"  {c}: {n}")
    print("Rows per source:")
    for s, n in sorted(Counter(r["source"] for r in rows).items()):
        print(f"  {s}: {n}")
    print("Rows per outlet_tier:", dict(Counter(r["outlet_tier"] for r in rows)))
    print("Items returned per feed/query (before filtering):")
    for k, n in feed_counts.items():
        print(f"  {k}: {n}")
    print("Feeds/queries with 0 items:", ", ".join(empty_feeds) if empty_feeds else "none")

    append_log("collect", len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())

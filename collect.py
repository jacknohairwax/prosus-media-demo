"""collect.py -- feeds.yaml + queries.yaml -> data/articles_raw.csv (window A, adapted to window C's schema).

Google News: one RSS request per queries.yaml phrase x googlenews edition (feeds.yaml `use: no`
entries skipped), q + window_suffix, one global >= min_seconds_between_requests floor for every
Google request. Plain RSS feeds from feeds.yaml (verified: yes): items naming a tracked company.
C's exclusions (with `unless: company_name`), 30-day window, dedup on url or normalised title,
Google redirect links resolved to outlet URLs (cache data/url_cache.json), entity/topic from the
query group. Writes the brief's schema plus url_source, url_resolved; stats to runs/collect_stats.json.

Run: python collect.py [--dry-run | --probe | --no-resolve]
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import datetime as dt
import hashlib
import html
import importlib
import json
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
URL_CACHE = ROOT / "data" / "url_cache.json"       # tracked: a fresh clone re-uses resolutions
STATS_JSON = ROOT / "runs" / "collect_stats.json"  # tracked: numbers only, like runs/log.csv

COLUMNS = [
    "id", "published", "source", "outlet", "outlet_tier", "title", "url", "summary",
    "language", "company", "topic", "entity", "sentiment", "confidence", "flag",
    "url_source", "url_resolved",   # added: Google redirect link as fetched; yes/no outlet URL found
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


# Window A's title-relevance filter and price-move ticker regex were removed: C's recall and
# executive phrases are body matches and C's `unless: company_name` rule only makes sense if
# headlines without the company name are kept; C measured precision with its exclusions alone.
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


# ---------------------------------------------------------------- config (window C schema)

GROUP_ORDER = ["core", "recall", "exec", "topic", "peer"]   # also the company/row priority order


def load_config(root: Path = ROOT) -> dict:
    """Read queries.yaml and feeds.yaml exactly as window C wrote them."""
    q = yaml.safe_load((root / "queries.yaml").read_text(encoding="utf-8")) or {}
    f = yaml.safe_load((root / "feeds.yaml").read_text(encoding="utf-8")) or {}
    gn = q["googlenews"]
    company_names = {c: re.compile(p) for c, p in (q.get("company_names") or {}).items()}
    exclusions = []
    for x in q.get("exclusions") or []:
        unless = x.get("unless")
        if unless not in (None, "company_name"):
            raise ValueError(f"exclusion {x.get('id')}: unknown unless value {unless!r}")
        exclusions.append({"id": x["id"], "rx": re.compile(x["pattern"]), "unless": unless})
    return {
        "q": q,
        "gn": gn,
        "editions": gn["editions"],
        "suffix": gn.get("window_suffix", ""),
        "min_gap": float(gn["min_seconds_between_requests"]),
        "company_names": company_names,
        "exclusions": exclusions,
        "executive_order": list((q.get("executives") or {}).keys()),
        "gn_feeds": {e["name"]: e for e in (f.get("googlenews") or [])},
        "rss_feeds": [e for e in (f.get("rss") or []) if isinstance(e, dict)],
    }


def gn_url(cfg: dict, q: str, edition: str) -> str:
    """base_url?q=<q + window_suffix, URL-encoded>&hl=&gl=&ceid= (byte-identical to feeds.yaml urls)."""
    ed = cfg["editions"][edition]
    params = {"q": q + cfg["suffix"], "hl": ed["hl"], "gl": ed["gl"], "ceid": ed["ceid"]}
    return cfg["gn"]["base_url"] + "?" + urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="")


def build_plan(cfg: dict) -> tuple[list[dict], list[dict]]:
    """One spec per queries.yaml phrase x googlenews edition, in GROUP_ORDER.

    Returns (plan, skipped). A spec is skipped when its feeds.yaml entry says use: no.
    Fields: name (= feeds.yaml name = `source`), url, edition, company, group, key, entity, topic.
    topic is set only for the `topics` group (feeds.yaml carries `topic:` on exactly those entries).
    """
    q = cfg["q"]
    phrases = []   # (group, key, company, entity, topic, phrase)
    for grp in ("core", "recall"):
        for p in (q.get("prosus") or {}).get(grp) or []:
            phrases.append((grp, None, "prosus", "company", "", p))
    for key, spec in (q.get("executives") or {}).items():
        for p in spec.get("queries") or []:
            phrases.append(("exec", key, "prosus", key, "", p))
    for key, plist in (q.get("topics") or {}).items():
        for p in plist or []:
            phrases.append(("topic", key, "prosus", "company", key, p))
    for key, spec in (q.get("peers") or {}).items():
        for p in spec.get("queries") or []:
            phrases.append(("peer", key, key, "company", "", p))

    plan, skipped = [], []
    for grp, key, company, entity, topic, p in phrases:
        for edition in cfg["editions"]:
            name = "_".join(x for x in ("gn", edition, grp, key, p["id"]) if x)
            spec = {"name": name, "url": gn_url(cfg, p["q"], edition), "edition": edition,
                    "company": company, "group": grp, "key": key or "", "entity": entity,
                    "topic": topic, "q": p["q"]}
            feed = cfg["gn_feeds"].get(name)
            if feed is not None and str(feed.get("use", "yes")).lower() == "no":
                skipped.append(spec)
                continue
            plan.append(spec)
    return plan, skipped


def usable_rss(cfg: dict) -> list[dict]:
    """rss entries with verified: yes and not use: no (rss entries carry no `use` key today)."""
    return [f for f in cfg["rss_feeds"]
            if str(f.get("verified", "")).lower() == "yes" and str(f.get("use", "yes")).lower() != "no"
            and str(f.get("type", "rss")).lower() == "rss" and f.get("url")]


# ---------------------------------------------------------------- HTTP: Google gate + other hosts

def is_google_host(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host == "google.com" or host.endswith(".google.com") or bool(re.match(r"^(.+\.)?google\.[a-z.]+$", host))


BLOCK_BODY_RE = re.compile(r"unusual traffic|/sorry/index|id=\"captcha-form\"|g-recaptcha", re.IGNORECASE)


def looks_blocked(status: int, url: str, location: str, body: str) -> str:
    """Return a reason string when a Google response is a rate-limit/captcha page, else ''."""
    if status == 429:
        return "HTTP 429"
    if status == 503:
        return "HTTP 503"
    for u in (url, location):
        if u and urllib.parse.urlsplit(u).path.startswith("/sorry"):
            return f"redirect to sorry page ({u[:80]})"
    if BLOCK_BODY_RE.search(body[:20000]):
        return "captcha/unusual-traffic page"
    return ""


class GoogleGate:
    """Single global rate limiter for every request to a Google host.

    Hard floor: min_gap seconds between the end of one Google request and the start of the next,
    counted across RSS search, article pages and batchexecute. Redirects are followed manually
    (allow_redirects=False) so every hop goes through the floor. After a block (429, 503,
    sorry/captcha page) every later call returns None without touching the network.
    """

    def __init__(self, min_gap: float, session: requests.Session | None = None,
                 clock=time.monotonic, sleep=time.sleep):
        self.min_gap = float(min_gap)
        self.session = session or _session
        self.clock, self.sleep = clock, sleep
        self.last_end: float | None = None
        self.count = 0
        self.blocked = ""
        self.request_starts: list[float] = []

    def request(self, method: str, url: str, max_hops: int = 3, **kw):
        hops = 0
        while True:
            if self.blocked:
                return None
            if not is_google_host(url):
                raise ValueError(f"GoogleGate called for non-Google host: {url}")
            if self.last_end is not None:
                wait = self.min_gap - (self.clock() - self.last_end)
                if wait > 0:
                    self.sleep(wait)
            self.request_starts.append(self.clock())
            self.count += 1
            try:
                r = self.session.request(method, url, allow_redirects=False, timeout=20, **kw)
            finally:
                self.last_end = self.clock()
            loc = r.headers.get("location", "") if r.is_redirect else ""
            loc = urllib.parse.urljoin(url, loc) if loc else ""
            reason = looks_blocked(r.status_code, url, loc, "" if r.is_redirect else r.text)
            if reason:
                self.blocked = reason
                print(f"GOOGLE BLOCK: {reason} on {url[:100]}; no more Google requests this run.")
                return None
            if r.is_redirect and loc and hops < max_hops and is_google_host(loc) \
                    and (urllib.parse.urlsplit(loc).hostname or "") == "news.google.com":
                url, method, hops = loc, "GET", hops + 1
                kw.pop("data", None)
                continue
            return r


def fetch_google_rss(gate: GoogleGate, url: str):
    """Google News RSS through the gate: at most 1 retry, only on network error or 5xx (not 503)."""
    for attempt in range(2):
        try:
            r = gate.request("GET", url)
        except requests.RequestException as e:
            print(f"  google rss attempt {attempt + 1} failed: {e.__class__.__name__}")
            continue
        if r is None:
            return None
        if r.status_code >= 500:
            print(f"  google rss attempt {attempt + 1}: HTTP {r.status_code}")
            continue
        if r.status_code != 200:
            print(f"  google rss: HTTP {r.status_code}; not retried")
            return None
        return feedparser.parse(r.content)
    return None


def fetch(url: str):
    """Window A's fetch for non-Google feeds (1 s gap, backoff on 429/5xx)."""
    if is_google_host(url):
        raise ValueError("Google URLs must go through GoogleGate")
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


_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_last_request = [0.0]


# ---------------------------------------------------------------- Google News URL resolution

BATCHEXECUTE_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"
POST_BATCH = 25      # links per batchexecute POST
_SG_RE = re.compile(r'data-n-a-sg="([^"]+)"')
_TS_RE = re.compile(r'data-n-a-ts="([^"]+)"')
_GARTURLREQ_CTX = [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None,
                    None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0]


def gn_article_id(url: str) -> str | None:
    p = urllib.parse.urlsplit(url or "")
    if (p.hostname or "") != "news.google.com":
        return None
    parts = [x for x in p.path.split("/") if x]
    if len(parts) >= 2 and parts[-2] in ("articles", "read"):
        return parts[-1]
    return None


def decode_offline(art_id: str) -> str | None:
    """Old-style ids are base64url protobuf holding the URL; new ones hold 'AU_yqL...' (needs Google)."""
    try:
        b = base64.urlsafe_b64decode(art_id + "=" * (-len(art_id) % 4))
    except (ValueError, binascii.Error):
        return None
    if b.startswith(b"\x08\x13\x22"):
        b = b[3:]
    elif b.startswith(b"\x22"):
        b = b[1:]
    else:
        return None
    n, shift, i = 0, 0, 0
    while i < len(b):               # protobuf varint length
        n |= (b[i] & 0x7F) << shift
        i += 1
        if not b[i - 1] & 0x80:
            break
        shift += 7
    raw = b[i:i + n]
    if len(raw) != n:
        return None
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return s if re.match(r"^https?://", s) else None


def build_batchexecute_body(items: list[tuple[str, str, str, str]]) -> str:
    """items: (req_id, art_id, ts, sig) -> form body 'f.req=...' (one POST for many links)."""
    envs = []
    for req_id, art_id, ts, sig in items:
        inner = ["garturlreq", _GARTURLREQ_CTX, art_id, int(ts) if str(ts).isdigit() else ts, sig]
        envs.append(["Fbv4je", json.dumps(inner, separators=(",", ":")), None, str(req_id)])
    return "f.req=" + urllib.parse.quote(json.dumps([envs], separators=(",", ":")))


def parse_batchexecute(text: str) -> list[tuple[str | None, str]]:
    """Return [(req_id or None, url)] from a batchexecute response."""
    body = text.split("\n\n", 1)[1] if "\n\n" in text else text
    body = body.strip()
    if body.startswith(")]}'"):
        body = body[4:].strip()
    start = body.find("[")
    if start < 0:
        return []
    try:
        rows, _ = json.JSONDecoder().raw_decode(body[start:])
    except json.JSONDecodeError:
        return []
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not (isinstance(row, list) and len(row) >= 3) or not (row[0] == "wrb.fr" or row[1] == "Fbv4je"):
            continue
        payload = row[2]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                continue
        if isinstance(payload, list) and len(payload) > 1 and payload[0] == "garturlres" \
                and isinstance(payload[1], str) and re.match(r"^https?://", payload[1]):
            rid = next((str(x) for x in reversed(row[3:]) if x is not None), None)
            out.append((rid, payload[1]))
    return out


class Resolver:
    """news.google.com/rss/articles/<id> -> outlet URL. Order: cache, offline decode, then Google
    (article page for signature+timestamp, then batched batchexecute POST). Each link gets at most
    one retry in total. Cache = {article_id: outlet_url}, written after every POST so runs resume."""

    def __init__(self, gate: GoogleGate | None, cache_path: Path = URL_CACHE, batch: int = POST_BATCH):
        self.gate, self.cache_path, self.batch = gate, cache_path, batch
        self.cache: dict[str, str] = {}
        if cache_path.exists():
            self.cache = json.loads(cache_path.read_text(encoding="utf-8") or "{}")
        self.stats = Counter()

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, indent=0, sort_keys=True, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.cache_path)

    def _page_params(self, art_id: str, src_url: str) -> str:
        qs = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(src_url).query))
        p = {k: qs.get(k, d) for k, d in (("hl", "en-US"), ("gl", "US"), ("ceid", "US:en"))}
        return (f"https://news.google.com/rss/articles/{art_id}?"
                + urllib.parse.urlencode(p, quote_via=urllib.parse.quote, safe=""))

    def _signature(self, art_id: str, src_url: str, retry_left: dict) -> tuple[str, str] | None:
        while True:
            try:
                r = self.gate.request("GET", self._page_params(art_id, src_url))
            except requests.RequestException:
                r = "error"
            if r is None:                 # blocked
                return None
            if r != "error" and r.status_code == 200:
                sg, ts = _SG_RE.search(r.text), _TS_RE.search(r.text)
                if sg and ts:
                    return html.unescape(sg.group(1)), html.unescape(ts.group(1))
                self.stats["no_signature"] += 1
                return None               # page served but no signature: retrying will not help
            if r != "error" and r.status_code < 500:
                self.stats[f"page_http_{r.status_code}"] += 1
                return None
            if retry_left.get(art_id, 0) <= 0:
                self.stats["page_failed"] += 1
                return None
            retry_left[art_id] -= 1

    def _post(self, items: list[tuple[str, str, str, str]]) -> dict[str, str]:
        try:
            r = self.gate.request("POST", BATCHEXECUTE_URL, data=build_batchexecute_body(items),
                                  headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
        except requests.RequestException:
            return {}
        if r is None or r.status_code != 200:
            return {}
        got = parse_batchexecute(r.text)
        ids = {rid for rid, _ in got}
        if got and None not in ids:
            by_req = dict(got)
            return {it[1]: by_req[it[0]] for it in items if it[0] in by_req}
        if len(got) == len(items):        # no request ids in the reply: positional
            return {it[1]: u for it, (_, u) in zip(items, got)}
        return {}

    def _flush(self, pending: list, retry_left: dict) -> None:
        if not pending:
            return
        items = [(str(i), a, ts, sg) for i, (a, ts, sg) in enumerate(pending)]
        got = self._post(items)
        self.stats["batchexecute_posts"] += 1
        for it in items:                  # missing from the batch: one single POST if retry left
            if it[1] in got or self.gate.blocked or retry_left.get(it[1], 0) <= 0:
                continue
            retry_left[it[1]] -= 1
            got.update(self._post([("0",) + it[1:]]))
            self.stats["batchexecute_posts"] += 1
        for a, u in got.items():
            self.cache[a] = u
            self.stats["resolved_google"] += 1
        self.save()

    def resolve(self, links: list[str]) -> dict[str, str]:
        """{redirect_url: outlet_url} for every link that could be resolved."""
        out, todo = {}, []
        for link in dict.fromkeys(links):
            a = gn_article_id(link)
            if a is None:
                self.stats["not_google_link"] += 1
                continue
            if a in self.cache:
                out[link] = self.cache[a]
                self.stats["cache_hit"] += 1
                continue
            dec = decode_offline(a)
            if dec:
                self.cache[a] = out[link] = dec
                self.stats["decoded_offline"] += 1
                continue
            todo.append((link, a))
        if todo and self.gate is None:
            self.stats["skipped_no_resolve"] += len(todo)
            todo = []
        retry_left = {a: 1 for _, a in todo}
        pending = []
        for link, a in todo:
            if self.gate.blocked:
                break
            sig = self._signature(a, link, retry_left)
            if sig:
                pending.append((a, sig[1], sig[0]))
            if len(pending) >= self.batch:
                self._flush(pending, retry_left)
                pending = []
        if not self.gate or not self.gate.blocked:
            self._flush(pending, retry_left)
        elif pending:
            self.stats["lost_to_block"] += len(pending)
        if todo:
            self.save()
        for link, a in todo:
            if a in self.cache:
                out[link] = self.cache[a]
        self.stats["unresolved"] = sum(1 for link in dict.fromkeys(links)
                                       if gn_article_id(link) and link not in out)
        return out


# ---------------------------------------------------------------- rows

def entry_to_row(e, source: str, company: str) -> dict | None:
    url = (e.get("link") or "").strip()
    pp = e.get("published_parsed") or e.get("updated_parsed")
    if not url or not pp:
        return None
    published = dt.date(pp.tm_year, pp.tm_mon, pp.tm_mday)
    src = e.get("source") or {}
    raw_outlet = strip_html(src.get("title", "") if hasattr(src, "get") else "")
    raw_title = strip_html(e.get("title", ""))
    title = strip_outlet_suffix(raw_title, raw_outlet)
    summary = strip_outlet_suffix(strip_html(e.get("summary", "")), raw_outlet)
    outlet = normalise_outlet(raw_outlet)
    if norm_title(summary) in (norm_title(title), norm_title(raw_title)):
        summary = ""   # feed summary just repeats the title: leave blank
    return {
        "published": published,
        "source": source,
        "outlet": outlet,
        "outlet_tier": outlet_tier(outlet),
        "title": title,
        "raw_title": raw_title,     # title as the feed gives it (Google: ends in " - <Source>")
        "url": url,
        "url_source": url,
        "summary": summary[:SUMMARY_MAX],
        "language": "",
        "company": company,
        "members": [],              # (company, group, key, entity, topic) of every query that found it
    }


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
        title = strip_html(r.get("title", ""))
        rows.append({
            "published": pub, "source": r.get("source") or "prosus_newsroom",
            "outlet": outlet, "outlet_tier": outlet_tier(outlet),
            "title": title, "raw_title": title, "url": r["url"], "url_source": r["url"],
            "summary": strip_html(r.get("summary", ""))[:SUMMARY_MAX],
            "language": "", "company": "prosus", "members": [],
        })
    print(f"collect_prosus_newsroom: {len(rows)} rows")
    return rows


def c_exclusion(row: dict, cfg: dict) -> tuple[str | None, bool]:
    """Window C's rule: drop when `pattern` matches the title as the feed gives it; with
    unless: company_name, keep anyway when company_names[<row's company>] also matches.
    Returns (exclusion id that dropped the row or None, whether an `unless` saved it)."""
    saved = False
    for x in cfg["exclusions"]:
        if not x["rx"].search(row["raw_title"]):
            continue
        if x["unless"] == "company_name":
            name_rx = cfg["company_names"].get(row["company"])
            if name_rx is not None and name_rx.search(row["raw_title"]):
                saved = True
                continue
        return x["id"], saved
    return None, saved


def a_filter(row: dict, title_ex_re: re.Pattern) -> str | None:
    """Window A's filters kept because they do not contradict C (junk-page classes only)."""
    if re.sub(r"[^a-z0-9]", "", row["outlet"].lower()) in OUTLET_BLOCK_KEYS:
        return "a_blocked_outlet"
    if title_ex_re.search(row["title"]):
        return "a_title_exclusion"
    if norm_name(row["outlet"]) in TICKER_OUTLETS:
        return "a_ticker_outlet"
    return None


def assign_entity_topic(members: list[tuple], company: str, executive_order: list[str]) -> tuple[str, str, bool]:
    """Precedence over the queries that found one (deduplicated) article, for its company only:
    entity: an executive key wins (queries.yaml order: bloisi, sgourdos, kolek, beinat), else
            'company' if any query group found it, else blank (rss/newsroom only: classifier fills).
    topic:  the fixed topic of the `topics` group(s); blank when none, or when two different
            topics found it (conflict -> classifier decides). Returns (entity, topic, conflict)."""
    ms = [m for m in members if m[0] == company]
    execs = {m[3] for m in ms if m[1] == "exec"}
    entity = next((k for k in executive_order if k in execs), "company" if ms else "")
    topics = sorted({m[4] for m in ms if m[4]})
    if len(topics) == 1:
        return entity, topics[0], False
    return entity, "", len(topics) > 1


class Clusters:
    """De-duplicate on url OR normalised title (as before); first row wins, later rows only add
    their query membership. merge_url() re-keys on the final (resolved) url."""

    def __init__(self):
        self.rows: list[dict] = []
        self.by_url: dict[str, int] = {}
        self.by_title: dict[str, int] = {}

    def add(self, row: dict) -> bool:
        nt = norm_title(row["title"])
        i = self.by_url.get(row["url"])
        if i is None and nt:
            i = self.by_title.get(nt)
        if i is not None:
            self.rows[i]["members"].extend(row["members"])
            return False
        self.by_url[row["url"]] = len(self.rows)
        if nt:
            self.by_title[nt] = len(self.rows)
        self.rows.append(row)
        return True


def dedup_final(rows: list[dict]) -> tuple[list[dict], int]:
    c = Clusters()
    for r in rows:
        c.add(r)
    return c.rows, len(rows) - len(c.rows)


def process(collected: list[dict], cfg: dict, today: dt.date, resolver: Resolver | None,
            stats: dict) -> list[dict]:
    """Window, C exclusions, A filters, dedup, URL resolution, entity/topic, id. No I/O except resolver."""
    cutoff = today - dt.timedelta(days=WINDOW_DAYS)
    title_ex_re = build_title_exclude_re()
    per = {c: Counter() for c in COMPANIES}
    excl = {c: Counter() for c in COMPANIES}
    clusters = Clusters()
    for r in collected:
        c = r["company"]
        per[c]["fetched"] += 1
        if r["published"] < cutoff or r["published"] > today + dt.timedelta(days=1):
            continue
        per[c]["in_window"] += 1
        xid, saved = c_exclusion(r, cfg)
        if saved:
            per[c]["kept_by_unless_company_name"] += 1
        if xid:
            excl[c][xid] += 1
            continue
        per[c]["after_c_exclusions"] += 1
        aid = a_filter(r, title_ex_re)
        if aid:
            excl[c][aid] += 1
            continue
        per[c]["after_a_filters"] += 1
        clusters.add(r)
    rows = clusters.rows

    gn_links = [r["url_source"] for r in rows if gn_article_id(r["url_source"])]
    resolved = resolver.resolve(gn_links) if resolver else {}
    for r in rows:
        if gn_article_id(r["url_source"]):
            if r["url_source"] in resolved:
                r["url"], r["url_resolved"] = resolved[r["url_source"]], "yes"
            else:
                r["url"], r["url_resolved"] = r["url_source"], "no"
        else:
            r["url_resolved"] = "yes"     # direct outlet link from an rss feed
    rows, dup_after_resolve = dedup_final(rows)

    conflicts = Counter()
    for r in rows:
        r["entity"], r["topic"], conflict = assign_entity_topic(r["members"], r["company"],
                                                                cfg["executive_order"])
        if conflict:
            conflicts[r["company"]] += 1
        r["id"] = hashlib.sha1(r["url"].encode("utf-8")).hexdigest()
    for c in COMPANIES:
        per[c]["after_dedup"] = sum(1 for r in rows if r["company"] == c)
    stats["per_company"] = {c: dict(per[c]) for c in COMPANIES}
    stats["dropped_by_rule"] = {c: dict(excl[c]) for c in COMPANIES}
    stats["duplicates_merged_after_url_resolution"] = dup_after_resolve
    stats["fixed_topic_conflicts_left_blank"] = dict(conflicts)
    stats["url_resolution"] = dict(resolver.stats) if resolver else {}
    stats["url_resolved"] = dict(Counter(r["url_resolved"] for r in rows))
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


def write_csv(rows: list[dict], path: Path) -> None:
    rows = sorted(rows, key=lambda r: (r["published"], r["id"]), reverse=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            out = {c: "" for c in COLUMNS}
            out.update({k: v for k, v in r.items() if k in COLUMNS})
            out["published"] = r["published"].isoformat()
            w.writerow(out)


def probe(cfg: dict) -> int:
    """Exactly one Google News RSS request (no redirects followed, no retry)."""
    url = gn_url(cfg, "Prosus", "en")
    print(f"probe: GET {url}")
    try:
        r = _session.get(url, timeout=20, allow_redirects=False)
    except requests.RequestException as e:
        print(f"probe: request failed: {e.__class__.__name__}: {str(e)[:120]}")
        return 1
    loc = urllib.parse.urljoin(url, r.headers.get("location", "")) if r.is_redirect else ""
    body = "" if r.is_redirect else r.text
    reason = looks_blocked(r.status_code, url, loc, body)
    items = len(feedparser.parse(r.content).entries) if r.status_code == 200 else 0
    print(f"probe: HTTP {r.status_code}; items {items}; blocked: {'yes (' + reason + ')' if reason else 'no'}"
          + (f"; redirect to {loc}" if loc else ""))
    if reason:
        return 3
    return 0 if r.status_code == 200 and items > 0 else 1


def dry_run(cfg: dict) -> int:
    """No network. Prints the request plan and checks it against feeds.yaml."""
    plan, skipped = build_plan(cfg)
    mism = [s["name"] for s in plan if s["name"] in cfg["gn_feeds"] and cfg["gn_feeds"][s["name"]]["url"] != s["url"]]
    missing = [s["name"] for s in plan if s["name"] not in cfg["gn_feeds"]]
    print(f"Google News RSS requests: {len(plan)} (skipped use: no: {len(skipped)}: "
          + ", ".join(s["name"] for s in skipped) + ")")
    print(f"by group: {dict(Counter(s['group'] for s in plan))}; by edition: {dict(Counter(s['edition'] for s in plan))}")
    print(f"floor {cfg['min_gap']:.0f} s -> RSS phase >= {len(plan) * cfg['min_gap'] / 60:.1f} min")
    print(f"plan urls identical to feeds.yaml: {len(plan) - len(mism) - len(missing)}/{len(plan)}; "
          f"mismatched: {mism or 'none'}; not in feeds.yaml: {missing or 'none'}")
    rss = usable_rss(cfg)
    print(f"rss feeds used: {[f['name'] for f in rss]}")
    cache = json.loads(URL_CACHE.read_text(encoding="utf-8")) if URL_CACHE.exists() else {}
    print(f"url cache entries: {len(cache)}")
    for s in plan[:3]:
        print(f"  e.g. {s['name']}: {s['url']}")
    return 0 if not mism and not missing else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--probe", action="store_true", help="one Google News RSS request; exit 0 ok, 3 blocked")
    ap.add_argument("--dry-run", action="store_true", help="print the request plan; no network")
    ap.add_argument("--no-resolve", action="store_true",
                    help="skip Google URL resolution (cache and offline decode still apply)")
    args = ap.parse_args(argv)
    cfg = load_config()
    if args.dry_run:
        return dry_run(cfg)
    if args.probe:
        return probe(cfg)

    today = dt.date.today()
    plan, skipped = build_plan(cfg)
    gate = GoogleGate(cfg["min_gap"])
    print(f"Google News: {len(plan)} RSS requests at >= {cfg['min_gap']:.0f} s apart "
          f"(~{len(plan) * cfg['min_gap'] / 60:.0f} min); {len(skipped)} skipped (use: no)")
    collected: list[dict] = []
    feed_counts: Counter = Counter()
    empty_feeds: list[str] = []
    for s in plan:
        d = fetch_google_rss(gate, s["url"])
        if gate.blocked:
            break
        n = len(d.entries) if d else 0
        feed_counts[s["name"]] = n
        if n == 0:
            empty_feeds.append(s["name"])
        for e in (d.entries if d else []):
            row = entry_to_row(e, s["name"], s["company"])
            if row:
                row["members"].append((s["company"], s["group"], s["key"], s["entity"], s["topic"]))
                collected.append(row)

    stats = {"date": today.isoformat(), "google_rss_planned": len(plan), "google_rss_skipped_use_no": len(skipped)}
    if gate.blocked:
        stats.update(google_blocked=gate.blocked, google_requests=gate.count,
                     google_rss_done=len(feed_counts))
        STATS_JSON.parent.mkdir(parents=True, exist_ok=True)
        STATS_JSON.write_text(json.dumps(stats, indent=2), encoding="utf-8")
        print(f"Google blocked during the RSS phase after {len(feed_counts)}/{len(plan)} feeds; "
              f"{RAW_CSV.relative_to(ROOT)} NOT overwritten. Stats: {STATS_JSON.relative_to(ROOT)}")
        return 3

    # plain RSS feeds: keep items whose title/summary names a tracked company (C's company_names)
    for feed in usable_rss(cfg):
        d = fetch(feed["url"])
        n = len(d.entries) if d else 0
        feed_counts[feed["name"]] = n
        if n == 0:
            empty_feeds.append(feed["name"])
        for e in (d.entries if d else []):
            text = f"{strip_html(e.get('title', ''))} {strip_html(e.get('summary', ''))}"
            company = next((c for c in COMPANIES if c in cfg["company_names"]
                            and cfg["company_names"][c].search(text)), None)
            if company:
                row = entry_to_row(e, feed["name"], company)
                if row:
                    row["outlet"] = feed.get("outlet") or row["outlet"] or feed["name"]
                    row["outlet_tier"] = feed.get("outlet_tier") or outlet_tier(row["outlet"])
                    collected.append(row)

    collected = run_newsroom() + collected
    resolver = Resolver(None if args.no_resolve else gate)
    rows = process(collected, cfg, today, resolver, stats)
    write_csv(rows, RAW_CSV)
    stats.update(google_requests=gate.count, google_blocked=gate.blocked or "",
                 rows=len(rows), rows_per_company=dict(Counter(r["company"] for r in rows)),
                 items_per_feed=dict(feed_counts), feeds_with_0_items=empty_feeds)
    STATS_JSON.parent.mkdir(parents=True, exist_ok=True)
    STATS_JSON.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nWrote {len(rows)} rows to {RAW_CSV.relative_to(ROOT)} (window {today - dt.timedelta(days=WINDOW_DAYS)} to {today})")
    print("Per company (fetched -> in window -> after C exclusions -> after A filters -> after dedup):")
    for c in COMPANIES:
        p = stats["per_company"][c]
        print(f"  {c}: {p.get('fetched', 0)} -> {p.get('in_window', 0)} -> {p.get('after_c_exclusions', 0)} -> "
              f"{p.get('after_a_filters', 0)} -> {p.get('after_dedup', 0)}; unless kept: "
              f"{p.get('kept_by_unless_company_name', 0)}; dropped by rule: {stats['dropped_by_rule'][c]}")
    print(f"URL resolution: {stats['url_resolution']}; url_resolved: {stats['url_resolved']}; "
          f"Google requests: {gate.count}; blocked: {gate.blocked or 'no'}")
    print("Rows per source:")
    for s, n in sorted(Counter(r["source"] for r in rows).items()):
        print(f"  {s}: {n}")
    print("Rows per outlet_tier:", dict(Counter(r["outlet_tier"] for r in rows)))
    print("Feeds/queries with 0 items:", ", ".join(empty_feeds) if empty_feeds else "none")
    print(f"Stats written to {STATS_JSON.relative_to(ROOT)}")
    append_log("collect", len(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())

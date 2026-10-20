"""Offline tests for collect.py: no socket can open while these run.

Run: python -m unittest tests/test_collect_offline.py -v
Covers query building vs feeds.yaml, the Google rate floor and block stop, C's exclusions and
`unless`, entity/topic precedence, dedup, URL decoding/resolution with a fake Google, probe.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
import hashlib
import importlib.util
import json
import socket
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import collect  # noqa: E402

_real_connect = socket.socket.connect


def _no_network(*a, **k):
    raise AssertionError("network access attempted during offline test")


def setUpModule():
    socket.socket.connect = _no_network
    socket.create_connection = _no_network


def tearDownModule():
    socket.socket.connect = _real_connect


CFG = collect.load_config()
TODAY = dt.date(2026, 10, 19)


# ------------------------------------------------------------------ fakes

class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeResp:
    def __init__(self, status=200, text="", headers=None):
        self.status_code, self.text = status, text
        self.content = text.encode("utf-8")
        self.headers = headers or {}
        self.is_redirect = status in (301, 302, 303, 307, 308) and "location" in self.headers


class FakeSession:
    """Answers from a callable(method, url, kw) -> FakeResp; records calls; each call takes 0.2 s."""

    def __init__(self, handler, clock=None):
        self.handler, self.clock, self.calls = handler, clock, []

    def request(self, method, url, allow_redirects=True, timeout=None, **kw):
        assert allow_redirects is False, "Google requests must not auto-follow redirects"
        self.calls.append((method, url))
        if self.clock:
            self.clock.t += 0.2
        return self.handler(method, url, kw)

    def get(self, url, **kw):
        return self.request("GET", url, **kw)


def old_style_id(url: str) -> str:
    raw = url.encode()
    b = b"\x08\x13\x22" + bytes([len(raw)]) + raw + b"\xd2\x01\x00"
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def new_style_id(n: int) -> str:
    tok = f"AU_yqLsynthetic{n:04d}abcdefghijklmnopqrstuvwxyz".encode()
    return base64.urlsafe_b64encode(b"\x08\x13\x22" + bytes([len(tok)]) + tok).decode().rstrip("=")


def gn_link(art_id: str) -> str:
    return f"https://news.google.com/rss/articles/{art_id}?oc=5"


def page_html(n: int) -> str:
    return f'<c-wiz><div jscontroller="x" data-n-a-sg="SIG{n}" data-n-a-ts="17600000{n:02d}"></div></c-wiz>'


def batch_reply(items: list[tuple[str, str]]) -> str:
    rows = [["wrb.fr", "Fbv4je", json.dumps(["garturlres", u, 1]), None, None, None, rid] for rid, u in items]
    rows += [["di", 42], ["af.httprm", 42, "x", 1]]
    return ")]}'\n\n" + json.dumps(rows)


def google_handler(outlet_for, fail_page=None, block_at=None, counter=None):
    """Fake Google: article page -> signature; batchexecute -> garturlres for each envelope."""
    counter = counter if counter is not None else {"n": 0}

    def h(method, url, kw):
        counter["n"] += 1
        if block_at is not None and counter["n"] >= block_at:
            return FakeResp(429, "Too Many Requests")
        if method == "GET":
            art = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
            n = int(base64.urlsafe_b64decode(art + "==")[4:].decode()[15:19])
            if fail_page and fail_page.get(n, 0) > 0:
                fail_page[n] -= 1
                return FakeResp(500, "err")
            return FakeResp(200, page_html(n))
        body = urllib.parse.unquote(kw["data"][len("f.req="):])
        envs = json.loads(body)[0]
        out = []
        for _, inner, _, rid in envs:
            inner = json.loads(inner)
            out.append((rid, outlet_for(inner[2])))
        return FakeResp(200, batch_reply(out))
    return h


# ------------------------------------------------------------------ tests

class TestPlan(unittest.TestCase):
    def test_plan_matches_feeds_yaml(self):
        plan, skipped = collect.build_plan(CFG)
        self.assertEqual(len(plan), 114)   # 122 - 8: sgourdos, kolek removed (2 queries x 2 editions each)
        self.assertEqual(len(skipped), 6)
        feeds = CFG["gn_feeds"]
        for s in skipped:
            self.assertEqual(feeds[s["name"]]["use"], "no")
        for s in plan:
            f = feeds[s["name"]]
            self.assertEqual(f["url"], s["url"], s["name"])
            self.assertEqual(f["use"], "yes")
            self.assertEqual(f["company"], s["company"])
            self.assertEqual(f.get("topic", ""), s["topic"], s["name"])
            if s["group"] == "peer":   # feeds.yaml says entity: none; the task maps peers -> company
                self.assertEqual((f["entity"], s["entity"]), ("none", "company"), s["name"])
            elif "entity" in f:        # topic entries carry no entity key
                self.assertEqual(f["entity"], s["entity"], s["name"])
            qs = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(s["url"]).query))
            self.assertTrue(qs["q"].endswith(" when:30d"))
            self.assertEqual(qs["q"], s["q"] + " when:30d")
            ed = CFG["editions"][s["edition"]]
            self.assertEqual((qs["hl"], qs["gl"], qs["ceid"]), (ed["hl"], ed["gl"], ed["ceid"]))
        self.assertEqual(len({s["url"] for s in plan}), 114)

    def test_rss_usable(self):
        names = [f["name"] for f in collect.usable_rss(CFG)]
        self.assertEqual(names, ["prosus_newsroom", "fd", "nrc", "nu_economie", "silicon_canals",
                                 "economic_times_tech"])


class TestGate(unittest.TestCase):
    def test_floor_redirects_and_block(self):
        clk = FakeClock()
        seq = iter([FakeResp(302, "", {"location": "https://news.google.com/rss/search?q=x"}),
                    FakeResp(200, "<rss></rss>"), FakeResp(200, "<rss></rss>"),
                    FakeResp(200, "<rss></rss>"), FakeResp(429, "slow down")])
        sess = FakeSession(lambda m, u, k: next(seq), clk)
        g = collect.GoogleGate(3, session=sess, clock=clk, sleep=clk.sleep)
        for _ in range(4):
            g.request("GET", "https://news.google.com/rss/search?q=a")
        self.assertEqual(g.blocked, "HTTP 429")
        self.assertEqual(len(sess.calls), 5)          # 1 redirect hop + 3 + the 429
        gaps = [b - a for a, b in zip(g.request_starts, g.request_starts[1:])]
        self.assertTrue(all(x >= 3.0 + 0.2 - 1e-9 for x in gaps), gaps)
        self.assertIsNone(g.request("GET", "https://news.google.com/rss/search?q=b"))
        self.assertEqual(len(sess.calls), 5)          # nothing sent after the block
        with self.assertRaises(ValueError):
            collect.GoogleGate(3, session=sess).request("GET", "https://fd.nl/?rss")

    def test_block_detection(self):
        lb = collect.looks_blocked
        self.assertTrue(lb(503, "https://news.google.com/x", "", ""))
        self.assertTrue(lb(302, "https://news.google.com/x", "https://www.google.com/sorry/index?continue=x", ""))
        self.assertTrue(lb(200, "u", "", "Our systems have detected unusual traffic from your computer"))
        self.assertFalse(lb(200, "https://news.google.com/x", "", "<rss><item>Prosus</item></rss>"))

    def test_fetch_refuses_google(self):
        with self.assertRaises(ValueError):
            collect.fetch("https://news.google.com/rss/search?q=x")


def mkrow(raw_title, company="prosus", members=None, url=None, published=TODAY, outlet="Reuters"):
    title = collect.strip_outlet_suffix(raw_title, outlet)
    return {"published": published, "source": "gn_en_core_prosus", "outlet": outlet,
            "outlet_tier": collect.outlet_tier(outlet), "title": title, "raw_title": raw_title,
            "url": url or gn_link(old_style_id("https://example.com/" + hashlib.md5(raw_title.encode()).hexdigest())),
            "url_source": "", "summary": "", "language": "", "company": company,
            "members": members if members is not None else [(company, "core", "", "company", "")]}


class TestExclusions(unittest.TestCase):
    def ex(self, title, company="prosus"):
        return collect.c_exclusion(mkrow(title, company), CFG)

    def test_each_rule(self):
        cases = {
            "job_ads": "Vacature: data analist bij Prosus - Indeed",
            "auto_stock_ticks": "Prosus-Aktie: was nun? - ad-hoc-news.de",
            "stock_tip_sites": "Is Prosus undervalued? - Simply Wall St",
            "forecast_spam": "Prosus stock forecast 2027 - Example",
            "quote_pages": "Prosus N.V. (PRX.AS) Stock Price, News, Quote - Yahoo Finance",
            "ticker_valuation": "Prosus (ENXTAM:PRX) valuation check - Yahoo",
            "directories": "Prosus funding rounds & list of investors - Tracxn",
            "live_scores": "Prosus vs Team X live score - Sofascore",
            "sponsorship_sport": "ZALANDO ARRIVAL MOMENT with the team - Royal Belgian FA",
            "shopping_giveaways": "Zalando kortingscode oktober - Example",
        }
        for rid, t in cases.items():
            self.assertEqual(self.ex(t)[0], rid, t)
        self.assertEqual(self.ex("Prosus completes JET deal - Reuters"), (None, False))

    def test_unless_company_name(self):
        wrap_named = "AEX hoger dankzij Prosus - NU.nl"
        wrap_plain = "AEX sluit hoger, Damrak wacht op rentebesluit - NU.nl"
        self.assertEqual(self.ex(wrap_named, "prosus"), (None, True))           # named -> kept
        self.assertEqual(self.ex("Naspers lifts JSE - Business Day", "prosus"), (None, True))
        self.assertEqual(self.ex(wrap_plain, "prosus")[0], "market_wrap_no_name")
        # the name must be the ROW's company: a Prosus headline does not save a Zalando row
        self.assertEqual(self.ex(wrap_named, "peer_zalando")[0], "market_wrap_no_name")
        self.assertEqual(self.ex("DAX: Zalando legt zu - Handelsblatt", "peer_zalando"), (None, True))
        # unless only applies to the rule that has it
        self.assertEqual(self.ex("Prosus stock forecast - Example")[0], "forecast_spam")

    def test_anchors_use_raw_title(self):
        r = mkrow("Prosus shares - ad-hoc-news.de", outlet="ad-hoc-news.de")
        self.assertEqual(r["title"], "Prosus shares")            # suffix stripped for display
        self.assertEqual(collect.c_exclusion(r, CFG)[0], "auto_stock_ticks")


class TestEntityTopic(unittest.TestCase):
    order = CFG["executive_order"]

    def test_precedence(self):
        a = collect.assign_entity_topic
        core = ("prosus", "core", "", "company", "")
        rec = ("prosus", "recall", "", "company", "")
        bl = ("prosus", "exec", "bloisi", "bloisi", "")
        be = ("prosus", "exec", "beinat", "beinat", "")
        t_ai = ("prosus", "topic", "ai", "company", "ai")
        t_v = ("prosus", "topic", "ventures_investments", "company", "ventures_investments")
        peer = ("peer_zalando", "peer", "peer_zalando", "company", "")
        self.assertEqual(a([core], "prosus", self.order), ("company", "", False))
        self.assertEqual(a([core, bl, rec], "prosus", self.order), ("bloisi", "", False))
        self.assertEqual(a([be, bl], "prosus", self.order), ("bloisi", "", False))
        self.assertEqual(a([be, t_ai], "prosus", self.order), ("beinat", "ai", False))
        self.assertEqual(a([t_ai, t_v], "prosus", self.order), ("company", "", True))
        self.assertEqual(a([peer, bl], "peer_zalando", self.order), ("company", "", False))
        self.assertEqual(a([], "prosus", self.order), ("", "", False))
        self.assertEqual(self.order, ["bloisi", "beinat"])


class TestDecoding(unittest.TestCase):
    def test_offline(self):
        u = "https://www.reuters.com/business/prosus-results-2026-10-01/"
        self.assertEqual(collect.decode_offline(old_style_id(u)), u)
        self.assertIsNone(collect.decode_offline(new_style_id(1)))
        self.assertIsNone(collect.decode_offline("not*base64"))
        self.assertEqual(collect.gn_article_id(gn_link("CBMiABC")), "CBMiABC")
        self.assertIsNone(collect.gn_article_id("https://fd.nl/x"))

    def test_batchexecute_roundtrip(self):
        body = collect.build_batchexecute_body([("0", "ID0", "1760000000", "S0"), ("1", "ID1", "1760000001", "S1")])
        envs = json.loads(urllib.parse.unquote(body[len("f.req="):]))[0]
        self.assertEqual([e[0] for e in envs], ["Fbv4je", "Fbv4je"])
        inner = json.loads(envs[1][1])
        self.assertEqual((inner[0], inner[2], inner[3], inner[4]), ("garturlreq", "ID1", 1760000001, "S1"))
        got = collect.parse_batchexecute(batch_reply([("0", "https://a.example/1"), ("1", "https://b.example/2")]))
        self.assertEqual(got, [("0", "https://a.example/1"), ("1", "https://b.example/2")])


class TestResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name) / "url_cache.json"

    def tearDown(self):
        self.tmp.cleanup()

    def gate(self, handler):
        clk = FakeClock()
        sess = FakeSession(handler, clk)
        return collect.GoogleGate(3, session=sess, clock=clk, sleep=clk.sleep), sess

    def test_resolve_cache_and_zero_rerun(self):
        links = [gn_link(new_style_id(i)) for i in range(30)] + [gn_link(old_style_id("https://x.example/a"))]
        g, sess = self.gate(google_handler(lambda a: f"https://outlet.example/{a[-6:]}"))
        res = collect.Resolver(g, self.cache, batch=25).resolve(links)
        self.assertEqual(len(res), 31)
        self.assertEqual(len(sess.calls), 30 + 2)          # 30 page GETs + 2 batched POSTs; offline link 0
        gaps = [b - a for a, b in zip(g.request_starts, g.request_starts[1:])]
        self.assertGreaterEqual(min(gaps), 3.0)
        g2, sess2 = self.gate(google_handler(lambda a: "https://never.example"))
        res2 = collect.Resolver(g2, self.cache).resolve(links)
        self.assertEqual(res2, res)
        self.assertEqual(len(sess2.calls), 0)               # re-run: zero requests

    def test_one_retry_per_link(self):
        links = [gn_link(new_style_id(i)) for i in range(3)]
        g, sess = self.gate(google_handler(lambda a: "https://o.example/" + a[-4:], fail_page={0: 1, 1: 5}))
        r = collect.Resolver(g, self.cache)
        res = r.resolve(links)
        gets = [u for m, u in sess.calls if m == "GET"]
        self.assertEqual(len(gets), 2 + 2 + 1)             # link0: fail+retry ok; link1: fail+retry fail; link2: 1
        self.assertEqual(len(res), 2)                       # link1 unresolved
        self.assertEqual(r.stats["unresolved"], 1)

    def test_block_stops_everything(self):
        links = [gn_link(new_style_id(i)) for i in range(10)]
        g, sess = self.gate(google_handler(lambda a: "https://o.example/x", block_at=4))
        r = collect.Resolver(g, self.cache)
        res = r.resolve(links)
        self.assertEqual(len(sess.calls), 4)                # 3 GETs, 4th is the 429, then nothing
        self.assertEqual(res, {})
        self.assertEqual(r.stats["unresolved"], 10)
        self.assertEqual(g.blocked, "HTTP 429")

    def test_missing_from_batch_gets_one_single_post(self):
        links = [gn_link(new_style_id(i)) for i in range(3)]
        posts = {"n": 0}

        def h(method, url, kw):
            if method == "GET":
                n = int(base64.urlsafe_b64decode(url.split("/")[-1].split("?")[0] + "==")[4:].decode()[15:19])
                return FakeResp(200, page_html(n))
            posts["n"] += 1
            envs = json.loads(urllib.parse.unquote(kw["data"][6:]))[0]
            if len(envs) > 1:                               # batch answers only the first link
                return FakeResp(200, batch_reply([(envs[0][3], "https://o.example/first")]))
            return FakeResp(200, batch_reply([(envs[0][3], "https://o.example/single")]))
        g, sess = self.gate(h)
        res = collect.Resolver(g, self.cache).resolve(links)
        self.assertEqual(posts["n"], 1 + 2)
        self.assertEqual(sorted(res.values()), ["https://o.example/first"] + ["https://o.example/single"] * 2)


RSS_TMPL = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>"""
ITEM = """<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate>
<description>{title}</description><source url="https://x">{outlet}</source></item>"""


def feed(items):
    return feedparser.parse(RSS_TMPL.format(items="".join(ITEM.format(**i) for i in items)))


class TestPipeline(unittest.TestCase):
    def test_process_end_to_end(self):
        d = "Sun, 18 Oct 2026 08:00:00 GMT"
        old = "Mon, 01 Jun 2026 08:00:00 GMT"
        L = {k: gn_link(old_style_id(f"https://news.example/{k}")) for k in "abcdefgh"}
        nl_dup = gn_link(new_style_id(7))     # same article, other edition, different redirect
        specs = {s["name"]: s for s in collect.build_plan(CFG)[0]}
        inputs = [
            ("gn_en_core_prosus", [
                dict(title="Bloisi says Prosus will double AI spend - Reuters", link=L["a"], date=d, outlet="Reuters"),
                dict(title="Prosus shares rise 3% after results - Bloomberg", link=L["b"], date=d, outlet="Bloomberg"),
                dict(title="Is Prosus cheap? - Simply Wall St", link=L["c"], date=d, outlet="Simply Wall St"),
                dict(title="AEX sluit hoger - NU.nl", link=L["d"], date=d, outlet="NU.nl"),
                dict(title="Old Prosus story - FD", link=L["e"], date=old, outlet="FD"),
                dict(title="Prosus and Tencent: an AI play - Financial Times", link=L["f"], date=d, outlet="Financial Times")]),
            ("gn_nl_core_prosus", [
                dict(title="Bloisi says Prosus will double AI spend - Reuters", link=nl_dup, date=d, outlet="Reuters")]),
            ("gn_en_exec_bloisi_name", [
                dict(title="Bloisi says Prosus will double AI spend - Reuters", link=L["a"], date=d, outlet="Reuters")]),
            ("gn_en_topic_results_results", [
                dict(title="Prosus shares rise 3% after results - Bloomberg", link=L["b"], date=d, outlet="Bloomberg")]),
            ("gn_en_topic_ai_ai", [
                dict(title="Prosus and Tencent: an AI play - Financial Times", link=L["f"], date=d, outlet="Financial Times")]),
            ("gn_en_topic_tencent_stake_tencent", [
                dict(title="Prosus and Tencent: an AI play - Financial Times", link=L["f"], date=d, outlet="Financial Times")]),
            ("gn_en_peer_peer_zalando_title", [
                dict(title="Zalando beats forecasts - Reuters", link=L["g"], date=d, outlet="Reuters"),
                dict(title="Euro Beinat: Zalando? no, Prosus AI - Sifted", link=L["h"], date=d, outlet="Sifted")]),
        ]
        collected = []
        for name, items in inputs:
            s = specs[name]
            for e in feed(items).entries:
                r = collect.entry_to_row(e, name, s["company"])
                r["members"].append((s["company"], s["group"], s["key"], s["entity"], s["topic"]))
                collected.append(r)
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "c.json"
            res = collect.Resolver(None, cache)            # no Google: cache + offline decode only
            stats = {}
            rows = collect.process(collected, CFG, TODAY, res, stats)
            out = Path(tmp) / "raw.csv"
            collect.write_csv(rows, out)
            with out.open() as f:
                got = list(csv.DictReader(f))
            spec = importlib.util.spec_from_file_location("check_raw", ROOT / "scripts" / "check_articles_raw.py")
            chk = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(chk)
            self.assertEqual(chk.main(out, TODAY), 0)
            self.assertEqual(list(got[0].keys()), collect.COLUMNS)
        by_title = {r["title"]: r for r in got}
        self.assertEqual(set(by_title), {
            "Bloisi says Prosus will double AI spend", "Prosus shares rise 3% after results",
            "Prosus and Tencent: an AI play", "Zalando beats forecasts",
            "Euro Beinat: Zalando? no, Prosus AI"})
        a = by_title["Bloisi says Prosus will double AI spend"]
        self.assertEqual((a["entity"], a["topic"], a["company"]), ("bloisi", "", "prosus"))
        self.assertEqual(a["url"], "https://news.example/a")
        self.assertEqual((a["url_source"], a["url_resolved"]), (L["a"], "yes"))
        self.assertEqual(a["id"], hashlib.sha1(b"https://news.example/a").hexdigest())
        b = by_title["Prosus shares rise 3% after results"]     # A's ticker regex no longer drops it
        self.assertEqual((b["entity"], b["topic"]), ("company", "results"))
        f = by_title["Prosus and Tencent: an AI play"]
        self.assertEqual(f["topic"], "")                          # ai vs tencent_stake -> classifier
        h = by_title["Euro Beinat: Zalando? no, Prosus AI"]
        self.assertEqual((h["company"], h["entity"]), ("peer_zalando", "company"))
        self.assertEqual(stats["per_company"]["prosus"]["fetched"], 11)
        self.assertEqual(stats["per_company"]["prosus"]["in_window"], 10)
        self.assertEqual(stats["dropped_by_rule"]["prosus"],
                         {"stock_tip_sites": 1, "market_wrap_no_name": 1})
        self.assertEqual(stats["per_company"]["prosus"]["after_dedup"], 3)
        self.assertEqual(stats["fixed_topic_conflicts_left_blank"], {"prosus": 1})
        for r in got:
            self.assertIn(r["url_resolved"], ("yes", "no"))
            self.assertEqual(r["id"], hashlib.sha1(r["url"].encode()).hexdigest())
            self.assertEqual(r["sentiment"] + r["confidence"] + r["flag"], "")

    def test_unresolved_keeps_redirect(self):
        link = gn_link(new_style_id(3))
        r = mkrow("Prosus news item - Reuters", url=link)
        r["url_source"] = link
        with tempfile.TemporaryDirectory() as tmp:
            rows = collect.process([r], CFG, TODAY, collect.Resolver(None, Path(tmp) / "c.json"), {})
        self.assertEqual((rows[0]["url"], rows[0]["url_source"], rows[0]["url_resolved"]), (link, link, "no"))
        self.assertEqual(rows[0]["id"], hashlib.sha1(link.encode()).hexdigest())


class TestProbe(unittest.TestCase):
    def run_probe(self, resp):
        calls = []
        orig = collect._session.get

        def fake_get(url, **kw):
            calls.append(url)
            self.assertFalse(kw.get("allow_redirects", True))
            return resp
        collect._session.get = fake_get
        try:
            code = collect.probe(CFG)
        finally:
            collect._session.get = orig
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], "https://news.google.com/rss/search?q=Prosus%20when%3A30d&hl=en-US&gl=US&ceid=US%3Aen")
        return code

    def test_codes(self):
        ok = RSS_TMPL.format(items=ITEM.format(title="Prosus x - Reuters", link="https://n", date="Sun, 18 Oct 2026 08:00:00 GMT", outlet="Reuters"))
        self.assertEqual(self.run_probe(FakeResp(200, ok)), 0)
        self.assertEqual(self.run_probe(FakeResp(429, "no")), 3)
        self.assertEqual(self.run_probe(FakeResp(503, "unusual traffic")), 3)
        self.assertEqual(self.run_probe(FakeResp(302, "", {"location": "https://www.google.com/sorry/index"})), 3)
        self.assertEqual(self.run_probe(FakeResp(200, "<html>captcha-form id=\"captcha-form\"</html>")), 3)


if __name__ == "__main__":
    unittest.main()

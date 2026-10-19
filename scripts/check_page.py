"""Static invariants for site/index.html (stdlib + pandas only).

1. Self-contained: no <script, <link, @import, url(; the only http(s) URLs are article hrefs from the CSV.
2. "Latest Prosus coverage" table = the 8 most recent valid Prosus rows (published desc, id asc), same order.
3. Every table link has target=_blank rel=noopener and a title equal to its text.
4. No leftovers of the removed sections (brief, CRM diagram).
Exit 1 on any failure.
"""
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import build_page  # noqa: E402


class Rows(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_table = self.in_tbody = False
        self.rows, self.cell, self.links, self.cur_link = [], None, [], None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table" and "latest" in a.get("class", ""):
            self.in_table = True
        elif self.in_table and tag == "tbody":
            self.in_tbody = True
        elif self.in_tbody and tag == "tr":
            self.rows.append({})
        elif self.in_tbody and tag == "td":
            self.cell = a.get("class", "")
            self.rows[-1].setdefault(self.cell, "")
        elif self.in_tbody and tag == "a":
            self.cur_link = dict(a, text="")
            self.links.append(self.cur_link)
        elif self.in_tbody and tag == "span" and a.get("class") == "o2":
            self.cell = "o2"
        elif tag == "time" and self.in_tbody:
            self.rows[-1]["iso"] = a.get("datetime")

    def handle_endtag(self, tag):
        if tag == "table":
            self.in_table = self.in_tbody = False
        elif tag == "a":
            self.cur_link = None
        elif tag == "td":
            self.cell = None

    def handle_data(self, data):
        if self.cur_link is not None:
            self.cur_link["text"] += data
        if self.cell and self.cell != "o2":
            self.rows[-1][self.cell] += data


def main():
    page = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
    df = build_page.load(build_page.pick_data(None))
    fails = []

    for bad in ("<script", "<link", "@import", "url("):
        n = page.lower().count(bad)
        print(f"count {bad!r}: {n}")
        if n:
            fails.append(f"{bad} present")

    p = Rows()
    p.feed(page)
    hrefs = {l["href"] for l in p.links}
    urls = set(re.findall(r"https?://[^\s\"'<>)]+", page))
    other = sorted(u for u in urls if u not in hrefs)
    print(f"external URLs other than article hrefs: {len(other)} {other}")
    if other:
        fails.append("non-article URLs")
    if not hrefs <= set(df["url"]):
        fails.append("href not in CSV url column")

    prosus = df[df["company"] == "prosus"]
    ok = prosus[prosus["sentiment"].isin(build_page.SENTIMENTS) & prosus["topic"].isin(build_page.TOPIC_LABELS)
                & prosus["published_dt"].notna()]
    exp = ok.sort_values(["published_dt", "id"], ascending=[False, True]).head(8)
    got = [(r.get("iso"), r.get("o", "").strip(), l["text"], r.get("s", "").strip())
           for r, l in zip(p.rows, p.links)]
    want = [(d.strftime("%Y-%m-%d"), o, t, s) for d, o, t, s in
            zip(exp["published_dt"], exp["outlet"], exp["title"], exp["sentiment"])]
    print(f"table rows: {len(p.rows)} (expected {len(want)}); match: {got == want}")
    if got != want:
        fails.append("table rows differ from CSV")
    for l in p.links:
        if l.get("target") != "_blank" or l.get("rel") != "noopener" or l.get("title") != l["text"]:
            fails.append(f"link attrs: {l['href']}")

    for junk in ("brief", "Placeholder", "CRM", "Organisation", "nan", "None"):
        if re.search(rf"\b{junk}\b", page):
            fails.append(f"leftover/junk: {junk}")

    for g in got:
        print(" | ".join([g[0], g[1], g[2][:40], g[3]]))
    print("FAIL: " + "; ".join(fails) if fails else "ALL CHECKS PASS")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

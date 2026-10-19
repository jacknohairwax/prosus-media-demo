"""Invariants for data/articles_raw.csv after collect.py (no network).

Run: python scripts/check_articles_raw.py [path]   exit 0 = all pass, 1 = a check failed
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from collect import COLUMNS, COMPANIES, gn_article_id, norm_title  # noqa: E402

TOPICS = {"", "results", "tencent_stake", "ai", "jet_integration", "ventures_investments",
          "governance_regulation", "other"}
ENTITIES = {"", "company", "bloisi", "sgourdos", "kolek", "beinat"}


def main(path: Path, today: dt.date | None = None) -> int:
    with path.open(newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        header, rows = rd.fieldnames, list(rd)
    fails: list[str] = []

    def check(name: str, bad: list):
        print(f"{'PASS' if not bad else 'FAIL'} {name}: {len(bad)} bad of {len(rows)}"
              + (f" e.g. {bad[:3]}" if bad else ""))
        if bad:
            fails.append(name)

    check("header = schema + url_source,url_resolved", [] if header == COLUMNS else [header])
    check("id = sha1(url)", [r["id"] for r in rows if r["id"] != hashlib.sha1(r["url"].encode()).hexdigest()])
    check("no duplicate id", [k for k, n in Counter(r["id"] for r in rows).items() if n > 1])
    check("no duplicate normalised title", [k for k, n in Counter(norm_title(r["title"]) for r in rows).items() if n > 1])
    check("company in schema", [r["company"] for r in rows if r["company"] not in COMPANIES])
    check("entity in collect's set", [r["entity"] for r in rows if r["entity"] not in ENTITIES])
    check("topic blank or taxonomy", [r["topic"] for r in rows if r["topic"] not in TOPICS])
    check("fixed topic only on prosus", [r["id"] for r in rows if r["topic"] and r["company"] != "prosus"])
    check("url_resolved yes/no", [r["url_resolved"] for r in rows if r["url_resolved"] not in ("yes", "no")])
    check("resolved => url is not a Google link",
          [r["url"] for r in rows if r["url_resolved"] == "yes" and gn_article_id(r["url"])])
    check("unresolved => url == url_source (Google link)",
          [r["url"] for r in rows if r["url_resolved"] == "no" and (r["url"] != r["url_source"] or not gn_article_id(r["url"]))])
    today = today or dt.date.today()
    check("published within 30 days", [r["published"] for r in rows
                                       if not re.match(r"^\d{4}-\d{2}-\d{2}$", r["published"])
                                       or not -1 <= (today - dt.date.fromisoformat(r["published"])).days <= 30])
    check("classifier columns blank", [r["id"] for r in rows if r["sentiment"] or r["confidence"] or r["flag"]])
    check("summary <= 600 chars", [r["id"] for r in rows if len(r["summary"]) > 600])
    print("rows per company:", dict(Counter(r["company"] for r in rows)),
          "| url_resolved:", dict(Counter(r["url_resolved"] for r in rows)),
          "| entity:", dict(Counter(r["entity"] for r in rows)),
          "| fixed topic:", dict(Counter(r["topic"] for r in rows if r["topic"])))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "articles_raw.csv"))

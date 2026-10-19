"""Offline test for classify.py: fake classifier, no API call, no network.

Run: python -m unittest tests/test_classify_offline.py -v
Checks: collect's entity and fixed topic survive classification, blanks are filled by the
classifier, url_source/url_resolved pass through, caps and spend limits unchanged.
"""

from __future__ import annotations

import csv
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import classify  # noqa: E402
import collect  # noqa: E402


class FakeClassifier:
    def __init__(self, spent_before):
        self.tin = self.tout = 0
        self.usd = 0.0
        self.stopped = False
        self.structured = self.effort = True

    def classify_batch(self, rows):
        return {r["id"]: {"language": "en", "topic": "other", "entity": "none", "sentiment": "neutral",
                          "flag": "", "confidence": 0.5} for r in rows}


class TestClassifyKeepsCollectFields(unittest.TestCase):
    def test_merge(self):
        socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
        self.assertEqual(classify.COLUMNS, collect.COLUMNS)
        self.assertEqual(classify.CAPS, {"prosus": 250, "peer_deliveryhero": 150, "peer_zalando": 150})
        self.assertEqual((classify.SPEND_STOP_USD, classify.PROJECT_CAP_USD), (8.0, 15.0))
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            raw = [
                dict(id="a" * 40, published="2026-10-18", company="prosus", entity="bloisi", topic="",
                     title="t1", url="https://o/1", url_source="https://news.google.com/rss/articles/X", url_resolved="yes"),
                dict(id="b" * 40, published="2026-10-17", company="prosus", entity="company", topic="results",
                     title="t2", url="https://news.google.com/rss/articles/Y", url_source="https://news.google.com/rss/articles/Y", url_resolved="no"),
                dict(id="c" * 40, published="2026-10-16", company="prosus", entity="", topic="",
                     title="t3", url="https://fd.nl/3", url_source="https://fd.nl/3", url_resolved="yes"),
            ]
            with (t / "raw.csv").open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=collect.COLUMNS)
                w.writeheader()
                for r in raw:
                    w.writerow({c: r.get(c, "") for c in collect.COLUMNS})
            (t / "log.csv").write_text("date,step,rows,tokens_in,tokens_out,usd\n")
            saved = (classify.RAW_CSV, classify.OUT_CSV, classify.LOG_CSV, classify.ENV_FILE, classify.Classifier)
            classify.RAW_CSV, classify.OUT_CSV, classify.LOG_CSV = t / "raw.csv", t / "out.csv", t / "log.csv"
            classify.ENV_FILE, classify.Classifier = t / ".env", FakeClassifier
            os.environ.setdefault("ANTHROPIC_API_KEY", "offline-test")
            try:
                self.assertEqual(classify.main(), 0)
            finally:
                (classify.RAW_CSV, classify.OUT_CSV, classify.LOG_CSV, classify.ENV_FILE,
                 classify.Classifier) = saved
            with (t / "out.csv").open() as f:
                out = {r["id"]: r for r in csv.DictReader(f)}
        a, b, c = out["a" * 40], out["b" * 40], out["c" * 40]
        self.assertEqual((a["entity"], a["topic"]), ("bloisi", "other"))       # entity kept, topic filled
        self.assertEqual((b["entity"], b["topic"]), ("company", "results"))    # fixed topic kept
        self.assertEqual((c["entity"], c["topic"]), ("none", "other"))         # rss row: classifier fills
        self.assertEqual((b["url_source"], b["url_resolved"]), (raw[1]["url_source"], "no"))
        self.assertEqual(list(next(iter(out.values())).keys()), classify.COLUMNS)


if __name__ == "__main__":
    unittest.main()

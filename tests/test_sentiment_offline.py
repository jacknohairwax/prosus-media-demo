"""Offline tests for classify.py --sentiment-only and extra-column pass-through (window F).

No API call, no network. Run: python -m unittest tests/test_sentiment_offline.py -v
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


class FakeSentiment:
    def __init__(self, spent_before, **kw):
        self.kw = kw
        self.tin, self.tout, self.usd, self.stopped = 10, 2, 0.0001, False
        self.structured = self.effort = True

    def classify_batch(self, rows):
        return {r["id"]: {"sentiment": "negative"} for r in rows}


def write(path, rows, cols):
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})


class TestSentimentOnly(unittest.TestCase):
    def setUp(self):
        socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(AssertionError("network"))
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.saved = (classify.RAW_CSV, classify.OUT_CSV, classify.LOG_CSV, classify.ENV_FILE,
                      classify.Classifier)
        classify.RAW_CSV, classify.OUT_CSV, classify.LOG_CSV = t / "raw.csv", t / "out.csv", t / "log.csv"
        classify.ENV_FILE = t / ".env"
        (t / "log.csv").write_text("date,step,rows,tokens_in,tokens_out,usd\n")
        os.environ.setdefault("ANTHROPIC_API_KEY", "offline-test")
        self.cols = classify.COLUMNS + ["on_target", "story_id", "is_canonical"]
        self.rows = [dict(id=c * 40, published="2026-10-1" + str(i), company="prosus", title=f"t{i}, \"q\"",
                          sentiment="positive", topic="ai", entity="company", confidence="0.8",
                          on_target="yes", story_id=c * 40, is_canonical="yes")
                     for i, c in enumerate("ab")]

    def tearDown(self):
        (classify.RAW_CSV, classify.OUT_CSV, classify.LOG_CSV, classify.ENV_FILE,
         classify.Classifier) = self.saved
        self.tmp.cleanup()

    def test_only_sentiment_changes(self):
        write(classify.OUT_CSV, self.rows, self.cols)
        before = classify.OUT_CSV.read_text().splitlines()
        classify.Classifier = FakeSentiment
        self.assertEqual(classify.sentiment_only(), 0)
        after = classify.OUT_CSV.read_text().splitlines()
        self.assertEqual(len(before), len(after))
        self.assertEqual(before[0], after[0])
        si = self.cols.index("sentiment")
        for b, a in zip(csv.reader(before[1:]), csv.reader(after[1:])):
            self.assertEqual(a[si], "negative")
            self.assertEqual(b[:si] + b[si + 1:], a[:si] + a[si + 1:])
        log = classify.LOG_CSV.read_text().splitlines()[-1].split(",")
        self.assertEqual((log[1], log[2]), ("classify_sentiment", "2"))

    def test_run_cap_and_prompt(self):
        self.assertEqual(classify.SENTIMENT_RUN_CAP_USD, 2.0)
        self.assertIn(classify.SENTIMENT_RULE, classify.SYSTEM_PROMPT)
        self.assertIn(classify.SENTIMENT_RULE, classify.SENTIMENT_SYSTEM_PROMPT)
        self.assertNotIn("{SENTIMENT_RULE}", classify.SYSTEM_PROMPT)

    def test_main_keeps_extra_columns(self):
        write(classify.OUT_CSV, self.rows[:1], self.cols)
        write(classify.RAW_CSV, self.rows, classify.COLUMNS)

        class FakeAll(FakeSentiment):
            def classify_batch(self, rows):
                return {r["id"]: {"language": "en", "topic": "other", "entity": "none",
                                  "sentiment": "neutral", "flag": "", "confidence": 0.5} for r in rows}
        classify.Classifier = FakeAll
        self.assertEqual(classify.main(), 0)
        with classify.OUT_CSV.open() as f:
            out = {r["id"]: r for r in csv.DictReader(f)}
        self.assertEqual(list(out["a" * 40].keys()), self.cols)
        self.assertEqual(out["a" * 40]["story_id"], "a" * 40)   # kept from the existing file
        self.assertEqual(out["b" * 40]["story_id"], "")         # new row: blank until stories.py


if __name__ == "__main__":
    unittest.main()

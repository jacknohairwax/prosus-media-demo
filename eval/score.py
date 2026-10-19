"""eval/score.py -- accuracy of the AI tags against Jack's hand labels.

  python eval/score.py                    score eval/eval_template.csv against data/articles.csv
  python eval/score.py path/to/labels.csv score another labelled copy
  python eval/score.py --make-template    (re)write eval/eval_template.csv: 50 random Prosus
                                          rows (fixed seed) with topic/sentiment/flag blank

A row counts as labelled if Jack filled topic or sentiment; on a labelled row a blank flag
means "nothing to escalate" and is compared as such. Topic and sentiment accuracy use only
rows where that column is filled.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
ARTICLES = ROOT / "data" / "articles.csv"
TEMPLATE = ROOT / "eval" / "eval_template.csv"
TEMPLATE_COLUMNS = ["id", "published", "outlet", "title", "url", "summary",
                    "topic", "sentiment", "flag"]
SAMPLE_N = 50
SEED = 42


def make_template() -> int:
    arts = pd.read_csv(ARTICLES, dtype=str, keep_default_na=False)
    prosus = arts[arts["company"] == "prosus"]
    n = min(SAMPLE_N, len(prosus))
    sample = prosus.sample(n=n, random_state=SEED).sort_values(["published", "id"], ascending=False)
    out = sample[TEMPLATE_COLUMNS[:6]].copy()
    for c in ("topic", "sentiment", "flag"):
        out[c] = ""
    out.to_csv(TEMPLATE, index=False)
    note = "" if n == SAMPLE_N else f" (only {len(prosus)} Prosus rows exist)"
    print(f"Wrote {n} rows to {TEMPLATE.relative_to(ROOT)}{note}")
    return 0


def score(labels_path: Path) -> int:
    labels = pd.read_csv(labels_path, dtype=str, keep_default_na=False)
    for c in ("topic", "sentiment", "flag"):
        labels[c] = labels[c].str.strip().str.lower()
    labelled = labels[(labels["topic"] != "") | (labels["sentiment"] != "")]
    if labelled.empty:
        print(f"n labelled: 0 of {len(labels)} rows in {labels_path.name}; nothing to score.")
        return 0
    arts = pd.read_csv(ARTICLES, dtype=str, keep_default_na=False)[["id", "topic", "sentiment", "flag"]]
    j = labelled.merge(arts, on="id", how="inner", suffixes=("_jack", "_ai"))
    missing = len(labelled) - len(j)
    print(f"n labelled: {len(labelled)} of {len(labels)} rows"
          + (f" ({missing} labelled ids not found in data/articles.csv, ignored)" if missing else ""))
    for c in ("topic", "sentiment", "flag"):
        rows = j if c == "flag" else j[j[f"{c}_jack"] != ""]
        if rows.empty:
            print(f"  {c:<9} n=0   accuracy: n/a")
            continue
        hits = int((rows[f"{c}_jack"] == rows[f"{c}_ai"]).sum())
        print(f"  {c:<9} n={len(rows):<3} accuracy: {hits / len(rows):.0%} ({hits}/{len(rows)})")
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--make-template":
        return make_template()
    return score(Path(argv[0]) if argv else TEMPLATE)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

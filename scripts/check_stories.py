"""Invariants for the stories.py columns in data/articles.csv (stdlib + pandas only).

1. on_target: yes/no on every prosus row, blank on every peer row; yes iff the title or summary
   names Prosus, Naspers, Prosus Ventures, AI House, Toqan or Luminate (whole word).
2. Every story_id is the id of a row in the same story; that row has is_canonical=yes and is the
   only canonical row of the story; is_canonical=yes iff story_id == id.
3. A story never mixes companies; off-target rows are singletons.
4. Canonical = best outlet_tier, then earliest published, then id asc, within each story.
5. Re-running stories.assign on the file gives the same three columns (idempotent).
6. Every row of a story is published within 3 days of the story's earliest row.
Exit 1 on any failure.
"""
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import stories  # noqa: E402

TERMS = re.compile(r"\b(Prosus|Naspers|Prosus Ventures|AI House|Toqan|Luminate)\b", re.I)


def main():
    df = pd.read_csv(stories.CSV, dtype=str, keep_default_na=False)
    fails = []
    print(f"rows: {len(df)}; columns present: {all(c in df.columns for c in stories.NEW_COLS)}")

    p = df["company"] == "prosus"
    want = (df["title"] + " " + df["summary"]).map(lambda t: "yes" if TERMS.search(t) else "no")
    bad1 = int((p & (df["on_target"] != want)).sum()) + int((~p & (df["on_target"] != "")).sum())
    print(f"1 on_target wrong: {bad1}; prosus yes/no: {df.loc[p, 'on_target'].value_counts().to_dict()}")
    if bad1:
        fails.append("on_target")

    ids = set(df["id"])
    canon_flag = (df["story_id"] == df["id"]).map({True: "yes", False: "no"})
    per_story = df[df["is_canonical"] == "yes"].groupby("story_id").size()
    bad2 = (int((~df["story_id"].isin(ids)).sum()) + int((df["is_canonical"] != canon_flag).sum())
            + int((per_story != 1).sum()) + int((~df["story_id"].isin(per_story.index)).sum()))
    print(f"2 story/canonical inconsistencies: {bad2}; stories: {df['story_id'].nunique()}")
    if bad2:
        fails.append("story_id/is_canonical")

    mixed = int((df.groupby("story_id")["company"].nunique() > 1).sum())
    size = df.groupby("story_id")["id"].transform("size")
    off_multi = int((p & (df["on_target"] == "no") & (size > 1)).sum())
    print(f"3 mixed-company stories: {mixed}; off-target rows in multi-row stories: {off_multi}")
    if mixed or off_multi:
        fails.append("story membership")

    rank = df["outlet_tier"].map(stories.TIER_RANK).fillna(9)
    best = (df.assign(_r=rank).sort_values(["_r", "published", "id"])
            .drop_duplicates("story_id").set_index("story_id")["id"])
    bad4 = int((best != best.index.to_series()).sum())
    print(f"4 canonical not the best row: {bad4}")
    if bad4:
        fails.append("canonical rule")

    base = [c for c in df.columns if c not in stories.NEW_COLS]
    again = stories.assign(df[base])
    bad5 = int((again[stories.NEW_COLS] != df[stories.NEW_COLS]).any(axis=1).sum())
    print(f"5 rows that change on re-run: {bad5}")
    if bad5:
        fails.append("not idempotent")

    d = pd.to_datetime(df["published"], errors="coerce")
    span = (d.groupby(df["story_id"]).max() - d.groupby(df["story_id"]).min()).dt.days
    print(f"6 stories spanning > {stories.MAX_DAYS} days: {int((span > stories.MAX_DAYS).sum())}; "
          f"max span: {int(span.max())} days")
    if (span > stories.MAX_DAYS).any():
        fails.append("story span")

    print("FAIL: " + "; ".join(fails) if fails else "ALL CHECKS PASS")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()

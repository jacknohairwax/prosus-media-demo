# Window D: integrated live run, 2026-10-19 (window 2026-09-19 to 2026-10-19)

- **Result:** `data/articles.csv` has 226 rows (Prosus 133, Zalando 57, Delivery Hero 36), all classified and all URLs resolved. Offline tests pass (19/19), `scripts/check_articles_raw.py` passes every check, and the Google probe returned HTTP 200 with 93 items (not blocked).
- **Rows per company** (fetched → after C's exclusions → after A's title terms → after dedup):

| Company | Fetched | After C exclusions | After A title terms | Final | C predicted (30 d) |
|---|---|---|---|---|---|
| Prosus | 600 | 449 | 449 | 133 | ~132 (4.4/day) |
| Delivery Hero | 101 | 42 | 39 | 36 | ~39 (1.3/day, lower bound) |
| Zalando | 214 | 115 | 103 | 57 | ~66 (2.2/day, lower bound) |

- **Top exclusion rules:** `auto_stock_ticks` dropped 217 rows in total, `stock_tip_sites` 38, `market_wrap_no_name` 17 (9 kept by `unless`). No caps were hit (250/150/150). **URL resolution:** 226/226 (100%), 358 Google requests at a 3 s minimum gap, never blocked.
- **Duration and spend:** collect took 18 min 56 s and classify 1 min 39 s; 53,874 tokens in and 12,103 out (claude-sonnet-5-5), USD 0.2288. The project total in `runs/log.csv` is USD 0.6333, against the USD 8 stop.
- **Window E's defects:** neither shows up in real data. No "stocks to watch" item and no "shares edge higher" headline (both were sample rows). `collect.py` was not changed.
- **What did not work in C's config:**
  - 69 of 122 Google queries returned 0 items. That matches C's count, but 55 of them are topic phrases. The 76 topic queries added 1 row after dedup and cost about 4 minutes of the run.
  - Dutch-only phrases also run in the en edition (`tencent_aandelen`, `aandeelhouders`), where they always return 0.
  - `prosus_newsroom` in feeds.yaml was skipped because `collect_prosus_newsroom.py` does not exist.
  - Window A's title-term list still runs after C's rules and drops 15 peer rows. C measured precision without it.
- **Not checked:** no independent audit of the classifier labels, and no manual sample of the 226 rows for off-target items. Example: "BAM verliest op Amsterdamse beurs" is tagged Prosus.

# Window F: quality fixes, 2026-10-20 (branch win-f-quality, on main 26efea1)

- **Stories:** new `stories.py` adds `story_id`, `is_canonical` and `on_target`; all 226 rows kept. Prosus: 76 on-target articles across 25 stories. Delivery Hero: 36 articles across 24 stories. Zalando: 57 across 35. Biggest clusters: Nalden/AI leadership (17 rows), JET back to growth (11), Arivihan round (9), Cosmochute round (6 rows in the data, not 3). Two re-audits found 0 false merges.
- **Off-target:** 57 of 133 Prosus rows fall out (all summaries are blank, so only titles are tested). Examples: "Premier Jetten in gesprek met Zuidas-Russen van Nebius", "BAM verliest op Amsterdamse beurs na overname aannemer A.Hak", "Karremans geeft Greenpeace gelijk: er zit te veel PFAS in…". Cost of the rule: 22 of the 57 concern Prosus or its own businesses but do not name it (9 JET/Thuisbezorgd, 9 Arivihan, iFood, PayU, OLX, and Prosus's own "Update on repurchase programme").
- **Sentiment (pos/neu/neg), before → after:** Prosus 75/55/3 → 20/110/3; Zalando 43/11/3 → 10/45/2; Delivery Hero 3/33/0, unchanged. 91 rows changed; every other column is byte-identical. On the page (76 on-target rows): 18% / 79% / 3%. An audited sample of 20 had 0 clear disagreements.
- **Table** (tier, then date desc; canonical on-target only):

| Date | Outlet | Tier | Title | Sentiment |
|---|---|---|---|---|
| 6 Oct | Economic Times | 1 | ElevenLabs CEO on AI slowdown; Prosus dials up fintech bets | neutral |
| 30 Sep | Economic Times | 1 | Arivihan raises $10 million from Prosus, Accel to expand AI… | neutral |
| 28 Sep | Economic Times | 1 | Prosus in talks to invest Rs 150 crore in Swiggy Instamart… | neutral |
| 22 Sep | Het Financieele Dagblad | 1 | Prosus haalt techondernemer Nalden binnen voor zijn AI-plannen | neutral |
| 15 Oct | Business Standard | 2 | Prosus Ventures leads $1.5 million funding in space startup | neutral |
| 14 Oct | The Globe and Mail | 2 | Prosus Advances Share Buyback Programme With €83.5 Million… | neutral |
| 12 Oct | Business Day | 2 | NEWS ANALYSIS iFood's ecosystem hits the spot for Naspers | positive |
| 8 Oct | News24 | 2 | Prosus turns the tide at JET as iFood fights Chinese onslaught | positive |

- **Spend:** USD 0.1043 for the sentiment re-run (30,851 tokens in, 4,260 out, claude-sonnet-5-5). Project total USD 0.7376.
- **Checks:** `check_page.py` and the new `check_stories.py` print ALL CHECKS PASS; the offline tests pass. Screenshots: `reports/window-f-390.png`, `reports/window-f-1280.png`. queries.yaml: sgourdos and kolek removed (deletions only, 122 → 114 queries).
- **Open:** row 1 is an ET newsletter roundup. It wins its story because the canonical rule picks tier first, then the earliest date. At 390 px the headlines are cut to about 35 characters. Four peer events are still split into 2–4 stories each; peers appear on the page only as counts. Not checked: dark mode, or a full run.sh (articles_raw.csv is not in this worktree).

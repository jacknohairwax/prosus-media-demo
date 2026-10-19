# Window B report: page

- Built: `build_page.py` (stdlib + pandas + jinja2), `templates/page.html.j2`, `site/index.html`, placeholder `brief.md` (3 items marked [Placeholder]). Page order follows the brief, items 1–5.
- Data: `data/sample_articles.csv` from main (d7c679e), 40 rows: prosus 26, peer_deliveryhero 7, peer_zalando 7. No own synthetic CSV was needed. Input order: articles.csv, then sample_articles.csv.
- Screenshots: `reports/screenshot-390.png`, `reports/screenshot-1280.png` (light; dark mode checked separately).
- Size: `site/index.html` is 15.8 KB, single file. 0 `<script`, 0 external URLs, 0 `url(`/`@import`/`<link`; system fonts only.
- Phone: no horizontal scroll at 360/390/1280. Smallest chart text 11.15px at 360. Contrast is at least 4.5:1 for text and 3:1 for chart marks, light and dark.
- Token spend: 0 (no API calls).
- Audit: three independent rounds. Every tile and bar was recomputed from the CSV and matches; rebuilds are byte-identical.
- Added beyond the brief:
  - A "Demo data: synthetic" banner when built from the sample (by filename or "SYNTHETIC SAMPLE" summaries).
  - A placeholder note on the brief.
  - An "N of M rows excluded" line for rows with a bad date, sentiment or topic. The charts and sentiment % all use the same valid rows.
- Wording changed for strangers: "What a paid media-monitoring licence (Meltwater, Cision) adds" (brief: "What a Meltwater or Cision licence adds"); CRM box "Topic area" (brief: "Area").

What the sample data could not exercise:
- Accuracy line: no eval output yet, so the page shows "pending Jack's review".
- Cost line: no `runs/log.csv` yet, so the page shows "pending first full run". The summing code is untested on real data.
- No gap weeks, no top-outlet ties, and no links in brief.md (that parser branch is untested).
- Top outlet counts all Prosus rows, including rows excluded from the charts.
- Unverified: Safari/Firefox, real phones, screen readers.
- Screenshots used the cached Chromium 1243 (Playwright, scratchpad only, not a repo dependency).

# Window E report: trim the page to monitoring and KPIs

- Base: `main` has no page code, so with Jack's approval `win-e-trim` is built on `origin/win-b-page` (b51d363). Merge B first, then E.
- Removed:
  - The "This week's brief" section, its brief.md parser, its CSS, and `brief.md` itself (build_page.py −69 lines net).
  - The CRM schema diagram (5 boxes).
- Kept: title, subtitle, synthetic-data banner, four tiles, two charts, and the footer (pipeline diagram, accuracy, cost, licence note, repo link). Tile and chart values are the same as B's.
- Added: a "Latest Prosus coverage" table after the charts. It shows the 8 newest valid Prosus rows (ties broken by `id`), with date, outlet, headline linked to `url` (`url_source` ignored, tested) and sentiment as a word. Each headline is one line, cut off with "…". Below 600px the table becomes two columns.
- Sample data: 17 Oct (Business Insider NL, Thuisbezorgd AI assistant, positive) back to 10 Oct (MarketScreener, Prosus shares edge higher, neutral). Mix: 3 positive, 4 neutral, 1 negative.
- Size: `site/index.html` is 17.5 KB, a single file with 0 `<script`, 0 `<link`/`@import`/`url(`, and links only to articles.
- Layout: no sideways scroll at 320, 360, 390 or 1280px. Light and dark mode work. Lowest text contrast in the table is 5.13:1.
- Screenshots: `reports/window-e-390.png`, `reports/window-e-1280.png` (light).
- Checks: `scripts/check_page.py` passes. An independent audit passed every build check, with byte-identical rebuilds. Its on-target check failed on 1 of 8 sample rows (below).
- Open issues:
  - "Top tech stocks to watch this week" (Moneycontrol, sample CSV line 23) shows in the table, but collect.py's relevance filter would drop it. Fix in Window A's sample, or add a Window C exclusion.
  - "Prosus shares edge higher" gets past collect.py's price-move filter ("edge" is not in its verbs). At 1280px "Business Insider Nede…" is cut off with no hover text.
- Not verified: Safari, Firefox, real phones, screen readers, real `data/articles.csv`. Token spend: 0.

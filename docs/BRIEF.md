# Prosus media intelligence demo — build brief

Owner: Jack. Planner: Cowork (reviews every report). Builders: Claude Code windows A, B, C.
Purpose: one self-explaining web page showing 30 days of public Prosus coverage, tagged by AI, with a three-item brief. Sent with the application for "AI Automation Intern, Communications" at Prosus.

## Hard rules (every window)
- Public sources only: RSS feeds and public web pages. No LinkedIn, no X scraping, no paywall bypass, no personal emails or phone numbers anywhere in the repo.
- Python 3.11, standard libs plus `feedparser`, `requests`, `pandas`, `anthropic`, `jinja2`. Nothing else without saying so in the report.
- API spend cap for the whole project: USD 15. Classification model: `claude-sonnet-5-5`. Key comes from `ANTHROPIC_API_KEY` in `.env`; never commit it. Log tokens per run to `runs/log.csv`.
- Branch per window: `win-a-collect`, `win-b-page`, `win-c-queries`. Do not merge; Cowork merges. One exception: Window A commits `data/sample_articles.csv` and `requirements.txt` directly to `main` as its first act, so B and C can pull them.
- Cap classification at 250 Prosus rows and 150 rows per peer per run. Classify on `title` + `summary` only; articles may be Dutch or English, the prompt must handle both.
- Report under 20 lines in `reports/<window>.md`: what was built, row counts, token spend, what failed, what is unverified.
- Do not start another window's scope.

## Repo layout
```
prosus-media-demo/
  docs/BRIEF.md            this file
  queries.yaml             search terms, topics, executives, exclusions, peers   (window C)
  feeds.yaml               RSS/source list with verified status                  (window C)
  collect.py               feeds + queries -> data/articles_raw.csv              (window A)
  classify.py              articles_raw -> data/articles.csv with tags           (window A)
  eval/eval_template.csv   50 rows for Jack to label; eval/score.py              (window A)
  build_page.py            data/articles.csv + brief.md -> site/index.html       (window B)
  templates/page.html.j2   page template, inline CSS, inline SVG charts          (window B)
  brief.md                 Jack writes: 3 flagged items, 1 recommendation each   (Jack)
  data/sample_articles.csv 40 synthetic rows in the final schema, for window B   (window A, first commit to main)
  requirements.txt         pinned libraries                                      (window A, first commit to main)
  run.sh                   collect -> classify -> build_page in one command      (window A)
  runs/log.csv             date, step, rows, tokens_in, tokens_out, usd; tracked in git (numbers only)
  reports/                 window reports
  README.md                what this is, how to run, what it cannot do           (Cowork, after merge)
  .github/workflows/daily.yml  optional daily run; needs ANTHROPIC_API_KEY as a repo secret (Cowork)
```
Integration point: if `collect_prosus_newsroom.py` exists (window C), `collect.py` imports and calls it; if not, it skips with a log line.

## Data schema: `data/articles.csv` (the contract between windows)
| column | type | values |
|---|---|---|
| id | str | sha1 of url |
| published | date | YYYY-MM-DD |
| source | str | feed name from feeds.yaml |
| outlet | str | publisher name, e.g. "Het Financieele Dagblad" |
| outlet_tier | str | **tier1** (FT, Reuters, Bloomberg, FD, NRC, Economic Times), **tier2** (national/trade), **tier3** (other) |
| title | str | |
| url | str | |
| summary | str | feed summary or first 600 chars; never full article text |
| language | str | en, nl, pt, hi, other |
| company | str | **prosus**, **peer_deliveryhero**, **peer_zalando** |
| topic | str | one of: **results**, **tencent_stake**, **ai**, **jet_integration**, **ventures_investments**, **governance_regulation**, **other** |
| entity | str | **company**, **bloisi**, **sgourdos**, **kolek**, **beinat**, **portfolio_brand**, **none** |
| sentiment | str | **positive**, **neutral**, **negative** (toward Prosus or the peer named) |
| confidence | float | 0–1 from the classifier |
| flag | str | **issue**, **opportunity**, blank. Blank = classifier saw nothing to escalate; never 0 |

Blank vs 0: `flag` blank means "nothing to escalate"; a count of 0 never appears in this column.

## Window A: collect + classify
- `collect.py`: read `feeds.yaml` and `queries.yaml`; for Google News use `https://news.google.com/rss/search?q=<query>&hl=en&gl=NL&ceid=NL:en` and the Dutch variant `hl=nl&gl=NL&ceid=NL:nl` (window C verifies the exact parameters); de-duplicate on normalised title + url; keep last 30 days; write `data/articles_raw.csv`. Target 100–250 rows for Prosus, 50+ each for the two peers.
- `classify.py`: one prompt, fixed taxonomy from this brief, JSON output, batch of 10 articles per call, retries, token logging. Skip rows already classified (idempotent).
- `eval/eval_template.csv`: 50 random Prosus rows with blank `topic`, `sentiment`, `flag` for Jack to fill; `eval/score.py` prints accuracy per column.
- First commit within 20 minutes, directly to `main` and pushed: `data/sample_articles.csv` with 40 synthetic rows in the schema above (mix of companies, topics, sentiments, two languages) and `requirements.txt`. Then switch to `win-a-collect` for everything else.
- `run.sh`: runs collect, classify, build_page in order; exits non-zero on any failure.
- Report: rows per company, rows per source, tokens and USD, feeds that returned nothing.

## Window B: page
- Pull `origin/main` every 15 minutes until `data/sample_articles.csv` appears; build against your own synthetic CSV meanwhile and delete it after.
- `build_page.py` reads `data/articles.csv` (fall back to `data/sample_articles.csv`) and `brief.md`; renders `site/index.html`, single file, no external scripts, inline CSS and inline SVG charts, works on a phone.
- Page order, top to bottom:
  1. Title "Prosus in the media — <start> to <end> 2026"; one line: "Public sources only, tagged by AI, reviewed by Jack Niu. Built for the AI Automation Intern application."
  2. Four tiles: articles found; sentiment split (pos/neu/neg %); share of voice = Prosus articles / (Prosus + peers) %; top outlet by count.
  3. Chart 1: articles per week, stacked by sentiment. Chart 2: topic mix, horizontal bars.
  4. "This week's brief": render `brief.md` (three items, each: what happened, why it matters, recommendation).
  5. Footer: pipeline diagram as inline SVG (collect → tag → review → page); accuracy from `eval/score.py` output if present; cost per month line; "What a Meltwater or Cision licence adds" (paywalled press, print, broadcast, audience figures, journalist database); CRM schema diagram as inline SVG (Contact, Organisation, Interaction, Event, Area); link placeholder for repo.
- Light and dark mode via `prefers-color-scheme`. No tracking, no fonts loaded from outside.
- Report: screenshot path, file size, anything the sample data could not exercise.

## Window C: queries and feeds
- `queries.yaml`: Prosus terms (Prosus, "Prosus N.V.", Naspers where it concerns Prosus); executives: Fabricio Bloisi, Basil Sgourdos, Patrick Kolek, Euro Beinat; topics with 4–8 search phrases each: results, tencent_stake, ai (Toqan, AI House, agents, Luminate), jet_integration (Just Eat Takeaway), ventures_investments (Prosus Ventures, India, LatAm), governance_regulation; exclusions (job ads, stock-tip spam); peers: Delivery Hero, Zalando with 3 phrases each.
- `feeds.yaml`: verified list, each entry with `name, url, type (rss|googlenews), language, outlet_tier, verified (yes/no), last_checked`. Try: Prosus newsroom RSS; Google News per query; FD, NRC, NU.nl, Silicon Canals, Reuters company topic, Economic Times tech, Techleap. Mark any that return 403 or empty as `verified: no`. Note which are paywalled (headline only).
- Prosus newsroom: if no RSS, write `collect_prosus_newsroom.py` that reads https://www.prosus.com/news-insights listing pages politely (1 request per 2 seconds, user-agent set).
- Report: feeds verified vs failed, estimated rows per day, any term that produces false positives.

## Cowork (planner) does
Reads the three reports, merges branches in order C, A, B, runs `run.sh` once on a fresh clone, checks the page against this brief, writes README.md, adds the optional daily workflow, sends Jack the eval template and the brief.md template.

## Done when
- `python collect.py && python classify.py && python build_page.py` runs clean on a fresh clone.
- `site/index.html` opens from file and from a published artifact and makes sense to a stranger in 30 seconds.
- `runs/log.csv` shows total USD under 15.

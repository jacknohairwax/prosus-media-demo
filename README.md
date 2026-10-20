# Prosus in the media — a working media-monitoring prototype

**Live page:** https://jacknohairwax.github.io/prosus-media-demo/

Built in two days by orchestrating Claude Code agents from a written brief (`docs/BRIEF.md`). I wrote the brief and the quality bar, reviewed every output, and made the calls on what to cut. Built for the *AI Automation Intern, Communications* application at Prosus; it covers priority use cases 1 (media monitoring) and 3 (KPI dashboard) from the posting, on public sources only.

## What it does

1. **Collect** — pulls 30 days of coverage for Prosus and two peers (Delivery Hero, Zalando) from Google News RSS and six verified outlet feeds, using 114 tested search queries in English and Dutch and 11 exclusion rules for stock-tip spam, market wraps and job ads (`collect.py`, `queries.yaml`, `feeds.yaml`).
2. **Tag** — Claude classifies each article's topic, entity, sentiment and whether it needs escalating, with neutral as the default for routine coverage (`classify.py`).
3. **Group** — near-identical headlines become one story; rows whose headline doesn't name Prosus are kept but excluded from the page (`stories.py`).
4. **Page** — one static HTML file: four KPI tiles, two charts, the eight top stories ranked by outlet tier, and a footer stating cost and what a paid licence would add (`build_page.py`).

## Numbers from the current run

226 articles collected, 133 about Prosus, 76 on-target across 25 stories. Running cost USD 0.74 for the month. Every run is logged in `runs/log.csv`.

## Run it

```
pip install -r requirements.txt
echo ANTHROPIC_API_KEY=... > .env
bash run.sh
```

## What it can't do

No paywalled press, print, broadcast, audience figures or journalist database. Those are what a Meltwater or Cision licence adds, and why this is a complement to one, not a replacement. No personal data is collected.

## How it was built

Six Claude Code windows, each with a scoped brief and a report under 20 lines, merged and reviewed centrally. Reports are in `reports/`.

# NCSL Girls Tables

Standings, schedules, age-group ratings and match predictions for every NCSL Fall 2026 girls travel division, rebuilt daily from NCSL's public Demosphere schedules.

**Site:** https://shaaude.github.io/ncsl-girls/

## How it runs

`.github/workflows/daily.yml` runs at 7 am Eastern every day and 9:30 pm Eastern on Saturdays and Sundays. Each run:

1. `scripts/fetch.py` downloads every girls division page listed in `data/registry.json`. On Monday mornings, or when run by hand with "discover" ticked, it first rescans the Demosphere ID range for new or renamed divisions.
2. `scripts/build.py` parses games (`scripts/parse.py`), compares them with yesterday's `data/games.json` to log changes in `data/changes.jsonl`, computes standings and ratings (`scripts/model.py`), and writes `site/data.json`.
3. Data is committed back to the repo and `site/` is published to GitHub Pages.

If a download fails, the previous data is kept; an empty fetch never overwrites history.

## Model

Poisson goals model with an attack and defence rating per team and a home advantage per age group, fit with an L2 penalty (ridge 1.0) toward the division average. Backtest on games from Sept 28 onward: log-loss 0.82 against 0.99 for base rates.

Teams in different divisions never meet, so divisions are placed on one scale with an assumed gap per tier (1.14 on the log-strength scale: a typical division winner rates like the median team one division up). The predictor lets you change it.

U9–U11 have no published scores, so only schedules are shown for those ages. Forfeits count 3–0 in standings and are excluded from ratings.

## Data files

- `data/registry.json`: girls divisions and their Demosphere IDs
- `data/games.json`: latest parsed games, keyed by Demosphere game key
- `data/changes.jsonl`: every detected change (result posted, score corrected, reschedule, venue change, added, removed)
- `data/ratings_history.json`: daily rating snapshots

Independent project, not affiliated with NCSL.

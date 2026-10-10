# NCSL Girls Tables

Standings, schedules, age-group ratings and match predictions for every NCSL Fall 2026 girls travel division, rebuilt daily from NCSL's public Demosphere schedules.

**Site:** https://shaaude.github.io/ncsl-girls/

## How it runs

`.github/workflows/daily.yml` runs at 7:17 am and 12:47 pm Eastern daily, plus 7:37 pm and 10:37 pm on Saturdays and Sundays (times shift an hour earlier in winter because GitHub schedules are in UTC). Each run:

1. `scripts/fetch.py` downloads every girls division page listed in `data/registry.json`. On Monday mornings, or when run by hand with "discover" ticked, it first rescans the Demosphere ID range for new or renamed divisions.
2. `scripts/build.py` parses games (`scripts/parse.py`), compares them with yesterday's `data/games.json` to log changes in `data/changes.jsonl`, computes standings and ratings (`scripts/model.py`), and writes `site/data.json`.
3. `scripts/build_national.py` rebuilds the National Girls data (a failure here never blocks the NCSL site).
4. `scripts/validate_site.py` checks the output; NCSL problems stop the deploy, national problems restore the previous national data.
5. Data is committed back to the repo and `site/` is published to GitHub Pages as one artifact.

If a download fails, the previous data is kept; an empty fetch never overwrites history.

## Model

NCSL model: Poisson goals model with an attack and defence rating per team and a home advantage per age group, fit with an L2 penalty (ridge 1.0) toward the division average. (Fixed Oct 10: division centring now shifts attack and defence equally, so it no longer alters expected goals.) Backtest on games from Sept 28 onward: log-loss 0.82 against 0.99 for base rates.

Teams in different divisions never meet, so divisions are placed on one scale with an assumed gap per tier (1.14 on the log-strength scale: a typical division winner rates like the median team one division up). The predictor lets you change it.

U9–U11 have no published scores, so only schedules are shown for those ages. Forfeits count 3–0 in standings and are excluded from ratings.

## National Girls

A separate section that combines results from outside NCSL (tournaments, other leagues) with NCSL league results, for the same age groups (GU9–GU17, GU19). **It never changes NCSL standings, NCSL ratings or the NCSL predictor.**

Pipeline (`scripts/build_national.py`, runs after the NCSL build each time):

1. **Sources** (`scripts/sources.py`): NCSL games from `data/games.json` (authoritative), CSV exports in `imports/csv/`, saved YouthSoccerGames pages in `imports/ysg/`. See `imports/README.md`.
2. **Identities** (`scripts/identities.py`, `data/team_aliases.json`): every team gets a stable internal ID. Outside teams are linked to NCSL teams only on game evidence (results that line up on the same dates with the same scores), never on name or club alone. Uncertain links wait in `data/review_queue.jsonl`; decide them in `data/identity_decisions.json`.
3. **Reconciliation** (`scripts/reconcile.py`): every report of a game is kept in `data/match_observations.jsonl` (history survives when a source drops a game); reports of the same real game merge into one canonical match with an immutable ID (`data/canonical_matches.jsonl`). Same-day rematches stay separate. Score disagreements go to the review queue; NCSL wins when it's one of the sources. Score changes are logged in `data/match_corrections.jsonl`.
4. **Eligibility**: only games dated after 2026-08-01 (`NATIONAL_CUTOFF` in `scripts/config.py`) and on or before the evaluation date; no forfeits, scrimmages, unplayed or disputed games, unconfirmed identities, or cross-age games.
5. **Model** (`scripts/national_model.py`): Poisson attack/defence per age group, recency-weighted (90-day half-life), margins capped at 6, neutral venue unless a real home game, ridge 1.0, uncertainty per team. No division-tier assumption: teams are compared only within a connected network. National rank needs the main network (≥50% of rated teams, linked by outside results) and ≥8 eligible games; fewer games = provisional.
6. **Outputs**: `site/national/index.json`, `site/national/{AGE}.json`, `site/national/teams/{id}.json` (lazy-loaded), written atomically so a failed build leaves the previous outputs in place.

Automated YouthSoccerGames collection (`scripts/collect_ysg.py`) is disabled: its robots.txt disallows crawling and no written permission is on file. It activates only if `data/permissions.json` records permission, and still obeys robots.txt and a 5-second delay.

### Model check (rolling weekly backtest, predicting only from earlier games)

On 143 NCSL games (within-division; Sept 26 and Oct 3 origins): national model log-loss 0.853, Brier 0.493; NCSL model 0.860 / 0.495; base rates 1.080 / 0.645. Updated every build in `site/national/index.json` → `validation`.

### Tests

`python -m pytest tests` covers NCSL preservation, deduplication, corrections, conflicts, same-day rematches, the date cutoff, identity linking, model behaviour and an end-to-end build. Test fixtures in `tests/fixtures/` are synthetic and never published.

## Data files

- `data/registry.json`: girls divisions and their Demosphere IDs
- `data/games.json`: latest parsed games, keyed by Demosphere game key
- `data/changes.jsonl`: every detected change (result posted, score corrected, reschedule, venue change, added, removed)
- `data/ratings_history.json`: daily rating snapshots

Independent project, not affiliated with NCSL.

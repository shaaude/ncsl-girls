# Imports: results from outside NCSL

Files added here are read by `scripts/build_national.py` on every run. They feed only the
**National Girls** section. They never change NCSL standings, NCSL ratings or the NCSL predictor.

Only add data you're allowed to use: results you saved yourself as an ordinary visitor,
exports a tournament director sent you, or sources whose terms allow reuse.

## `csv/` — tournament exports, licensed feeds, hand entry

One row per game. Required columns: `date` (YYYY-MM-DD), `team_a`, `team_b`, `score_a`,
`score_b`, `age_group` (e.g. `U12`, `GU12` or birth year `2015`).

Optional: `time`, `competition`, `competition_type` (`league`, `tournament`, `showcase`,
`scrimmage`), `venue_type` (`home` = team_a at home, or `neutral`), `team_a_id`, `team_b_id`
(the source's own team IDs; strongly recommended), `team_a_state`, `team_b_state` (two-letter),
`gender` (default girls), `status` (`final`, `scheduled`, `forfeit`), `source` (`event` for an
official tournament export, otherwise `csv`), `source_match_id`, `url`.

Example:

    date,team_a,team_a_id,team_b,team_b_id,score_a,score_b,age_group,competition,competition_type,venue_type,source
    2026-09-06,Arlington SA 2015G Red,GS123,Bethesda 2015G Blue,GS456,2,1,U12,Labor Day Cup,tournament,neutral,event

## `ysg/` — saved YouthSoccerGames team pages

Open a team page in your browser, press Ctrl+S (Cmd+S on a Mac), choose "Webpage, HTML only",
and upload the file here. The parser was written from the site's published structure and must
be checked against the first real page; anything it can't read is listed under "import
problems" in `site/national/index.json` rather than guessed.

Automated collection (`scripts/collect_ysg.py`, weekly workflow) runs under the written
permission recorded in `data/permissions.json`: directory pages weekly, team pages once a week
between 1 and 5 AM Eastern, 10 seconds between requests, games from 2026-08-01 on. Its output
lands in `ysg/directory/` and `ysg/extracted/`; don't edit those by hand.

## Team identity decisions

Teams from these files are linked to NCSL teams only when their games line up with the NCSL
team's games. Uncertain links appear in `data/review_queue.jsonl`. To decide one, add it to
`data/identity_decisions.json`:

    {"csv:name:GU12:2015g arlington red sa": "T1234abcd56", "ysg:300441534": "new", "ysg:999": "reject"}

(a canonical team ID to merge into, `"new"` for a separate team, or `"reject"`).

Only games dated after August 1, 2026 count.

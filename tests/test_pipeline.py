"""End-to-end national build on a temporary copy of the real NCSL data plus a SYNTHETIC
tournament export. Nothing here touches the repository's data or site folders."""
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    for d in ("data", "site", "imports/csv", "imports/ysg"):
        (tmp_path / d).mkdir(parents=True)
    for f in ("registry.json", "games.json", "fetch_report.json"):
        shutil.copy(ROOT / "data" / f, tmp_path / "data" / f)
    import build_national, identities, reconcile, sources
    monkeypatch.setattr(build_national, "DATA", tmp_path / "data")
    monkeypatch.setattr(build_national, "SITE", tmp_path / "site")
    monkeypatch.setattr(identities, "ALIASES", tmp_path / "data" / "team_aliases.json")
    monkeypatch.setattr(reconcile, "DATA", tmp_path / "data")
    monkeypatch.setattr(sources, "IMPORTS", tmp_path / "imports")
    # default-argument paths captured at import time
    monkeypatch.setattr(identities.Identities.__init__, "__defaults__", (tmp_path / "data" / "team_aliases.json",))
    monkeypatch.setattr(reconcile.ObservationStore.__init__, "__defaults__", (tmp_path / "data" / "match_observations.jsonl",))
    monkeypatch.setattr(reconcile.reconcile, "__defaults__", (tmp_path / "data" / "match_index.json",))
    return tmp_path


def ncsl_game(div_name):
    games = json.loads((ROOT / "data" / "games.json").read_text())
    reg = {d["id"]: d for d in json.loads((ROOT / "data" / "registry.json").read_text())["divisions"]}
    return [g for g in games.values() if reg[g["division_id"]]["name"] == div_name and g["status"] == "final"]


def test_pipeline_dedupes_and_links_divisions(sandbox):
    import build_national
    d1, d2 = ncsl_game("GU13 Division 1"), ncsl_game("GU13 Division 2")
    g1, g2 = d1[0], d2[0]
    # SYNTHETIC export: re-reports one real NCSL league game (must dedupe) under different team
    # names and IDs, plus invented tournament games between a Div 1 and a Div 2 team.
    t1, t2 = g1["home"], g1["away"]
    other1 = next(g for g in d1 if t1["key"] in (g["home"]["key"], g["away"]["key"]) and g["gamekey"] != g1["gamekey"])
    o1_home = other1["home"]["key"] == t1["key"]
    rows = ["date,team_a,team_a_id,team_b,team_b_id,score_a,score_b,age_group,gender,competition,competition_type,source",
            f"{g1['date']},Ext {t1['name']},EXT1,Ext {t2['name']},EXT2,{g1['home_score']},{g1['away_score']},U13,girls,NCSL,league,event",
            f"{other1['date']},Ext {t1['name']},EXT1,Ext opp,EXT9,{other1['home_score'] if o1_home else other1['away_score']},{other1['away_score'] if o1_home else other1['home_score']},U13,girls,NCSL,league,event",
            f"2026-09-05,Ext {t1['name']},EXT1,Ext {g2['home']['name']},EXT3,2,1,U13,girls,Synthetic Labor Day Cup,tournament,event",
            "2026-07-20,Old Team A,EXT7,Old Team B,EXT8,9,0,U13,girls,Synthetic Summer Cup,tournament,event"]
    (sandbox / "imports" / "csv" / "synthetic_export.csv").write_text("\n".join(rows) + "\n")
    idx = build_national.main(date(2026, 10, 10))
    tot = idx["totals"]
    assert tot["duplicates_merged"] >= 1                 # the re-reported league game counted once
    assert tot["ncsl_teams_linked_to_other_sources"] >= 1
    assert idx["exclusions"].get("on or before cutoff", 0) >= 1    # the July game
    gu13 = json.loads((sandbox / "site" / "national" / "GU13.json").read_text())
    names = {t["name"] for t in gu13["teams"]}
    assert "Old Team A" not in names                     # pre-cutoff teams never rated
    # the tournament game links Div 1 and Div 2 into one network only if EXT3 was linked;
    # either way, nothing is ranked nationally without the evidence
    assert gu13["coverage"]["teams_rated"] >= 20
    profile = json.loads((sandbox / "site" / "national" / "teams" / f"{idx['ncsl_map'][t1['key']]}.json").read_text())
    assert any(a["source_key"] == "event:EXT1" for a in profile["aliases"])


def test_failed_build_keeps_previous_outputs(sandbox, monkeypatch):
    import build_national
    build_national.main(date(2026, 10, 10))
    before = (sandbox / "site" / "national" / "index.json").read_text()
    monkeypatch.setattr(build_national.nm, "fit", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        build_national.main(date(2026, 10, 10))
    assert (sandbox / "site" / "national" / "index.json").read_text() == before

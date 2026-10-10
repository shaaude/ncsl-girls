"""Source adapters (SYNTHETIC fixtures)."""
from pathlib import Path

from config import normalize_age
from sources import csv_observations, parse_ysg_team_page, ysg_observations

FIX = Path(__file__).resolve().parent / "fixtures"


def test_age_normalisation():
    assert normalize_age("U12") == "GU12" and normalize_age("2015") == "GU12"
    assert normalize_age("2014/15") == "GU13" and normalize_age("U18") == "GU19"
    assert normalize_age("U8") is None


def test_ysg_parser_on_synthetic_page():
    info, games, warn = parse_ysg_team_page((FIX / "ysg_team_page_synthetic.html").read_text())
    assert info["id"] == "900000001" and info["age"] == "GU12" and info["girls"] and info["state"] == "VA"
    assert len(games) == 3
    assert [g["status"] for g in games] == ["final", "final", "scheduled"]
    assert games[0]["event_id"] == "99999"


def test_ysg_same_day_games_get_distinct_keys():
    obs, problems = ysg_observations([FIX / "ysg_team_page_synthetic.html"], extracted=[])
    assert len({o["obs_id"] for o in obs}) == 3


def test_csv_adapter(tmp_path):
    p = tmp_path / "synthetic.csv"
    p.write_text("date,team_a,team_b,score_a,score_b,age_group,gender,competition,competition_type\n"
                 "2026-09-13,Testville 2015G,Sampleton 2015G,2,1,U12,girls,Synthetic Cup,tournament\n"
                 "13/09/2026,Bad Date,Row,1,0,U12,girls,,\n"
                 "2026-09-13,Boys Team,Other,1,0,U12,boys,,\n")
    obs, problems = csv_observations([p])
    assert len(obs) == 1 and obs[0]["a"]["age"] == "GU12" and obs[0]["status"] == "final"
    assert len(problems) == 2


def test_ysg_parser_on_real_layout():
    info, games, warn = parse_ysg_team_page((FIX / "ysg_team_real_layout_synthetic.html").read_text())
    assert info["id"] == "900000021" and info["age"] == "GU12" and info["state"] == "VA"
    assert len(games) == 4
    g = games[0]
    assert (g["a_id"], g["a_score"], g["b_id"], g["b_score"]) == ("900000022", 1, "900000021", 2)
    assert g["date"] == "2026-09-13" and g["event_id"] == "77777" and "Labor Day Cup" in g["competition"]
    assert games[1]["a_score"] == 0 and games[1]["b_score"] == 0          # same-day rematch kept
    assert games[2]["date"] == "2026-07-20"                                  # parser keeps; collector filters
    assert games[3]["status"] == "scheduled" and games[3]["a_score"] is None


def test_ysg_page_without_canonical_link_uses_given_id():
    html = (FIX / "ysg_team_real_layout_synthetic.html").read_text().replace(
        '<link rel="canonical" href="https://youthsoccergames.com/team/900000021">', "")
    info, games, warn = parse_ysg_team_page(html, team_id="900000021")
    assert all(g["a_id"] and g["b_id"] for g in games) and games[0]["b_id"] == "900000021"
    info2, games2, _ = parse_ysg_team_page(html)
    obs, problems = ysg_observations(paths=[], extracted=[])
    from sources import _ysg_obs_from_games
    out = _ysg_obs_from_games({"id": "900000021", "age": "GU12"}, games2, "t", {}, [])
    assert all(o["a"]["source_key"] != "ysg:None" for o in out)

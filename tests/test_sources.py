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
    obs, problems = ysg_observations([FIX / "ysg_team_page_synthetic.html"])
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

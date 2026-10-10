"""YouthSoccerGames collector safeguards (SYNTHETIC fixtures; no network)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import collect_ysg as c
from config import EASTERN
from sources import parse_ysg_directory

FIX = Path(__file__).resolve().parent / "fixtures"


def test_no_permission_means_no_fetch(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(c, "PERM", tmp_path / "missing.json")
    monkeypatch.setattr(c, "Fetcher", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    monkeypatch.setattr("sys.argv", ["collect_ysg.py", "profiles"])
    assert c.main() == 0
    assert "disabled" in capsys.readouterr().out


def test_mode_not_in_permission_fetches_nothing(monkeypatch, capsys):
    monkeypatch.setattr(c, "Fetcher", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    c.run_profiles({"granted": True, "evidence": "x", "modes": ["directories"]})
    assert "not in the recorded permission" in capsys.readouterr().out


def test_profile_window_is_1_to_5_am_eastern():
    d = datetime(2026, 10, 14, tzinfo=EASTERN)
    assert not c.in_window(d.replace(hour=0, minute=59))
    assert c.in_window(d.replace(hour=1)) and c.in_window(d.replace(hour=4, minute=59))
    assert not c.in_window(d.replace(hour=5)) and not c.in_window(d.replace(hour=14))


def test_profiles_outside_window_fetch_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "REPORT", tmp_path / "r.json")
    monkeypatch.setattr(c, "in_window", lambda t=None: False)
    monkeypatch.setattr(c, "Fetcher", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    c.run_profiles({"granted": True, "evidence": "x", "modes": ["profiles"]})
    assert "skipped" in json.loads((tmp_path / "r.json").read_text())["runs"][-1]


def test_minimum_delay_is_10_seconds():
    f = c.Fetcher({"min_seconds_between_requests": 3, "robots_txt_override_by_permission": True})
    assert f.delay == 10


def test_directory_parser_keeps_ids_names_clubs_not_ranks():
    d = parse_ysg_directory((FIX / "ysg_directory_synthetic.html").read_text())
    assert d["yeargen"] == "u12-girls"
    assert d["teams"] == [{"id": "900000001", "name": "Testville SC 2015G Red", "club": "Testville SC"},
                          {"id": "900000002", "name": "Sampleton FC 2015G", "club": "Sampleton FC"}]


def test_queue_skips_teams_fetched_this_week(monkeypatch, tmp_path):
    (tmp_path / "directory").mkdir()
    (tmp_path / "directory" / "virginia_u12.json").write_text(json.dumps(
        {"state": "VA", "age": "GU12", "teams": [{"id": "1", "name": "Testville SC 2015G Red"}, {"id": "2", "name": "Other FC"}]}))
    monkeypatch.setattr(c, "YSG", tmp_path)
    monkeypatch.setattr(c, "ROOT", tmp_path)
    now = datetime.now(timezone.utc)
    log = {"profiles": {"1": (now - timedelta(days=2)).isoformat()}, "directories": {}}
    q = [t for t, _ in c.profile_queue(log, now)]
    assert "1" not in q and "2" in q


def test_structure_digest_removes_text():
    html = (FIX / "ysg_team_page_synthetic.html").read_text()
    digest = "\n".join(c.structure_digest(html))
    assert "Testville" not in digest and "Sampleton" not in digest and "<table" in digest


def test_extracted_games_from_aug_1(tmp_path, monkeypatch):
    import sources
    monkeypatch.setattr(sources, "IMPORTS", tmp_path)
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"team": {"id": "1", "age": "GU12", "name": "T"}, "fetched_at": "2026-10-14T06:30:00Z",
                             "games": [{"a_id": "1", "a_name": "T", "b_id": "2", "b_name": "U", "a_score": 1, "b_score": 0,
                                        "date": "2026-09-01", "status": "final"}]}))
    obs, problems = sources.ysg_observations(paths=[], extracted=[p])
    assert len(obs) == 1 and obs[0]["source"] == "ysg"
    # the collector itself drops anything earlier than the permitted window
    assert c.SCORE_FROM == "2026-08-01"


def test_directory_parser_real_layout():
    d = parse_ysg_directory((FIX / "ysg_directory_real_layout_synthetic.html").read_text())
    assert d["teams"] == [{"id": "900000011", "name": "Testville SC 2015G Red", "club": "Testville Soccer Club"},
                          {"id": "900000012", "name": "Sampleton FC 2015G Blue", "club": "Sampleton FC"}]


def test_age_slugs_cover_supported_ages():
    from config import normalize_age, SUPPORTED_AGES
    assert {normalize_age(a) for a in c.AGE_SLUGS} - {None} == set(SUPPORTED_AGES)


def test_queue_puts_likely_ncsl_teams_first():
    arl = {"name": "Red", "club": "Arlington Soccer Association"}
    other = {"name": "Red", "club": "Baltimore Armour"}
    assert c.ncsl_match_score(arl, "ARL 2015G Red (U12)") > c.ncsl_match_score(other, "ARL 2015G Red (U12)")
    assert "loud" in c.club_tags("Loudoun Soccer") and "asa" in c.club_tags("Arlington Soccer Association")


def test_weekend_test_window():
    perm = {"test_runs_until_eastern": "2026-10-11T23:59"}
    assert c.test_window_active(perm, datetime(2026, 10, 10, 13, 0, tzinfo=EASTERN))
    assert not c.test_window_active(perm, datetime(2026, 10, 12, 0, 1, tzinfo=EASTERN))
    assert not c.test_window_active({}, datetime(2026, 10, 10, 13, 0, tzinfo=EASTERN))


def test_test_runs_do_not_count_as_weekly(monkeypatch, tmp_path):
    monkeypatch.setattr(c, "LOG", tmp_path / "log.json")
    monkeypatch.setattr(c, "REPORT", tmp_path / "r.json")
    monkeypatch.setattr(c, "YSG", tmp_path)
    monkeypatch.setattr(c, "ROOT", tmp_path)
    class F:
        def __init__(self, *a, **k): self.count = 0
        def can_continue(self): return False
    monkeypatch.setattr(c, "Fetcher", F)
    c.run_profiles({"granted": True, "evidence": "x", "modes": ["profiles"],
                    "test_runs_until_eastern": "2099-01-01T00:00"}, max_pages=1, test=True)
    assert json.loads((tmp_path / "log.json").read_text()).get("weekly_profile_runs", []) == []

"""Identity resolution and the national date window (SYNTHETIC data)."""
from datetime import date

from helpers import obs, seq
from identities import Identities, similarity
from national_model import eligibility
from reconcile import link_identities, reconcile


def m(d, status="final", a=2, b=1, age="GU12", ctype="tournament"):
    return {"date": d, "status": status, "a_score": a, "b_score": b, "age_a": age, "age_b": age, "competition_type": ctype}


AS_OF = date(2026, 10, 10)


def test_august_1_excluded_august_2_included():
    assert eligibility(m("2026-08-01"), AS_OF) == "on or before cutoff"
    assert eligibility(m("2026-08-02"), AS_OF) is None


def test_old_results_excluded():
    assert eligibility(m("2026-05-15"), AS_OF) == "on or before cutoff"


def test_future_and_unplayed_excluded():
    assert eligibility(m("2026-10-11"), AS_OF) == "after evaluation date"
    assert eligibility(m("2026-10-01", status="scheduled", a=None, b=None), AS_OF) == "not played"


def test_forfeits_conflicts_scrimmages_cross_age_excluded():
    assert eligibility(m("2026-09-01", status="forfeit"), AS_OF) == "forfeit or administrative"
    assert eligibility(m("2026-09-01", status="conflict"), AS_OF) == "score conflict"
    assert eligibility(m("2026-09-01", ctype="scrimmage"), AS_OF) == "scrimmage"
    x = m("2026-09-01"); x["age_b"] = "GU13"
    assert eligibility(x, AS_OF) == "cross-age"


def test_alias_maps_to_ncsl_team_with_game_evidence(tmp_path):
    ids = Identities(tmp_path / "a.json")
    red = ids.register_ncsl("111", "ARL 2015G Red (U12)", "GU12")
    opp = ids.register_ncsl("222", "VSA U12G Royal", "GU12")
    ids.ensure_source_team("ysg:9", "Arlington SA 2015G Red", "GU12")
    ids.mappings["ysg:8"] = {"canonical_id": opp, "status": "confirmed", "by": "manual"}
    assert ids.status("ysg:9") == "pending"
    rows = seq([obs("ncsl", "ncsl:d", "g1", "2026-09-20", ("ncsl:111", "x"), ("ncsl:222", "y"), 2, 0, upstream="ncsl:g1"),
                obs("ysg", "ysg:9", "y1", "2026-09-20", ("ysg:9", "x"), ("ysg:8", "y"), 2, 0)])
    link_identities(ids, rows)
    assert ids.resolve("ysg:9") == red


def test_link_by_games_even_when_names_differ(tmp_path):
    """Both external teams unknown; two NCSL games on two dates match their results exactly."""
    ids = Identities(tmp_path / "a.json")
    red = ids.register_ncsl("111", "ARL 2015G Red (U12)", "GU12")
    roy = ids.register_ncsl("222", "VSA U12G Premier Royal", "GU12")
    oth = ids.register_ncsl("333", "LOUD U12G Silver", "GU12")
    ids.ensure_source_team("ysg:1", "Arlington Soccer Association Red", "GU12")
    ids.ensure_source_team("ysg:2", "Virginia Soccer Assoc Royal", "GU12")
    ids.ensure_source_team("ysg:3", "Loudoun Soccer Silver", "GU12")
    rows = seq([obs("ncsl", "ncsl:d", "g1", "2026-09-20", ("ncsl:111", "x"), ("ncsl:222", "y"), 2, 0, upstream="ncsl:g1"),
                obs("ncsl", "ncsl:d", "g2", "2026-09-27", ("ncsl:333", "z"), ("ncsl:111", "x"), 1, 3, upstream="ncsl:g2"),
                obs("ysg", "ysg:1", "y1", "2026-09-20", ("ysg:1", "a"), ("ysg:2", "b"), 2, 0),
                obs("ysg", "ysg:1", "y2", "2026-09-27", ("ysg:1", "a"), ("ysg:3", "c"), 3, 1)])
    link_identities(ids, rows)
    assert ids.resolve("ysg:1") == red


def test_same_club_different_squads_stay_distinct(tmp_path):
    ids = Identities(tmp_path / "a.json")
    red = ids.register_ncsl("111", "ARL 2015G Red (U12)", "GU12")
    m_ = ids.ensure_source_team("ysg:77", "ARL 2015G Blue", "GU12")
    link_identities(ids, [])
    # name resembles Red, but with no shared games it must not be merged into Red
    assert not (m_["status"] == "confirmed" and m_["canonical_id"] == red)
    assert similarity("ARL 2015G Red", "ARL 2015G Blue") < 1


def test_unresolved_identity_does_not_create_match(tmp_path):
    ids = Identities(tmp_path / "a.json")
    ids.register_ncsl("111", "ARL 2015G Red (U12)", "GU12")
    ids.ensure_source_team("ysg:77", "ARL 2015G Red", "GU12")     # resembles Red, no evidence
    ids.ensure_source_team("ysg:78", "Elsewhere FC 2015G", "GU12")
    rows = seq([obs("ysg", "ysg:77", "y1", "2026-09-20", ("ysg:77", "x"), ("ysg:78", "y"), 5, 0)])
    link_identities(ids, rows)
    assert ids.status("ysg:77") == "probable"
    matches, review, st = reconcile(rows, ids, tmp_path / "i.json")
    assert matches == [] and st["unresolved_observations"] == 1
    assert any(r["type"] == "probable_identity" for r in review)


def test_manual_decision_wins(tmp_path):
    ids = Identities(tmp_path / "a.json")
    red = ids.register_ncsl("111", "ARL 2015G Red (U12)", "GU12")
    ids.ensure_source_team("ysg:77", "ARL 2015G Red", "GU12")
    ids.apply_manual({"ysg:77": red})
    assert ids.resolve("ysg:77") == red and ids.mappings["ysg:77"]["by"] == "manual"


def test_mappings_persist(tmp_path):
    p = tmp_path / "a.json"
    ids = Identities(p); cid = ids.register_ncsl("111", "ARL Red", "GU12"); ids.save()
    assert Identities(p).resolve("ncsl:111") == cid

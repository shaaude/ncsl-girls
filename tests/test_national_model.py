"""National model behaviour (SYNTHETIC data)."""
from datetime import date

import national_model as nm


def g(a, b, sa, sb, d="2026-09-20", venue="neutral"):
    return {"team_a": a, "team_b": b, "a_score": sa, "b_score": sb, "date": d, "venue_type": venue}


NET1 = [g("A", "B", 3, 0), g("B", "C", 1, 1), g("C", "A", 0, 2), g("A", "B", 2, 1, "2026-09-27")]
NET2 = [g("X", "Y", 1, 0), g("Y", "Z", 2, 2)]
AS_OF = date(2026, 10, 10)


def test_probabilities_sum_to_one():
    mdl = nm.fit(NET1, AS_OF)
    w, d, l = nm.predict(mdl, "A", "C")
    assert abs(w + d + l - 1) < 1e-9


def test_reproducible():
    a = nm.fit(NET1 + NET2, AS_OF); b = nm.fit(NET1 + NET2, AS_OF)
    assert a["teams"] == b["teams"] and a["mu"] == b["mu"]


def test_disconnected_networks_not_compared():
    mdl = nm.fit(NET1 + NET2, AS_OF)
    assert mdl["networks"] == [3, 3]
    assert nm.predict(mdl, "A", "X") is None


def test_stronger_team_rated_higher():
    mdl = nm.fit(NET1, AS_OF)
    r = {t: nm.rating(v["att"], v["def"]) for t, v in mdl["teams"].items()}
    assert r["A"] > r["B"]


def test_margin_cap():
    assert nm.capped(12, 0) == (6, 0) and nm.capped(1, 9) == (1, 7)


def test_no_temporal_leakage_from_eligibility():
    later = {"date": "2026-10-11", "status": "final", "a_score": 1, "b_score": 0, "age_a": "GU12", "age_b": "GU12"}
    assert nm.eligibility(later, AS_OF) == "after evaluation date"


def test_calibration_reported():
    ev = nm.evaluate([((0.6, 0.2, 0.2), 0), ((0.3, 0.3, 0.4), 2), ((0.5, 0.3, 0.2), 1)])
    assert ev["n"] == 3 and ev["calibration"] and 0 <= ev["ece"] <= 1


def test_uncertainty_shrinks_with_more_games():
    few = nm.fit(NET1[:2], AS_OF)["teams"]["B"]["se"]
    many = nm.fit(NET1 * 4, AS_OF)["teams"]["B"]["se"]
    assert many < few

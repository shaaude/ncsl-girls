"""The NCSL application must keep working exactly as before (uses the real committed data)."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DATA = json.loads((ROOT / "site" / "data.json").read_text())


def test_all_31_divisions_present():
    reg = json.loads((ROOT / "data" / "registry.json").read_text())["divisions"]
    assert len(reg) == 31
    assert {d["id"] for d in reg} == {d["id"] for d in DATA["divisions"]}


def test_no_gu18_division():
    assert not any(d["age"] == "GU18" for d in DATA["divisions"])


def test_standings_come_only_from_ncsl_results():
    """Recompute every NCSL standings row from that division's own NCSL games."""
    for d in DATA["divisions"]:
        if not d["scored"]:
            continue
        tally = {}
        for g in d["games"]:
            hk, ak, hs, as_, st = g[4], g[5], g[6], g[7], g[8]
            if st not in ("final", "forfeit") or hs is None:
                continue
            for me, gf, ga in ((hk, hs, as_), (ak, as_, hs)):
                t = tally.setdefault(me, [0, 0])
                t[0] += 1; t[1] += 3 if gf > ga else 1 if gf == ga else 0
        for r in d["standings"]:
            gp, pts = tally.get(r["key"], [0, 0])
            assert (r["gp"], r["pts"]) == (gp, pts), (d["name"], r["key"])


def test_build_py_does_not_read_external_sources():
    src = (ROOT / "scripts" / "build.py").read_text()
    for forbidden in ("imports/", "ysg", "canonical_matches", "national"):
        assert forbidden not in src


def test_schedule_fields_intact():
    g = DATA["divisions"][0]["games"][0]
    assert len(g) == 11      # gamekey, no, date, time, home, away, hs, as, status, venue, resched
    assert all(len(x["games"][0]) == 11 for x in DATA["divisions"] if x["games"])


def test_ncsl_prediction_model_available():
    from model import outcome_probs
    w, d, l = outcome_probs(1.6, 1.1)
    assert abs(w + d + l - 1) < 1e-9 and w > l


def test_centering_does_not_change_expected_goals():
    """fit_age_group must return the same expected goals as the raw optimum."""
    import math
    import model
    games = [("a", "b", 3, 0), ("b", "c", 1, 1), ("c", "a", 0, 2), ("d", "e", 2, 1), ("e", "d", 0, 0), ("a", "c", 1, 0)]
    team_div = {"a": "1", "b": "1", "c": "1", "d": "2", "e": "2"}
    captured = {}
    real = model.minimize
    def spy(*a, **k):
        r = real(*a, **k); captured["x"] = r.x.copy(); return r
    model.minimize = spy
    try:
        mu, h, A, D = model.fit_age_group(games, team_div, 1.0)
    finally:
        model.minimize = real
    teams = sorted(team_div); n = len(teams); x = captured["x"]; idx = {t: i for i, t in enumerate(teams)}
    for hk, ak, _, _ in games:
        raw = math.exp(x[0] + x[1] + x[2 + idx[hk]] - x[2 + n + idx[ak]])
        out = math.exp(mu + h + A[hk] - D[ak])
        assert abs(raw - out) / raw < 1e-9

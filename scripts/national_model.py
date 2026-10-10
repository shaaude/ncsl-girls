"""National girls rating model.

One Poisson attack/defence model fit on all eligible canonical matches, every age together, so
each team has ONE rating whatever bracket it played in (a 2014/15 team's games in a U12
tournament and in its U13 league both count):

  expected goals(i vs j) = exp(mu + mu_age[bracket] + home*[i at home] + att_i - def_j)

  * bracket = the older of the two sides' entry ages; mu_age lets scoring levels differ by age
    (lightly pulled toward the overall mu); home advantage likewise per age, pulled toward one
    overall value.
  * Games between teams more than one year apart are excluded.

  * Home advantage applies only when the venue is known to be one side's home (NCSL league
    games). Tournament and unknown venues are treated as neutral.
  * Recency: each game is weighted 0.5 ** (days_old / RECENCY_HALF_LIFE_DAYS).
  * Margins are capped at MARGIN_CAP goals so a 12-0 counts like a 6-0.
  * Games from outside NCSL are weighted OUTSIDE_WEIGHT relative to NCSL league games.
  * L2 regularisation (NATIONAL_RIDGE) toward each team's starting point: its NCSL division
    placement (tier gap held at the NCSL model's value), or 0 for outside teams. The division
    placement is what keeps NCSL divisions comparable when no games link them.
  * Teams are comparable inside a "network": game-connected teams, with every group holding
    NCSL teams joined through division placement. Outside teams never linked to an NCSL team by
    games form separate networks, each centred on its own average (not on the same scale).
  * Uncertainty: approximate standard error of each team's strength from the curvature of the
    objective (diagonal Laplace approximation).
"""
import math
from collections import defaultdict
from datetime import date

import numpy as np
from scipy.optimize import minimize

from config import (MARGIN_CAP, MIN_RANKED_GAMES, NATIONAL_CUTOFF, NATIONAL_RIDGE,
                    RECENCY_HALF_LIFE_DAYS, SUPPORTED_AGES)

EXCLUSION_ORDER = ["on or before cutoff", "after evaluation date", "not played", "forfeit or administrative",
                   "score conflict", "scrimmage", "age gap over one year", "unsupported age"]
AGE_MU_RIDGE = 2.0
AGE_HOME_RIDGE = 5.0
MAX_AGE_GAP = 1


def age_n(age):
    return int(age[2:]) if age and age[2:].isdigit() else 0


def bracket_age(m):
    """The age level a game was played at: the older of the two sides' entry ages."""
    ages = [a for a in (m.get("age_a"), m.get("age_b")) if a]
    return max(ages, key=age_n) if ages else "all"


def eligibility(match, as_of, cutoff=NATIONAL_CUTOFF):
    """Return None if the match may train the national model, else the reason it may not."""
    d = date.fromisoformat(match["date"])
    if d <= cutoff:
        return "on or before cutoff"
    if d > as_of:
        return "after evaluation date"
    if match["status"] == "forfeit":
        return "forfeit or administrative"
    if match["status"] == "conflict":
        return "score conflict"
    if match["status"] != "final" or match["a_score"] is None or match["b_score"] is None:
        return "not played"
    if match.get("competition_type") == "scrimmage":
        return "scrimmage"
    if match["age_a"] not in SUPPORTED_AGES or match["age_b"] not in SUPPORTED_AGES:
        return "unsupported age"
    if abs(age_n(match["age_a"]) - age_n(match["age_b"])) > MAX_AGE_GAP:
        return "age gap over one year"
    return None


def capped(a, b, cap=MARGIN_CAP):
    if a - b > cap:
        a = b + cap
    elif b - a > cap:
        b = a + cap
    return a, b


def components(teams, games):
    parent = {t: t for t in teams}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for g in games:
        ra, rb = find(g["team_a"]), find(g["team_b"])
        if ra != rb:
            parent[ra] = rb
    groups = defaultdict(list)
    for t in teams:
        groups[find(t)].append(t)
    return sorted(groups.values(), key=lambda g: (-len(g), min(g)))


OUTSIDE_WEIGHT = 0.5       # weight of games from outside NCSL relative to NCSL league games
TIER_GAP_PRIOR = 1.14      # NCSL model's division gap (attack+defence), used as the starting belief
TIER_GAP_RIDGE = 1000.0     # effectively held at the NCSL value; estimating it from this season's few cross-division games predicted worse


def fit(games, as_of, ridge=NATIONAL_RIDGE, half_life=RECENCY_HALF_LIFE_DAYS, tier=None):
    """games: eligible matches (any ages). Returns a model dict.

    tier: optional {team: c} for NCSL teams, c = division flight minus the age group's average
    flight. Each NCSL team's strength is then pulled toward -gap*c instead of 0, and the gap
    itself is estimated from games that cross divisions (starting from the NCSL model's 1.14).
    This is what lets NCSL divisions with no games between them be compared, the way the NCSL
    predictor does; every team's own results still move it away from that starting point.
    Teams compared only through this assumption are flagged (linked_by_games = False)."""
    teams = sorted({g["team_a"] for g in games} | {g["team_b"] for g in games})
    if not teams:
        return {"mu": 0.3, "mu_age": {}, "home": 0.0, "teams": {}, "networks": [], "n_games": 0}
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    ages = sorted({bracket_age(g) for g in games}, key=age_n)
    aidx = {a: i for i, a in enumerate(ages)}
    K = len(ages)
    AG = np.array([aidx[bracket_age(g)] for g in games])
    A = np.array([idx[g["team_a"]] for g in games]); B = np.array([idx[g["team_b"]] for g in games])
    sc = [capped(g["a_score"], g["b_score"]) for g in games]
    GA = np.array([s[0] for s in sc], float); GB = np.array([s[1] for s in sc], float)
    HOME = np.array([1.0 if g.get("venue_type") == "home" else 0.0 for g in games])
    W = np.array([0.5 ** (max(0, (as_of - date.fromisoformat(g["date"])).days) / half_life)
                  * (1.0 if "ncsl" in g.get("sources", ["ncsl"]) else OUTSIDE_WEIGHT) for g in games])
    tier = tier or {}
    C = np.array([float(tier.get(t, 0.0)) for t in teams]) / 2      # half to attack, half to defence
    use_tier = bool(np.any(C != 0))

    # x = [mu, h, off_1..off_K, hoff_1..hoff_K, gap, att_1..att_n, def_1..def_n]
    gi = 2 + 2 * K
    o0 = gi + 1

    def f(x):
        mu, h, off, hoff, a, d = x[0], x[1], x[2:2 + K], x[2 + K:gi], x[o0:o0 + n], x[o0 + n:]
        gap = x[gi] if use_tier else 0.0
        pa, pd = a + gap * C, d + gap * C            # distance from the division-based starting point
        base = mu + off[AG]
        hh = h + hoff[AG]
        la = base + hh * HOME + a[A] - d[B]
        lb = base + a[B] - d[A]
        ea, eb = np.exp(la), np.exp(lb)
        nll = np.sum(W * (ea - GA * la)) + np.sum(W * (eb - GB * lb))
        pen = (ridge * (np.sum(pa ** 2) + np.sum(pd ** 2)) + 5.0 * h ** 2 + AGE_MU_RIDGE * np.sum(off ** 2)
               + AGE_HOME_RIDGE * np.sum(hoff ** 2) + TIER_GAP_RIDGE * (x[gi] - TIER_GAP_PRIOR) ** 2)
        ra, rb = W * (ea - GA), W * (eb - GB)
        g = np.zeros_like(x)
        g[0] = ra.sum() + rb.sum(); g[1] = (ra * HOME).sum() + 10.0 * h
        go = np.zeros(K); np.add.at(go, AG, ra + rb)
        g[2:2 + K] = go + 2 * AGE_MU_RIDGE * off
        gh = np.zeros(K); np.add.at(gh, AG, ra * HOME)
        g[2 + K:gi] = gh + 2 * AGE_HOME_RIDGE * hoff
        g[gi] = (2 * ridge * np.sum(C * (pa + pd)) if use_tier else 0.0) + 2 * TIER_GAP_RIDGE * (x[gi] - TIER_GAP_PRIOR)
        ga = np.zeros(n); gd = np.zeros(n)
        np.add.at(ga, A, ra); np.add.at(ga, B, rb)
        np.add.at(gd, B, -ra); np.add.at(gd, A, -rb)
        g[o0:o0 + n] = ga + 2 * ridge * pa; g[o0 + n:] = gd + 2 * ridge * pd
        return nll + pen, g

    x0 = np.zeros(o0 + 2 * n)
    x0[0] = math.log(max(0.05, float((GA * W).sum() + (GB * W).sum()) / max(1e-9, 2 * W.sum())))
    x0[gi] = TIER_GAP_PRIOR
    res = minimize(f, x0, jac=True, method="L-BFGS-B", options={"maxiter": 5000, "ftol": 1e-13, "gtol": 1e-9})
    mu, h = float(res.x[0]), float(res.x[1])
    off, hoff = res.x[2:2 + K].copy(), res.x[2 + K:gi].copy()
    gap = float(res.x[gi]) if use_tier else None
    a, d = res.x[o0:o0 + n].copy(), res.x[o0 + n:].copy()

    # curvature for uncertainty (diagonal Fisher information)
    base = mu + off[AG]
    la = base + (h + hoff[AG]) * HOME + a[A] - d[B]; lb = base + a[B] - d[A]
    ea, eb = np.exp(la), np.exp(lb)
    ia = np.full(n, 2 * ridge); idd = np.full(n, 2 * ridge)
    np.add.at(ia, A, W * ea); np.add.at(ia, B, W * eb)
    np.add.at(idd, B, W * ea); np.add.at(idd, A, W * eb)
    se = np.sqrt(1 / ia + 1 / idd)

    # "network": teams that can be compared. Game-connected components are joined when they hold
    # NCSL teams placed by division (the tier assumption links them); others stand alone.
    comps = components(teams, games)
    comp_of = {t: k for k, c in enumerate(comps) for t in c}
    tiered = [k for k, c in enumerate(comps) if any(t in tier for t in c)]
    groups = ([sorted(t for k in tiered for t in comps[k])] if tiered else []) + \
             [c for k, c in enumerate(comps) if k not in tiered]
    groups.sort(key=lambda g: (-len(g), min(g)))
    out, net_of = {}, {}
    for k, net in enumerate(groups):
        ids = [idx[t] for t in net]
        c = (a[ids].mean() + d[ids].mean()) / 2      # common shift keeps predictions unchanged
        a[ids] -= c; d[ids] -= c
        for t in net:
            net_of[t] = k
    nets = groups
    games_of = defaultdict(int)
    for g in games:
        games_of[g["team_a"]] += 1; games_of[g["team_b"]] += 1
    for t in teams:
        i = idx[t]
        out[t] = {"att": float(a[i]), "def": float(d[i]), "se": float(se[i]), "games": games_of[t],
                  "network": net_of[t], "component": comp_of[t]}
    return {"mu": mu, "mu_age": {ag: mu + float(off[i]) for ag, i in aidx.items()}, "home": h,
            "home_age": {ag: h + float(hoff[i]) for ag, i in aidx.items()}, "tier_gap": gap, "teams": out,
            "networks": [len(n_) for n_ in nets], "n_games": len(games), "converged": bool(res.success)}


def rating(att, dfn):
    return 1500 + 250 * (att + dfn)


def probs(lh, la, max_goals=10):
    from math import exp, factorial
    ph = [exp(-lh) * lh ** k / factorial(k) for k in range(max_goals + 1)]
    pa = [exp(-la) * la ** k / factorial(k) for k in range(max_goals + 1)]
    w = sum(ph[i] * pa[j] for i in range(max_goals + 1) for j in range(max_goals + 1) if i > j)
    dr = sum(ph[i] * pa[i] for i in range(max_goals + 1))
    l = sum(ph[i] * pa[j] for i in range(max_goals + 1) for j in range(max_goals + 1) if i < j)
    s = w + dr + l
    return w / s, dr / s, l / s


def predict(model, ta, tb, a_home=False, age=None):
    """age: the bracket the game is played at (sets the scoring level); None = overall level."""
    A, B = model["teams"][ta], model["teams"][tb]
    if A["network"] != B["network"]:
        return None
    mu = model.get("mu_age", {}).get(age, model["mu"]) if age else model["mu"]
    home = model.get("home_age", {}).get(age, model["home"]) if age else model["home"]
    lh = math.exp(mu + (home if a_home else 0) + A["att"] - B["def"])
    la = math.exp(mu + B["att"] - A["def"])
    return probs(lh, la)


def evaluate(pairs):
    """pairs: list of ((pw, pd, pl), outcome_index). Returns log-loss, Brier, calibration."""
    if not pairs:
        return {"n": 0}
    ll = br = 0.0
    bins = defaultdict(lambda: [0, 0.0, 0])   # bucket -> [count, sum predicted, hits]
    for p, k in pairs:
        ll -= math.log(max(p[k], 1e-9))
        br += sum((p[j] - (1 if j == k else 0)) ** 2 for j in range(3))
        for j in range(3):
            b = min(9, int(p[j] * 10))
            bins[b][0] += 1; bins[b][1] += p[j]; bins[b][2] += (j == k)
    n = len(pairs)
    cal = [{"bucket": f"{b*10}-{b*10+10}%", "n": c, "predicted": round(s / c, 3), "observed": round(h / c, 3)}
           for b, (c, s, h) in sorted(bins.items())]
    ece = sum(c * abs(s / c - h / c) for c, s, h in bins.values()) / (3 * n)
    return {"n": n, "log_loss": round(ll / n, 4), "brier": round(br / n, 4), "ece": round(ece, 4), "calibration": cal}


def outcome(a, b):
    return 0 if a > b else 1 if a == b else 2

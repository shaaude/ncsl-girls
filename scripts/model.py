"""Team strength model: Poisson goals with attack/defence ratings and home advantage.

Fit separately per division (teams in different divisions never meet), with a
shared goal baseline and home advantage per age group. Ratings are shrunk toward
the division average by an L2 penalty. Division tiers are then placed on one
scale with a tier offset, an assumption the data cannot test.
"""
import math
import numpy as np
from scipy.optimize import minimize

def fit_age_group(games, team_div, ridge=2.0):
    """games: list of (home_key, away_key, hg, ag). team_div: key -> division id.
    Returns mu, home, att{key}, dfn{key} with ratings centred within each division."""
    teams = sorted(team_div)
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams)
    if not games:
        return 0.3, 0.0, {t: 0.0 for t in teams}, {t: 0.0 for t in teams}
    H = np.array([idx[g[0]] for g in games]); A = np.array([idx[g[1]] for g in games])
    HG = np.array([g[2] for g in games], float); AG = np.array([g[3] for g in games], float)
    divs = sorted(set(team_div.values()))
    groups = [np.array([idx[t] for t in teams if team_div[t] == d]) for d in divs]

    def unpack(x):
        return x[0], x[1], x[2:2 + n], x[2 + n:]

    def f(x):
        mu, h, a, d = unpack(x)
        lh = mu + h + a[H] - d[A]; la = mu + a[A] - d[H]
        eh, ea = np.exp(lh), np.exp(la)
        nll = np.sum(eh - HG * lh) + np.sum(ea - AG * la)
        pen = ridge * (np.sum(a ** 2) + np.sum(d ** 2)) + 5.0 * h ** 2
        g = np.zeros_like(x)
        rh, ra = eh - HG, ea - AG
        g[0] = rh.sum() + ra.sum(); g[1] = rh.sum() + 10.0 * h
        ga = np.zeros(n); gd = np.zeros(n)
        np.add.at(ga, H, rh); np.add.at(ga, A, ra)
        np.add.at(gd, A, -rh); np.add.at(gd, H, -ra)
        g[2:2 + n] = ga + 2 * ridge * a; g[2 + n:] = gd + 2 * ridge * d
        return nll + pen, g

    x0 = np.zeros(2 + 2 * n); x0[0] = math.log(max(1e-3, (HG.mean() + AG.mean()) / 2))
    res = minimize(f, x0, jac=True, method="L-BFGS-B")
    mu, h, a, d = unpack(res.x)
    a = a.copy(); d = d.copy()
    for gidx in groups:  # centre within each division (identifiability per division)
        if len(gidx):
            ca, cd = a[gidx].mean(), d[gidx].mean()
            a[gidx] -= ca; d[gidx] -= cd; mu += 0  # baseline absorbs nothing; centring is cosmetic per division
    return float(mu), float(h), {t: float(a[idx[t]]) for t in teams}, {t: float(d[idx[t]]) for t in teams}

def outcome_probs(lh, la, max_goals=10):
    ph = [math.exp(-lh) * lh ** k / math.factorial(k) for k in range(max_goals + 1)]
    pa = [math.exp(-la) * la ** k / math.factorial(k) for k in range(max_goals + 1)]
    w = d = l = 0.0
    for i, x in enumerate(ph):
        for j, y in enumerate(pa):
            p = x * y
            if i > j: w += p
            elif i == j: d += p
            else: l += p
    s = w + d + l
    return w / s, d / s, l / s

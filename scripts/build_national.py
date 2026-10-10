"""Build the national girls dataset and site outputs.

  python scripts/build_national.py [--as-of YYYY-MM-DD]

Reads   data/games.json, data/registry.json, data/fetch_report.json (NCSL, authoritative)
        imports/csv/*.csv, imports/ysg/*.html (external results; see imports/README.md)
        data/identity_decisions.json (manual identity decisions, optional)
        imports/ysg/directory/*.json (club and squad names, to tie a team's separate YSG records)
Writes  data/team_aliases.json, data/match_observations.jsonl, data/match_index.json,
        data/canonical_matches.jsonl, data/match_corrections.jsonl, data/review_queue.jsonl,
        data/national_ratings_history.json, data/record_links.json, site/national/**

NCSL standings are never touched here: build.py computes them from NCSL results alone.
All outputs are written to a temporary folder first and swapped in only if the whole build
succeeds, so a failure leaves the previous national site intact.
"""
import argparse
import json
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import (MAIN_NETWORK_MIN_SHARE, MIN_RANKED_GAMES, NATIONAL_CUTOFF, STATE_REGION,
                    SUPPORTED_AGES, eastern_today)
from identities import Identities
from reconcile import ObservationStore, corrections, link_identities, load_jsonl, reconcile, write_jsonl
import match_records
from sources import csv_observations, ncsl_observations, ysg_observations
import national_model as nm
import model as ncsl_model

ROOT = Path(__file__).resolve().parent.parent
DATA, SITE = ROOT / "data", ROOT / "site"


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_ncsl():
    reg = json.loads((DATA / "registry.json").read_text())
    by_div = {d["id"]: d for d in reg["divisions"]}
    games = json.loads((DATA / "games.json").read_text()) if (DATA / "games.json").exists() else {}
    fr = json.loads((DATA / "fetch_report.json").read_text()) if (DATA / "fetch_report.json").exists() else {}
    return by_div, games, fr.get("fetched_at")


def register_ncsl_teams(ids, games, by_div):
    for g in games.values():
        div = by_div.get(g["division_id"])
        if not div:
            continue
        for t in (g["home"], g["away"]):
            if t["key"]:
                ids.register_ncsl(t["key"], t["name"], div["age"], club=t.get("club"), code=t.get("code"),
                                  division=g["division_id"])


def backtest(eligible, ncsl_games_by_age, as_of, tier=None):
    """Rolling-origin weekly backtest. For each origin date, fit only on games played on or before
    it and predict the following 7 days. Compares, on the same NCSL fixtures: the pooled national
    model (one rating per team across ages), the previous per-age national model (same-age games
    only), the NCSL baseline and base rates."""
    first = min((m["date"] for m in eligible), default=None)
    if not first:
        return {"note": "no eligible matches yet"}
    start = date.fromisoformat(first) + timedelta(days=14)
    origins = []
    d = start
    while d < as_of:
        origins.append(d); d += timedelta(days=7)
    nat_all, nat_ncsl, old_ncsl, base_ncsl, rate_ncsl, cov = [], [], [], [], [], Counter()
    nat_out, rate_out = [], []
    same = defaultdict(lambda: [[], []])         # fixtures both national variants predicted
    vs_base = defaultdict(list)                  # fixtures both national and NCSL baseline predicted
    for origin in origins:
        train = [m for m in eligible if date.fromisoformat(m["date"]) <= origin]
        test = [m for m in eligible if origin < date.fromisoformat(m["date"]) <= min(as_of, origin + timedelta(days=7))]
        if not train or not test:
            continue
        mdl = nm.fit(train, origin, tier=tier)
        tr_ncsl = [m for m in train if "ncsl" in m["sources"]]
        mdl_ncsl = nm.fit(tr_ncsl, origin, tier=tier) if tr_ncsl else None
        per_age = {}
        for age in {m["age_a"] for m in test if m["age_a"] == m["age_b"]}:
            tr = [m for m in train if m["age_a"] == age and m["age_b"] == age]
            per_age[age] = nm.fit(tr, origin) if tr else None
        counts = Counter(nm.outcome(m["a_score"], m["b_score"]) for m in train)
        tot = sum(counts.values())
        rates = tuple(counts.get(k, 0) / tot for k in range(3))
        base_cache = {}
        for m in test:
            k = nm.outcome(m["a_score"], m["b_score"])
            home = m.get("venue_type") == "home"
            p = None
            if m["team_a"] in mdl["teams"] and m["team_b"] in mdl["teams"]:
                p = nm.predict(mdl, m["team_a"], m["team_b"], a_home=home, age=nm.bracket_age(m))
            if p:
                nat_all.append((p, k))
                if "ncsl" not in m["sources"]:
                    nat_out.append((p, k)); rate_out.append((rates, k))
            if "ncsl" not in m["sources"] or m["age_a"] != m["age_b"]:
                continue
            age = m["age_a"]
            if age not in base_cache:
                ng = [g for g in ncsl_games_by_age.get(age, []) if date.fromisoformat(g["date"]) <= origin]
                team_div = {}
                for g in ncsl_games_by_age.get(age, []):
                    team_div[g["home"]] = g["div"]; team_div[g["away"]] = g["div"]
                base_cache[age] = ncsl_model.fit_age_group([(g["home"], g["away"], g["hs"], g["as"]) for g in ng],
                                                           team_div, 1.0) if ng else None
            base = base_cache[age]
            if not (base and m.get("ncsl_home") in base[2] and m.get("ncsl_away") in base[2]):
                continue
            pb = ncsl_baseline_probs(base, m["ncsl_home"], m["ncsl_away"])
            # national predictions are oriented team_a vs team_b; NCSL ones home vs away
            a_is_home = m["ncsl_a_key"] == m["ncsl_home"]
            kk = k if a_is_home else 2 - k
            flip = (lambda q: q) if a_is_home else (lambda q: (q[2], q[1], q[0]))
            cov["fixtures"] += 1
            base_ncsl.append((pb, kk))
            rate_ncsl.append((flip(rates), kk))
            if p:
                cov["pooled"] += 1
                nat_ncsl.append((flip(p), kk))
                pn = None
                if mdl_ncsl and m["team_a"] in mdl_ncsl["teams"] and m["team_b"] in mdl_ncsl["teams"]:
                    pn = nm.predict(mdl_ncsl, m["team_a"], m["team_b"], a_home=home, age=nm.bracket_age(m))
                if pn:
                    vs_base["national"].append((flip(p), kk)); vs_base["ncsl_baseline"].append((pb, kk))
                    vs_base["national_trained_on_ncsl_games_only"].append((flip(pn), kk))
            om = per_age.get(age)
            po = None
            if om and m["team_a"] in om["teams"] and m["team_b"] in om["teams"]:
                po = nm.predict(om, m["team_a"], m["team_b"], a_home=home)
            if po:
                cov["per_age"] += 1
                old_ncsl.append((flip(po), kk))
            if p and po:
                same["pooled"][0].append((flip(p), kk)); same["per_age"][0].append((flip(po), kk))
    return {
        "method": "rolling weekly origins; each fit uses only games on or before the origin and predicts the next 7 days",
        "origins": [o.isoformat() for o in origins],
        "national_all_fixtures": nm.evaluate(nat_all),
        "outside_fixtures": {"national_model": nm.evaluate(nat_out), "base_rates": nm.evaluate(rate_out)},
        "ncsl_fixtures": {
            "national_model": nm.evaluate(nat_ncsl),
            "national_model_coverage": f"{cov['pooled']} of {cov['fixtures']} fixtures (others: teams in different networks)",
            "previous_per_age_model": nm.evaluate(old_ncsl),
            "pooled_vs_per_age_same_fixtures": {"pooled": nm.evaluate(same["pooled"][0]),
                                                "per_age": nm.evaluate(same["per_age"][0])},
            "ncsl_baseline": nm.evaluate(base_ncsl),
            "national_vs_ncsl_baseline_same_fixtures": {k: nm.evaluate(v) for k, v in vs_base.items()},
            "base_rates": nm.evaluate(rate_ncsl),
        },
    }


def division_offsets(teams, by_div):
    """{canonical id: flight - average flight of NCSL teams in its age} for NCSL teams."""
    flight = {}
    for cid, t in teams.items():
        div = by_div.get(t.get("ncsl_division") or "")
        f = str((div or {}).get("flight", "")).strip().upper()
        if t.get("ncsl_key") and f:
            if f.isdigit():
                flight[cid] = int(f)
            elif len(f) == 1 and f.isalpha():
                flight[cid] = ord(f) - ord("A") + 1        # U9-U11 divisions A, B, C... (A strongest)
    by_age = defaultdict(list)
    for cid, f in flight.items():
        by_age[teams[cid]["age"]].append(f)
    mean = {a: sum(v) / len(v) for a, v in by_age.items()}
    return {cid: f - mean[teams[cid]["age"]] for cid, f in flight.items()}


ncsl_model_tiers = {}
NCSL_TIER_GAP = 1.14


def ncsl_baseline_probs(base, home, away):
    """The existing NCSL model's prediction, including its division-tier gap."""
    import math
    mu, h, A, D = base
    def st(key):
        o = -NCSL_TIER_GAP * (ncsl_model_tiers.get(key, 1) - 1)
        return A[key] + o / 2, D[key] + o / 2
    ah, dh = st(home); aa, da = st(away)
    return ncsl_model.outcome_probs(math.exp(mu + h + ah - da), math.exp(mu + aa - dh))


def main(as_of=None):
    t0 = time.time()
    as_of = as_of or eastern_today()
    built = now()
    by_div, games, ncsl_fetched = load_ncsl()
    if not games:
        raise SystemExit("no NCSL games available; refusing to build national outputs")
    for g in games.values():
        div = by_div.get(g["division_id"])
        for t in (g["home"], g["away"]):
            if div and t["key"]:
                ncsl_model_tiers[t["key"]] = int(div["flight"]) if div["flight"].isdigit() else 1

    ids = Identities()
    register_ncsl_teams(ids, games, by_div)

    obs_ncsl = ncsl_observations(games, by_div, ncsl_fetched)
    obs_csv, prob_csv = csv_observations()
    obs_ysg, prob_ysg = ysg_observations()
    for o in obs_csv + obs_ysg:
        for side in ("a", "b"):
            t = o[side]
            ids.ensure_source_team(t["source_key"], t["name"], t["age"], state=t.get("state"), club=t.get("club"))
    dec_path = DATA / "identity_decisions.json"
    if dec_path.exists():
        ids.apply_manual(json.loads(dec_path.read_text()))

    store = ObservationStore()
    store.upsert(obs_ncsl, "ncsl")
    for src in ("csv", "event"):
        store.upsert([o for o in obs_csv if o["source"] == src], src)
    store.upsert(obs_ysg, "ysg")
    all_obs = store.all()
    newly_linked = link_identities(ids, all_obs)
    # a team's separate YSG tournament/league records: same club, same squad label, same age
    decisions = json.loads(dec_path.read_text()) if dec_path.exists() else {}
    rec = match_records.compute({"teams": ids.teams, "mappings": ids.mappings}, decisions)
    newly_linked += ids.apply_record_links(rec["links"])
    (DATA / "record_links.json").write_text(json.dumps(rec, indent=1, sort_keys=True))

    prev_matches = load_jsonl(DATA / "canonical_matches.jsonl")
    matches, review, stats = reconcile(all_obs, ids)
    corr = corrections(prev_matches, matches, built)

    # NCSL keys on each match (for baseline comparison and NCSL team links)
    obs_by_id = store.rows
    for m in matches:
        n = next((obs_by_id[o] for o in m["observations"] if obs_by_id[o]["source"] == "ncsl"), None)
        if n:
            m["ncsl_home"] = n["a"]["source_key"][5:]; m["ncsl_away"] = n["b"]["source_key"][5:]
            m["ncsl_a_key"] = n["a"]["source_key"][5:] if ids.resolve(n["a"]["source_key"]) == m["team_a"] else n["b"]["source_key"][5:]

    # ---- eligibility
    excluded = Counter()
    eligible = []
    for m in matches:
        r = nm.eligibility(m, as_of)
        m["eligible"] = r is None
        m["exclusion"] = r
        if r:
            excluded[r] += 1
        else:
            eligible.append(m)
    team_age = {cid: t["age"] for cid, t in ids.teams.items()}
    excluded["team identity not confirmed"] += stats["unresolved_observations"]

    # ---- fit (all ages together) and per-age tables
    hist_path = DATA / "national_ratings_history.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else []
    prev_snap = next((h for h in reversed(hist) if h["date"] <= (as_of - timedelta(days=6)).isoformat()), None)
    teams = ids.teams
    age_out, team_profiles, snapshot = {}, {}, {}
    upcoming = defaultdict(list)
    for m in matches:
        if m["status"] == "scheduled" and m["date"] >= as_of.isoformat():
            upcoming[m["team_a"]].append(m); upcoming[m["team_b"]].append(m)

    matches_by_team = defaultdict(list)
    for m in matches:
        matches_by_team[m["team_a"]].append(m); matches_by_team[m["team_b"]].append(m)
    # NCSL division placement, centred per age (Div 1 strongest): the starting point that lets
    # divisions with no games between them be compared
    tier = division_offsets(ids.teams, by_div)
    # ONE fit over every age: each team gets one rating whatever bracket it played in
    mdl = nm.fit(eligible, as_of, tier=tier) if eligible else {"mu": 0.3, "mu_age": {}, "home": 0.0, "teams": {},
                                                    "networks": [], "n_games": 0}
    # display scale: 1500 = the average NCSL team of the team's own age group (the model itself
    # is one scale across ages; this shift changes no prediction or within-age order)
    strength = {cid: r["att"] + r["def"] for cid, r in mdl["teams"].items()}
    age_center = {}
    for age in SUPPORTED_AGES:
        v = [strength[c] for c in strength if team_age.get(c) == age and teams[c].get("ncsl_key")
             and mdl["teams"][c]["network"] == 0]
        age_center[age] = sum(v) / len(v) if v else 0.0
    rating_all = {cid: round(nm.rating(r["att"], r["def"]) - 250 * (age_center.get(team_age.get(cid), 0.0)
                                                                    if r["network"] == 0 else 0.0))
                  for cid, r in mdl["teams"].items()}
    external_nets = {mdl["teams"][m["team_a"]]["network"] for m in eligible if "ncsl" not in m["sources"]}
    eligible_by_team = defaultdict(list)
    for m in eligible:
        eligible_by_team[m["team_a"]].append(m); eligible_by_team[m["team_b"]].append(m)
    for age in SUPPORTED_AGES:
        rated = {cid: r for cid, r in mdl["teams"].items() if team_age.get(cid) == age}
        ms = sorted({m["match_id"]: m for cid in rated for m in eligible_by_team[cid]}.values(), key=lambda m: m["date"])
        # networks numbered within this age group, largest first (0 = main network)
        net_sizes = Counter(r["network"] for r in rated.values())
        local = {g: i for i, (g, _) in enumerate(sorted(net_sizes.items(), key=lambda kv: (-kv[1], kv[0])))}
        nets = [net_sizes[g] for g in sorted(local, key=local.get)]
        main_g = next((g for g, i in local.items() if i == 0), None)
        main_share = (nets[0] / len(rated)) if rated else 0
        # game-connected pieces inside the main network (joined only by division placement)
        comp_sizes = Counter(r["component"] for r in rated.values() if r["network"] == main_g)
        comp_local = {c: i for i, (c, _) in enumerate(sorted(comp_sizes.items(), key=lambda kv: (-kv[1], kv[0])))}
        # A network made only of one league's games is that league's table, not a national
        # comparison. National ranks need the main network to include results from outside NCSL.
        main_external = main_g in external_nets
        national_ok = bool(rated) and main_share >= MAIN_NETWORK_MIN_SHARE and main_external
        rows = []
        for cid, r in rated.items():
            t = teams[cid]
            rows.append({"id": cid, "name": t["name"], "club": t.get("club"),
                         "state": t.get("state"), "region": STATE_REGION.get(t.get("state") or ""),
                         "ncsl_key": t.get("ncsl_key"), "ncsl_division": t.get("ncsl_division"),
                         "att": round(r["att"], 4), "def": round(r["def"], 4), "se": round(r["se"], 3),
                         "rating": rating_all[cid], "games": r["games"],
                         "network": local[r["network"]], "main_network": r["network"] == main_g and national_ok,
                         "linked_group": comp_local.get(r["component"])})
        # records, SOS (opponents of any age, on the one shared rating scale)
        rating_of = rating_all
        rec = defaultdict(lambda: {"all": [0, 0, 0], "league": [0, 0, 0], "tournament": [0, 0, 0], "opp": []})
        for m in ms:
            for me, op, gf, ga in ((m["team_a"], m["team_b"], m["a_score"], m["b_score"]),
                                   (m["team_b"], m["team_a"], m["b_score"], m["a_score"])):
                k = 0 if gf > ga else 1 if gf == ga else 2
                rec[me]["all"][k] += 1
                bucket = "league" if m["competition_type"] == "league" else "tournament" if m["competition_type"] in ("tournament", "showcase") else None
                if bucket:
                    rec[me][bucket][k] += 1
                rec[me]["opp"].append(rating_of.get(op))
        for x in rows:
            r = rec[x["id"]]
            x["record"] = r["all"]; x["league_record"] = r["league"]; x["tournament_record"] = r["tournament"]
            opp = [o for o in r["opp"] if o is not None]
            x["sos"] = round(statistics.mean(opp)) if opp else None
            x["status"] = ("ranked" if x["main_network"] and x["games"] >= MIN_RANKED_GAMES
                           else "provisional" if x["main_network"] else "separate network")
            # how sure the rating is, from its standard error (games played and how informative)
            x["reliability"] = ("low" if x["status"] == "separate network" or x["se"] >= 0.55 else
                                "high" if x["se"] < 0.42 else "medium")
        # ranks within main network (ranked teams only); state/region ranks likewise
        ranked = sorted([x for x in rows if x["status"] == "ranked"], key=lambda x: -x["rating"])
        for i, x in enumerate(ranked, 1):
            x["national_rank"] = i
        for key in ("state", "region"):
            groups = defaultdict(list)
            for x in ranked:
                if x[key]:
                    groups[x[key]].append(x)
            for g in groups.values():
                for i, x in enumerate(g, 1):
                    x[f"{key}_rank"] = i
        for x in rows:
            snapshot[x["id"]] = {"rating": x["rating"], "rank": x.get("national_rank")}
            if prev_snap and x["id"] in prev_snap["teams"] and x.get("national_rank") and prev_snap["teams"][x["id"]].get("rank"):
                x["rank_move"] = prev_snap["teams"][x["id"]]["rank"] - x["national_rank"]
        rows.sort(key=lambda x: (x["status"] != "ranked", x["status"] != "provisional", -x["rating"]))
        known = [cid for cid, t in teams.items() if t["age"] == age]
        age_out[age] = {
            "age": age, "built_at": built, "as_of": as_of.isoformat(),
            "model": {"mu": round(mdl["mu_age"].get(age, mdl["mu"]), 5),
                      "tier_gap": round(mdl["tier_gap"], 3) if mdl.get("tier_gap") is not None else None,
                      "home": round(mdl.get("home_age", {}).get(age, mdl["home"]), 5),
                      "n_games": len(ms), "pooled_games_all_ages": mdl["n_games"]},
            "national_ranking_available": national_ok,
            "coverage": {"teams_known": len(known), "teams_rated": len(rows),
                         "teams_ranked": len(ranked), "eligible_matches": len(ms),
                         "networks": len(nets), "largest_network": nets[0] if nets else 0,
                         "games_against_other_ages": sum(1 for m in ms if m["age_a"] != m["age_b"]
                                                         or team_age.get(m["team_a"]) != team_age.get(m["team_b"])),
                         "main_network_share": round(main_share, 3), "main_network_has_external": main_external,
                         "game_linked_groups_in_main_network": len(comp_sizes),
                         "sources": sorted({s for m in ms for s in m["sources"]})},
            "teams": rows,
        }
        # profiles: every match a team played, whatever age bracket it was entered in
        by_team = matches_by_team
        row_of = {x["id"]: x for x in rows}
        for cid in known:
            t = teams[cid]
            hist_t = [{"date": h["date"], "rating": h["teams"].get(cid, {}).get("rating")} for h in hist[-30:]]
            prof_matches = []
            for m in sorted(by_team.get(cid, []), key=lambda m: m["date"]):
                me_a = m["team_a"] == cid
                op = m["team_b"] if me_a else m["team_a"]
                gf, ga = (m["a_score"], m["b_score"]) if me_a else (m["b_score"], m["a_score"])
                prof_matches.append({"id": m["match_id"], "date": m["date"], "time": m.get("time"), "opp": op,
                                     "opp_name": teams[op]["name"], "gf": gf, "ga": ga, "status": m["status"],
                                     "venue": ("H" if me_a else "A") if m["venue_type"] == "home" else "N" if m["venue_type"] == "neutral" else "?",
                                     "competition": m.get("competition"), "type": m["competition_type"],
                                     "sources": m["sources"], "urls": m["urls"][:3],
                                     "eligible": m.get("eligible", False), "exclusion": m.get("exclusion")})
            team_profiles[cid] = {
                "id": cid, "name": t["name"], "age": age, "state": t.get("state"),
                "region": STATE_REGION.get(t.get("state") or ""), "club": t.get("club"),
                "ncsl_key": t.get("ncsl_key"), "ncsl_division": t.get("ncsl_division"),
                "aliases": ids.aliases_of(cid), "summary": row_of.get(cid), "matches": prof_matches,
                "history": hist_t, "last_verified": max((m["last_verified"] for m in by_team.get(cid, [])), default=None),
            }

    hist = [h for h in hist if h["date"] != as_of.isoformat()] + [{"date": as_of.isoformat(), "teams": snapshot}]
    hist = hist[-180:]

    # ---- backtest
    ncsl_by_age = defaultdict(list)
    for g in games.values():
        div = by_div.get(g["division_id"])
        if div and g["status"] == "final" and g["date"]:
            ncsl_by_age[div["age"]].append({"home": g["home"]["key"], "away": g["away"]["key"], "hs": g["home_score"],
                                            "as": g["away_score"], "date": g["date"], "div": g["division_id"]})
    validation = backtest(eligible, ncsl_by_age, as_of, tier)

    # ---- sources and coverage
    perm = json.loads((DATA / "permissions.json").read_text()) if (DATA / "permissions.json").exists() else {}
    src_counts = Counter(o["source"] for o in all_obs)
    latest = lambda s: max((o.get("retrieved_at") or "" for o in all_obs if o["source"] == s), default=None)
    ncsl_cids = {cid for cid, t in teams.items() if t.get("ncsl_key")}
    ncsl_linked = {m["canonical_id"] for k, m in ids.mappings.items()
                   if not k.startswith("ncsl:") and m["status"] == "confirmed" and m["canonical_id"] in ncsl_cids}
    external_matches = [m for m in matches if "ncsl" not in m["sources"]]          # games NCSL doesn't have
    external_played = [m for m in external_matches if m.get("eligible")]
    index = {
        "built_at": built, "as_of": as_of.isoformat(), "cutoff": NATIONAL_CUTOFF.isoformat(),
        "cutoff_rule": f"matches dated after {NATIONAL_CUTOFF.isoformat()}",
        "min_ranked_games": MIN_RANKED_GAMES,
        "sources": {
            "ncsl": {"label": "NCSL (Demosphere)", "observations": src_counts.get("ncsl", 0), "last_retrieved": ncsl_fetched,
                     "automated": True},
            "ysg": {"label": "YouthSoccerGames", "observations": src_counts.get("ysg", 0), "last_retrieved": latest("ysg"),
                    "automated": bool(perm.get("youthsoccergames", {}).get("granted")),
                    "note": ("Collected under written permission from YouthSoccerGames (2026-10-10): directory pages weekly, "
                             "team pages once a week 1-5 AM Eastern, games from Aug 1, 2026."
                             if perm.get("youthsoccergames", {}).get("granted") else
                             "Automated collection is off: no written permission is on file. Saved pages in imports/ysg/ are processed.")},
            "csv": {"label": "Tournament and other exports (CSV)", "observations": src_counts.get("csv", 0) + src_counts.get("event", 0),
                    "last_retrieved": max(filter(None, [latest("csv"), latest("event")]), default=None), "automated": False},
        },
        "totals": {"canonical_teams": len(teams), "ncsl_teams": len(ncsl_cids),
                   "ncsl_teams_linked_to_other_sources": len(ncsl_linked),
                   "non_ncsl_teams": len(teams) - len(ncsl_cids),
                   "observations": stats["observations"], "canonical_matches": len(matches),
                   "eligible_matches": len(eligible),
                   "duplicates_merged": stats["duplicates_merged"],
                   "score_conflicts": stats["conflicts"],
                   "unresolved_conflicts": sum(1 for r in review if r["type"] == "score_conflict" and not r["resolved_by_authority"]),
                   "review_queue": len(review), "identities_linked_this_run": newly_linked,
                   "external_matches": len(external_matches), "external_eligible_matches": len(external_played),
                   "ncsl_games_confirmed_by_other_sources": sum(1 for m in matches if "ncsl" in m["sources"] and len(m["sources"]) > 1)},
        "exclusions": dict(excluded),
        "import_problems": (prob_csv + prob_ysg)[:200],
        "ages": [{"age": a, **age_out[a]["coverage"], "national_ranking_available": age_out[a]["national_ranking_available"]}
                 for a in SUPPORTED_AGES],
        "latest_external": [{"date": m["date"], "a": teams[m["team_a"]]["name"], "b": teams[m["team_b"]]["name"],
                             "score": [m["a_score"], m["b_score"]], "competition": m.get("competition"),
                             "age": m["age_a"], "sources": m["sources"]}
                            for m in sorted(external_played, key=lambda m: m["date"], reverse=True)[:12]],
        "ncsl_map": {t["ncsl_key"]: cid for cid, t in teams.items() if t.get("ncsl_key")},
        "validation": validation,
        "build_seconds": None,
    }

    # ---- write everything atomically
    tmp = SITE / "national.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "teams").mkdir(parents=True)
    for a, o in age_out.items():
        (tmp / f"{a}.json").write_text(json.dumps(o, separators=(",", ":")))
    for cid, p in team_profiles.items():
        (tmp / "teams" / f"{cid}.json").write_text(json.dumps(p, separators=(",", ":")))
    index["build_seconds"] = round(time.time() - t0, 1)
    (tmp / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    final = SITE / "national"
    old = SITE / "national.old"
    if old.exists():
        shutil.rmtree(old)
    if final.exists():
        final.rename(old)
    tmp.rename(final)
    if old.exists():
        shutil.rmtree(old)

    ids.save(); store.save()
    write_jsonl(DATA / "canonical_matches.jsonl", matches)
    write_jsonl(DATA / "review_queue.jsonl", review)
    if corr:
        with (DATA / "match_corrections.jsonl").open("a") as f:
            for c in corr:
                f.write(json.dumps(c) + "\n")
    hist_path.write_text(json.dumps(hist, separators=(",", ":")))
    print(f"national: {len(teams)} teams, {len(matches)} matches, {index['totals']['eligible_matches']} eligible, "
          f"{stats['duplicates_merged']} duplicates merged, {stats['conflicts']} conflicts, {len(review)} review items, "
          f"{index['build_seconds']}s")
    return index


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    a = ap.parse_args()
    main(date.fromisoformat(a.as_of) if a.as_of else None)

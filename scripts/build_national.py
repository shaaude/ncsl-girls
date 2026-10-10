"""Build the national girls dataset and site outputs.

  python scripts/build_national.py [--as-of YYYY-MM-DD]

Reads   data/games.json, data/registry.json, data/fetch_report.json (NCSL, authoritative)
        imports/csv/*.csv, imports/ysg/*.html (external results; see imports/README.md)
        data/identity_decisions.json (manual identity decisions, optional)
Writes  data/team_aliases.json, data/match_observations.jsonl, data/match_index.json,
        data/canonical_matches.jsonl, data/match_corrections.jsonl, data/review_queue.jsonl,
        data/national_ratings_history.json, site/national/**

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


def backtest(matches_by_age, ncsl_games_by_age, as_of):
    """Rolling-origin weekly backtest. For each origin date, fit only on games played on or before
    it and predict the following 7 days. Compares the national model, the NCSL baseline and base
    rates on the same NCSL fixtures, plus the national model on all eligible fixtures."""
    first = min((m["date"] for ms in matches_by_age.values() for m in ms), default=None)
    if not first:
        return {"note": "no eligible matches yet"}
    start = date.fromisoformat(first) + timedelta(days=14)
    origins = []
    d = start
    while d < as_of:
        origins.append(d); d += timedelta(days=7)
    nat_all, nat_ncsl, base_ncsl, rate_ncsl, nat_ncsl_cov = [], [], [], [], [0, 0]
    for origin in origins:
        for age, ms in matches_by_age.items():
            train = [m for m in ms if date.fromisoformat(m["date"]) <= origin]
            test = [m for m in ms if origin < date.fromisoformat(m["date"]) <= min(as_of, origin + timedelta(days=7))]
            if not train or not test:
                continue
            mdl = nm.fit(train, origin)
            # baseline: existing NCSL model trained on NCSL league games up to the origin
            ng = [g for g in ncsl_games_by_age.get(age, []) if date.fromisoformat(g["date"]) <= origin]
            team_div = {}
            for g in ncsl_games_by_age.get(age, []):
                team_div[g["home"]] = g["div"]; team_div[g["away"]] = g["div"]
            base = ncsl_model.fit_age_group([(g["home"], g["away"], g["hs"], g["as"]) for g in ng], team_div, 1.0) if ng else None
            counts = Counter(nm.outcome(m["a_score"], m["b_score"]) for m in train)
            tot = sum(counts.values())
            rates = tuple(counts.get(k, 0) / tot for k in range(3))
            for m in test:
                k = nm.outcome(m["a_score"], m["b_score"])
                p = None
                if m["team_a"] in mdl["teams"] and m["team_b"] in mdl["teams"]:
                    p = nm.predict(mdl, m["team_a"], m["team_b"], a_home=m.get("venue_type") == "home")
                if p:
                    nat_all.append((p, k))
                if "ncsl" in m["sources"] and base and m.get("ncsl_home") in base[2] and m.get("ncsl_away") in base[2]:
                    pb = ncsl_baseline_probs(base, m["ncsl_home"], m["ncsl_away"])
                    # national predictions are oriented team_a vs team_b; NCSL ones home vs away
                    a_is_home = m["ncsl_a_key"] == m["ncsl_home"]
                    kk = k if a_is_home else 2 - k
                    nat_ncsl_cov[1] += 1
                    base_ncsl.append((pb, kk))
                    rate_ncsl.append((rates if a_is_home else (rates[2], rates[1], rates[0]), kk))
                    if p:
                        nat_ncsl_cov[0] += 1
                        nat_ncsl.append((p if a_is_home else (p[2], p[1], p[0]), kk))
    return {
        "method": "rolling weekly origins; each fit uses only games on or before the origin and predicts the next 7 days",
        "origins": [o.isoformat() for o in origins],
        "national_all_fixtures": nm.evaluate(nat_all),
        "ncsl_fixtures": {
            "national_model": nm.evaluate(nat_ncsl),
            "national_model_coverage": f"{nat_ncsl_cov[0]} of {nat_ncsl_cov[1]} fixtures (others: teams in different networks)",
            "ncsl_baseline": nm.evaluate(base_ncsl),
            "base_rates": nm.evaluate(rate_ncsl),
        },
    }


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
            ids.ensure_source_team(t["source_key"], t["name"], t["age"], state=t.get("state"))
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
    eligible_by_age = defaultdict(list)
    for m in matches:
        r = nm.eligibility(m, as_of)
        m["eligible"] = r is None
        m["exclusion"] = r
        if r:
            excluded[r] += 1
        else:
            eligible_by_age[m["age_a"]].append(m)
    excluded["team identity not confirmed"] += stats["unresolved_observations"]

    # ---- fit per age
    hist_path = DATA / "national_ratings_history.json"
    hist = json.loads(hist_path.read_text()) if hist_path.exists() else []
    prev_snap = next((h for h in reversed(hist) if h["date"] <= (as_of - timedelta(days=6)).isoformat()), None)
    teams = ids.teams
    age_out, team_profiles, snapshot = {}, {}, {}
    upcoming = defaultdict(list)
    for m in matches:
        if m["status"] == "scheduled" and m["date"] >= as_of.isoformat():
            upcoming[m["team_a"]].append(m); upcoming[m["team_b"]].append(m)

    for age in SUPPORTED_AGES:
        ms = eligible_by_age.get(age, [])
        mdl = nm.fit(ms, as_of) if ms else {"mu": 0.3, "home": 0.0, "teams": {}, "networks": [], "n_games": 0}
        rated = mdl["teams"]
        nets = mdl["networks"]
        main_share = (nets[0] / len(rated)) if rated else 0
        # A network made only of one league's games is that league's table, not a national
        # comparison. National ranks need the main network to include results from outside NCSL.
        main_external = any(any(s != "ncsl" for s in m["sources"]) for m in ms
                            if rated.get(m["team_a"], {}).get("network") == 0)
        national_ok = bool(rated) and main_share >= MAIN_NETWORK_MIN_SHARE and main_external
        rows = []
        for cid, r in rated.items():
            t = teams[cid]
            rows.append({"id": cid, "name": t["name"], "club": t.get("club"),
                         "state": t.get("state"), "region": STATE_REGION.get(t.get("state") or ""),
                         "ncsl_key": t.get("ncsl_key"), "ncsl_division": t.get("ncsl_division"),
                         "att": round(r["att"], 4), "def": round(r["def"], 4), "se": round(r["se"], 3),
                         "rating": round(nm.rating(r["att"], r["def"])), "games": r["games"],
                         "network": r["network"], "main_network": r["network"] == 0 and national_ok})
        # records, SOS
        rating_of = {x["id"]: x["rating"] for x in rows}
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
            x["reliability"] = ("high" if x["status"] == "ranked" and x["se"] < 0.45 else
                                "medium" if x["status"] == "ranked" else "low")
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
            "model": {"mu": round(mdl["mu"], 5), "home": round(mdl["home"], 5), "n_games": mdl["n_games"]},
            "national_ranking_available": national_ok,
            "coverage": {"teams_known": len(known), "teams_rated": len(rows),
                         "teams_ranked": len(ranked), "eligible_matches": len(ms),
                         "networks": len(nets), "largest_network": nets[0] if nets else 0,
                         "main_network_share": round(main_share, 3), "main_network_has_external": main_external,
                         "sources": sorted({s for m in ms for s in m["sources"]})},
            "teams": rows,
        }
        # profiles
        by_team = defaultdict(list)
        for m in matches:
            if m["age_a"] == age or m["age_b"] == age:
                by_team[m["team_a"]].append(m); by_team[m["team_b"]].append(m)
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
    validation = backtest(eligible_by_age, ncsl_by_age, as_of)

    # ---- sources and coverage
    perm = json.loads((DATA / "permissions.json").read_text()) if (DATA / "permissions.json").exists() else {}
    src_counts = Counter(o["source"] for o in all_obs)
    latest = lambda s: max((o.get("retrieved_at") or "" for o in all_obs if o["source"] == s), default=None)
    ncsl_cids = {cid for cid, t in teams.items() if t.get("ncsl_key")}
    ncsl_linked = {m["canonical_id"] for k, m in ids.mappings.items()
                   if not k.startswith("ncsl:") and m["status"] == "confirmed" and m["canonical_id"] in ncsl_cids}
    external_matches = [m for m in matches if any(s != "ncsl" for s in m["sources"])]
    index = {
        "built_at": built, "as_of": as_of.isoformat(), "cutoff": NATIONAL_CUTOFF.isoformat(),
        "cutoff_rule": f"matches dated after {NATIONAL_CUTOFF.isoformat()}",
        "min_ranked_games": MIN_RANKED_GAMES,
        "sources": {
            "ncsl": {"label": "NCSL (Demosphere)", "observations": src_counts.get("ncsl", 0), "last_retrieved": ncsl_fetched,
                     "automated": True},
            "ysg": {"label": "YouthSoccerGames", "observations": src_counts.get("ysg", 0), "last_retrieved": latest("ysg"),
                    "automated": bool(perm.get("youthsoccergames", {}).get("granted")),
                    "note": "Automated collection is off: robots.txt disallows it and no written permission is on file. Saved pages in imports/ysg/ are processed."},
            "csv": {"label": "Tournament and other exports (CSV)", "observations": src_counts.get("csv", 0) + src_counts.get("event", 0),
                    "last_retrieved": max(filter(None, [latest("csv"), latest("event")]), default=None), "automated": False},
        },
        "totals": {"canonical_teams": len(teams), "ncsl_teams": len(ncsl_cids),
                   "ncsl_teams_linked_to_other_sources": len(ncsl_linked),
                   "non_ncsl_teams": len(teams) - len(ncsl_cids),
                   "observations": stats["observations"], "canonical_matches": len(matches),
                   "eligible_matches": sum(len(v) for v in eligible_by_age.values()),
                   "duplicates_merged": stats["duplicates_merged"],
                   "score_conflicts": stats["conflicts"],
                   "unresolved_conflicts": sum(1 for r in review if r["type"] == "score_conflict" and not r["resolved_by_authority"]),
                   "review_queue": len(review), "identities_linked_this_run": newly_linked,
                   "external_matches": len(external_matches)},
        "exclusions": dict(excluded),
        "import_problems": (prob_csv + prob_ysg)[:200],
        "ages": [{"age": a, **age_out[a]["coverage"], "national_ranking_available": age_out[a]["national_ranking_available"]}
                 for a in SUPPORTED_AGES],
        "latest_external": [{"date": m["date"], "a": teams[m["team_a"]]["name"], "b": teams[m["team_b"]]["name"],
                             "score": [m["a_score"], m["b_score"]], "competition": m.get("competition"),
                             "age": m["age_a"], "sources": m["sources"]}
                            for m in sorted(external_matches, key=lambda m: m["date"], reverse=True)[:12]],
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

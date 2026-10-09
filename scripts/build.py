"""Parse raw pages -> games, standings, change log, ratings -> site/data.json."""
import json, math, re, statistics, sys, time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parse import parse_division
from model import fit_age_group

ROOT = Path(__file__).resolve().parent.parent
DATA, SITE = ROOT / "data", ROOT / "site"
RIDGE = 1.0
SCORED_MIN_AGE = 12          # NCSL does not publish scores below U12
DEFAULT_TIER_GAP = 1.14      # log-strength units between adjacent divisions (see README)
ET = timezone(timedelta(hours=-4))

def age_num(age): return int(age[2:])

def load_games(reg):
    divs = {}
    for d in reg["divisions"]:
        p = parse_division(d["id"])
        p.update(age=d["age"], flight=d["flight"], name=d["name"])
        p["tier"] = int(d["flight"]) if d["flight"].isdigit() else None
        divs[d["id"]] = p
    return divs

def standings(div):
    rows = {}
    def row(t):
        return rows.setdefault(t["key"], {"key": t["key"], "gp": 0, "w": 0, "t": 0, "l": 0,
                                          "gf": 0, "ga": 0, "pts": 0, "ff": 0, "form": []})
    for g in div["games"]:
        row(g["home"]); row(g["away"])
        if g["status"] not in ("final", "forfeit") or g["home_score"] is None:
            continue
        for me, op, gf, ga in ((g["home"], g["away"], g["home_score"], g["away_score"]),
                               (g["away"], g["home"], g["away_score"], g["home_score"])):
            r = row(me)
            r["gp"] += 1; r["gf"] += gf; r["ga"] += ga
            res = "W" if gf > ga else "T" if gf == ga else "L"
            r[{"W": "w", "T": "t", "L": "l"}[res]] += 1
            r["pts"] += {"W": 3, "T": 1, "L": 0}[res]
            r["ff"] += g["status"] == "forfeit"
            r["form"].append(res)
    out = []
    for r in rows.values():
        r["gd"] = r["gf"] - r["ga"]
        r["ppg"] = round(r["pts"] / r["gp"], 2) if r["gp"] else 0.0
        r["form"] = "".join(r["form"][-5:])
        out.append(r)
    out.sort(key=lambda r: (-r["ppg"], -r["pts"], -r["gd"], -r["gf"], r["key"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out

def detect_changes(prev, cur, now):
    ch = []
    fields = [("date", "date"), ("time", "time"), ("venue", "venue")]
    for k, g in cur.items():
        p = prev.get(k)
        base = {"at": now, "gamekey": k, "division_id": g["division_id"], "game_no": g["game_no"],
                "home": g["home"]["name"], "away": g["away"]["name"], "date": g["date"]}
        if p is None:
            if prev:
                ch.append({**base, "type": "new_game"})
            continue
        if p["score_raw"] != g["score_raw"]:
            t = "result" if p["home_score"] is None and g["home_score"] is not None else \
                "forfeit" if g["status"] == "forfeit" else "score_change"
            ch.append({**base, "type": t, "from": p["score_raw"], "to": g["score_raw"]})
        for f, label in fields:
            if p.get(f) != g.get(f):
                ch.append({**base, "type": f"{label}_change", "from": p.get(f), "to": g.get(f)})
    for k, p in prev.items():
        if k not in cur:
            ch.append({"at": now, "gamekey": k, "division_id": p["division_id"], "game_no": p["game_no"],
                       "home": p["home"]["name"], "away": p["away"]["name"], "date": p["date"],
                       "type": "removed"})
    return ch

def main():
    reg = json.loads((DATA / "registry.json").read_text())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    divs = load_games(reg)

    # ---- games snapshot + change log
    cur = {g["gamekey"]: g for d in divs.values() for g in d["games"]}
    gpath = DATA / "games.json"
    prev = json.loads(gpath.read_text()) if gpath.exists() else {}
    changes = detect_changes(prev, cur, now)
    if cur:  # never overwrite history with an empty fetch
        gpath.write_text(json.dumps(cur, separators=(",", ":"), sort_keys=True))
    cpath = DATA / "changes.jsonl"
    with cpath.open("a") as f:
        for c in changes:
            f.write(json.dumps(c) + "\n")
    all_changes = [json.loads(l) for l in cpath.read_text().splitlines() if l.strip()] if cpath.exists() else []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=21)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent_changes = [c for c in all_changes if c["at"] >= cutoff][-600:]

    # ---- teams
    teams = {}
    for d in divs.values():
        for g in d["games"]:
            for t in (g["home"], g["away"]):
                if t["key"] and t["key"] not in teams:
                    teams[t["key"]] = {"key": t["key"], "name": t["name"], "code": t["code"], "club": t["club"],
                                       "div": d["id"], "age": d["age"]}

    # ---- ratings per scored age group
    ages = defaultdict(list)
    for d in divs.values():
        ages[d["age"]].append(d)
    age_out = []
    tier_gap = DEFAULT_TIER_GAP
    for age, dl in sorted(ages.items(), key=lambda kv: age_num(kv[0])):
        dl.sort(key=lambda d: (d["tier"] or 0, d["flight"]))
        entry = {"age": age, "divisions": [d["id"] for d in dl], "scored": age_num(age) >= SCORED_MIN_AGE}
        if entry["scored"]:
            team_div = {k: t["div"] for k, t in teams.items() if t["age"] == age}
            games = [(g["home"]["key"], g["away"]["key"], g["home_score"], g["away_score"])
                     for d in dl for g in d["games"] if g["status"] == "final"]
            mu, home, A, D = fit_age_group(games, team_div, RIDGE)
            n_scored = defaultdict(int)
            for h, a, _, _ in games:
                n_scored[h] += 1; n_scored[a] += 1
            for k in team_div:
                tier = divs[team_div[k]]["tier"] or 1
                teams[k]["n_scored"] = n_scored[k]
                teams[k]["att0"], teams[k]["def0"] = A[k], D[k]   # within-division rating
                teams[k]["tier"] = tier
            entry.update(mu=mu, home=home, n_games=len(games))
        age_out.append(entry)

    def apply_ratings():
        for e in age_out:
            if not e["scored"]:
                continue
            ks = [k for k, t in teams.items() if t["age"] == e["age"]]
            for k in ks:
                o = -tier_gap * (teams[k]["tier"] - 1)
                teams[k]["att"] = teams[k]["att0"] + o / 2
                teams[k]["def"] = teams[k]["def0"] + o / 2
            abar = statistics.mean(teams[k]["att"] for k in ks)
            dbar = statistics.mean(teams[k]["def"] for k in ks)
            rated = [k for k in ks if teams[k]["n_scored"] > 0]
            sbar = statistics.mean(teams[k]["att"] + teams[k]["def"] for k in rated) if rated else 0.0
            for k in ks:
                t = teams[k]
                # Rating: linear in log-strength, 1500 = average rated team in the age group.
                # +250 means about 2.7x the goals-for/goals-against ratio against the same opponent.
                t["power"] = round(1500 + 250 * (t["att"] + t["def"] - sbar))
            for k in ks:
                if teams[k]["n_scored"] == 0:   # forfeits only, or no games yet: no rating
                    for f in ("att0", "def0", "att", "def", "power"):
                        teams[k].pop(f, None)
            for i, k in enumerate(sorted(rated, key=lambda k: -teams[k]["power"]), 1):
                teams[k]["age_rank"] = i
            e["n_teams"] = len(rated)
    apply_ratings()

    # ---- rating history (one snapshot per day)
    hpath = DATA / "ratings_history.json"
    hist = json.loads(hpath.read_text()) if hpath.exists() else []
    today = datetime.now(ET).date().isoformat()
    snap = {"date": today, "power": {k: t["power"] for k, t in teams.items() if "power" in t},
            "rank": {k: t["age_rank"] for k, t in teams.items() if "age_rank" in t}}
    hist = [h for h in hist if h["date"] != today] + [snap]
    hist = hist[-120:]
    hpath.write_text(json.dumps(hist, separators=(",", ":")))

    # ---- site payload
    def g_out(g):
        return [g["gamekey"], g["game_no"], g["date"], g["time"], g["home"]["key"], g["away"]["key"],
                g["home_score"], g["away_score"], g["status"], g["venue"],
                2 if g["rescheduled_recent"] else 1 if g["rescheduled"] else 0]
    div_out = []
    for d in divs.values():
        div_out.append({"id": d["id"], "name": d["name"], "age": d["age"], "flight": d["flight"], "tier": d["tier"],
                        "source_as_of": d["source_as_of"], "scored": age_num(d["age"]) >= SCORED_MIN_AGE,
                        "standings": standings(d), "games": [g_out(g) for g in d["games"]]})
    team_out = {k: {kk: (round(v, 4) if isinstance(v, float) else v) for kk, v in t.items()} for k, t in teams.items()}
    prev_rank = {}
    if len(hist) > 1:
        old = next((h for h in reversed(hist[:-1]) if h["date"] <= (datetime.now(ET).date() - timedelta(days=6)).isoformat()), hist[0])
        prev_rank = old["rank"]
    for k, t in team_out.items():
        if k in prev_rank and "age_rank" in t:
            t["rank_move"] = prev_rank[k] - t["age_rank"]
    fr = DATA / "fetch_report.json"
    payload = {
        "built_at": now, "tier_gap": tier_gap, "ridge": RIDGE,
        "fetch": json.loads(fr.read_text()) if fr.exists() else None,
        "ages": age_out, "divisions": sorted(div_out, key=lambda d: (age_num(d["age"]), d["tier"] or 0, d["flight"])),
        "teams": team_out, "changes": recent_changes,
        "history": {"dates": [h["date"] for h in hist][-30:],
                    "power": {k: [h["power"].get(k) for h in hist[-30:]] for k in teams}},
    }
    SITE.mkdir(exist_ok=True)
    (SITE / "data.json").write_text(json.dumps(payload, separators=(",", ":")))
    print(f"built: {len(div_out)} divisions, {len(teams)} teams, {len(cur)} games, {len(changes)} changes")

if __name__ == "__main__":
    main()

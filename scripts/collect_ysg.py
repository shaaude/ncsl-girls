"""YouthSoccerGames collector, run only under the written permission in data/permissions.json.

Permission (support@youthsoccergames.com, 2026-10-10) covers:
  1. State/age/gender directory pages, e.g. /teams/virginia/girls/u12  -> mode "directories"
  2. Team profile pages, once weekly, 1:00-5:00 AM Eastern only          -> mode "profiles"
  3. At least 10 seconds between requests; game scores only from 2026-08-01 on.

  python scripts/collect_ysg.py directories [--states virginia,maryland] [--max-pages N]
  python scripts/collect_ysg.py profiles    [--max-pages N]
  python scripts/collect_ysg.py status

Nothing is fetched unless data/permissions.json grants the mode. Raw pages are not stored:
directory pages become imports/ysg/directory/{state}_{age}.json (team id, name, club; their
rankings are not kept), team pages become imports/ysg/extracted/{team}.json (games dated
2026-08-01 or later). data/ysg_collection_report.json records every run.
"""
import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import EASTERN, SUPPORTED_AGES, normalize_age
from identities import similarity
from sources import STATE_SLUGS, parse_ysg_directory, parse_ysg_team_page

ROOT = Path(__file__).resolve().parent.parent
PERM = ROOT / "data" / "permissions.json"
YSG = ROOT / "imports" / "ysg"
LOG = ROOT / "data" / "ysg_fetch_log.json"
CACHE = ROOT / ".cache" / "ysg"          # raw pages: never committed; persisted via the Actions cache
REPORT = ROOT / "data" / "ysg_collection_report.json"
BASE = "https://youthsoccergames.com"
UA = "ncsl-girls-tables/1.1 (non-commercial; permission granted by YouthSoccerGames 2026-10-10; +https://github.com/shaaude/ncsl-girls)"
SCORE_FROM = "2026-08-01"            # YSG permission: scores on or after Aug 1, 2026
PRIORITY_STATES = ["virginia", "maryland", "district-of-columbia"]
MIDATLANTIC = {"VA", "MD", "DC", "PA", "NJ", "DE", "WV", "NC", "NY"}


def now_utc():
    return datetime.now(timezone.utc)


def permission():
    if not PERM.exists():
        return None
    p = json.loads(PERM.read_text()).get("youthsoccergames", {})
    return p if p.get("granted") and p.get("evidence") else None


class Fetcher:
    def __init__(self, perm, deadline=None, max_pages=None):
        self.delay = max(10, int(perm.get("min_seconds_between_requests", 10)))
        self.deadline, self.max_pages = deadline, max_pages
        self.count, self.last = 0, 0.0
        self.robots_override = bool(perm.get("robots_txt_override_by_permission"))
        self.rp = None
        if not self.robots_override:
            self.rp = urllib.robotparser.RobotFileParser(BASE + "/robots.txt")
            self.rp.read()

    def can_continue(self):
        if self.max_pages is not None and self.count >= self.max_pages:
            return False
        if self.deadline and datetime.now(EASTERN) >= self.deadline:
            return False
        return True

    def get(self, path):
        url = BASE + path
        if self.rp and not self.rp.can_fetch(UA, url):
            raise PermissionError(f"robots.txt disallows {url} and no override is recorded")
        wait = self.delay - (time.time() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.time()
        self.count += 1
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, ""
        except Exception as e:
            return -1, str(e)


def load_log():
    return json.loads(LOG.read_text()) if LOG.exists() else {"profiles": {}, "directories": {}}


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True))
    tmp.replace(path)


def report(entry):
    rep = json.loads(REPORT.read_text()) if REPORT.exists() else {"runs": []}
    rep["runs"] = (rep["runs"] + [entry])[-60:]
    save_json(REPORT, rep)


def structure_digest(html, limit=120):
    """Tag/attribute outline of a page with all visible text removed - lets the parser be checked
    against the real layout without republishing any of the page's content."""
    out = []
    for m in re.finditer(r"<(/?)([a-zA-Z0-9]+)([^>]*)>", html):
        tag, attrs = m.group(2).lower(), m.group(3)
        if tag in ("script", "style", "meta", "link", "svg", "path", "head", "noscript"):
            continue
        keep = re.findall(r'\b(class|id|data-[a-z-]+)="([^"]{0,40})"', attrs)
        href = re.search(r'href="(/[a-z]+/|https?://[^/"]+/)', attrs)
        bits = " ".join(f'{k}="{v}"' for k, v in keep if not (k.startswith("data-") and k not in ("data-teamid", "data-yeargen")))
        if href:
            bits += f' href="{href.group(1)}…"'
        out.append(f"<{m.group(1)}{tag}{(' ' + bits) if bits else ''}>")
        if len(out) >= 4000:
            break
    # keep the region around the first game-like table
    i = next((k for k, line in enumerate(out) if 'href="/team/' in line), 0)
    return out[max(0, i - 40): i + limit]


# ------------------------------------------------------------------ directories (approved, any time)
def discover_slugs(f):
    """Read the state and age links from one directory page instead of guessing URL formats."""
    st, html = f.get("/teams/virginia/girls/u12")
    if st != 200:
        return list(STATE_SLUGS), [f"u{n}" for n in range(9, 20)], st, html
    states = sorted(set(re.findall(r'/teams/([a-z-]+)/girls/u\d+', html)) | {"virginia"})
    ages = sorted(set(re.findall(r'/teams/[a-z-]+/girls/(u\d{1,2})\b', html)), key=lambda a: int(a[1:]))
    return states, ages, st, html


def run_directories(perm, states_arg=None, max_pages=None):
    if "directories" not in perm.get("modes", []):
        print("directory crawling is not in the recorded permission; nothing fetched"); return
    f = Fetcher(perm, max_pages=max_pages)
    log = load_log()
    states, ages, st0, html0 = discover_slugs(f)
    ages = [a for a in ages if normalize_age(a) in SUPPORTED_AGES]
    order = [s for s in PRIORITY_STATES if s in states] + [s for s in states if s not in PRIORITY_STATES]
    if states_arg:
        order = [s for s in states_arg.split(",") if s]
    done, empty, failed = 0, 0, []
    started = now_utc().isoformat()
    for state in order:
        for age_slug in ages:
            if not f.can_continue():
                break
            key = f"{state}_{age_slug}"
            if state == "virginia" and age_slug == "u12" and st0 == 200:
                status, html = st0, html0
            else:
                status, html = f.get(f"/teams/{state}/girls/{age_slug}")
            if status != 200:
                failed.append({"page": key, "status": status}); continue
            (CACHE / "dir").mkdir(parents=True, exist_ok=True)
            (CACHE / "dir" / f"{key}.html").write_text(html)
            parsed = parse_ysg_directory(html)
            if not parsed["teams"]:
                empty += 1
            save_json(YSG / "directory" / f"{key}.json", {
                "state": STATE_SLUGS.get(state), "state_slug": state, "age": normalize_age(age_slug),
                "age_slug": age_slug, "fetched_at": now_utc().isoformat(), "teams": parsed["teams"]})
            log["directories"][key] = now_utc().isoformat()
            done += 1
    save_json(LOG, log)
    if html0 and not (YSG / "samples" / "directory_structure.txt").exists():
        save_digest("directory_structure.txt", html0)
    report({"mode": "directories", "started": started, "finished": now_utc().isoformat(), "pages": f.count,
            "saved": done, "empty_pages": empty, "failed": failed[:50], "states_found": len(states), "ages": ages})
    print(f"directories: {done} saved, {empty} empty, {len(failed)} failed, {f.count} requests")


def save_digest(name, html):
    p = YSG / "samples" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("STRUCTURE ONLY - all page text removed. Used to check the parser.\n" + "\n".join(structure_digest(html)))


# ------------------------------------------------------------------ team profiles (weekly, 1-5 AM ET)
def in_window(t=None):
    t = t or datetime.now(EASTERN)
    return 1 <= t.hour < 5


def profile_queue(log, today_utc):
    """Order: (1) YSG teams already linked to NCSL teams, (2) directory teams that resemble an NCSL
    team (same age, VA/MD/DC), (3) opponents found on fetched pages in Mid-Atlantic states,
    (4) other directory teams in priority states. Skips anything fetched in the last 7 days."""
    aliases = json.loads((ROOT / "data" / "team_aliases.json").read_text()) if (ROOT / "data" / "team_aliases.json").exists() else {"teams": {}, "mappings": {}}
    teams = aliases["teams"]
    recent = {tid for tid, ts in log["profiles"].items() if datetime.fromisoformat(ts) > today_utc - timedelta(days=7)}
    q, seen = [], set()

    def add(tid, why):
        if tid and tid not in seen and tid not in recent:
            seen.add(tid); q.append((tid, why))
    for k, m in aliases["mappings"].items():
        if k.startswith("ysg:") and m.get("canonical_id") in teams and teams[m["canonical_id"]].get("ncsl_key"):
            add(k[4:], "linked to NCSL team")
    directory = {}
    for p in sorted((YSG / "directory").glob("*.json")):
        d = json.loads(p.read_text())
        for t in d["teams"]:
            directory[t["id"]] = {**t, "state": d["state"], "age": d["age"]}
    ncsl = [t for t in teams.values() if t.get("ncsl_key")]
    by_age = {}
    for t in ncsl:
        by_age.setdefault(t["age"], []).append(t["name"])
    cands = []
    for tid, t in directory.items():
        if t["state"] in ("VA", "MD", "DC") and t["age"] in by_age:
            best = max((similarity(t["name"], n) for n in by_age[t["age"]]), default=0)
            if best >= 0.4:
                cands.append((-best, tid))
    for _, tid in sorted(cands):
        add(tid, "resembles an NCSL team")
    for p in sorted((YSG / "extracted").glob("*.json")):
        d = json.loads(p.read_text())
        for g in d["games"]:
            for oid in (g["a_id"], g["b_id"]):
                if directory.get(oid, {}).get("state") in MIDATLANTIC:
                    add(oid, "opponent of a collected team")
    for tid, t in directory.items():
        if t["state"] in ("VA", "MD", "DC"):
            add(tid, "VA/MD/DC directory")
    return q


def run_profiles(perm, max_pages=None, force_window=False):
    if "profiles" not in perm.get("modes", []):
        print("team profile crawling is not in the recorded permission; nothing fetched"); return
    now_et = datetime.now(EASTERN)
    if not in_window(now_et) and not force_window:
        msg = f"outside the permitted 1:00-5:00 AM Eastern window (now {now_et:%H:%M} ET); nothing fetched"
        print(msg)
        report({"mode": "profiles", "started": now_utc().isoformat(), "skipped": msg})
        return
    log = load_log()
    last = max(log["profiles"].values(), default=None)
    if last and datetime.fromisoformat(last) > now_utc() - timedelta(days=6) and not force_window:
        msg = f"team profiles were already collected this week ({last}); once weekly only"
        print(msg); report({"mode": "profiles", "started": now_utc().isoformat(), "skipped": msg}); return
    deadline = now_et.replace(hour=4, minute=50, second=0, microsecond=0)
    f = Fetcher(perm, deadline=deadline, max_pages=max_pages or int(perm.get("max_profile_pages_per_week", 1200)))
    queue = profile_queue(log, now_utc())
    started = now_utc().isoformat()
    saved = games_total = no_games = 0
    reasons, digests = {}, len(list((YSG / "samples").glob("team_structure_*.txt"))) if (YSG / "samples").exists() else 0
    directory = {}
    for p in sorted((YSG / "directory").glob("*.json")):
        d = json.loads(p.read_text())
        for t in d["teams"]:
            directory[t["id"]] = {"state": d["state"], "age": d["age"], "club": t.get("club"), "name": t["name"]}
    for tid, why in queue:
        if not f.can_continue():
            break
        status, html = f.get(f"/team/{tid}")
        if status != 200:
            continue
        (CACHE / "team").mkdir(parents=True, exist_ok=True)
        (CACHE / "team" / f"{tid}.html").write_text(html)
        info, games, warn = parse_ysg_team_page(html)
        meta = directory.get(tid, {})
        info["id"] = tid
        info["age"] = info.get("age") or meta.get("age")
        info["state"] = info.get("state") or meta.get("state")
        info["club"] = meta.get("club")
        info["name"] = info.get("name") or meta.get("name")
        kept = [g for g in games if g["date"] >= SCORE_FROM]
        save_json(YSG / "extracted" / f"{tid}.json", {"team": info, "fetched_at": now_utc().isoformat(),
                                                      "games": kept, "warnings": warn, "games_on_page": len(games)})
        log["profiles"][tid] = now_utc().isoformat()
        saved += 1; games_total += len(kept); no_games += not games
        reasons[why] = reasons.get(why, 0) + 1
        if digests < 3:
            save_digest(f"team_structure_{digests + 1}.txt", html); digests += 1
        if saved % 25 == 0:
            save_json(LOG, log)
    save_json(LOG, log)
    report({"mode": "profiles", "started": started, "finished": now_utc().isoformat(), "requests": f.count,
            "pages_saved": saved, "pages_with_no_games_parsed": no_games, "games_kept": games_total,
            "queue_length": len(queue), "by_reason": reasons})
    print(f"profiles: {saved} pages, {games_total} games kept (from {SCORE_FROM}), {no_games} pages with no games parsed")


def reparse():
    """Re-extract directory and team files from cached raw pages (no network)."""
    log = load_log()
    nd = nt = games_total = 0
    for p in sorted((CACHE / "dir").glob("*.html")):
        state, age_slug = p.stem.rsplit("_", 1)
        parsed = parse_ysg_directory(p.read_text())
        prev = YSG / "directory" / f"{p.stem}.json"
        fetched = json.loads(prev.read_text())["fetched_at"] if prev.exists() else log["directories"].get(p.stem)
        save_json(prev, {"state": STATE_SLUGS.get(state), "state_slug": state, "age": normalize_age(age_slug),
                         "age_slug": age_slug, "fetched_at": fetched, "teams": parsed["teams"]})
        nd += 1
    directory = {}
    for p in sorted((YSG / "directory").glob("*.json")):
        d = json.loads(p.read_text())
        for t in d["teams"]:
            directory[t["id"]] = {"state": d["state"], "age": d["age"], "club": t.get("club"), "name": t["name"]}
    for p in sorted((CACHE / "team").glob("*.html")):
        tid = p.stem
        html = p.read_text()
        info, games, warn = parse_ysg_team_page(html)
        meta = directory.get(tid, {})
        info.update(id=tid, age=info.get("age") or meta.get("age"), state=info.get("state") or meta.get("state"),
                    club=meta.get("club"), name=info.get("name") or meta.get("name"))
        kept = [g for g in games if g["date"] >= SCORE_FROM]
        save_json(YSG / "extracted" / f"{tid}.json", {"team": info, "fetched_at": log["profiles"].get(tid),
                                                      "games": kept, "warnings": warn, "games_on_page": len(games)})
        nt += 1; games_total += len(kept)
    report({"mode": "reparse", "started": now_utc().isoformat(), "directories": nd, "team_pages": nt, "games_kept": games_total})
    print(f"reparse: {nd} directory pages, {nt} team pages, {games_total} games kept")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["directories", "profiles", "reparse", "bootstrap", "status"])
    ap.add_argument("--states")
    ap.add_argument("--max-pages", type=int)
    a = ap.parse_args()
    perm = permission()
    if not perm:
        print("YouthSoccerGames collection disabled: no written permission recorded in data/permissions.json.")
        return 0
    if a.mode == "status":
        print(json.dumps({"permission": perm.get("evidence"), "modes": perm.get("modes"), "log": {k: len(v) for k, v in load_log().items()}}, indent=1))
    elif a.mode == "reparse":
        reparse()
    elif a.mode == "bootstrap":
        # first run after permission: directories for VA/MD/DC only, so the parser can be checked
        if not list((YSG / "directory").glob("*.json")):
            run_directories(perm, ",".join(PRIORITY_STATES), a.max_pages)
        else:
            reparse()
    elif a.mode == "directories":
        run_directories(perm, a.states, a.max_pages)
    else:
        run_profiles(perm, a.max_pages)
    return 0


if __name__ == "__main__":
    sys.exit(main())

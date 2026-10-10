"""Source adapters. Each adapter turns one source's data into match OBSERVATIONS.

An observation is one source's report of one game. The same real game may be observed several
times (NCSL schedule, each team's YouthSoccerGames page, a tournament export); reconcile.py
merges them into one canonical match.

Observation fields
  obs_id            stable hash of (source, source_record_key)
  source            ncsl | ysg | csv | event
  observer          which page/file reported it (rows from one observer are distinct games)
  source_record_key unique within the source
  upstream_id       verified match ID from the competition's own system, if any (e.g. "ncsl:11635923")
  date, time        ISO date; time string or None
  competition       name; competition_type: league | tournament | showcase | scrimmage | unknown
  venue_type        home | neutral | unknown  (home means `a` was the home team)
  a, b              {source_key, name, age, state, club}
  a_score, b_score  ints or None
  status            final | scheduled | forfeit | cancelled | unknown
  seq               order of this game among same-day games vs the same opponent on this observer
  url, retrieved_at
"""
import csv
import hashlib
import html as htmllib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from config import normalize_age

ROOT = Path(__file__).resolve().parent.parent
IMPORTS = ROOT / "imports"


def obs_id(source, key):
    return "O" + hashlib.sha1(f"{source}|{key}".encode()).hexdigest()[:14]


def file_date(path):
    """Commit time of an import file (when it was added to the repo); falls back to mtime."""
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cI", "--", str(path)], cwd=ROOT,
                             capture_output=True, text=True, timeout=20).stdout.strip()
        if out:
            return out
    except Exception:
        pass
    return datetime.fromtimestamp(Path(path).stat().st_mtime).isoformat()


def assign_seq(obs):
    """Number same-day games against the same opponent within each observer: 0, 1, ..."""
    groups = {}
    for o in sorted(obs, key=lambda o: (o["observer"], o["date"] or "", o.get("time") or "", o["source_record_key"])):
        k = (o["observer"], o["date"], frozenset((o["a"]["source_key"], o["b"]["source_key"])))
        o["seq"] = groups.get(k, 0)
        groups[k] = o["seq"] + 1
    return obs


# --------------------------------------------------------------------------- NCSL / Demosphere
def ncsl_observations(games, registry_by_div, retrieved_at):
    """games: data/games.json dict (gamekey -> parsed game). Always authoritative for NCSL games."""
    out = []
    for gk, g in games.items():
        div = registry_by_div.get(g["division_id"])
        if not div:
            continue
        age = div["age"]
        st = {"final": "final", "forfeit": "forfeit", "scheduled": "scheduled"}.get(g["status"], "unknown")
        if g["status"] in ("no_score", "other"):
            st = "unknown"
        out.append({
            "obs_id": obs_id("ncsl", gk), "source": "ncsl", "observer": f"ncsl:{g['division_id']}",
            "source_record_key": gk, "upstream_id": f"ncsl:{gk}",
            "date": g["date"], "time": g["time"],
            "competition": f"NCSL Fall 2026 {div['name']}", "competition_type": "league",
            "venue_type": "home",
            "a": {"source_key": f"ncsl:{g['home']['key']}", "name": g["home"]["name"], "age": age},
            "b": {"source_key": f"ncsl:{g['away']['key']}", "name": g["away"]["name"], "age": age},
            "a_score": g["home_score"], "b_score": g["away_score"], "status": st,
            "url": f"https://elements.demosphere.com/80738/schedules/Fall2026/{g['division_id']}.html",
            "retrieved_at": retrieved_at,
        })
    return assign_seq(out)


# --------------------------------------------------------------------------- generic CSV imports
CSV_COLUMNS = ["date", "time", "competition", "competition_type", "venue_type",
               "team_a", "team_a_id", "team_a_state", "team_b", "team_b_id", "team_b_state",
               "score_a", "score_b", "status", "age_group", "gender", "source", "source_match_id", "url"]


def csv_observations(paths=None):
    """imports/csv/*.csv - results from tournament exports, licensed feeds or hand entry.
    Required columns: date, team_a, team_b, score_a, score_b, age_group. See imports/README.md."""
    out, problems = [], []
    paths = paths if paths is not None else sorted((IMPORTS / "csv").glob("*.csv"))
    for p in paths:
        retrieved = file_date(p)
        with open(p, newline="", encoding="utf-8-sig") as f:
            for i, row in enumerate(csv.DictReader(f), start=2):
                row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                gender = (row.get("gender") or "girls").lower()
                if gender not in ("g", "girls", "female", "f"):
                    problems.append({"file": p.name, "line": i, "reason": "not a girls game"})
                    continue
                age = normalize_age(row.get("age_group"))
                if not age:
                    problems.append({"file": p.name, "line": i, "reason": f"unsupported age {row.get('age_group')!r}"})
                    continue
                try:
                    d = datetime.strptime(row["date"], "%Y-%m-%d").date().isoformat()
                except Exception:
                    problems.append({"file": p.name, "line": i, "reason": f"bad date {row.get('date')!r} (use YYYY-MM-DD)"})
                    continue
                if not row.get("team_a") or not row.get("team_b"):
                    problems.append({"file": p.name, "line": i, "reason": "missing team name"})
                    continue
                src = (row.get("source") or "csv").lower()
                src = src if src in ("event", "csv") else "csv"
                def team(side):
                    tid = row.get(f"team_{side}_id")
                    name = row[f"team_{side}"]
                    sk = f"{src}:{tid}" if tid else f"{src}:name:{age}:{' '.join(sorted(name.lower().split()))}"
                    return {"source_key": sk, "name": name, "age": age, "state": (row.get(f"team_{side}_state") or None)}
                def num(x):
                    return int(x) if re.fullmatch(r"\d{1,2}", x or "") else None
                a_s, b_s = num(row.get("score_a")), num(row.get("score_b"))
                status = (row.get("status") or ("final" if a_s is not None and b_s is not None else "scheduled")).lower()
                rec_key = row.get("source_match_id") or f"{p.name}:{i}"
                out.append({
                    "obs_id": obs_id(src, rec_key), "source": src, "observer": f"{src}:{p.name}",
                    "source_record_key": rec_key,
                    "upstream_id": f"{src}:{row['source_match_id']}" if row.get("source_match_id") and src == "event" else None,
                    "date": d, "time": row.get("time") or None,
                    "competition": row.get("competition") or None,
                    "competition_type": (row.get("competition_type") or "unknown").lower(),
                    "venue_type": (row.get("venue_type") or "unknown").lower(),
                    "a": team("a"), "b": team("b"), "a_score": a_s, "b_score": b_s, "status": status,
                    "url": row.get("url") or None, "retrieved_at": retrieved,
                })
    return assign_seq(out), problems


# --------------------------------------------------------------------------- YouthSoccerGames snapshots
DATE_PATTERNS = [("%b %d, %Y", r"[A-Z][a-z]{2} \d{1,2}, 20\d\d"), ("%B %d, %Y", r"[A-Z][a-z]+ \d{1,2}, 20\d\d"),
                 ("%m/%d/%Y", r"\d{1,2}/\d{1,2}/20\d\d"), ("%Y-%m-%d", r"20\d\d-\d\d-\d\d"),
                 ("%m/%d/%y", r"\b\d{1,2}/\d{1,2}/\d\d\b"), ("%d %b %Y", r"\b\d{1,2} [A-Z][a-z]{2} 20\d\d")]


def _text(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _find_date(s):
    for fmt, pat in DATE_PATTERNS:
        m = re.search(pat, s)
        if m:
            try:
                return datetime.strptime(m.group(0), fmt).date().isoformat()
            except ValueError:
                pass
    return None


def _games_in_pane(pane_html, own_id, own_name):
    """Game blocks inside a #gamehistory / #upcominggames pane (real layout checked 2026-10-10):
    each outer <tr class="border-bottom ..."> holds an inner table whose two team rows have a
    td.col-10 (logo + name; the opponent links to /team/{id}, the page's own team has no link)
    and a td.col-1 with the score; other rows carry the date and the competition."""
    games, warn = [], []
    blocks = re.split(r'<tr\b[^>]*class="[^"]*border-bottom[^"]*"[^>]*>', pane_html)[1:]
    for blk in blocks:
        rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", blk, re.S)
        teams, other_text, event = [], [], None
        for r in rows:
            cells = re.findall(r"<td\b([^>]*)>(.*?)</td>", r, re.S)
            col10 = next(((a, c) for a, c in cells if "col-10" in a), None)
            if col10:
                link = re.search(r'/team/(\d+)', col10[1])
                score_cell = next((c for a, c in cells if "col-1" in a and "col-10" not in a), "")
                sc = re.search(r"\b(\d{1,2})\b", _text(score_cell))
                teams.append({"id": link.group(1) if link else own_id,
                              "name": _text(col10[1]) or (None if link else own_name),
                              "score": int(sc.group(1)) if sc else None})
            else:
                other_text.append(_text(r))
                ev = re.search(r'href="([^"]*events/(\d+)[^"]*)"', r)
                if ev:
                    event = ev
        if len(teams) != 2:
            if teams or other_text:
                warn.append(f"game block with {len(teams)} team rows skipped")
            continue
        joined = " | ".join(t for t in other_text if t)
        d = _find_date(joined)
        if not d:
            warn.append("game without a readable date skipped")
            continue
        comp = joined
        for _, pat in DATE_PATTERNS:
            comp = re.sub(pat, "", comp)
        comp = re.sub(r"\bWin Prob\b[^|]*", "", comp)
        comp = re.sub(r"(\s*\|\s*-?\s*)+", " | ", comp).strip(" |-·,") or None
        played = teams[0]["score"] is not None and teams[1]["score"] is not None
        games.append({"a_id": teams[0]["id"], "a_name": teams[0]["name"], "b_id": teams[1]["id"], "b_name": teams[1]["name"],
                      "a_score": teams[0]["score"] if played else None, "b_score": teams[1]["score"] if played else None,
                      "date": d, "competition": comp, "event_url": event.group(1) if event else None,
                      "event_id": event.group(2) if event else None, "status": "final" if played else "scheduled"})
    return games, warn


def _pane(html, pane_id):
    m = re.search(r'<div[^>]*\bid="' + pane_id + r'"[^>]*>', html)
    if not m:
        return ""
    rest = html[m.end():]
    nxt = re.search(r'<div[^>]*class="tab-pane[^"]*"', rest)
    return rest[: nxt.start()] if nxt else rest


def parse_ysg_team_page(html, team_id=None):
    """Parse a YouthSoccerGames team page. Returns (team_info, games, warnings).
    Uses the #gamehistory and #upcominggames tabs; falls back to the older generic table scan
    only if those tabs are missing. Anything unreadable is reported, never guessed."""
    warn = []
    m = re.search(r'<link[^>]+rel="canonical"[^>]+href="[^"]*/team/(\d+)', html) or \
        re.search(r'href="https?://(?:www\.)?youthsoccergames\.com/team/(\d+)"', html)
    tid = team_id or (m.group(1) if m else None)    # the page's own team is not linked on its page
    title = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    name = _text(title.group(1)) if title else None
    head = _text(html[:20000])
    am = re.search(r"\b(?:U|GU)\s?\d{1,2}\b", head)
    age = normalize_age(am.group(0)) if am else (normalize_age(name) if name else None)
    girls = bool(re.search(r"\bgirls?\b", head, re.I))
    st = re.search(r'/teams/([a-z-]+)/girls/', html)
    state = STATE_SLUGS.get(st.group(1)) if st else None
    games = []
    hist, upc = _pane(html, "gamehistory"), _pane(html, "upcominggames")
    if hist or upc:
        played, w1 = _games_in_pane(hist, tid, name)
        upcoming, w2 = _games_in_pane(upc, tid, name)
        done = {(g["date"], frozenset((g["a_id"], g["b_id"]))) for g in played}
        # an upcoming entry already in the history is the same game: keep the scored copy only
        games = played + [g for g in upcoming if (g["date"], frozenset((g["a_id"], g["b_id"]))) not in done]
        warn += w1 + w2
    else:
        games, w = _legacy_tables(html)
        warn += w
    if not games:
        warn.append("no games found; the page layout may differ from what the parser expects")
    return {"id": tid, "name": name, "age": age, "girls": girls, "state": state}, games, warn


def _legacy_tables(html):
    """Generic fallback: inner tables with two /team/{id} links (used by the synthetic fixture)."""
    warn, games = [], []
    for tbl in re.findall(r"<table\b[^>]*>(?:(?!<table\b).)*?</table>", html, re.S):
        links = re.findall(r'href="(?:https?://(?:www\.)?youthsoccergames\.com)?/team/(\d+)"[^>]*>(.*?)</a>', tbl, re.S)
        if len(links) != 2:
            continue
        rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", tbl, re.S)
        scores = []
        for (lid, _) in links:
            r = next((r for r in rows if f"/team/{lid}" in r), "")
            cells = [_text(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", r, re.S)]
            sc = next((c for c in reversed(cells) if re.fullmatch(r"\d{1,2}", c)), None)
            scores.append(int(sc) if sc is not None else None)
        txt = _text(tbl)
        d = _find_date(txt)
        if not d:
            warn.append("game table without a readable date skipped")
            continue
        ev = re.search(r'href="([^"]*gotsport[^"]*events/(\d+)[^"]*)"', tbl)
        comp_row = next((r for r in rows if "/team/" not in r), "")
        comp = _text(comp_row)
        comp = re.sub(r"\b(" + "|".join(p for _, p in DATE_PATTERNS) + r")\b", "", comp).strip(" -|·,") or None
        played = None not in scores
        games.append({"a_id": links[0][0], "a_name": _text(links[0][1]), "b_id": links[1][0], "b_name": _text(links[1][1]),
                      "a_score": scores[0], "b_score": scores[1], "date": d, "competition": comp,
                      "event_url": ev.group(1) if ev else None, "event_id": ev.group(2) if ev else None,
                      "status": "final" if played else "scheduled"})
    return games, warn


STATE_SLUGS = {s.lower().replace(" ", "-"): a for s, a in [
    ("Alabama", "AL"), ("Alaska", "AK"), ("Arizona", "AZ"), ("Arkansas", "AR"), ("California", "CA"),
    ("Colorado", "CO"), ("Connecticut", "CT"), ("Delaware", "DE"), ("District of Columbia", "DC"),
    ("Florida", "FL"), ("Georgia", "GA"), ("Hawaii", "HI"), ("Idaho", "ID"), ("Illinois", "IL"),
    ("Indiana", "IN"), ("Iowa", "IA"), ("Kansas", "KS"), ("Kentucky", "KY"), ("Louisiana", "LA"),
    ("Maine", "ME"), ("Maryland", "MD"), ("Massachusetts", "MA"), ("Michigan", "MI"), ("Minnesota", "MN"),
    ("Mississippi", "MS"), ("Missouri", "MO"), ("Montana", "MT"), ("Nebraska", "NE"), ("Nevada", "NV"),
    ("New Hampshire", "NH"), ("New Jersey", "NJ"), ("New Mexico", "NM"), ("New York", "NY"),
    ("North Carolina", "NC"), ("North Dakota", "ND"), ("Ohio", "OH"), ("Oklahoma", "OK"), ("Oregon", "OR"),
    ("Pennsylvania", "PA"), ("Rhode Island", "RI"), ("South Carolina", "SC"), ("South Dakota", "SD"),
    ("Tennessee", "TN"), ("Texas", "TX"), ("Utah", "UT"), ("Vermont", "VT"), ("Virginia", "VA"),
    ("Washington", "WA"), ("West Virginia", "WV"), ("Wisconsin", "WI"), ("Wyoming", "WY")]}


def parse_ysg_directory(html):
    """Parse a state/age/gender directory page (approved for crawling). Returns team rows.
    Real layout (checked 2026-10-10): each <tr data-teamid> has a club cell (logo + club name),
    a team-name cell (td.w-45) and a chevron link, all linking to /team/{id}. Rankings on the
    page are deliberately NOT kept: national ratings here come only from games."""
    rows = []
    yg = re.search(r'data-yeargen="([^"]+)"', html)
    for attrs, body in re.findall(r'<tr\b([^>]*\bdata-teamid="\d+"[^>]*)>(.*?)</tr>', html, re.S):
        tid = re.search(r'data-teamid="(\d+)"', attrs).group(1)
        cells = re.findall(r"<td\b([^>]*)>(.*?)</td>", body, re.S)
        name = club = None
        for cattrs, cbody in cells:
            if f"/team/{tid}" not in cbody:
                continue
            txt = _text(cbody)
            if not txt:
                continue
            if "w-45" in cattrs:
                name = txt
            elif "club-logo" in cbody and club is None:
                club = txt
        if name is None:   # fallback: last non-empty link text that isn't the club
            texts = [_text(c) for _, c in cells if f"/team/{tid}" in c and _text(c)]
            texts = [t for t in texts if t != club]
            name = texts[-1] if texts else None
        dc = re.search(r'data-club="([^"]*)"', attrs)
        if dc and dc.group(1).strip():
            club = htmllib.unescape(dc.group(1)).strip()
        if name:
            rows.append({"id": tid, "name": name, "club": club})
    return {"yeargen": yg.group(1) if yg else None, "teams": rows}


def ysg_directory_index():
    """team id -> {name, club, state, age} from collected directory files."""
    idx = {}
    for p in sorted((IMPORTS / "ysg" / "directory").glob("*.json")):
        d = json.loads(p.read_text())
        for t in d.get("teams", []):
            idx[t["id"]] = {"name": t["name"], "club": t.get("club"), "state": d.get("state"), "age": d.get("age")}
    return idx


def _ysg_obs_from_games(info, games, retrieved, directory, problems=None):
    out = []
    for g in games:
        g = dict(g)
        g["a_id"] = g.get("a_id") or info["id"]
        g["b_id"] = g.get("b_id") or info["id"]
        if g["a_id"] == g["b_id"]:
            if problems is not None:
                problems.append({"file": f"team {info['id']}", "reason": f"game on {g.get('date')} has no opponent ID; skipped"})
            continue
        rk = f"{info['id']}|{g['date']}|{min(g['a_id'], g['b_id'])}|{max(g['a_id'], g['b_id'])}|{g.get('event_id') or g.get('competition') or ''}"
        def team(i, n):
            meta = directory.get(i, {})
            return {"source_key": f"ysg:{i}", "name": meta.get("name") or n, "age": info["age"],
                    "state": meta.get("state") or (info.get("state") if i == info["id"] else None),
                    "club": meta.get("club")}
        out.append({
            "obs_id": None, "source": "ysg", "observer": f"ysg:{info['id']}", "source_record_key": rk,
            "upstream_id": None, "date": g["date"], "time": None, "competition": g.get("competition"),
            "competition_type": "unknown", "venue_type": "unknown",
            "a": team(g["a_id"], g["a_name"]), "b": team(g["b_id"], g["b_name"]),
            "a_score": g["a_score"], "b_score": g["b_score"], "status": g["status"],
            "url": g.get("event_url") or f"https://youthsoccergames.com/team/{info['id']}",
            "retrieved_at": retrieved, "event_id": g.get("event_id"),
        })
    return out


def ysg_observations(paths=None, extracted=None):
    """YouthSoccerGames games from two places:
      imports/ysg/*.html             team pages a person saved in a browser
      imports/ysg/extracted/*.json   games extracted by scripts/collect_ysg.py under written permission
    Games before 2026-08-01 are dropped at extraction, per YouthSoccerGames' permission terms."""
    out, problems = [], []
    directory = ysg_directory_index()
    paths = paths if paths is not None else sorted((IMPORTS / "ysg").glob("*.htm*"))
    extracted = extracted if extracted is not None else sorted((IMPORTS / "ysg" / "extracted").glob("*.json"))
    for p in paths:
        html = p.read_text(encoding="utf-8", errors="replace")
        info, games, warn = parse_ysg_team_page(html)
        for w in warn:
            problems.append({"file": p.name, "reason": w})
        if not info["id"]:
            m = re.search(r"(\d{6,})", p.name)
            info["id"] = m.group(1) if m else None
        meta = directory.get(info["id"] or "", {})
        info["age"] = info["age"] or meta.get("age")
        info["state"] = info["state"] or meta.get("state")
        if not info["id"]:
            problems.append({"file": p.name, "reason": "could not tell which team this page belongs to"}); continue
        if not info["girls"] and not meta:
            problems.append({"file": p.name, "reason": "page does not say girls; skipped"}); continue
        if not info["age"]:
            problems.append({"file": p.name, "reason": "could not read the age group; skipped"}); continue
        out += _ysg_obs_from_games(info, [g for g in games if g["date"] >= "2026-08-01"], file_date(p), directory, problems)
    for p in extracted:
        d = json.loads(p.read_text())
        info = d["team"]
        for w in d.get("warnings", []):
            problems.append({"file": f"extracted/{p.name}", "reason": w})
        if not info.get("age"):
            problems.append({"file": f"extracted/{p.name}", "reason": "no age group; skipped"}); continue
        out += _ysg_obs_from_games(info, d["games"], d["fetched_at"], directory, problems)
    assign_seq(out)
    for o in out:   # same-day rematches on one page get distinct keys through seq
        o["source_record_key"] += f"|{o['seq']}"
        o["obs_id"] = obs_id("ysg", o["source_record_key"])
    return out, problems

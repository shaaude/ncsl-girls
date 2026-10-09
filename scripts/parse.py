"""Parse saved Demosphere division pages into structured games."""
import html as htmllib, json, re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"

ROW_RE = re.compile(r"<tr class=\"([^\"]*)\"([^>]*)>(.*?)</tr>", re.S)
DIV_RE = re.compile(r"\b(GU\d{1,2})\s+Division\s+([A-Za-z0-9]+)")
ASOF_RE = re.compile(r"current as of ([0-9/]+ [0-9:]+ [ap]m)", re.I)

def text(s):
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", htmllib.unescape(s)).strip()

def parse_team(cell_attrs, cell_html):
    tm = re.search(r'data-tmkey="(\d*)"', cell_attrs)
    cb = re.search(r'data-cbkey="(\d*)"', cell_attrs)
    t = text(cell_html)
    m = re.match(r"(N[GB]\d+)\s+(.*)", t)
    code, name = (m.group(1), m.group(2)) if m else (None, t)
    return {"key": tm.group(1) if tm and tm.group(1) else None,
            "club": cb.group(1) if cb and cb.group(1) else None,
            "code": code, "name": name}

def parse_score(raw):
    r = raw.strip()
    m = re.match(r"^(\d+)\s*-\s*(\d+)\s*(FFT)?$", r, re.I)
    if m:
        return int(m.group(1)), int(m.group(2)), ("forfeit" if m.group(3) else "final")
    if r.lower() == "vs" or r in ("", "--", "-"):
        return None, None, ("scheduled" if r.lower() == "vs" else "no_score")
    return None, None, "other"

def parse_page(html):
    games, cur_date = [], None
    for cls, attrs, body in ROW_RE.findall(html):
        if cls.startswith("gm-hdr-5d1h") and "5d1ha" not in cls:
            d = text(body)
            try:
                cur_date = datetime.strptime(d, "%a, %B %d, %Y").date().isoformat()
            except ValueError:
                cur_date = None
            continue
        if "sch-main-gm" not in cls:
            continue
        gk = re.search(r'data-gamekey="(\d+)"', attrs)
        tg = re.search(r'data-tgkey="(\d+)"', attrs)
        cells = re.findall(r"<td([^>]*)>(.*?)</td>", body, re.S)
        home_i = next(i for i, (a, _) in enumerate(cells) if "schedtm1" in a)
        away_i = next(i for i, (a, _) in enumerate(cells) if "schedtm2" in a)
        code = re.search(r'class="gamecode">\s*([^<]*)<', body)
        tim = next((text(c) for a, c in cells if 'class="tim"' in a), "")
        score_raw = text(cells[home_i + 1][1])
        loc = cells[away_i + 1][1] if away_i + 1 < len(cells) else ""
        fac = re.search(r"Display/\+(\d*)\+", loc)
        hs, as_, status = parse_score(score_raw)
        games.append({
            "gamekey": gk.group(1) if gk else None,
            "division_id": tg.group(1) if tg else None,
            "game_no": code.group(1).strip() if code else None,
            "date": cur_date,
            "time": tim or None,
            "home": parse_team(*cells[home_i]),
            "away": parse_team(*cells[away_i]),
            "home_score": hs, "away_score": as_,
            "status": status, "score_raw": score_raw,
            "venue": text(loc) or None,
            "facility_key": fac.group(1) if fac and fac.group(1) else None,
            "rescheduled_recent": "reasonRED" in body,
            "rescheduled": "reasonYELLOW" in body or "reasonRED" in body,
        })
    return games

def parse_division(div_id):
    files = sorted(RAW.glob(f"{div_id}*.html"))
    games, name, asof = {}, None, None
    for f in files:
        h = f.read_text(encoding="utf-8")
        m = DIV_RE.search(h)
        if m and not name:
            name = f"{m.group(1)} Division {m.group(2)}"
        a = ASOF_RE.search(h)
        if a:
            ts = datetime.strptime(a.group(1), "%m/%d/%Y %I:%M %p")
            asof = max(asof, ts) if asof else ts
        for g in parse_page(h):
            if g["division_id"] == str(div_id):
                games[g["gamekey"]] = g
    return {"id": str(div_id), "name": name,
            "source_as_of": asof.isoformat() if asof else None,
            "games": sorted(games.values(), key=lambda g: (g["date"] or "", g["time"] or "", g["game_no"] or ""))}

if __name__ == "__main__":
    import sys
    d = parse_division(sys.argv[1])
    print(json.dumps({k: v for k, v in d.items() if k != "games"}), len(d["games"]))
    for g in d["games"][:3]:
        print(json.dumps(g))

"""Find each NCSL team's other YouthSoccerGames records (tournament and league entries kept under
separate YSG team IDs), using the YSG directory pages for VA, MD and DC.

YSG often lists one squad twice: a league record ("ARL Red", linked to NCSL by matching game
results) and a tournament record ("Arlington SA Red", from tournament software). Tournament games
are never in NCSL, so they cannot be linked by matching scores. They are linked here by:

  * club   - the YSG club must be the NCSL team's club. The club is learned from NCSL teams of the
             same NCSL club that are already linked by evidence, or else from a unique club whose
             abbreviations match the NCSL name's prefix ("ARL" -> Arlington Soccer Association);
  * label  - the squad label left after removing club words, ages and birth years must be the
             same, and not empty ("Red" == "Red", "Pre-GA II" != "Pre-GA");
  * age    - the YSG age must be the NCSL age or one younger (YSG files many 2014/15 teams under
             the younger year), and the same YSG age as the team's already linked league record,
             when there is one.

A record is linked only if it fits exactly one NCSL team and that team's club has no other squad
it could be. Everything else is written to a review list for a person to decide. Manual decisions
(data/identity_decisions.json) always win.

Output: data/record_links.json  {"links": {ysg_key: canonical_id}, "review": [...], ...}
"""
import json
import re
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
YSG_DIR = ROOT / "imports" / "ysg" / "directory"
OUT = DATA / "record_links.json"

GENERIC = {"girls", "girl", "g", "gu", "team", "ncsl", "travel", "fc", "sc", "sa", "soccer", "club", "youth", "ys",
           "the", "of", "and", "association", "football", "futbol", "yso", "ysa", "athletic", "athletics", "inc",
           "u", "w", "f", "sports", "league", "rec"}
AGE_TOKEN = re.compile(r"^(u\d{1,2}g?|g?u\d{1,2}|g\d{2,4}|\d{2,4}g|(19|20)\d\d|\d{2}|b?g\d{2}/?\d*|\d{4}/\d{2,4})$")


def club_norm(club):
    s = re.sub(r"\(.*?\)", " ", (club or "").lower())
    s = re.sub(r"\s*-\s*(travel|rec|recreational|competitive)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def is_rec(club):
    return bool(re.search(r"-\s*rec\b", (club or "").lower()))


def sig_words(club):
    return [w for w in re.split(r"[^a-z]+", club_norm(club)) if w and w not in GENERIC]


def club_tags(club):
    """Abbreviations an NCSL name might use for a club: 'Arlington Soccer Association' ->
    {'arlington', 'arl', 'arli', 'asa'}; 'Springfield SYC Soccer' -> {..., 'syc'}."""
    words = [w for w in re.split(r"[^a-z]+", club_norm(club)) if w]
    if not words:
        return set()
    sig = sig_words(club)
    tags = {words[0], words[0][:3], words[0][:4], "".join(w[0] for w in words)}
    if sig:
        tags |= set(sig) | {sig[0][:3], sig[0][:4], "".join(w[0] for w in sig)}
    return {t for t in tags if len(t) >= 2}


def club_words(club):
    return {w for w in re.split(r"[^a-z]+", (club or "").lower()) if w}


def label(name, club=None, prefix=None):
    """Squad label: the words that tell a club's squads apart ("red", "pre", "ga", "ii")."""
    s = (name or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"(\d)\s*/\s*(\d)", r"\1/\2", s)
    drop = GENERIC | club_words(club) | club_tags(club)
    if prefix:
        drop.add(prefix)
    out = set()
    for tok in re.split(r"[^a-z0-9/]+", s):
        if not tok or tok in drop:
            continue
        if AGE_TOKEN.match(tok) or re.fullmatch(r"\d[\d/]+g?", tok) or re.fullmatch(r"g[\d/]+", tok):
            continue
        tok = re.sub(r"^(g|u)(\d{2,4})(/\d+)?$", "", tok)
        if tok:
            out.add(tok)
    return frozenset(out)


def ysg_age(name, ncsl_age):
    """The YSG age a squad is filed under. YSG uses the younger birth year of a two-year squad
    ("2014/15" -> 2015 -> U12 in the 2026-27 season). Names without birth years use the U-age
    written in the name, else the NCSL age (or one younger) is allowed."""
    from config import SEASON_END_YEAR
    s = (name or "").lower()
    years = []
    for m in re.finditer(r"(?<![\du])((?:20)?\d{2})(?:\s*[/-]\s*((?:20)?\d{2}))?(?!\d)", s):
        for y in m.groups():
            if y:
                y = int(y) if len(y) == 4 else 2000 + int(y)
                if 2004 <= y <= 2021:
                    years.append(y)
    if years and not re.search(r"\bu\s?\d", s.replace("(", " ")) or years and len(years) >= 2:
        n = SEASON_END_YEAR - max(years)
        return widen({f"GU{n}"} if len(years) >= 2 else {f"GU{n}", f"GU{n - 1}"})
    m = re.search(r"\bu\s?(\d{1,2})", s)
    if m:
        return widen({f"GU{int(m.group(1))}"})
    n = age_num(ncsl_age)
    return {f"GU{n}", f"GU{n - 1}"} if n else set()


def widen(ages):
    """YSG lists its oldest girls under U18 and U19 interchangeably."""
    if ages & {"GU18", "GU19"}:
        ages = ages | {"GU18", "GU19"}
    return ages


def age_num(age):
    return int(age[2:]) if age and age[2:].isdigit() else None


def load_directory():
    """ysg id -> {name, club, state, ages:set}"""
    d = {}
    for p in sorted(YSG_DIR.glob("*.json")):
        j = json.loads(p.read_text())
        for t in j["teams"]:
            e = d.setdefault(t["id"], {"name": t["name"], "club": t.get("club"), "state": j["state"], "ages": set()})
            e["ages"].add(j["age"])
    return d


def match(aliases, directory, decisions):
    teams, maps = aliases["teams"], aliases["mappings"]
    ncsl = {cid: t for cid, t in teams.items() if t.get("ncsl_key")}

    # YSG records already tied to an NCSL team (evidence or manual)
    linked = defaultdict(list)          # cid -> [ysg id]
    taken = set()
    for k, m in maps.items():
        if (k.startswith("ysg:") and m["status"] == "confirmed" and m.get("canonical_id") in ncsl
                and m.get("by") in ("auto-evidence", "manual")):       # not this matcher's own links
            linked[m["canonical_id"]].append(k[4:]); taken.add(k[4:])
    for k, v in decisions.items():
        if k.startswith("ysg:"):
            taken.add(k[4:])
            if v in ncsl:
                linked[v].append(k[4:])

    def prefix(name):
        return re.split(r"[^a-z]+", name.lower())[0]

    # club of each NCSL team: from its own / club-mates' linked records, else prefix -> club
    club_by_ncslclub, club_by_prefix = defaultdict(set), defaultdict(set)
    for cid, ys in linked.items():
        for y in ys:
            c = directory.get(y, {}).get("club") or maps.get(f"ysg:{y}", {}).get("club")
            if c:
                club_by_ncslclub[ncsl[cid].get("ncsl_club")].add(c)
                club_by_prefix[prefix(ncsl[cid]["name"])].add(c)
    local_clubs = {e["club"] for e in directory.values()
                   if e["club"] and e["state"] in ("VA", "MD", "DC") and not is_rec(e["club"])}

    def acronym(c):
        return "".join(w[0] for w in re.split(r"[^a-z]+", club_norm(c)) if w)

    def same_family(c, known):
        """'Springfield SYC Soccer' ~ 'Springfield South County Youth - Travel' (same first word);
        'NVSC' ~ 'Northern Virginia Soccer Club - Travel' (one is the other's initials)."""
        if club_norm(c) == club_norm(known):
            return True
        a, b = sig_words(c), sig_words(known)
        if a and b and a[0] == b[0]:
            return True
        return bool(a and b and (a[0] == acronym(known) or b[0] == acronym(c)))

    def one_family(clubs):
        cl = sorted(clubs)
        return all(same_family(cl[0], c) for c in cl[1:])

    def clubs_of(t):
        """All YSG club names this NCSL team's club appears under (league records and tournament
        records often spell the club differently)."""
        p = prefix(t["name"])
        known = club_by_ncslclub.get(t.get("ncsl_club")) or club_by_prefix.get(p) or set()
        if len(known) > 1 and not one_family(known):
            return None, "linked records disagree on the club"
        fits = {c for c in local_clubs if p in club_tags(c)}
        if known:
            fam = set(known)
            for k in known:
                fam |= {c for c in local_clubs if club_norm(c) == club_norm(k)} | {c for c in fits if same_family(c, k)}
            return fam, "learned"
        if not fits:
            return None, "no club fits the prefix"
        if one_family(fits):
            return fits, "prefix-abbreviation"
        return None, "several clubs fit the prefix: " + "; ".join(sorted(fits))

    # directory records by club
    by_club = defaultdict(list)
    for yid, e in directory.items():
        if e["club"] and e["state"] in ("VA", "MD", "DC"):
            by_club[e["club"]].append(yid)

    proposals = defaultdict(set)          # ysg id -> {cid}
    team_club, notes = {}, {}
    for cid, t in ncsl.items():
        fam, how = clubs_of(t)
        team_club[cid] = sorted(fam) if fam else None
        if not fam:
            notes[cid] = how
            continue
        lab = label(t["name"], None, prefix(t["name"]))
        lab = frozenset(w for w in lab if not any(w in club_words(c) or w in club_tags(c) for c in fam))
        if not lab:
            notes[cid] = "name has no squad label to compare"
            continue
        ok_ages = ysg_age(t["name"], t["age"])
        league_ages = set()
        for y in linked.get(cid, []):
            league_ages |= directory.get(y, {}).get("ages", set())
        for yid in (y for c in fam for y in by_club[c]):
            if yid in taken:
                continue
            e = directory[yid]
            ages = e["ages"] & ok_ages
            if league_ages:
                ages &= league_ages
            if ages and label(e["name"], e["club"]) == lab:
                proposals[yid].add(cid)

    # an NCSL team whose club has two NCSL squads with the same label within a year is ambiguous
    links, review = {}, []
    for yid, cids in sorted(proposals.items()):
        e = directory[yid]
        if len(cids) == 1:
            links[f"ysg:{yid}"] = next(iter(cids))
        else:
            review.append({"ysg_key": f"ysg:{yid}", "ysg_name": e["name"], "ysg_club": e["club"],
                           "ysg_ages": sorted(e["ages"]), "reason": "fits more than one NCSL team",
                           "ncsl_candidates": sorted(f"{ncsl[c]['name']} ({ncsl[c]['age']})" for c in cids),
                           "candidate_ids": sorted(cids)})
    # near misses worth a look: same club, adjacent age, label differs only slightly
    return links, review, team_club, notes


def compute(aliases, decisions, directory=None):
    """aliases: {"teams": ..., "mappings": ...} (data/team_aliases.json or a live Identities)."""
    directory = load_directory() if directory is None else directory
    links, review, team_club, notes = match(aliases, directory, decisions)
    teams = aliases["teams"]
    gaining = {cid for cid in links.values()}
    return {"links": links, "review": review,
            "summary": {"directory_records": len(directory),
                        "ncsl_teams": sum(1 for t in teams.values() if t.get("ncsl_key")),
                        "ncsl_teams_with_club": sum(1 for c in team_club.values() if c),
                        "records_linked": len(links), "ncsl_teams_gaining_records": len(gaining),
                        "ambiguous": len(review)},
            "teams_without_club": sorted(f"{teams[c]['name']} ({teams[c]['age']}): {why}" for c, why in notes.items()
                                         if "club" in why)}


def main():
    aliases = json.loads((DATA / "team_aliases.json").read_text())
    dec_p = DATA / "identity_decisions.json"
    decisions = json.loads(dec_p.read_text()) if dec_p.exists() else {}
    out = compute(aliases, decisions)
    OUT.write_text(json.dumps(out, indent=1, sort_keys=True))
    print(json.dumps(out["summary"]))
    return out


if __name__ == "__main__":
    main()

"""Canonical team identities shared by every source.

A canonical team has a stable internal ID ("T" + 10 hex chars) that never depends on any
one source system. Each source's own team key maps to a canonical team with a status:

  confirmed   - safe to use in national model training
  probable    - looks like a known team but lacks evidence; held for review, not used
  unresolved  - no decision yet; not used

Rules
  * NCSL (Demosphere) teams are registered first and are always confirmed.
  * A new source team starts "pending". It is linked to an EXISTING canonical team only on
    game evidence: its results line up with that team's games on the same dates with the same
    scores (2+ dates, or 1 date plus a resembling name), with no tied rival. A similar name
    alone, or the same club and age group, is never enough. Names can differ completely
    ("Arlington SA Red" vs "ARL 2015G Red"); the games decide.
  * A pending team with no evidence becomes its own squad, unless its name closely resembles a
    known team, in which case it is held for review (probable) and does not train.
  * Manual decisions (by: "manual") are never changed automatically.
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALIASES = ROOT / "data" / "team_aliases.json"

STOP = {"girls", "girl", "g", "fc", "sc", "soccer", "club", "academy", "the", "youth", "ys", "u", "gu", "ncsl",
        "travel", "team", "of", "and", "association", "sa", "yso", "premier"}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_canonical_id(seed):
    return "T" + hashlib.sha1(seed.encode()).hexdigest()[:10]


def norm_name(name):
    s = (name or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)          # drop "(U13)" style suffixes
    s = re.sub(r"[^a-z0-9/]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_tokens(name):
    toks = [t for t in re.split(r"[ /]", norm_name(name)) if t and t not in STOP]
    out = set()
    for t in toks:
        if re.fullmatch(r"(19|20)\d\d", t):
            out.add(t[2:])                      # 2014 -> 14 so "2014/15" and "14/15" agree
        elif re.fullmatch(r"\d{1,2}", t):
            out.add(t.zfill(2))
        else:
            out.add(t)
    return out


def similarity(a, b):
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / len(ta | tb)
    seq = SequenceMatcher(None, norm_name(a), norm_name(b)).ratio()
    return round(0.6 * jac + 0.4 * seq, 3)


class Identities:
    def __init__(self, path=ALIASES):
        self.path = Path(path)
        if self.path.exists():
            d = json.loads(self.path.read_text())
        else:
            d = {"teams": {}, "mappings": {}}
        self.teams = d["teams"]          # canonical_id -> team record
        self.mappings = d["mappings"]    # source_team_key -> {canonical_id, status, ...}

    # ---------- persistence
    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"teams": self.teams, "mappings": self.mappings}, indent=1, sort_keys=True))
        tmp.replace(self.path)

    # ---------- lookups
    def resolve(self, source_key):
        m = self.mappings.get(source_key)
        if m and m["status"] == "confirmed":
            return m["canonical_id"]
        return None

    def status(self, source_key):
        m = self.mappings.get(source_key)
        return m["status"] if m else "unresolved"

    def aliases_of(self, cid):
        return [{"source_key": k, **{kk: v for kk, v in m.items() if kk in ("status", "name", "by", "at")}}
                for k, m in self.mappings.items() if m.get("canonical_id") == cid]

    # ---------- registration
    def register_ncsl(self, tmkey, name, age, club=None, code=None, division=None):
        key = f"ncsl:{tmkey}"
        m = self.mappings.get(key)
        if m:
            t = self.teams[m["canonical_id"]]
            t.update(name=name, age=age, ncsl_key=tmkey, ncsl_division=division)
            if club:
                t["ncsl_club"] = club
            return m["canonical_id"]
        cid = new_canonical_id(key)
        self.teams[cid] = {"id": cid, "name": name, "age": age, "gender": "G", "state": None, "region": None,
                           "club": None, "ncsl_key": tmkey, "ncsl_club": club, "ncsl_code": code,
                           "ncsl_division": division, "created": now()}
        self.mappings[key] = {"canonical_id": cid, "status": "confirmed", "by": "ncsl", "name": name, "at": now()}
        return cid

    def candidates(self, name, age, exclude_sources=()):
        """Existing canonical teams in the same age group whose names resemble this one."""
        out = []
        for cid, t in self.teams.items():
            if t["age"] != age:
                continue
            s = similarity(name, t["name"])
            if s >= 0.45:
                out.append((s, cid))
        return sorted(out, reverse=True)[:5]

    def ensure_source_team(self, source_key, name, age, state=None, club=None):
        """Called for every external team seen. A new key starts as "pending"; after the evidence
        pass (link_with_evidence), finalize_pending() either confirms it as a new squad or holds
        it for review."""
        m = self.mappings.get(source_key)
        if m:
            m["name"] = name or m.get("name")
            if m["status"] == "confirmed":
                t = self.teams[m["canonical_id"]]
                if state and not t.get("state"):
                    t["state"] = state
                if club and not t.get("club"):
                    t["club"] = club
            return m
        cands = [c for c in self.candidates(name, age) if c[0] >= 0.6]
        m = {"canonical_id": None, "status": "pending", "by": "auto", "name": name, "age": age,
             "state": state, "club": club,
             "candidates": [{"canonical_id": c, "similarity": s} for s, c in cands], "at": now()}
        self.mappings[source_key] = m
        return m

    def link_with_evidence(self, source_key, evidence):
        """evidence: canonical_id -> number of distinct dates on which this source team's results
        line up with the canonical team's games (same date, same score, consistent opponent).
        Confirms a unique winner with 2+ matching dates, or 1 date plus a resembling name."""
        m = self.mappings.get(source_key)
        if not m or m.get("by") == "manual":
            return m
        relink = m["status"] == "confirmed" and m.get("by") == "auto-new"
        if m["status"] not in ("pending", "probable") and not relink:
            return m
        scored = sorted(((n, cid) for cid, n in evidence.items() if n > 0), reverse=True)
        if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
            return m
        n, cid = scored[0]
        if relink and cid == m["canonical_id"]:
            return m
        if relink and self.teams.get(cid, {}).get("ncsl_key") is None:
            return m              # only fold an auto-created squad into an established (NCSL) team
        if n >= 2 or similarity(m.get("name"), self.teams[cid]["name"]) >= 0.35:
            old = m.get("canonical_id") if relink else None
            m.update(canonical_id=cid, status="confirmed", by="auto-evidence", evidence_dates=n, at=now())
            if old and not any(mm.get("canonical_id") == old for mm in self.mappings.values()):
                self.teams.pop(old, None)     # the placeholder squad had no other identities
            t = self.teams[cid]
            for f in ("state", "club"):
                if m.get(f) and not t.get(f):
                    t[f] = m[f]
        return m

    def finalize_pending(self, scored_keys=None):
        """After the evidence pass: a pending team that resembles a known team is held for review
        (probable, not used in training); a team seen only in unscored games stays pending (there
        is nothing to link it by yet); anything else becomes a separate squad of its own."""
        for key, m in self.mappings.items():
            if m["status"] != "pending":
                continue
            if scored_keys is not None and key not in scored_keys:
                continue
            if m.get("candidates"):
                m.update(status="probable", canonical_id=m["candidates"][0]["canonical_id"], at=now())
            else:
                cid = new_canonical_id(key)
                self.teams.setdefault(cid, {"id": cid, "name": m.get("name"), "age": m.get("age"), "gender": "G",
                                            "state": m.get("state"), "region": None, "club": m.get("club"),
                                            "ncsl_key": None, "created": now()})
                m.update(status="confirmed", canonical_id=cid, by="auto-new", at=now())

    def apply_record_links(self, links):
        """links: {source_key: canonical_id} from match_records (same club, same squad label, same
        age). Ties a team's separate tournament/league records to it. Never overrides a manual
        decision or a link made on game evidence. Returns the number of mappings changed."""
        changed = 0
        for key, cid in links.items():
            m = self.mappings.get(key)
            if not m or cid not in self.teams or m.get("by") in ("manual", "auto-evidence"):
                continue
            if m.get("canonical_id") == cid and m["status"] == "confirmed":
                continue
            old = m.get("canonical_id") if m.get("by") == "auto-new" else None
            m.update(canonical_id=cid, status="confirmed", by="auto-label", at=now())
            if old and old != cid and not any(mm.get("canonical_id") == old for mm in self.mappings.values()):
                self.teams.pop(old, None)
            t = self.teams[cid]
            for f in ("state", "club"):
                if m.get(f) and not t.get(f):
                    t[f] = m[f]
            changed += 1
        return changed

    def apply_manual(self, decisions):
        """decisions: {source_key: canonical_id | "new" | "reject"} from data/identity_decisions.json."""
        for key, dec in decisions.items():
            m = self.mappings.get(key)
            if not m:
                continue
            if dec == "new":
                cid = new_canonical_id(key)
                if cid not in self.teams:
                    self.teams[cid] = {"id": cid, "name": m.get("name"), "age": m.get("age"), "gender": "G",
                                       "state": m.get("state"), "region": None, "club": m.get("club"),
                                       "ncsl_key": None, "created": now()}
                m.update(canonical_id=cid, status="confirmed", by="manual", at=now())
            elif dec == "reject":
                m.update(status="unresolved", by="manual", at=now())
            elif dec in self.teams:
                m.update(canonical_id=dec, status="confirmed", by="manual", at=now())

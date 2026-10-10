"""Merge observations from every source into unique canonical matches.

Matching (strongest first)
  1. Same verified upstream match ID.
  2. Same canonical teams on the same date: within one observer, each row is a separate game
     (so a same-day group game and final stay separate); across observers, rows are paired by
     upstream ID, then by their order that day (seq), then by an agreeing score.
  3. Anything else stays separate rather than being merged on a guess.

Canonical match IDs are immutable: once an observation belongs to a match ID, that ID is
reused on every later run. Scores are never part of a match's identity.

Scores: the most authoritative source wins (ncsl > event export > csv > ysg). If two
observations of the same match disagree, the match is flagged in the review queue; when the
disagreeing sources have equal standing the match is marked "conflict" and kept out of
training. Score changes over time are written to the correction log, never silently.
"""
import hashlib
import json
from collections import defaultdict
from pathlib import Path

from config import SOURCE_PRIORITY

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_jsonl(path):
    p = Path(path)
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def write_jsonl(path, rows):
    p = Path(path)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    tmp.replace(p)


class ObservationStore:
    """Append-only memory of every observation ever seen (latest version per obs_id), so games
    stay available after they drop off a source website."""

    def __init__(self, path=DATA / "match_observations.jsonl"):
        self.path = Path(path)
        self.rows = {r["obs_id"]: r for r in load_jsonl(self.path)}

    def upsert(self, observations, source):
        """Replace this source's observations with a fresh pull. Observations that vanished
        from the source are kept (marked last_seen) rather than deleted."""
        changed = 0
        fresh = {o["obs_id"] for o in observations}
        for o in observations:
            prev = self.rows.get(o["obs_id"])
            o = dict(o)
            o["first_seen"] = prev.get("first_seen", o.get("retrieved_at")) if prev else o.get("retrieved_at")
            o["missing_from_source"] = False
            if prev:
                hist = prev.get("score_history", [])
                if (prev.get("a_score"), prev.get("b_score")) != (o.get("a_score"), o.get("b_score")) and prev.get("a_score") is not None:
                    hist = hist + [{"a_score": prev["a_score"], "b_score": prev["b_score"], "until": o.get("retrieved_at")}]
                o["score_history"] = hist
            if prev != o:
                changed += 1
            self.rows[o["obs_id"]] = o
        if observations:   # an empty pull never marks everything missing
            for oid, r in self.rows.items():
                if r["source"] == source and oid not in fresh:
                    r["missing_from_source"] = True
        return changed

    def save(self):
        write_jsonl(self.path, sorted(self.rows.values(), key=lambda r: (r.get("date") or "", r["obs_id"])))

    def all(self):
        """Every observation, minus stale copies: a row no longer on its source page is dropped
        when the same page now lists a game on the same date between the same two teams (the
        page was re-read and the row's text changed, e.g. a parser fix). Rows that simply
        dropped off a page are kept."""
        live = defaultdict(set)
        for r in self.rows.values():
            if not r.get("missing_from_source"):
                live[(r["observer"], r.get("date"))].add(frozenset((r["a"]["source_key"], r["b"]["source_key"])))
        return [r for r in self.rows.values()
                if not (r.get("missing_from_source")
                        and frozenset((r["a"]["source_key"], r["b"]["source_key"])) in live.get((r["observer"], r.get("date")), ()))]


def _oriented(o, first_cid, cid_of):
    """Return (score_for_first, score_for_second) for observation o in the order of first_cid."""
    if cid_of(o["a"]["source_key"]) == first_cid:
        return o["a_score"], o["b_score"]
    return o["b_score"], o["a_score"]


def link_identities(ids, observations):
    """Evidence pass for pending/probable source teams. A source team's final results are compared
    with games already attributed to confirmed teams (e.g. NCSL league games): each date where a
    confirmed team played with the same score (from that team's side) - and against an opponent
    consistent with this game's opponent when that opponent is already known - counts as one
    matching date for that confirmed team."""
    fp = defaultdict(list)     # (date, my score, opp score) -> [(cid, opp cid)]
    for o in observations:
        ca, cb = ids.resolve(o["a"]["source_key"]), ids.resolve(o["b"]["source_key"])
        if ca and cb and o["status"] == "final" and o["date"] and o["a_score"] is not None:
            fp[(o["date"], o["a_score"], o["b_score"])].append((ca, cb))
            fp[(o["date"], o["b_score"], o["a_score"])].append((cb, ca))
    rows = defaultdict(list)
    for o in observations:
        if o["status"] != "final" or not o["date"] or o["a_score"] is None:
            continue
        rows[o["a"]["source_key"]].append((o["date"], o["a_score"], o["b_score"], o["b"]["source_key"]))
        rows[o["b"]["source_key"]].append((o["date"], o["b_score"], o["a_score"], o["a"]["source_key"]))
    linked = 0
    for _round in range(3):          # newly linked teams can anchor their opponents next round
        changed = 0
        for key, m in list(ids.mappings.items()):
            if m.get("by") == "manual":
                continue
            if m["status"] not in ("pending", "probable") and not (m["status"] == "confirmed" and m.get("by") == "auto-new"):
                continue
            dates = defaultdict(set)
            for d, mine, theirs, opp_key in rows.get(key, []):
                opp = ids.resolve(opp_key)
                for cid, ocid in fp.get((d, mine, theirs), []):
                    if ids.teams.get(cid, {}).get("age") != m.get("age"):
                        continue
                    if opp and ocid != opp:
                        continue
                    if cid == m.get("canonical_id"):
                        continue              # a team's own games are not evidence about itself
                    if any(mm.get("canonical_id") == cid and mm["status"] == "confirmed" and k.split(":")[0] == key.split(":")[0]
                           and mm.get("by") != "auto-label"        # a tournament record doesn't block the league one
                           for k, mm in ids.mappings.items() if k != key):
                        continue          # that team already has an ID in this source
                    dates[cid].add(d)
            before = m["status"]
            ids.link_with_evidence(key, {c: len(v) for c, v in dates.items()})
            if m["status"] != before:
                changed += 1
        linked += changed
        if not changed:
            break
    ids.finalize_pending(scored_keys=set(rows))
    return linked


def reconcile(observations, ids, match_index_path=DATA / "match_index.json"):
    """Returns (matches, review, stats). matches: list of canonical match dicts."""
    idx_path = Path(match_index_path)
    index = json.loads(idx_path.read_text()) if idx_path.exists() else {}   # obs_id -> match_id
    cid_of = ids.resolve

    resolved, unresolved = [], []
    for o in observations:
        ca, cb = cid_of(o["a"]["source_key"]), cid_of(o["b"]["source_key"])
        if not o.get("date"):
            unresolved.append((o, "no date"))
        elif ca and cb and ca != cb:
            resolved.append(o)
        else:
            unresolved.append((o, "team identity not confirmed"))

    groups = defaultdict(list)
    for o in resolved:
        pair = tuple(sorted((cid_of(o["a"]["source_key"]), cid_of(o["b"]["source_key"]))))
        groups[(o["date"], pair)].append(o)

    clusters = []   # list of lists of observations, each = one real game
    for (date, pair), obs in groups.items():
        by_observer = defaultdict(list)
        for o in obs:
            by_observer[o["observer"]].append(o)
        # seed from the most authoritative observer with the most rows that day
        order = sorted(by_observer, key=lambda k: (SOURCE_PRIORITY.get(by_observer[k][0]["source"], 9), -len(by_observer[k]), k))
        cl = []
        for ob in order:
            rows = sorted(by_observer[ob], key=lambda o: o.get("seq", 0))
            taken = set()
            for o in rows:
                target = None
                # 1. upstream id
                if o.get("upstream_id"):
                    target = next((i for i, c in enumerate(cl) if i not in taken and any(x.get("upstream_id") == o["upstream_id"] for x in c)), None)
                # 2. previously assigned match id
                if target is None and o["obs_id"] in index:
                    mid = index[o["obs_id"]]
                    target = next((i for i, c in enumerate(cl) if i not in taken and any(index.get(x["obs_id"]) == mid for x in c)), None)
                # 3. same order that day, no conflicting upstream ids
                if target is None:
                    cands = [i for i, c in enumerate(cl) if i not in taken and not any(
                        x.get("upstream_id") and o.get("upstream_id") and x["upstream_id"] != o["upstream_id"] for x in c)]
                    same_seq = [i for i in cands if any(x.get("seq", 0) == o.get("seq", 0) for x in cl[i])]
                    if len(cl) and len(cands) == 1:
                        target = cands[0]
                    elif same_seq:
                        target = same_seq[0]
                    else:
                        sc = _oriented(o, pair[0], cid_of)
                        same_score = [i for i in cands if any(_oriented(x, pair[0], cid_of) == sc for x in cl[i])]
                        target = same_score[0] if same_score else None
                if target is None:
                    cl.append([o]); taken.add(len(cl) - 1)
                else:
                    cl[target].append(o); taken.add(target)
        clusters.extend(cl)

    matches, review, new_index = [], [], {}
    stats = {"observations": len(observations), "resolved_observations": len(resolved),
             "unresolved_observations": len(unresolved), "duplicates_merged": 0, "conflicts": 0}
    used_ids = set()
    for c in clusters:
        prior = sorted({index[o["obs_id"]] for o in c if o["obs_id"] in index} - used_ids)
        if prior:
            mid = prior[0]
        else:
            seed = min(c, key=lambda o: o["obs_id"])["obs_id"]
            mid = "M" + hashlib.sha1(seed.encode()).hexdigest()[:12]
        used_ids.add(mid)
        for o in c:
            new_index[o["obs_id"]] = mid
        stats["duplicates_merged"] += len(c) - 1
        best = min(c, key=lambda o: (SOURCE_PRIORITY.get(o["source"], 9), o["status"] != "final", o["obs_id"]))
        first = cid_of(best["a"]["source_key"]); second = cid_of(best["b"]["source_key"])
        finals = [o for o in c if o["status"] == "final" and o["a_score"] is not None]
        scores = {(o["source"], _oriented(o, first, cid_of)) for o in finals}
        distinct = {s for _, s in scores}
        status = best["status"]
        a_s, b_s = best["a_score"], best["b_score"]
        if best["status"] != "final" and finals:   # authoritative source has no score yet; use next best
            alt = min(finals, key=lambda o: (SOURCE_PRIORITY.get(o["source"], 9), o["obs_id"]))
            a_s, b_s = _oriented(alt, first, cid_of); status = "final"
        conflict = len(distinct) > 1
        if conflict:
            stats["conflicts"] += 1
            top = min(SOURCE_PRIORITY.get(o["source"], 9) for o in finals)
            top_scores = {_oriented(o, first, cid_of) for o in finals if SOURCE_PRIORITY.get(o["source"], 9) == top}
            resolved_by_authority = len(top_scores) == 1
            review.append({"type": "score_conflict", "match_id": mid, "date": best["date"],
                           "teams": [first, second], "resolved_by_authority": resolved_by_authority,
                           "reports": [{"source": o["source"], "observer": o["observer"], "score": list(_oriented(o, first, cid_of)),
                                        "url": o.get("url")} for o in finals]})
            if resolved_by_authority:
                a_s, b_s = next(iter(top_scores))
            else:
                status = "conflict"
        comp_obs = [o for o in c if o.get("competition")]
        ctypes = {o.get("competition_type") for o in c} - {"unknown", None}
        venue = best.get("venue_type") if best.get("venue_type") in ("home", "neutral") else \
            ("neutral" if "tournament" in ctypes or "showcase" in ctypes else "unknown")
        matches.append({
            "match_id": mid, "date": best["date"], "time": best.get("time"),
            "team_a": first, "team_b": second, "a_score": a_s, "b_score": b_s, "status": status,
            "venue_type": venue,
            "competition": (min(comp_obs, key=lambda o: SOURCE_PRIORITY.get(o["source"], 9))["competition"] if comp_obs else None),
            "competition_type": ("league" if "league" in ctypes else next(iter(ctypes)) if ctypes else "unknown"),
            "age_a": best["a"]["age"], "age_b": best["b"]["age"],
            "sources": sorted({o["source"] for o in c}), "observations": sorted(o["obs_id"] for o in c),
            "urls": sorted({o["url"] for o in c if o.get("url")}),
            "last_verified": max((o.get("retrieved_at") or "") for o in c),
        })
    for o, reason in unresolved:
        # only scored games need a decision; future fixtures resolve themselves once teams link
        if o["source"] != "ncsl" and o["status"] == "final":
            review.append({"type": "unresolved_identity", "obs_id": o["obs_id"], "date": o.get("date"),
                           "teams": [o["a"]["name"], o["b"]["name"]], "source": o["source"], "reason": reason})
    for key, m in ids.mappings.items():
        if m["status"] == "probable":
            review.append({"type": "probable_identity", "source_key": key, "name": m.get("name"),
                           "candidates": m.get("candidates", []),
                           "how_to_decide": "add to data/identity_decisions.json: {source_key: canonical_id | 'new' | 'reject'}"})
    idx_path.write_text(json.dumps(new_index, indent=0, sort_keys=True))
    matches.sort(key=lambda m: (m["date"], m["match_id"]))
    return matches, review, stats


def corrections(prev_matches, matches, at):
    """Score and date changes for existing canonical matches (append to data/match_corrections.jsonl)."""
    prev = {m["match_id"]: m for m in prev_matches}
    out = []
    for m in matches:
        p = prev.get(m["match_id"])
        if not p:
            continue
        for f in ("a_score", "b_score", "date", "status"):
            if p.get(f) != m.get(f):
                out.append({"at": at, "match_id": m["match_id"], "field": f, "from": p.get(f), "to": m.get(f)})
    return out

"""Deduplication, corrections and conflicts (SYNTHETIC data)."""
import json

from helpers import obs, seq
from identities import Identities
from reconcile import ObservationStore, corrections, reconcile


def ids_with(tmp_path, *keys):
    ids = Identities(tmp_path / "aliases.json")
    for k, name in keys:
        ids.ensure_source_team(k, name, "GU12")
    ids.finalize_pending()
    return ids


A = ("ysg:1", "Testville SC 2015G Red")
B = ("ysg:2", "Sampleton FC 2015G Blue")


def test_same_game_on_two_team_pages_counts_once(tmp_path):
    ids = ids_with(tmp_path, A, B)
    rows = seq([obs("ysg", "ysg:1", "k1", "2026-09-13", A, B, 2, 1),
                obs("ysg", "ysg:2", "k2", "2026-09-13", B, A, 1, 2)])
    m, review, st = reconcile(rows, ids, tmp_path / "idx.json")
    assert len(m) == 1 and st["duplicates_merged"] == 1 and not review


def test_ncsl_and_external_copy_count_once(tmp_path):
    ids = Identities(tmp_path / "aliases.json")
    h = ids.register_ncsl("111", "ARL Synthetic Red", "GU12")
    a = ids.register_ncsl("222", "VSA Synthetic Blue", "GU12")
    ids.mappings["ysg:9"] = {"canonical_id": h, "status": "confirmed", "by": "manual"}
    ids.mappings["ysg:8"] = {"canonical_id": a, "status": "confirmed", "by": "manual"}
    rows = seq([obs("ncsl", "ncsl:div", "g1", "2026-09-20", ("ncsl:111", "x"), ("ncsl:222", "y"), 3, 1,
                    upstream="ncsl:g1", ctype="league", venue="home"),
                obs("ysg", "ysg:9", "y1", "2026-09-20", ("ysg:9", "x"), ("ysg:8", "y"), 3, 1)])
    m, review, st = reconcile(rows, ids, tmp_path / "idx.json")
    assert len(m) == 1 and m[0]["sources"] == ["ncsl", "ysg"] and m[0]["venue_type"] == "home"


def test_two_same_day_games_stay_separate(tmp_path):
    ids = ids_with(tmp_path, A, B)
    rows = seq([obs("ysg", "ysg:1", "k1", "2026-09-13", A, B, 2, 1),
                obs("ysg", "ysg:1", "k2", "2026-09-13", A, B, 0, 0),
                obs("ysg", "ysg:2", "k3", "2026-09-13", B, A, 1, 2),
                obs("ysg", "ysg:2", "k4", "2026-09-13", B, A, 0, 0)])
    m, review, st = reconcile(rows, ids, tmp_path / "idx.json")
    assert len(m) == 2 and st["duplicates_merged"] == 2 and not review
    assert sorted((x["a_score"], x["b_score"]) for x in m) in ([(0, 0), (2, 1)], [(0, 0), (1, 2)])


def test_conflicting_scores_go_to_review(tmp_path):
    ids = ids_with(tmp_path, A, B)
    rows = seq([obs("ysg", "ysg:1", "k1", "2026-09-13", A, B, 2, 1),
                obs("ysg", "ysg:2", "k2", "2026-09-13", B, A, 1, 3)])
    m, review, st = reconcile(rows, ids, tmp_path / "idx.json")
    assert len(m) == 1 and m[0]["status"] == "conflict"
    assert review[0]["type"] == "score_conflict" and not review[0]["resolved_by_authority"]


def test_authoritative_source_settles_conflict(tmp_path):
    ids = Identities(tmp_path / "aliases.json")
    h = ids.register_ncsl("111", "ARL Synthetic Red", "GU12"); a = ids.register_ncsl("222", "VSA Synthetic", "GU12")
    ids.mappings["ysg:9"] = {"canonical_id": h, "status": "confirmed", "by": "manual"}
    ids.mappings["ysg:8"] = {"canonical_id": a, "status": "confirmed", "by": "manual"}
    rows = seq([obs("ncsl", "ncsl:div", "g1", "2026-09-20", ("ncsl:111", "x"), ("ncsl:222", "y"), 3, 1, upstream="ncsl:g1"),
                obs("ysg", "ysg:9", "y1", "2026-09-20", ("ysg:9", "x"), ("ysg:8", "y"), 2, 1)])
    m, review, _ = reconcile(rows, ids, tmp_path / "idx.json")
    assert m[0]["status"] == "final" and (m[0]["a_score"], m[0]["b_score"]) == (3, 1)
    assert review[0]["resolved_by_authority"]


def test_score_correction_updates_same_match(tmp_path):
    ids = ids_with(tmp_path, A, B)
    store = ObservationStore(tmp_path / "obs.jsonl")
    store.upsert(seq([obs("ysg", "ysg:1", "k1", "2026-09-13", A, B, 2, 1)]), "ysg")
    m1, _, _ = reconcile(store.all(), ids, tmp_path / "idx.json")
    store.upsert(seq([obs("ysg", "ysg:1", "k1", "2026-09-13", A, B, 3, 1)]), "ysg")
    m2, _, _ = reconcile(store.all(), ids, tmp_path / "idx.json")
    assert m1[0]["match_id"] == m2[0]["match_id"] and m2[0]["a_score"] == 3
    c = corrections(m1, m2, "now")
    assert c and c[0]["field"] == "a_score" and (c[0]["from"], c[0]["to"]) == (2, 3)
    assert store.rows[m2[0]["observations"][0]]["score_history"][0]["a_score"] == 2


def test_match_ids_are_stable_and_scoreless(tmp_path):
    ids = ids_with(tmp_path, A, B)
    rows = seq([obs("ysg", "ysg:1", "k1", "2026-09-13", A, B, 2, 1)])
    m1, _, _ = reconcile(rows, ids, tmp_path / "idx.json")
    rows[0]["a_score"] = 5
    m2, _, _ = reconcile(rows, ids, tmp_path / "idx.json")
    assert m1[0]["match_id"] == m2[0]["match_id"]


def test_empty_pull_keeps_history(tmp_path):
    store = ObservationStore(tmp_path / "obs.jsonl")
    store.upsert(seq([obs("ysg", "ysg:1", "k1", "2026-09-13", A, B, 2, 1)]), "ysg")
    store.upsert([], "ysg")
    assert len(store.all()) == 1 and not store.all()[0]["missing_from_source"]


def test_stale_copy_of_a_game_is_dropped_when_the_page_is_reread(tmp_path):
    """A page re-read after a parser fix reports the same game with different text; the old
    row must not survive as a second game."""
    from reconcile import ObservationStore
    st = ObservationStore(tmp_path / "obs.jsonl")
    def ob(oid, comp):
        return {"obs_id": oid, "source": "ysg", "observer": "ysg:1", "date": "2026-10-04",
                "a": {"source_key": "ysg:1"}, "b": {"source_key": "ysg:2"}, "a_score": 1, "b_score": 0,
                "competition": comp, "retrieved_at": "x"}
    st.upsert([ob("old", "Win Prob | - NCSL Fall 2026"), ob("gone", "Other")], "ysg")
    st.rows["gone"]["date"] = "2026-09-01"
    st.upsert([ob("new", "NCSL Fall 2026")], "ysg")
    ids = {r["obs_id"] for r in st.all()}
    assert "new" in ids and "old" not in ids
    assert "gone" in ids            # a game that just dropped off the page is kept

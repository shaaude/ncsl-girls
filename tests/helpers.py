"""Builders for SYNTHETIC observations used by the tests. All values are invented."""
from sources import obs_id, assign_seq


def obs(source, observer, key, date, a, b, sa, sb, status="final", age="GU12", upstream=None,
        ctype="tournament", venue="neutral", comp="Synthetic Cup"):
    return {"obs_id": obs_id(source, key), "source": source, "observer": observer, "source_record_key": key,
            "upstream_id": upstream, "date": date, "time": None, "competition": comp, "competition_type": ctype,
            "venue_type": venue, "a": {"source_key": a[0], "name": a[1], "age": age},
            "b": {"source_key": b[0], "name": b[1], "age": age}, "a_score": sa, "b_score": sb, "status": status,
            "url": None, "retrieved_at": "2026-10-10T00:00:00Z"}


def seq(rows):
    return assign_seq(rows)

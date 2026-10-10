"""Tying an NCSL team's separate YSG records (tournament entries) to it by club, label and age."""
import match_records as mr
from identities import Identities


def _setup():
    teams = {
        "Tred": {"id": "Tred", "name": "ARL 2014/15G Red", "age": "GU13", "ncsl_key": "1", "ncsl_club": "c1"},
        "Tblk": {"id": "Tblk", "name": "ARL 2014/15G Black (U12)", "age": "GU12", "ncsl_key": "2", "ncsl_club": "c1"},
        "Tsyc": {"id": "Tsyc", "name": "SYC U12G Blue", "age": "GU12", "ncsl_key": "3", "ncsl_club": "c2"},
        "Tl11": {"id": "Tl11", "name": "LOUD U11G White", "age": "GU11", "ncsl_key": "4", "ncsl_club": "c3"},
        "Tl12": {"id": "Tl12", "name": "LOUD U12G White", "age": "GU12", "ncsl_key": "5", "ncsl_club": "c3"},
        "Tnp1": {"id": "Tnp1", "name": "NPSC United 2013 Girls", "age": "GU13", "ncsl_key": "6", "ncsl_club": "c4"},
        "Tnp2": {"id": "Tnp2", "name": "NPSC United 2014 Girls", "age": "GU12", "ncsl_key": "7", "ncsl_club": "c4"},
    }
    maps = {
        "ysg:100": {"canonical_id": "Tred", "status": "confirmed", "by": "auto-evidence", "club": "Arlington Soccer Association"},
        "ysg:200": {"canonical_id": "Tsyc", "status": "confirmed", "by": "auto-evidence",
                    "club": "Springfield South County Youth  - Travel"},
    }
    directory = {
        "100": {"name": "ARL Red", "club": "Arlington Soccer Association", "state": "VA", "ages": {"GU12"}},
        "101": {"name": "Arlington SA Red", "club": "Arlington Soccer Association", "state": "VA", "ages": {"GU12"}},
        "102": {"name": "Arlington SA Red", "club": "Arlington Soccer Association", "state": "VA", "ages": {"GU14"}},
        "103": {"name": "Arlington SA Black", "club": "Arlington Soccer Association", "state": "VA", "ages": {"GU12"}},
        "104": {"name": "Arlington SA Red", "club": "Arlington Soccer Association - Rec", "state": "VA", "ages": {"GU12"}},
        "200": {"name": "SYC Blue", "club": "Springfield South County Youth  - Travel", "state": "VA", "ages": {"GU12"}},
        "201": {"name": "SYC Blue", "club": "Springfield SYC Soccer", "state": "VA", "ages": {"GU12"}},
        "202": {"name": "SYC Blue II", "club": "Springfield SYC Soccer", "state": "VA", "ages": {"GU12"}},
        "300": {"name": "Loudoun White", "club": "Loudoun Soccer Club", "state": "VA", "ages": {"GU11"}},
        "400": {"name": "NPSC United Girls", "club": "Northern Piedmont Sports Club - Travel", "state": "VA",
                "ages": {"GU13"}},
        "500": {"name": "Arlington SA Red", "club": "Arlington Soccer Association", "state": "TX", "ages": {"GU12"}},
    }
    return {"teams": teams, "mappings": maps}, directory


def test_links_by_club_label_and_age():
    aliases, directory = _setup()
    out = mr.compute(aliases, {}, directory)
    links = out["links"]
    assert links["ysg:101"] == "Tred"            # tournament record of ARL Red (same YSG age as its league record)
    assert "ysg:102" not in links                 # different age
    assert links["ysg:103"] == "Tblk"            # club learned from a club-mate's linked record
    assert "ysg:104" not in links                 # rec program
    assert "ysg:500" not in links                 # out of area
    assert links["ysg:201"] == "Tsyc"            # club spelled differently on the tournament record
    assert "ysg:202" not in links                 # "Blue II" is a different squad
    assert links["ysg:300"] == "Tl11"            # U-age in the name pins the YSG age
    assert "ysg:100" not in links and "ysg:200" not in links      # already linked by evidence


def test_ambiguous_records_go_to_review():
    aliases, directory = _setup()
    out = mr.compute(aliases, {}, directory)
    keys = {r["ysg_key"] for r in out["review"]}
    assert "ysg:400" in keys and "ysg:400" not in out["links"]


def test_manual_decision_wins():
    aliases, directory = _setup()
    out = mr.compute(aliases, {"ysg:101": "reject"}, directory)
    assert "ysg:101" not in out["links"]


def test_ysg_age_from_birth_years():
    assert mr.ysg_age("ARL 2014/15G Red", "GU13") == {"GU12"}
    assert mr.ysg_age("ARL 2011/12G Black (U15)", "GU15") == {"GU15"}
    assert mr.ysg_age("MSI U16 (10/11) Girls Silver", "GU16") == {"GU16"}
    assert mr.ysg_age("LOUD U12G White", "GU12") == {"GU12"}
    assert mr.ysg_age("MCLN U18 Girls White", "GU19") == {"GU18", "GU19"}
    assert mr.label("Arlington SA Pre-Academy 2", "Arlington Soccer Association") == {"pre", "academy", "2"}


def test_apply_record_links_respects_evidence_and_manual(tmp_path):
    ids = Identities(tmp_path / "a.json")
    cid = ids.register_ncsl("1", "ARL 2014/15G Red", "GU13")
    other = ids.register_ncsl("2", "ARL 2014/15G Black", "GU13")
    ids.ensure_source_team("ysg:101", "Arlington SA Red", "GU12")
    ids.ensure_source_team("ysg:102", "ARL Black", "GU12")
    ids.ensure_source_team("ysg:103", "Something", "GU12")
    ids.finalize_pending(scored_keys={"ysg:101"})                  # 101 becomes its own squad (auto-new)
    placeholder = ids.mappings["ysg:101"]["canonical_id"]
    ids.mappings["ysg:102"].update(canonical_id=other, status="confirmed", by="auto-evidence")
    ids.mappings["ysg:103"].update(canonical_id=other, status="confirmed", by="manual")
    n = ids.apply_record_links({"ysg:101": cid, "ysg:102": cid, "ysg:103": cid, "ysg:999": cid})
    assert n == 1
    assert ids.mappings["ysg:101"]["canonical_id"] == cid and ids.mappings["ysg:101"]["by"] == "auto-label"
    assert placeholder not in ids.teams
    assert ids.mappings["ysg:102"]["canonical_id"] == other
    assert ids.mappings["ysg:103"]["canonical_id"] == other

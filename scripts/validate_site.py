"""Checks run before every deploy.

NCSL checks are hard failures (exit 1): the deploy is stopped and the live site stays as it was.
National checks are soft: if they fail, the previous committed national outputs are restored
and the NCSL site still deploys.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "site"
sys.path.insert(0, str(ROOT / "scripts"))
from config import SUPPORTED_AGES


def check_ncsl():
    d = json.loads((SITE / "data.json").read_text())
    reg = json.loads((ROOT / "data" / "registry.json").read_text())["divisions"]
    assert len(d["divisions"]) == len(reg) >= 31, "division count dropped"
    assert sum(len(v["games"]) for v in d["divisions"]) > 1000, "too few games"
    assert all(v["standings"] for v in d["divisions"]), "a division has no teams"
    assert (SITE / "index.html").stat().st_size > 20000


def check_national():
    ix = json.loads((SITE / "national" / "index.json").read_text())
    assert ix["cutoff"] == "2026-08-01"
    for age in SUPPORTED_AGES:
        ad = json.loads((SITE / "national" / f"{age}.json").read_text())
        for t in ad["teams"]:
            prof = json.loads((SITE / "national" / "teams" / f"{t['id']}.json").read_text())
            elig = [m for m in prof["matches"] if m["eligible"]]
            assert elig, f"{t['name']} rated without eligible games"
            assert all(m["date"] > ix["cutoff"] for m in elig), "pre-cutoff game counted"
            if t.get("national_rank"):
                assert ad["national_ranking_available"] and t["games"] >= ix["min_ranked_games"]


def main():
    try:
        check_ncsl()
    except Exception as e:
        print(f"::error::NCSL site check failed: {e}")
        return 1
    try:
        check_national()
        print("site checks passed")
    except Exception as e:
        print(f"::warning::national outputs failed checks ({e}); restoring the previous national outputs")
        subprocess.run(["git", "checkout", "--", "site/national"], cwd=ROOT)
    return 0


if __name__ == "__main__":
    sys.exit(main())

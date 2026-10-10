"""Automated YouthSoccerGames collector, DISABLED unless permission is on file.

It runs only when data/permissions.json contains
    {"youthsoccergames": {"granted": true, "evidence": "<link or note of written permission>",
                          "min_seconds_between_requests": 5, "modes": ["ncsl-connected"]}}
AND the site's robots.txt allows the request. Otherwise it exits without contacting the site.

Modes
  ncsl-connected  team pages for teams already linked to NCSL teams, then their opponents
  nationwide      state x age directory pages, then team pages (only if permission covers it)

Pages are saved to imports/ysg/auto/ and processed by build_national.py like hand-saved pages.
"""
import json
import sys
import time
import urllib.request
import urllib.robotparser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PERM = ROOT / "data" / "permissions.json"
OUT = ROOT / "imports" / "ysg" / "auto"
BASE = "https://youthsoccergames.com"
UA = "ncsl-girls-tables/1.0 (+https://github.com/shaaude/ncsl-girls)"


def permission():
    if not PERM.exists():
        return None
    p = json.loads(PERM.read_text()).get("youthsoccergames", {})
    return p if p.get("granted") and p.get("evidence") else None


def main():
    p = permission()
    if not p:
        print("YouthSoccerGames collection disabled: no written permission recorded in data/permissions.json.")
        return 0
    rp = urllib.robotparser.RobotFileParser(BASE + "/robots.txt")
    try:
        rp.read()
    except Exception as e:
        print(f"could not read robots.txt ({e}); not collecting")
        return 0
    delay = max(5, int(p.get("min_seconds_between_requests", 5)))
    aliases = json.loads((ROOT / "data" / "team_aliases.json").read_text()) if (ROOT / "data" / "team_aliases.json").exists() else {"mappings": {}}
    ids = sorted({k.split(":", 1)[1] for k, m in aliases["mappings"].items() if k.startswith("ysg:") and m["status"] == "confirmed"})
    OUT.mkdir(parents=True, exist_ok=True)
    fetched = 0
    for tid in ids[: int(p.get("max_pages_per_run", 300))]:
        url = f"{BASE}/team/{tid}"
        if not rp.can_fetch(UA, url):
            print(f"robots.txt disallows {url}; stopping")
            break
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                (OUT / f"{tid}.html").write_bytes(r.read())
                fetched += 1
        except Exception as e:
            print(f"failed {url}: {e}")
        time.sleep(delay)
    print(f"fetched {fetched} team pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Fetch NCSL Fall 2026 girls division pages from Demosphere.

  python scripts/fetch.py            # refresh divisions in data/registry.json
  python scripts/fetch.py --discover # rescan the ID range, rebuild the registry, then fetch
"""
import json, re, sys, time, urllib.request, urllib.error
from pathlib import Path

BASE = "https://elements.demosphere.com/80738/schedules/Fall2026/"
UA = "ncsl-girls-tables/0.2 (personal non-commercial standings site; github.com/shaaude/ncsl-girls)"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
REG = ROOT / "data" / "registry.json"
SCAN_RANGE = (116702700, 116703300)
DELAY = 0.3
NAME_RE = re.compile(r"\b(GU\d{1,2})\s+Division\s+([A-Za-z0-9]+)")
MONTH_RE = re.compile(r"Fall2026/(\d+)\.(\d{5,6})\.html")

def get(url, tries=3):
    for n in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (300, 404):
                return e.code, ""
            err = e.code
        except Exception as e:
            err = str(e)
        time.sleep(2 * (n + 1))
    return -1, str(err)

def discover():
    found = {}
    for did in range(*SCAN_RANGE):
        status, html = get(f"{BASE}{did}.html", tries=1)
        time.sleep(DELAY)
        if status != 200:
            continue
        m = NAME_RE.search(html)
        tg = re.search(r'data-tgkey="(\d+)"', html)
        if m and tg and tg.group(1) not in found:
            found[tg.group(1)] = {"id": tg.group(1), "name": f"{m.group(1)} Division {m.group(2)}",
                                  "age": m.group(1), "flight": m.group(2)}
            print("found", found[tg.group(1)], flush=True)
    reg = {"discovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "divisions": sorted(found.values(), key=lambda d: (int(d["age"][2:]), d["flight"]))}
    REG.write_text(json.dumps(reg, indent=1))
    return reg

def fetch_all(reg):
    RAW.mkdir(parents=True, exist_ok=True)
    report = {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "ok": [], "failed": []}
    for d in reg["divisions"]:
        did = d["id"]
        status, html = get(f"{BASE}{did}.html")
        time.sleep(DELAY)
        if status != 200 or "sch-main-gm" not in html and "GAME#" not in html:
            report["failed"].append({"id": did, "status": status})
            continue
        (RAW / f"{did}.html").write_text(html, encoding="utf-8")
        months = sorted({mm for i, mm in MONTH_RE.findall(html) if i == did})
        for mm in months:
            s2, h2 = get(f"{BASE}{did}.{mm}.html")
            time.sleep(DELAY)
            if s2 == 200:
                (RAW / f"{did}.{mm}.html").write_text(h2, encoding="utf-8")
            else:
                report["failed"].append({"id": f"{did}.{mm}", "status": s2})
        report["ok"].append(did)
    (ROOT / "data" / "fetch_report.json").write_text(json.dumps(report, indent=1))
    print(f"fetched {len(report['ok'])} divisions, {len(report['failed'])} failures")
    return report

if __name__ == "__main__":
    reg = discover() if "--discover" in sys.argv or not REG.exists() else json.loads(REG.read_text())
    fetch_all(reg)

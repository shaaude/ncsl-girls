"""Discover NCSL Fall 2026 divisions on Demosphere and save raw girls pages.

Stdlib only. Scans a range of division IDs, records every division found
(boys and girls), and saves raw HTML for girls divisions, including any
per-month pages a division links to.
"""
import json, re, sys, time, urllib.request, urllib.error
from pathlib import Path

BASE = "https://elements.demosphere.com/80738/schedules/Fall2026/"
UA = "ncsl-girls-tables/0.1 (personal non-commercial standings site)"
ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
START, END = int(sys.argv[1]) if len(sys.argv) > 1 else 116702700, int(sys.argv[2]) if len(sys.argv) > 2 else 116703300
DELAY = 0.3

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace") if e.fp else ""
    except Exception as e:
        return -1, str(e)

HEAD_RE = re.compile(r"\b([GB]U\d{1,2}\s+Division\s+[A-Za-z0-9]+)", re.I)
MONTH_RE = re.compile(r"Fall2026/(\d+)\.(\d{5,6})\.html")

def main():
    RAW.mkdir(parents=True, exist_ok=True)
    found, log = [], []
    for did in range(START, END + 1):
        status, html = get(f"{BASE}{did}.html")
        time.sleep(DELAY)
        if status != 200:
            if status not in (300, 404):
                log.append({"id": did, "status": status})
            continue
        m = HEAD_RE.search(html)
        name = re.sub(r"\s+", " ", m.group(1)).strip() if m else None
        months = sorted({mm for i, mm in MONTH_RE.findall(html) if i == str(did)})
        rec = {"id": did, "name": name, "months": months, "bytes": len(html)}
        found.append(rec)
        print(did, name, months, flush=True)
        if name and name.upper().startswith("GU"):
            (RAW / f"{did}.html").write_text(html, encoding="utf-8")
            for mm in months:
                s2, h2 = get(f"{BASE}{did}.{mm}.html")
                time.sleep(DELAY)
                if s2 == 200:
                    (RAW / f"{did}.{mm}.html").write_text(h2, encoding="utf-8")
    out = {"scanned": [START, END], "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "divisions": found, "errors": log}
    (ROOT / "data" / "divisions_scan.json").write_text(json.dumps(out, indent=1))
    print(f"found {len(found)} divisions, {sum(1 for f in found if (f['name'] or '').upper().startswith('GU'))} girls")

if __name__ == "__main__":
    main()

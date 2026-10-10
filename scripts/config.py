"""Shared settings for the NCSL and national pipelines."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")

# National results window: matches dated strictly AFTER this date are eligible.
NATIONAL_CUTOFF = date(2026, 8, 1)

# Fall 2026 season: age group = 2027 - birth year (U19 covers 2008 and older; there is no GU18).
SEASON_END_YEAR = 2027
SUPPORTED_AGES = ["GU9", "GU10", "GU11", "GU12", "GU13", "GU14", "GU15", "GU16", "GU17", "GU19"]

MIN_RANKED_GAMES = 8          # fully qualified national ranking
NATIONAL_RIDGE = 1.0
RECENCY_HALF_LIFE_DAYS = 90
MARGIN_CAP = 6                # goals beyond a 6-goal margin carry no extra signal
MAIN_NETWORK_MIN_SHARE = 0.5  # a connected network must hold half the rated teams to rank nationally

SOURCE_PRIORITY = {"ncsl": 0, "event": 1, "csv": 2, "ysg": 3}   # lower = more authoritative

STATE_REGION = {
    **{s: "Northeast" for s in "CT ME MA NH RI VT NJ NY PA".split()},
    **{s: "Mid-Atlantic" for s in "DE DC MD VA WV".split()},
    **{s: "Southeast" for s in "AL FL GA KY MS NC SC TN".split()},
    **{s: "Midwest" for s in "IL IN IA KS MI MN MO NE ND OH SD WI".split()},
    **{s: "South Central" for s in "AR LA OK TX".split()},
    **{s: "Mountain" for s in "AZ CO ID MT NV NM UT WY".split()},
    **{s: "Pacific" for s in "AK CA HI OR WA".split()},
}

def eastern_today():
    return datetime.now(EASTERN).date()

def age_from_birth_year(by):
    u = SEASON_END_YEAR - int(by)
    if u >= 18:
        u = 19
    return f"GU{u}" if f"GU{u}" in SUPPORTED_AGES else None

def normalize_age(label):
    """Accept 'GU12', 'U12', 'G12', 'u12-girls', '2015', '2014/15' etc. Returns a supported age or None."""
    import re
    s = str(label or "").upper()
    m = re.search(r"(?:^|[^0-9])(20\d\d)(?:\s*/\s*(?:20)?(\d\d))?", s)
    if m:
        by = int(m.group(1))
        if m.group(2):                      # '2014/15' teams play at the older year's age
            by = min(by, 2000 + int(m.group(2)))
        return age_from_birth_year(by)
    m = re.search(r"U\s*(\d{1,2})", s) or re.search(r"G(\d{1,2})\b", s)
    if m:
        u = int(m.group(1))
        u = 19 if u >= 18 else u
        return f"GU{u}" if f"GU{u}" in SUPPORTED_AGES else None
    return None

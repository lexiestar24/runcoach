"""Parse the half-marathon training plan (.ics) into structured workouts,
and match planned workouts against actual Garmin activities."""
import os
import re
import datetime as dt

ICS_PATH = os.environ.get(
    "RUNCOACH_ICS",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan.ics"),
)
MILE_M = 1609.344


def _unfold(text):
    # RFC5545 line folding: a newline followed by space/tab continues the previous line.
    return re.sub(r"\r?\n[ \t]", "", text)


def _unescape(v):
    return v.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").strip()


def _classify(summary, desc):
    # Classify from the WORKOUT text only. Every event ends with a boilerplate
    # "Prehab ... Effort guide: ... COMFORTABLY-HARD/TEMPO ..." footer, so we must
    # cut that off or everything looks like a tempo run.
    workout = re.split(r"prehab|effort guide", desc, flags=re.I)[0]
    s = (summary + " " + workout).lower()
    if "half marathon" in s or "13.1" in s:
        return "race"
    if "shakeout" in s:
        return "shakeout"
    if "long run" in s:
        return "long"
    # rep work in miles ("2 x 0.75 mi") = intervals; note "4 x 20-sec" strides won't match
    if "interval" in s or re.search(r"\d+\s*[x×]\s*[\d.]+\s*mi", s):
        return "interval"
    if "comfortably-hard" in s or "tempo" in s or "goal pace" in s:
        return "tempo"
    if "strides" in s:
        return "quality"  # easy + strides session
    return "easy"


def _planned_miles(summary, desc):
    # Prefer the description (fuller); fall back to summary. Grab the first "<n> mi/mile".
    for text in (desc, summary):
        m = re.search(r"(\d+\.?\d*)\s*(?:mi\b|mile)", text.lower())
        if m:
            return float(m.group(1))
    return None


def load_plan():
    """Return a list of workout dicts sorted by date."""
    with open(ICS_PATH, "r", encoding="utf-8") as f:
        raw = _unfold(f.read())

    workouts = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw, re.DOTALL):
        def field(name):
            m = re.search(rf"^{name}[:;][^\n]*", block, re.MULTILINE)
            if not m:
                return ""
            line = m.group(0)
            return _unescape(line.split(":", 1)[1]) if ":" in line else ""

        dtstart = field("DTSTART")
        summary = field("SUMMARY")
        desc = field("DESCRIPTION")
        if not dtstart:
            continue
        try:
            date = dt.datetime.strptime(dtstart[:8], "%Y%m%d").date()
        except ValueError:
            continue
        wk = re.search(r"[Ww]k\s*(\d+)", summary)
        workouts.append({
            "date": date.isoformat(),
            "weekday": date.strftime("%a"),
            "week": int(wk.group(1)) if wk else None,
            "summary": summary,
            "description": desc,
            "type": _classify(summary, desc),
            "planned_miles": _planned_miles(summary, desc),
        })
    workouts.sort(key=lambda w: w["date"])
    return workouts


def plan_with_actuals(conn):
    """Join each planned workout to the run activity on the same date (if any).

    A workout can be rescheduled to a different day (reschedules table); if so we
    look for the run on the new date instead of the originally planned one.
    """
    plan = load_plan()
    runs = {}
    for r in conn.execute(
        "SELECT * FROM activities WHERE type LIKE '%running%' ORDER BY start_local"
    ).fetchall():
        runs.setdefault(r["date"], dict(r))  # first run of the day

    conn.execute("CREATE TABLE IF NOT EXISTS reschedules (orig_date TEXT PRIMARY KEY, new_date TEXT, note TEXT)")
    resched = {row["orig_date"]: row["new_date"]
               for row in conn.execute("SELECT orig_date, new_date FROM reschedules").fetchall()}

    out = []
    for w in plan:
        match_date = resched.get(w["date"], w["date"])
        actual = runs.get(match_date)
        item = dict(w)
        if match_date != w["date"]:
            item["moved_to"] = match_date
        if actual:
            keys = actual.keys()
            item["actual"] = {
                "distance_mi": round((actual["distance_m"] or 0) / MILE_M, 2),
                "duration_min": round((actual["duration_s"] or 0) / 60, 1),
                "avg_hr": actual["avg_hr"],
                "max_hr": actual["max_hr"],
                "avg_pace_min_per_mi": round((actual["avg_pace_s_per_km"] or 0) * (MILE_M / 1000) / 60, 2)
                if actual["avg_pace_s_per_km"] else None,
                "avg_cadence": actual["avg_cadence"],
                "elevation_gain": actual["elevation_gain"],
                "vo2max": actual["vo2max"],
                "type": actual["type"],
                "notes": (actual["notes"] if "notes" in keys else None),
            }
            item["status"] = "done"
        else:
            item["status"] = "upcoming" if w["date"] >= dt.date.today().isoformat() else "missed"
        out.append(item)
    return out


if __name__ == "__main__":
    for w in load_plan()[:6]:
        print(w["date"], w["weekday"], "wk", w["week"], "|", w["type"], "|", w["planned_miles"], "mi |", w["summary"])

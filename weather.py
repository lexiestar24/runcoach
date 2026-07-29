"""Run conditions (temperature, humidity, dew point) for outdoor activities.

Garmin's own weather endpoint returns nulls for Lexie's device, so conditions are
backfilled from Open-Meteo's free archive API (no key required) and cached
per-activity in SQLite so each run is only ever fetched once.

Treadmill/indoor runs are recorded with indoor=1 and are never heat-adjusted --
comparing an air-conditioned treadmill mile to a 70F-dew-point outdoor mile is
the single easiest way to draw a wrong conclusion.
"""
import json
import datetime as dt
import urllib.request
import urllib.error

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# Fallback when an activity has no GPS fix but is still outdoors (St Paul, MN).
DEFAULT_LAT, DEFAULT_LON = 44.9537, -93.0900
TZ = "America/Chicago"

SCHEMA = """
CREATE TABLE IF NOT EXISTS conditions (
    activity_id   INTEGER PRIMARY KEY,
    date          TEXT,
    temp_f        REAL,
    humidity      REAL,
    dew_f         REAL,
    wind_mph      REAL,
    indoor        INTEGER DEFAULT 0,
    source        TEXT,
    updated_at    TEXT DEFAULT (datetime('now'))
);
"""

# Dew point is the best single predictor of how much humidity blunts a runner's
# ability to shed heat. Piecewise-linear bpm penalty at a fixed easy effort,
# anchored to the widely used runner dew-point comfort bands.
_DEW_CURVE = [(55, 0.0), (60, 1.5), (65, 3.5), (70, 6.5), (75, 10.0), (80, 14.0)]


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    conn.commit()


def _interp(x, pts):
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return 0.0


def hr_heat_offset(temp_f, dew_f):
    """Estimated bpm that heat/humidity adds to HR at a constant easy effort.

    Returns 0 when conditions are unknown or benign. This is an estimate from
    published dew-point guidance, not a measurement -- it is used to compare
    runs fairly, and every surfaced number says "est." for that reason.
    """
    if dew_f is None:
        return 0.0
    off = _interp(dew_f, _DEW_CURVE)
    if temp_f is not None and temp_f > 72:
        off += (temp_f - 72) * 0.12
    return round(off, 1)


def comfort_label(temp_f, dew_f):
    """Plain-language read on the conditions, from a runner's perspective."""
    if dew_f is None:
        return "unknown conditions"
    if dew_f < 55:
        band = "comfortable"
    elif dew_f < 60:
        band = "mildly humid"
    elif dew_f < 65:
        band = "humid, noticeable"
    elif dew_f < 70:
        band = "uncomfortably humid"
    elif dew_f < 75:
        band = "oppressive"
    else:
        band = "dangerously humid"
    if temp_f is not None:
        return f"{temp_f:.0f}F, dew point {dew_f:.0f}F -- {band}"
    return f"dew point {dew_f:.0f}F -- {band}"


def _fetch_hour(lat, lon, date, hour):
    url = (
        f"{ARCHIVE_URL}?latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={date}&end_date={date}"
        "&hourly=temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone={TZ}"
    )
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    h = data.get("hourly") or {}
    times = h.get("time") or []
    if not times:
        return None
    idx = min(max(hour, 0), len(times) - 1)

    def at(key):
        vals = h.get(key) or []
        return vals[idx] if idx < len(vals) else None

    return {
        "temp_f": at("temperature_2m"),
        "humidity": at("relative_humidity_2m"),
        "dew_f": at("dew_point_2m"),
        "wind_mph": at("wind_speed_10m"),
    }


def backfill(conn, limit=200, verbose=False):
    """Fetch and cache conditions for any run activity that lacks them."""
    ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(activities)")}
    latcol = "start_lat" if "start_lat" in cols else None
    rows = conn.execute(
        """SELECT a.activity_id, a.start_local, a.date, a.type
           FROM activities a LEFT JOIN conditions c ON c.activity_id = a.activity_id
           WHERE a.type LIKE '%running%' AND c.activity_id IS NULL
           ORDER BY a.start_local DESC LIMIT ?""",
        (limit,),
    ).fetchall()

    done = 0
    for r in rows:
        aid, start, date, typ = r["activity_id"], r["start_local"], r["date"], r["type"]
        indoor = "treadmill" in (typ or "").lower() or "indoor" in (typ or "").lower()
        if indoor:
            conn.execute(
                "INSERT OR REPLACE INTO conditions (activity_id,date,indoor,source) VALUES (?,?,1,'indoor')",
                (aid, date),
            )
            done += 1
            continue

        lat, lon = DEFAULT_LAT, DEFAULT_LON
        if latcol:
            g = conn.execute(
                "SELECT start_lat, start_lon FROM activities WHERE activity_id=?", (aid,)
            ).fetchone()
            if g and g["start_lat"] is not None:
                lat, lon = g["start_lat"], g["start_lon"]

        try:
            hour = int((start or "00 12").split(" ")[1].split(":")[0])
        except (IndexError, ValueError):
            hour = 12
        try:
            w = _fetch_hour(lat, lon, date, hour)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            if verbose:
                print(f"  conditions {date}: {type(e).__name__} {e}")
            continue  # stay offline-safe: no conditions is fine, wrong ones are not
        if not w:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO conditions
               (activity_id,date,temp_f,humidity,dew_f,wind_mph,indoor,source,updated_at)
               VALUES (?,?,?,?,?,?,0,'open-meteo',datetime('now'))""",
            (aid, date, w["temp_f"], w["humidity"], w["dew_f"], w["wind_mph"]),
        )
        done += 1
    conn.commit()
    return done


def by_activity(conn):
    """activity_id -> conditions dict (with derived heat offset + label)."""
    ensure_schema(conn)
    out = {}
    for r in conn.execute("SELECT * FROM conditions").fetchall():
        d = dict(r)
        if d.get("indoor"):
            d["hr_heat_offset"] = 0.0
            d["label"] = "treadmill (indoor, climate controlled)"
        else:
            d["hr_heat_offset"] = hr_heat_offset(d.get("temp_f"), d.get("dew_f"))
            d["label"] = comfort_label(d.get("temp_f"), d.get("dew_f"))
        out[d["activity_id"]] = d
    return out


if __name__ == "__main__":
    import db

    conn = db.connect()
    n = backfill(conn, verbose=True)
    print(f"Cached conditions for {n} activities.")
    for aid, c in sorted(by_activity(conn).items()):
        print(f"  {c['date']}  {c['label']}  (est +{c['hr_heat_offset']} bpm)")
    conn.close()

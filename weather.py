"""Run conditions (temperature, humidity, dew point) for outdoor activities.

Garmin's own weather endpoint returns nulls for Lexie's device, so conditions are
backfilled from Open-Meteo's free archive API (no key required) and cached
per-activity in SQLite so each run is only ever fetched once.

Conditions cover the WHOLE run, not just the start hour. A 9am start at 63F that
finishes at 11am in full sun at 75F is a different run from its first mile, so
the stored temp/dew/wind are time-weighted averages over the run, the hourly
series is kept (`hourly_json`) so a segment such as the last 2 miles can be
read on its own, and sunshine is folded in as an effective-temperature bump.

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

# Columns added after first release (see ensure_schema).
_EXTRA_COLS = {
    "solar_wm2": "REAL",      # mean shortwave radiation over the run
    "temp_end_f": "REAL",     # air temperature at the finish
    "hourly_json": "TEXT",    # [[epoch_s, temp_f, dew_f, solar_wm2, wind_mph, rh], ...]
    "start_epoch": "REAL",
    "end_epoch": "REAL",
}

# Full sun adds radiant heat load that air temperature misses: black-globe and
# WBGT readings in direct sun run several degrees above shade air temperature.
# Modelled as an effective-temperature bump of ~1F per 100 W/m2, capped at 8F
# (full midday sun is ~800-900 W/m2). An estimate, labelled as one.
SUN_F_PER_WM2 = 0.01
SUN_CAP_F = 8.0

# Dew point is the best single predictor of how much humidity blunts a runner's
# ability to shed heat. Piecewise-linear bpm penalty at a fixed easy effort,
# anchored to the widely used runner dew-point comfort bands.
_DEW_CURVE = [(55, 0.0), (60, 1.5), (65, 3.5), (70, 6.5), (75, 10.0), (80, 14.0)]


def ensure_schema(conn):
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(conditions)")}
    for c, typ in _EXTRA_COLS.items():
        if c not in cols:
            conn.execute(f"ALTER TABLE conditions ADD COLUMN {c} {typ}")
    conn.commit()


def sun_bump_f(solar_wm2):
    """Effective-temperature degrees added by sunshine (0 in shade or at night)."""
    if not solar_wm2 or solar_wm2 <= 0:
        return 0.0
    return min(solar_wm2 * SUN_F_PER_WM2, SUN_CAP_F)


def effective_temp_f(temp_f, solar_wm2=None):
    """Air temperature plus the radiant load of the sun."""
    if temp_f is None:
        return None
    return temp_f + sun_bump_f(solar_wm2)


def _interp(x, pts):
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return 0.0


def hr_heat_offset(temp_f, dew_f, solar_wm2=None):
    """Estimated bpm that heat/humidity adds to HR at a constant easy effort.

    Returns 0 when conditions are unknown or benign. This is an estimate from
    published dew-point guidance, not a measurement -- it is used to compare
    runs fairly, and every surfaced number says "est." for that reason.
    """
    if dew_f is None:
        return 0.0
    off = _interp(dew_f, _DEW_CURVE)
    eff = effective_temp_f(temp_f, solar_wm2)
    if eff is not None and eff > 72:
        off += (eff - 72) * 0.12
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


def _fetch_day(lat, lon, date):
    """Hourly series for one local day: [[epoch_s, temp, dew, solar, wind, rh], ...]."""
    url = (
        f"{ARCHIVE_URL}?latitude={lat:.4f}&longitude={lon:.4f}"
        f"&start_date={date}&end_date={date}"
        "&hourly=temperature_2m,relative_humidity_2m,dew_point_2m,wind_speed_10m,shortwave_radiation"
        f"&temperature_unit=fahrenheit&wind_speed_unit=mph&timezone={TZ}&timeformat=unixtime"
    )
    with urllib.request.urlopen(url, timeout=20) as r:
        data = json.load(r)
    h = data.get("hourly") or {}
    times = h.get("time") or []
    out = []
    for i, t in enumerate(times):
        def at(key):
            vals = h.get(key) or []
            return vals[i] if i < len(vals) else None
        if at("temperature_2m") is None:
            continue
        out.append([t, at("temperature_2m"), at("dew_point_2m"), at("shortwave_radiation"),
                    at("wind_speed_10m"), at("relative_humidity_2m")])
    return out


# Open-Meteo's radiation is the mean over the PRECEDING hour, so it is placed at
# the half hour; temperature, dew point and wind are instantaneous on the hour.
_SOLAR_SHIFT_S = -1800


def _sample(hourly, epoch):
    """Linearly interpolated conditions at one instant from the hourly series."""
    if not hourly:
        return None

    def interp(col, shift=0):
        pts = [(row[0] + shift, row[col]) for row in hourly if row[col] is not None]
        if not pts:
            return None
        return _interp(epoch, pts)

    return {"temp_f": interp(1), "dew_f": interp(2), "solar_wm2": interp(3, _SOLAR_SHIFT_S),
            "wind_mph": interp(4), "humidity": interp(5)}


def window(hourly, t0, t1, step_s=60):
    """Time-weighted average conditions between two instants, plus the heat offset.

    The heat offset is averaged per minute rather than computed from the averages,
    because the dew-point curve is non-linear: a run that starts mild and ends hot
    costs more than its average temperature suggests.
    """
    if not hourly or t0 is None:
        return None
    t1 = max(t1 or t0, t0)
    n = max(int((t1 - t0) // step_s), 0) + 1
    rows = [_sample(hourly, t0 + i * (t1 - t0) / max(n - 1, 1)) for i in range(n)]
    rows = [r for r in rows if r and r["temp_f"] is not None]
    if not rows:
        return None

    def mean(k):
        v = [r[k] for r in rows if r[k] is not None]
        return sum(v) / len(v) if v else None

    offs = [hr_heat_offset(r["temp_f"], r["dew_f"], r["solar_wm2"]) for r in rows]
    temp, solar = mean("temp_f"), mean("solar_wm2")
    return {
        "temp_f": round(temp, 1), "dew_f": round(mean("dew_f"), 1) if mean("dew_f") is not None else None,
        "solar_wm2": round(solar or 0.0), "wind_mph": mean("wind_mph"), "humidity": mean("humidity"),
        "temp_end_f": round(rows[-1]["temp_f"], 1),
        "eff_temp_f": round(effective_temp_f(temp, solar), 1),
        "eff_temp_end_f": round(effective_temp_f(rows[-1]["temp_f"], rows[-1]["solar_wm2"]), 1),
        "hr_heat_offset": round(sum(offs) / len(offs), 1),
    }


def _local_epoch(start_local):
    """'YYYY-MM-DD HH:MM:SS' in Central time -> epoch seconds."""
    from zoneinfo import ZoneInfo
    t = dt.datetime.strptime(start_local, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo(TZ))
    return t.timestamp()


def backfill(conn, limit=200, verbose=False):
    """Fetch and cache conditions for any run activity that lacks them."""
    ensure_schema(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(activities)")}
    latcol = "start_lat" if "start_lat" in cols else None
    # Rows cached before whole-run conditions existed (no hourly_json) are
    # refetched once so every run is judged on the same basis.
    rows = conn.execute(
        """SELECT a.activity_id, a.start_local, a.date, a.type, a.duration_s
           FROM activities a LEFT JOIN conditions c ON c.activity_id = a.activity_id
           WHERE a.type LIKE '%running%'
             AND (c.activity_id IS NULL OR (c.indoor = 0 AND c.hourly_json IS NULL))
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
            t0 = _local_epoch(start)
        except (TypeError, ValueError):
            continue
        t1 = t0 + (r["duration_s"] or 0)
        try:
            hourly = _fetch_day(lat, lon, date)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
            if verbose:
                print(f"  conditions {date}: {type(e).__name__} {e}")
            continue  # stay offline-safe: no conditions is fine, wrong ones are not
        w = window(hourly, t0, t1)
        if not w:
            continue
        conn.execute(
            """INSERT OR REPLACE INTO conditions
               (activity_id,date,temp_f,humidity,dew_f,wind_mph,indoor,source,updated_at,
                solar_wm2,temp_end_f,hourly_json,start_epoch,end_epoch)
               VALUES (?,?,?,?,?,?,0,'open-meteo',datetime('now'),?,?,?,?,?)""",
            (aid, date, w["temp_f"], w["humidity"], w["dew_f"], w["wind_mph"],
             w["solar_wm2"], w["temp_end_f"], json.dumps(hourly), t0, t1),
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
            hourly = json.loads(d["hourly_json"]) if d.get("hourly_json") else None
            w = window(hourly, d.get("start_epoch"), d.get("end_epoch")) if hourly else None
            if w:
                d["hr_heat_offset"] = w["hr_heat_offset"]
                d["eff_temp_f"] = w["eff_temp_f"]
                d["eff_temp_end_f"] = w["eff_temp_end_f"]
            else:
                d["hr_heat_offset"] = hr_heat_offset(d.get("temp_f"), d.get("dew_f"))
                d["eff_temp_f"] = d.get("temp_f")
            d["label"] = comfort_label(d.get("temp_f"), d.get("dew_f"))
            end = d.get("temp_end_f")
            if end is not None and d.get("temp_f") is not None and abs(end - d["temp_f"]) >= 5:
                d["label"] += f" (finished {end:.0f}F)"
            if (d.get("solar_wm2") or 0) >= 300:
                d["label"] += ", sunny"
        d["hourly"] = json.loads(d.pop("hourly_json")) if d.get("hourly_json") else None
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

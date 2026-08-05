"""Pull Garmin Connect data into SQLite.

Usage:
    python ingest.py            # incremental: last 3 days + recent activities
    python ingest.py --days 60  # backfill N days of daily/sleep history
"""
import os
import sys
import time
import argparse
import datetime as dt

from garminconnect import Garmin
import db


def _login():
    g = Garmin()
    g.login(os.path.expanduser("~/.garminconnect"))
    return g


def _num(v):
    return v if isinstance(v, (int, float)) else None


def upsert_daily(conn, date, stats):
    conn.execute(
        """INSERT INTO daily (date, resting_hr, min_hr, max_hr, avg_stress, rhr_7day,
                body_battery_low, body_battery_high, steps, total_kcal, active_kcal, resp_avg, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(date) DO UPDATE SET
                resting_hr=excluded.resting_hr, min_hr=excluded.min_hr, max_hr=excluded.max_hr,
                avg_stress=excluded.avg_stress, rhr_7day=excluded.rhr_7day,
                body_battery_low=excluded.body_battery_low, body_battery_high=excluded.body_battery_high,
                steps=excluded.steps, total_kcal=excluded.total_kcal, active_kcal=excluded.active_kcal,
                resp_avg=excluded.resp_avg, updated_at=datetime('now')""",
        (
            date,
            _num(stats.get("restingHeartRate")),
            _num(stats.get("minHeartRate")),
            _num(stats.get("maxHeartRate")),
            _num(stats.get("averageStressLevel")),
            _num(stats.get("lastSevenDaysAvgRestingHeartRate")),
            _num(stats.get("bodyBatteryLowestValue")),
            _num(stats.get("bodyBatteryHighestValue")),
            _num(stats.get("totalSteps")),
            _num(stats.get("totalKilocalories")),
            _num(stats.get("activeKilocalories")),
            _num(stats.get("avgWakingRespirationValue")),
        ),
    )


def _local_ts(ms):
    """Garmin's epoch-millisecond timestamps as local wall-clock time."""
    if not isinstance(ms, (int, float)):
        return None
    return dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def upsert_sleep(conn, date, dto):
    if not dto:
        return
    scores = dto.get("sleepScores") or {}
    overall = (scores.get("overall") or {}).get("value") if isinstance(scores, dict) else None
    conn.execute(
        """INSERT INTO sleep (date, total_seconds, deep_seconds, light_seconds, rem_seconds,
                awake_seconds, score, resp_avg, sleep_stress, sleep_start, sleep_end, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(date) DO UPDATE SET
                total_seconds=excluded.total_seconds, deep_seconds=excluded.deep_seconds,
                light_seconds=excluded.light_seconds, rem_seconds=excluded.rem_seconds,
                awake_seconds=excluded.awake_seconds, score=excluded.score,
                resp_avg=excluded.resp_avg, sleep_stress=excluded.sleep_stress,
                sleep_start=excluded.sleep_start, sleep_end=excluded.sleep_end,
                updated_at=datetime('now')""",
        (
            date,
            _num(dto.get("sleepTimeSeconds")),
            _num(dto.get("deepSleepSeconds")),
            _num(dto.get("lightSleepSeconds")),
            _num(dto.get("remSleepSeconds")),
            _num(dto.get("awakeSleepSeconds")),
            _num(overall),
            _num(dto.get("averageRespirationValue")),
            _num(dto.get("avgSleepStress")),
            _local_ts(dto.get("sleepStartTimestampGMT")),
            _local_ts(dto.get("sleepEndTimestampGMT")),
        ),
    )


def upsert_intraday(conn, payload):
    """Store the within-day Body Battery / stress curve from get_stress_data().

    One call returns both series on the same 3-minute timestamps. Garmin encodes
    "could not measure" as a negative stress value; that is stored as NULL rather
    than as a very calm minute. Rows are keyed by the sample's own local date, so
    a reading that lands either side of midnight is filed on the right day.
    """
    if not payload:
        return 0
    rows = {}
    for ts, val in (payload.get("stressValuesArray") or []):
        d = dt.datetime.fromtimestamp(ts / 1000)
        rows.setdefault((d.strftime("%Y-%m-%d"), d.hour * 60 + d.minute), [None, None])[1] = \
            (val if isinstance(val, (int, float)) and val >= 0 else None)
    for entry in (payload.get("bodyBatteryValuesArray") or []):
        # [timestamp, status, level, version]; older firmware omits the version
        if len(entry) < 3:
            continue
        ts, level = entry[0], entry[2]
        d = dt.datetime.fromtimestamp(ts / 1000)
        rows.setdefault((d.strftime("%Y-%m-%d"), d.hour * 60 + d.minute), [None, None])[0] = _num(level)

    conn.executemany(
        """INSERT INTO intraday (date, minute, body_battery, stress) VALUES (?,?,?,?)
           ON CONFLICT(date, minute) DO UPDATE SET
                body_battery=COALESCE(excluded.body_battery, body_battery),
                stress=COALESCE(excluded.stress, stress)""",
        [(d, m, v[0], v[1]) for (d, m), v in rows.items()],
    )
    return len(rows)


def sync_intraday(g, conn, dates):
    """Fetch the Body Battery / stress curve for each date. Never fatal."""
    total = 0
    for d in dates:
        try:
            total += upsert_intraday(conn, g.get_stress_data(d) or {})
        except Exception as e:
            print(f"  intraday {d}: {e}", file=sys.stderr)
    # 60 days is well past what any baseline looks at; drop the rest
    cutoff = (dt.date.today() - dt.timedelta(days=60)).isoformat()
    conn.execute("DELETE FROM intraday WHERE date < ?", (cutoff,))
    conn.commit()
    return total


def upsert_activity(conn, a):
    start = a.get("startTimeLocal")
    date = start.split(" ")[0] if start else None
    dist = _num(a.get("distance"))
    dur = _num(a.get("duration"))
    pace = (dur / (dist / 1000.0)) if dist and dur and dist > 0 else None
    conn.execute(
        """INSERT INTO activities (activity_id, start_local, date, type, name, distance_m, duration_s,
                moving_s, avg_hr, max_hr, avg_pace_s_per_km, calories, avg_cadence, elevation_gain, vo2max,
                hr_z1_s, hr_z2_s, hr_z3_s, hr_z4_s, hr_z5_s, notes, start_lat, start_lon, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(activity_id) DO UPDATE SET
                avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, vo2max=excluded.vo2max,
                distance_m=excluded.distance_m, duration_s=excluded.duration_s,
                notes=excluded.notes, start_lat=excluded.start_lat, start_lon=excluded.start_lon,
                updated_at=datetime('now')""",
        (
            a.get("activityId"),
            start,
            date,
            (a.get("activityType") or {}).get("typeKey"),
            a.get("activityName"),
            dist,
            dur,
            _num(a.get("movingDuration")),
            _num(a.get("averageHR")),
            _num(a.get("maxHR")),
            pace,
            _num(a.get("calories")),
            _num(a.get("averageRunningCadenceInStepsPerMinute")),
            _num(a.get("elevationGain")),
            _num(a.get("vO2MaxValue")),
            _num(a.get("hrTimeInZone_1")),
            _num(a.get("hrTimeInZone_2")),
            _num(a.get("hrTimeInZone_3")),
            _num(a.get("hrTimeInZone_4")),
            _num(a.get("hrTimeInZone_5")),
            (a.get("description") or None),
            _num(a.get("startLatitude")),
            _num(a.get("startLongitude")),
        ),
    )


def run_intraday(days=1):
    """Just today's Body Battery / stress curve: one API call, runs often.

    Readiness is scored for the moment you are about to run, so the curve has to
    be fresher than the 06:15 daily sync can keep it.
    """
    db.init_db()
    g = _login()
    conn = db.connect()
    today = dt.date.today()
    n = sync_intraday(g, conn, [(today - dt.timedelta(days=i)).isoformat() for i in range(days)])
    conn.close()
    print(f"Intraday sync: {n} samples over {days} day(s).")


def run(days=3, activities_count=30):
    db.init_db()
    g = _login()
    conn = db.connect()
    today = dt.date.today()

    # Daily stats + sleep for the last `days` days
    for i in range(days):
        d = (today - dt.timedelta(days=i)).isoformat()
        try:
            upsert_daily(conn, d, g.get_stats(d) or {})
        except Exception as e:
            print(f"  stats {d}: {e}", file=sys.stderr)
        try:
            sl = g.get_sleep_data(d) or {}
            upsert_sleep(conn, d, sl.get("dailySleepDTO") or {})
        except Exception as e:
            print(f"  sleep {d}: {e}", file=sys.stderr)
        conn.commit()
        if days > 5:
            time.sleep(0.6)  # be gentle on backfill

    # Within-day Body Battery / stress curves for the same span
    try:
        n = sync_intraday(g, conn, [(today - dt.timedelta(days=i)).isoformat() for i in range(days)])
        print(f"  intraday: {n} samples")
    except Exception as e:
        print(f"  intraday: {e}", file=sys.stderr)

    # Activities
    try:
        for a in g.get_activities(0, activities_count) or []:
            upsert_activity(conn, a)
        conn.commit()
    except Exception as e:
        print(f"  activities: {e}", file=sys.stderr)

    # Per-run weather (Garmin returns nulls for her device). Never fatal.
    try:
        import weather
        n = weather.backfill(conn)
        print(f"  conditions cached for {n} activities")
    except Exception as e:
        print(f"  conditions: {e}", file=sys.stderr)

    conn.close()
    print(f"Ingest complete: {days} days of daily/sleep, {activities_count} recent activities.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=None)
    ap.add_argument("--activities", type=int, default=30)
    ap.add_argument("--intraday", action="store_true",
                    help="only refresh the Body Battery / stress curve (cheap, runs often)")
    args = ap.parse_args()
    if args.intraday:
        # today only unless asked otherwise: this runs every 20 minutes
        run_intraday(days=args.days or 1)
    else:
        run(days=args.days or 3, activities_count=args.activities)

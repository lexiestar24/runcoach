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


def upsert_sleep(conn, date, dto):
    if not dto:
        return
    scores = dto.get("sleepScores") or {}
    overall = (scores.get("overall") or {}).get("value") if isinstance(scores, dict) else None
    conn.execute(
        """INSERT INTO sleep (date, total_seconds, deep_seconds, light_seconds, rem_seconds,
                awake_seconds, score, resp_avg, sleep_stress, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(date) DO UPDATE SET
                total_seconds=excluded.total_seconds, deep_seconds=excluded.deep_seconds,
                light_seconds=excluded.light_seconds, rem_seconds=excluded.rem_seconds,
                awake_seconds=excluded.awake_seconds, score=excluded.score,
                resp_avg=excluded.resp_avg, sleep_stress=excluded.sleep_stress, updated_at=datetime('now')""",
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
        ),
    )


def upsert_activity(conn, a):
    start = a.get("startTimeLocal")
    date = start.split(" ")[0] if start else None
    dist = _num(a.get("distance"))
    dur = _num(a.get("duration"))
    pace = (dur / (dist / 1000.0)) if dist and dur and dist > 0 else None
    conn.execute(
        """INSERT INTO activities (activity_id, start_local, date, type, name, distance_m, duration_s,
                moving_s, avg_hr, max_hr, avg_pace_s_per_km, calories, avg_cadence, elevation_gain, vo2max,
                hr_z1_s, hr_z2_s, hr_z3_s, hr_z4_s, hr_z5_s, notes, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, datetime('now'))
           ON CONFLICT(activity_id) DO UPDATE SET
                avg_hr=excluded.avg_hr, max_hr=excluded.max_hr, vo2max=excluded.vo2max,
                distance_m=excluded.distance_m, duration_s=excluded.duration_s,
                notes=excluded.notes, updated_at=datetime('now')""",
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
        ),
    )


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

    # Activities
    try:
        for a in g.get_activities(0, activities_count) or []:
            upsert_activity(conn, a)
        conn.commit()
    except Exception as e:
        print(f"  activities: {e}", file=sys.stderr)

    conn.close()
    print(f"Ingest complete: {days} days of daily/sleep, {activities_count} recent activities.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--activities", type=int, default=30)
    args = ap.parse_args()
    run(days=args.days, activities_count=args.activities)

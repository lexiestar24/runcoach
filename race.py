"""Race day: goal-pace projection, the pacing plan, and the race-pace sessions.

The projection is built from the runner's OWN long runs rather than a lookup table,
because every long run in the block was run in Minnesota summer air at 9am and
the race is a 7am November gun in Madison. Comparing an August 12:30 to a
November 12:30 without removing the weather is the single easiest way to talk
yourself into the wrong race pace, in either direction.

Everything here is an estimate and is labelled as one. It gets less of an
estimate every time a goal-race-pace session is logged: once those exist they
are used as direct evidence and the modelled credits are stood down (see
`projection`). Before Oct 10 this is a model. After Oct 10 it is a measurement.
"""
import datetime as dt
import statistics

import weather

MILE_M = 1609.344
RACE_DATE = dt.date(2026, 11, 15)
RACE_DISTANCE_MI = 13.1
TRAINING_START = "2026-06-22"

# Race-day assumptions for Madison, 7:00 AM gun in mid-November. Climate normal,
# not a forecast -- swapped for the real forecast inside the last ~10 days.
RACE_TEMP_F = 35.0
RACE_DEW_F = 28.0

# Effort targets. Her recalibrated zones: easy 155-162, long run 152-158,
# tempo 170-178. Race effort for a ~2.5h first half sits between long run and
# tempo, and nowhere near tempo -- tempo is by definition a 40-60 min ceiling.
RACE_HR_LOW, RACE_HR_HIGH = 160, 168
RACE_HR_MID = 164

# Fallback when there is not enough data to fit her own slope. Published easy-pace
# guidance puts a bpm at roughly 2-3 sec/mi; her own fit lands at ~2.5.
DEFAULT_SEC_PER_BPM = 2.5
SLOPE_BOUNDS = (1.5, 4.0)

COOL_OPTIMUM_F = 50.0


def _pace_min_per_mi(row):
    if not row["distance_m"] or not row["duration_s"]:
        return None
    return (row["duration_s"] / 60.0) / (row["distance_m"] / MILE_M)


def temp_pace_penalty_pct(temp_f):
    """Percent slower than optimum for a 2h+ effort, from air temperature alone.

    Kept separate from weather.hr_heat_offset, which is dew-point driven and only
    adds a temperature term above 72F. Her long runs sit at 65-72F, where the
    humidity cost is captured but the plain "it is 30 degrees warmer than ideal"
    cost is not. Below the optimum a little is given back for clothing and cold legs.
    """
    if temp_f is None:
        return 0.0
    if temp_f >= COOL_OPTIMUM_F:
        return min(0.10 * (temp_f - COOL_OPTIMUM_F), 8.0)
    return min(0.04 * (COOL_OPTIMUM_F - temp_f), 1.5)


def hr_pace_slope(conn):
    """Seconds per mile that one bpm is worth for her, fitted from her own runs.

    Regresses pace against heat-adjusted average HR over outdoor runs of 2.5 mi or
    more (shorter runs are dominated by the warm-up and skew the fit). Clamped to a
    plausible range so a thin or noisy sample can never produce a silly projection.
    """
    cond = weather.by_activity(conn)
    pts = []
    for r in conn.execute(
        "SELECT * FROM activities WHERE type LIKE '%running%' AND date >= ? ORDER BY date",
        (TRAINING_START,),
    ).fetchall():
        c = cond.get(r["activity_id"], {})
        pace = _pace_min_per_mi(r)
        if c.get("indoor") or not r["avg_hr"] or pace is None:
            continue
        if (r["distance_m"] or 0) / MILE_M < 2.5:
            continue
        pts.append((r["avg_hr"] - (c.get("hr_heat_offset") or 0.0), pace))

    if len(pts) < 6:
        return DEFAULT_SEC_PER_BPM, len(pts), False
    mx = statistics.mean(p[0] for p in pts)
    my = statistics.mean(p[1] for p in pts)
    denom = sum((p[0] - mx) ** 2 for p in pts)
    if denom == 0:
        return DEFAULT_SEC_PER_BPM, len(pts), False
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / denom   # min/mi per bpm
    sec = abs(slope) * 60
    fitted = SLOPE_BOUNDS[0] <= sec <= SLOPE_BOUNDS[1]
    return (round(sec, 2) if fitted else DEFAULT_SEC_PER_BPM), len(pts), fitted


def recent_long_runs(conn, n=5, min_mi=4.5):
    """Her last few real long runs, with conditions attached."""
    cond = weather.by_activity(conn)
    out = []
    for r in conn.execute(
        "SELECT * FROM activities WHERE type LIKE '%running%' AND date >= ? ORDER BY date DESC",
        (TRAINING_START,),
    ).fetchall():
        if (r["distance_m"] or 0) / MILE_M < min_mi:
            continue
        c = cond.get(r["activity_id"], {})
        pace = _pace_min_per_mi(r)
        if pace is None:
            continue
        out.append({
            "date": r["date"],
            "distance_mi": round((r["distance_m"] or 0) / MILE_M, 2),
            "pace": pace,
            "avg_hr": r["avg_hr"],
            "temp_f": c.get("temp_f"),
            "dew_f": c.get("dew_f"),
            "indoor": bool(c.get("indoor")),
            "heat_bpm": c.get("hr_heat_offset") or 0.0,
        })
        if len(out) >= n:
            break
    return list(reversed(out))


def _grp_evidence(conn, plan_workouts):
    """Actuals from the long runs that carried a goal-race-pace segment.

    Once these exist they beat any model, so `projection` hands over to them.
    Matched off the plan rather than a date list so editing the .ics is enough.
    """
    out = []
    for w in plan_workouts or []:
        s = (w.get("summary") or "") + " " + (w.get("description") or "")
        if "goal race pace" not in s.lower() or not w.get("actual"):
            continue
        a = w["actual"]
        out.append({
            "date": w["date"], "summary": w["summary"],
            "distance_mi": a.get("distance_mi"), "avg_hr": a.get("avg_hr"),
            "pace": a.get("avg_pace_min_per_mi"),
        })
    return out


def _fmt_pace(p):
    if p is None:
        return "--"
    m = int(p)
    s = int(round((p - m) * 60))
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}"


def _fmt_time(total_min):
    h = int(total_min // 60)
    m = int(round(total_min - h * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{h}:{m:02d}"


def projection(conn, plan_workouts=None, today=None):
    """Goal race pace, as a band, with every credit shown separately.

    Model: take the median pace of her recent long runs, then add back what the
    conditions and the calendar are holding down. Credits are deliberately
    conservative -- the failure mode that ruins a first half is a projection that
    is too fast, believed at mile 2.
    """
    today = today or dt.date.today()
    weeks_out = max((RACE_DATE - today).days, 0) / 7.0
    sec_per_bpm, n_pts, fitted = hr_pace_slope(conn)
    longs = [l for l in recent_long_runs(conn) if not l["indoor"]]

    grp = _grp_evidence(conn, plan_workouts)
    if not longs:
        return {"available": False, "reason": "No outdoor long runs logged yet."}

    base = statistics.median([l["pace"] for l in longs])
    base_hr = statistics.median([l["avg_hr"] for l in longs if l["avg_hr"]] or [158])
    base_temp = statistics.median([l["temp_f"] for l in longs if l["temp_f"]] or [68])
    base_heat = statistics.median([l["heat_bpm"] for l in longs])

    credits = []

    # 1. Humidity. Her long runs carry a dew-point HR tax; race morning carries none.
    humid = max(base_heat - weather.hr_heat_offset(RACE_TEMP_F, RACE_DEW_F), 0) * sec_per_bpm
    credits.append(("Cool, dry air (vs summer dew point)", humid,
                    f"long runs cost ~{base_heat:.1f} bpm to humidity; race morning ~0"))

    # 2. Air temperature, separately from humidity (see temp_pace_penalty_pct).
    tcredit = (temp_pace_penalty_pct(base_temp) - temp_pace_penalty_pct(RACE_TEMP_F)) / 100.0 * base * 60
    credits.append(("Racing at ~35F instead of ~%.0fF" % base_temp, max(tcredit, 0),
                    "air temperature alone, on top of the humidity credit"))

    # 3. Taper. She has never once run rested; every long run sits on training legs.
    credits.append(("Taper (you have never run on fresh legs)", 12.0,
                    "two weeks of reduced volume before the gun"))

    # 4. Remaining aerobic development, decaying to nothing on race day.
    fitness = min(2.0 * weeks_out, 25.0)
    credits.append((f"{weeks_out:.0f} more weeks of base building", fitness,
                    "long run goes to 12 mi; conservative 2 sec/mi per week remaining"))

    # 5. Race effort sits above long-run effort. This is the only credit she
    #    controls on the day, and the only one she can overspend.
    effort = max(RACE_HR_MID - base_hr, 0) * sec_per_bpm
    credits.append((f"Race effort (HR ~{RACE_HR_MID}) vs long-run effort (HR ~{base_hr:.0f})", effort,
                    "the only credit you spend on the day, and the only one you can overspend"))

    total = sum(c[1] for c in credits)
    central = base - total / 60.0

    if grp:
        # Direct evidence beats the model. Race pace is what she actually held for
        # a goal-pace segment on tired legs, minus only the credits still ahead of
        # her (taper, and whatever base building is left).
        held = statistics.median([g["pace"] for g in grp if g["pace"]] or [central])
        remaining = 12.0 + min(2.0 * weeks_out, 25.0)
        central = held - remaining / 60.0
        basis = (f"Measured: {len(grp)} goal-race-pace session(s) logged, median "
                 f"{_fmt_pace(held)}/mi. Model stood down in favour of real data.")
    else:
        basis = (f"Modelled from {len(longs)} recent long runs (median {_fmt_pace(base)}/mi "
                 f"at HR {base_hr:.0f}, {base_temp:.0f}F). No goal-race-pace session logged yet.")

    lo, hi = central - 20 / 60.0, central + 20 / 60.0
    return {
        "available": True,
        "basis": basis,
        "measured": bool(grp),
        "baseline_pace": round(base, 2),
        "baseline_pace_str": _fmt_pace(base),
        "baseline_hr": round(base_hr),
        "baseline_temp_f": round(base_temp),
        "sec_per_bpm": sec_per_bpm,
        "slope_fitted": fitted,
        "slope_n": n_pts,
        "weeks_out": round(weeks_out, 1),
        "credits": [{"label": l, "sec_per_mi": round(v), "why": w} for l, v, w in credits],
        "total_credit_sec": round(total),
        "central_pace": round(central, 2),
        "central_pace_str": _fmt_pace(central),
        "band_str": f"{_fmt_pace(lo)}-{_fmt_pace(hi)}",
        "finish_central": _fmt_time(central * RACE_DISTANCE_MI),
        "finish_band": f"{_fmt_time(lo * RACE_DISTANCE_MI)}-{_fmt_time(hi * RACE_DISTANCE_MI)}",
        "ceiling_first_4mi": _fmt_pace(central + 40 / 60.0),
        "grp_sessions": grp,
    }


def phases(proj):
    """The four-phase race plan. Pace is a CEILING early, an outcome late."""
    c = proj.get("central_pace") if proj.get("available") else None
    def ceil(extra):
        return f"no faster than {_fmt_pace(c + extra / 60.0)}/mi" if c else "hold back"
    return [
        {"miles": "1-4", "hr": "155-162", "pace": ceil(40),
         "job": "Sit on your hands. Should feel almost too easy.",
         "talk": "Full sentences"},
        {"miles": "5-9", "hr": "158-165", "pace": f"settle to ~{_fmt_pace(c)}/mi" if c else "settle",
         "job": "Settle in. Let the pace come to you.",
         "talk": "Short sentences"},
        {"miles": "10-12", "hr": "163-170", "pace": "whatever the effort gives",
         "job": "Now you are allowed to work.",
         "talk": "A few words"},
        {"miles": "13", "hr": "whatever is left", "pace": "empty it",
         "job": "Enjoy this part. You earned it.",
         "talk": "Nothing"},
    ]


def race_day_notes():
    """Fixed race-day execution notes. These do not move with the data."""
    return [
        {"kind": "warn", "title": "The one mistake that costs you the day",
         "text": "Going out at 10:30/mi because it is cold and there are 3000 people around you. "
                 "A first mile 30 sec too SLOW costs you 30 seconds. A first mile 30 sec too FAST "
                 "costs you five minutes."},
        {"kind": "good", "title": "Run/walk, planned, from mile 1",
         "text": "Walk 30-45 sec at every aid station (roughly every 1.5-2 mi). You drink properly, "
                 "HR drops 8-10 beats, and you rejoin at the same pace instead of decaying. Planned "
                 "walking is a strategy; walking at mile 10 because you have to is a rescue. "
                 "You already know this works."},
        {"kind": "info", "title": "Fuel",
         "text": "Gel or chews every ~40 min starting around mile 3-4, so roughly miles 4, 8 and 11. "
                 "Sip at every station. Nothing on race day that you have not used on a long run."},
        {"kind": "info", "title": "Cold start, likely 30-40F",
         "text": "Dress for 15-20F warmer than it is; you will be hot by mile 2. Throwaway layer to "
                 "the start line, gloves and a hat you can ditch. Do not overdress."},
        {"kind": "info", "title": "Logistics",
         "text": "Gun time 7:00 AM, Madison WI. Alarm 5:00 AM. Wk21 Sat is a 1.5 mi shakeout the day "
                 "before, and Wk21 Thu is 2 mi easy + 4 strides."},
    ]


def summary(conn, plan_workouts=None):
    proj = projection(conn, plan_workouts)
    return {
        "race_date": RACE_DATE.isoformat(),
        "days_out": (RACE_DATE - dt.date.today()).days,
        "gun_time": "7:00 AM",
        "where": "Madison, WI",
        "projection": proj,
        "phases": phases(proj),
        "notes": race_day_notes(),
        "hr_target": {"low": RACE_HR_LOW, "high": RACE_HR_HIGH, "mid": RACE_HR_MID},
    }


if __name__ == "__main__":
    import json, db
    conn = db.connect()
    import plan as planmod
    print(json.dumps(summary(conn, planmod.plan_with_actuals(conn)), indent=2))
    conn.close()

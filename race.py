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
import re
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

# The race is run in phases, not at one pace: easy start, settle, work, empty it.
# (label, miles, HR band, HR the phase is run at). Each phase's pace is the race-
# effort pace moved by (RACE_HR_MID - phase HR) x her own sec-per-bpm slope.
PHASES = [
    ("1-4", 4.0, "155-162", 158.5),
    ("5-9", 5.0, "158-165", 161.5),
    ("10-12", 3.0, "163-170", 166.5),
    ("13", 1.1, "whatever is left", 172.0),
]

# Planned walk breaks at aid stations, roughly every 1.75 mi through mile 12
# (none in the last mile). Walking still covers ground, so the cost is only the
# time beyond what running that stretch would have taken.
WALK_EVERY_MI = 1.75
WALK_S = 37.5                 # 30-45 sec
WALK_PACE_MIN = 18.0          # brisk walk, min/mi

# Miles past her longest training run are new ground; assume they cost a little
# (no data on her late-race fade yet, so this is a conservative guess).
FADE_SEC_PER_MI = 20.0

# The Oct 12-21 shin break and the shortened return (long run tops out at 8-10
# mi, not 12) mean the last weeks hold fitness rather than build it.
BASE_BUILDING_SEC_PER_WEEK = 0.0
BASE_BUILDING_WHY = "shin break: these weeks hold fitness, they don't add to it"

# Nobody runs the measured line: weaving and wide corners put ~0.8% on the
# distance actually covered, and the clock times that distance.
COURSE_EXTRA = 0.008


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


_SEG_RE = re.compile(r"(last\s+)?(\d+(?:\.\d+)?)\s*(?:mi\s+)?(EASY|at GOAL RACE PACE)", re.I)


def grp_window(text, total_mi):
    """(start_mi, end_mi) of the goal-race-pace block, read from the plan text.

    Handles "8 EASY + last 2 at GOAL RACE PACE" (anchored to where she actually
    finished, so a long or short run still reads its real last 2 miles) and
    "4 EASY + 4 at GOAL RACE PACE + 4 EASY" (race pace in the middle).
    """
    at = 0.0
    for last, n, kind in _SEG_RE.findall(text or ""):
        n = float(n)
        if "race" in kind.lower():
            if last or not total_mi:
                return (max((total_mi or n) - n, 0.0), total_mi or n)
            return (at, at + n)
        at += n
    return None


def _segment(conn, activity_id, start_mi, end_mi):
    """Pace / HR / timing over [start_mi, end_mi], built from the run's laps.

    A lap belongs to the block when its midpoint falls inside it, so mile auto-laps
    and a manual lap press at the start of the block both work.
    """
    laps = conn.execute("SELECT * FROM laps WHERE activity_id=? ORDER BY lap_index",
                        (activity_id,)).fetchall()
    cum, picked = 0.0, []
    for l in laps:
        d = (l["distance_m"] or 0) / MILE_M
        mid = cum + d / 2
        if start_mi - 0.05 <= mid <= end_mi + 0.05 and d > 0:
            picked.append(l)
        cum += d
    if not picked:
        return None
    dist = sum(l["distance_m"] for l in picked) / MILE_M
    dur = sum(l["duration_s"] for l in picked)
    hr_t = [(l["avg_hr"], l["duration_s"]) for l in picked if l["avg_hr"]]
    hr = sum(h * t for h, t in hr_t) / sum(t for _, t in hr_t) if hr_t else None
    return {
        "distance_mi": round(dist, 2), "pace": (dur / 60.0) / dist if dist else None,
        "avg_hr": round(hr, 1) if hr else None, "max_hr": max((l["max_hr"] or 0) for l in picked) or None,
        "t0": picked[0]["start_offset_s"], "t1": picked[-1]["start_offset_s"] + picked[-1]["duration_s"],
        "laps": [l["lap_index"] for l in picked],
    }


def _grp_evidence(conn, plan_workouts):
    """Actuals from the goal-race-pace BLOCK of each long run that carried one.

    Only the race-pace miles count: averaging in 8 easy miles would make race pace
    look like long-run pace. Conditions are read for the block itself, from the
    run's hourly weather, because a 9am start and an 11am finish in full sun are
    not the same air. Falls back to the whole run (flagged) when laps are missing.
    Matched off the plan rather than a date list so editing the .ics is enough.
    """
    cond = weather.by_activity(conn)
    out = []
    for w in plan_workouts or []:
        s = (w.get("summary") or "") + " " + (w.get("description") or "")
        if "goal race pace" not in s.lower() or not w.get("actual"):
            continue
        a = w["actual"]
        c = cond.get(a.get("activity_id")) or {}
        win = grp_window(w.get("description") or w.get("summary"), a.get("distance_mi"))
        seg = _segment(conn, a["activity_id"], *win) if win else None
        g = {"date": w["date"], "summary": w["summary"],
             "distance_mi": a.get("distance_mi"), "run_pace": a.get("avg_pace_min_per_mi"),
             "run_hr": a.get("avg_hr"), "segment": bool(seg)}
        if seg:
            t0 = (c.get("start_epoch") or 0) + seg["t0"] if c.get("start_epoch") else None
            t1 = (c.get("start_epoch") or 0) + seg["t1"] if c.get("start_epoch") else None
            wx = weather.window(c.get("hourly"), t0, t1) if (c.get("hourly") and t0) else None
            g.update({"grp_mi": seg["distance_mi"], "pace": seg["pace"], "avg_hr": seg["avg_hr"],
                      "max_hr": seg["max_hr"], "laps": seg["laps"],
                      "window": [round(win[0], 2), round(win[1], 2)]})
        else:
            wx = None
            g.update({"grp_mi": a.get("distance_mi"), "pace": a.get("avg_pace_min_per_mi"),
                      "avg_hr": a.get("avg_hr")})
        if wx:
            g.update({"temp_f": wx["temp_f"], "dew_f": wx["dew_f"], "solar_wm2": wx["solar_wm2"],
                      "eff_temp_f": wx["eff_temp_f"]})
        elif c and not c.get("indoor"):
            g.update({"temp_f": c.get("temp_f"), "dew_f": c.get("dew_f"),
                      "solar_wm2": c.get("solar_wm2"), "eff_temp_f": c.get("eff_temp_f", c.get("temp_f"))})
        g["pace_str"] = _fmt_pace(g.get("pace"))
        out.append(g)
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
    fitness = min(BASE_BUILDING_SEC_PER_WEEK * weeks_out, 25.0)
    credits.append(("No more base building", fitness, BASE_BUILDING_WHY))

    # 5. Race effort sits above long-run effort. This is the only credit she
    #    controls on the day, and the only one she can overspend.
    effort = max(RACE_HR_MID - base_hr, 0) * sec_per_bpm
    credits.append((f"Race effort (HR ~{RACE_HR_MID}) vs long-run effort (HR ~{base_hr:.0f})", effort,
                    "the only credit you spend on the day, and the only one you can overspend"))

    total = sum(c[1] for c in credits)
    central = base - total / 60.0

    measured_base = None
    if grp:
        # Direct evidence beats the model. Start from what she actually held over
        # the race-pace miles, then correct only for what differs on race day:
        # the air during those miles (heat + sun) vs a cold November morning, the
        # effort she ran them at vs race effort, and the credits still ahead of her.
        held = statistics.median([g["pace"] for g in grp if g.get("pace")] or [central])
        hr = statistics.median([g["avg_hr"] for g in grp if g.get("avg_hr")] or [RACE_HR_MID])
        eff = statistics.median([g["eff_temp_f"] for g in grp if g.get("eff_temp_f") is not None] or [base_temp])
        dew = statistics.median([g["dew_f"] for g in grp if g.get("dew_f") is not None] or [RACE_DEW_F])
        segs = sum(1 for g in grp if g.get("segment"))
        credits = []
        # Air temperature with the sun folded in (pace cost), and humidity alone
        # (HR cost) so the temperature is not counted twice.
        tcredit = (temp_pace_penalty_pct(eff) - temp_pace_penalty_pct(RACE_TEMP_F)) / 100.0 * held * 60
        credits.append((f"Racing at ~35F instead of ~{eff:.0f}F (air + sun) during those miles", max(tcredit, 0),
                        "heat for the race-pace miles themselves, not the start of the run"))
        humid = max(weather.hr_heat_offset(None, dew) - weather.hr_heat_offset(None, RACE_DEW_F), 0) * sec_per_bpm
        credits.append(("Humidity", humid, f"dew point ~{dew:.0f}F during the race-pace miles"))
        effort = (RACE_HR_MID - hr) * sec_per_bpm
        credits.append((f"Race effort (HR ~{RACE_HR_MID}) vs what you ran them at (HR ~{hr:.0f})", effort,
                        "ran above race effort, so this one adds time back" if effort < 0 else
                        "room left between what you ran and race effort"))
        credits.append(("Taper (you have never run on fresh legs)", 12.0,
                        "two weeks of reduced volume before the gun"))
        credits.append(("No more base building",
                        min(BASE_BUILDING_SEC_PER_WEEK * weeks_out, 25.0), BASE_BUILDING_WHY))
        total = sum(c[1] for c in credits)
        central = held - total / 60.0
        measured_base = (held, hr)
        basis = (f"Measured: {len(grp)} goal-race-pace session(s), median {_fmt_pace(held)}/mi at HR "
                 f"{hr:.0f} over the race-pace miles only"
                 + ("" if segs == len(grp) else " (some sessions had no laps; whole run used)")
                 + ". Model stood down in favour of real data.")
    else:
        basis = (f"Modelled from {len(longs)} recent long runs (median {_fmt_pace(base)}/mi "
                 f"at HR {base_hr:.0f}, {base_temp:.0f}F). No goal-race-pace session logged yet.")

    longest = max([l["distance_mi"] for l in recent_long_runs(conn, n=50) if not l["indoor"]] or [0.0])
    plan = race_plan(central, sec_per_bpm, longest)
    lo_plan = race_plan(central - 20 / 60.0, sec_per_bpm, longest)
    hi_plan = race_plan(central + 20 / 60.0, sec_per_bpm, longest)
    return {
        "available": True,
        "basis": basis,
        "measured": bool(grp),
        "baseline_pace": round(measured_base[0] if measured_base else base, 2),
        "baseline_pace_str": _fmt_pace(measured_base[0] if measured_base else base),
        "baseline_hr": round(measured_base[1] if measured_base else base_hr),
        "baseline_label": "race-pace segment pace" if measured_base else "long-run pace",
        "baseline_temp_f": round(base_temp),
        "sec_per_bpm": sec_per_bpm,
        "slope_fitted": fitted,
        "slope_n": n_pts,
        "weeks_out": round(weeks_out, 1),
        "credits": [{"label": l, "sec_per_mi": round(v), "why": w} for l, v, w in credits],
        "total_credit_sec": round(total),
        # race-effort running pace (HR 164), before phases and walk breaks
        "effort_pace": round(central, 2),
        "effort_pace_str": _fmt_pace(central),
        # what the official result will say: finish / 13.1, walks included
        "central_pace": round(plan["avg_pace"], 2),
        "central_pace_str": _fmt_pace(plan["avg_pace"]),
        "band_str": f"{_fmt_pace(lo_plan['avg_pace'])}-{_fmt_pace(hi_plan['avg_pace'])}",
        "finish_central": _fmt_time(plan["finish_min"]),
        "finish_band": f"{_fmt_time(lo_plan['finish_min'])}-{_fmt_time(hi_plan['finish_min'])}",
        "ceiling_first_4mi": _fmt_pace(plan["ceiling_first_4mi"]),
        "race_plan": plan,
        "grp_sessions": grp,
    }


def race_plan(effort_pace, sec_per_bpm, longest_mi=None):
    """Finish time built phase by phase, with walk breaks and real distance covered.

    `effort_pace` is the running pace at race effort (HR 164). The first miles
    run below it and the last ones above it; the planned walks and the extra
    distance nobody avoids are added on top. Returns per-phase paces too.
    """
    k = COURSE_EXTRA + 1.0
    phases_out, run_min, mile_at = [], 0.0, 0.0
    for label, miles, band, hr in PHASES:
        pace = effort_pace + (RACE_HR_MID - hr) * sec_per_bpm / 60.0
        run_min += pace * miles * k
        phases_out.append({"miles": label, "hr": band, "pace": round(pace, 2),
                           "pace_str": _fmt_pace(pace), "minutes": round(pace * miles * k, 1)})
        mile_at += miles
    stops = int(12.0 // WALK_EVERY_MI)
    run_pace = effort_pace
    walk_cost_s = stops * (WALK_S - (WALK_S / (WALK_PACE_MIN * 60.0)) * run_pace * 60.0)
    beyond = max(RACE_DISTANCE_MI - longest_mi, 0.0) if longest_mi else 0.0
    fade_s = beyond * FADE_SEC_PER_MI
    finish = run_min + (walk_cost_s + fade_s) / 60.0
    # Top of the opening HR band (162) marks the fastest the first 4 miles should go.
    ceiling = effort_pace + (RACE_HR_MID - 162) * sec_per_bpm / 60.0
    return {
        "phases": phases_out,
        "walk_stops": stops,
        "walk_cost_s": round(walk_cost_s),
        "longest_mi": round(longest_mi, 1) if longest_mi else None,
        "beyond_mi": round(beyond, 1),
        "fade_cost_s": round(fade_s),
        "course_mi": round(RACE_DISTANCE_MI * k, 2),
        "finish_min": round(finish, 1),
        "avg_pace": finish / RACE_DISTANCE_MI,
        "ceiling_first_4mi": ceiling,
    }


def phases(proj):
    """The four-phase race plan. Pace is a CEILING early, an outcome late."""
    rp = (proj.get("race_plan") or {}) if proj.get("available") else {}
    ps = {p["miles"]: p for p in rp.get("phases", [])}
    def at(lab, fmt, fallback):
        return fmt.format(ps[lab]["pace_str"]) if lab in ps else fallback
    ceiling = _fmt_pace(rp["ceiling_first_4mi"]) if rp else None
    return [
        {"miles": "1-4", "hr": "155-162",
         "pace": f"~{ps['1-4']['pace_str']}/mi, never faster than {ceiling}/mi" if rp else "hold back",
         "job": "Sit on your hands. Should feel almost too easy.",
         "talk": "Full sentences"},
        {"miles": "5-9", "hr": "158-165", "pace": at("5-9", "settle to ~{}/mi", "settle"),
         "job": "Settle in. Let the pace come to you.",
         "talk": "Short sentences"},
        {"miles": "10-12", "hr": "163-170", "pace": at("10-12", "~{}/mi, whatever the effort gives", "whatever the effort gives"),
         "job": "Now you are allowed to work.",
         "talk": "A few words"},
        {"miles": "13", "hr": "whatever is left", "pace": at("13", "empty it (~{}/mi)", "empty it"),
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

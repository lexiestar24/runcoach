"""The coaching logic: readiness, milestones, tips, and plan adjustments.

Philosophy baked in (per the training plan): recovery-first, longevity over pace,
keep easy days genuinely easy so hard days have room. Sleep is treated as a
wellbeing signal, not a performance stick.
"""
import datetime as dt
import statistics
import db
import plan as planmod

RACE_DATE = dt.date(2026, 11, 7)
TRAINING_START = "2026-06-22"   # plan week 1; ignore pre-plan May runs
MILE_M = 1609.344
HARD_TYPES = {"tempo", "interval", "long", "race"}


# ---------- small helpers ----------
def _rows(conn, q, args=()):
    return [dict(r) for r in conn.execute(q, args).fetchall()]


def _recent_daily(conn, days=30):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return _rows(conn, "SELECT * FROM daily WHERE date >= ? ORDER BY date", (since,))


def _recent_sleep(conn, days=30):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return _rows(conn, "SELECT * FROM sleep WHERE date >= ? ORDER BY date", (since,))


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


# ---------- readiness ----------
def readiness(conn):
    """0-100 readiness with plain-language reasons. Recovery-focused."""
    daily = _recent_daily(conn, 30)
    sleep = _recent_sleep(conn, 30)
    score = 100
    reasons = []

    # Resting HR vs personal 30-day baseline
    rhr_series = [d["resting_hr"] for d in daily if d["resting_hr"]]
    today_rhr = rhr_series[-1] if rhr_series else None
    base_rhr = _median(rhr_series[:-1]) if len(rhr_series) > 3 else _median(rhr_series)
    if today_rhr and base_rhr:
        delta = today_rhr - base_rhr
        if delta >= 6:
            score -= 22
            reasons.append(f"Resting HR {today_rhr} is {delta:.0f} bpm above your baseline ({base_rhr:.0f}). A classic 'not fully recovered' flag.")
        elif delta >= 3:
            score -= 10
            reasons.append(f"Resting HR {today_rhr} is slightly up ({delta:+.0f} vs {base_rhr:.0f}).")
        else:
            reasons.append(f"Resting HR {today_rhr} is right around baseline ({base_rhr:.0f}), a good sign.")

    # Last night's sleep score
    sc_series = [s["score"] for s in sleep if s["score"]]
    last_sleep = sc_series[-1] if sc_series else None
    if last_sleep is not None:
        if last_sleep < 55:
            score -= 20
            reasons.append(f"Sleep score {last_sleep} last night was rough. Sleep tracks how you're feeling, not your training, so go gentle.")
        elif last_sleep < 70:
            score -= 10
            reasons.append(f"Sleep score {last_sleep} was so-so.")
        else:
            reasons.append(f"Sleep score {last_sleep}, solid rest.")

    # Sleep debt over last 3 nights (hours)
    hrs = [s["total_seconds"] / 3600 for s in sleep[-3:] if s["total_seconds"]]
    if len(hrs) >= 2 and (sum(hrs) / len(hrs)) < 6.5:
        score -= 10
        reasons.append(f"Averaging {sum(hrs)/len(hrs):.1f}h over the last {len(hrs)} nights, some sleep debt building.")

    # Stress
    stress_series = [d["avg_stress"] for d in daily if d["avg_stress"]]
    if stress_series:
        recent_stress = stress_series[-1]
        base_stress = _median(stress_series)
        if recent_stress and base_stress and recent_stress > base_stress + 12:
            score -= 8
            reasons.append(f"Average stress ({recent_stress}) is up vs your norm ({base_stress:.0f}).")

    score = max(0, min(100, score))
    if score >= 75:
        label, verdict = "Ready", "Green light. Your body's up for a normal session today."
    elif score >= 55:
        label, verdict = "Moderate", "Slightly under. Fine to run, but keep easy days truly easy and don't force a hard one."
    else:
        label, verdict = "Take it easy", "Recovery signals are down. Swap intensity for easy movement or rest. This is the longevity play."

    return {
        "score": score,
        "label": label,
        "verdict": verdict,
        "reasons": reasons,
        "today_rhr": today_rhr,
        "baseline_rhr": round(base_rhr) if base_rhr else None,
        "last_sleep_score": last_sleep,
    }


# ---------- weekly load ----------
def week_bounds(d=None):
    d = d or dt.date.today()
    monday = d - dt.timedelta(days=d.weekday())
    return monday, monday + dt.timedelta(days=6)


def weekly_load(conn):
    mon, sun = week_bounds()
    runs = _rows(conn,
        "SELECT * FROM activities WHERE type LIKE '%running%' AND date >= ? AND date <= ? ORDER BY start_local",
        (mon.isoformat(), sun.isoformat()))
    actual_mi = sum((r["distance_m"] or 0) for r in runs) / MILE_M
    pw = [w for w in planmod.load_plan() if mon.isoformat() <= w["date"] <= sun.isoformat()]
    planned_mi = sum((w["planned_miles"] or 0) for w in pw)
    return {
        "week_start": mon.isoformat(),
        "week_end": sun.isoformat(),
        "planned_miles": round(planned_mi, 1),
        "actual_miles": round(actual_mi, 1),
        "runs_done": len(runs),
        "runs_planned": len(pw),
    }


# ---------- milestones ----------
def milestones(conn):
    runs = _rows(conn, "SELECT * FROM activities WHERE type LIKE '%running%' AND date >= '" + TRAINING_START + "' ORDER BY start_local")
    total_mi = sum((r["distance_m"] or 0) for r in runs) / MILE_M
    longest = max(((r["distance_m"] or 0) / MILE_M for r in runs), default=0)
    vo2 = [r["vo2max"] for r in runs if r["vo2max"]]
    # trend over the recent training block, not all-time (avoids pre-training readings)
    vo2_recent = vo2[-6:]
    today = dt.date.today()
    days_to_race = (RACE_DATE - today).days
    plan = planmod.load_plan()
    cur = next((w for w in plan if w["date"] >= today.isoformat()), None)
    done = sum(1 for w in planmod.plan_with_actuals(conn) if w["status"] == "done")
    elapsed = sum(1 for w in plan if w["date"] < today.isoformat())
    return {
        "total_runs": len(runs),
        "total_miles": round(total_mi, 1),
        "longest_run_mi": round(longest, 1),
        "vo2max": vo2[-1] if vo2 else None,
        "vo2max_change": round(vo2_recent[-1] - vo2_recent[0], 1) if len(vo2_recent) > 1 else None,
        "days_to_race": days_to_race,
        "weeks_to_race": round(days_to_race / 7, 1),
        "current_week": cur["week"] if cur else None,
        "workouts_done": done,
        "workouts_elapsed": elapsed,
        "consistency_pct": round(100 * done / elapsed) if elapsed else None,
    }


# ---------- per-run evaluation (for the detail view) ----------
def evaluate(w):
    """Assess one completed workout against what the plan asked for."""
    a = w.get("actual")
    if not a:
        return None
    ptype = w["type"]
    planned = w.get("planned_miles")
    dist = a.get("distance_mi")
    hr = a.get("avg_hr")
    hard = ptype in ("tempo", "interval", "race")
    points = []
    rating = "solid"

    # distance vs plan
    if planned and dist is not None:
        diff = dist - planned
        if abs(diff) <= 0.3:
            points.append(f"Distance on target: {dist} mi vs {planned} planned.")
        elif diff > 0:
            points.append(f"Went a little long: {dist} mi vs {planned} planned (+{diff:.1f}).")
        else:
            points.append(f"Came up short: {dist} mi vs {planned} planned ({diff:.1f}).")
            if diff < -0.75:
                rating = "short"

    # effort vs the intent of the day
    if hr:
        if hard:
            if hr >= 165:
                points.append(f"Effort on point: avg HR {hr:.0f} reached the tempo zone (165-175).")
            elif hr >= 158:
                points.append(f"A touch under the tempo zone: avg HR {hr:.0f} (target 165-175). A bit more push next time.")
                rating = "solid" if rating == "solid" else rating
            else:
                points.append(f"Stayed easy for a hard day: avg HR {hr:.0f}, below the tempo zone. Fine if the legs were cooked, but the quality stimulus was light.")
                rating = "easy-for-hard"
        else:
            if hr <= 155:
                points.append(f"Kept it genuinely easy: avg HR {hr:.0f}. Exactly what easy days are for.")
            elif hr <= 165:
                points.append(f"Easy effort, slightly elevated (avg HR {hr:.0f}). In the current heat that is expected, not a problem.")
            else:
                points.append(f"Ran this easy day hot: avg HR {hr:.0f}. Heat inflates it, but try backing off so the hard days have room.")
                rating = "ran-hot"

    if a.get("max_hr") and not hard and a["max_hr"] >= 186:
        points.append(f"Max HR touched {a['max_hr']:.0f}, likely a hill or a surge near the end.")

    headline = {
        "solid": "Solid session, right on plan.",
        "short": "Got it done, just short of the planned distance.",
        "ran-hot": "Good work, though this easy run ran warm.",
        "easy-for-hard": "Completed, but easier than the workout called for.",
    }.get(rating, "Nice work.")
    return {"rating": rating, "headline": headline, "points": points}


# ---------- tips ----------
def tips(conn):
    out = []
    daily = _recent_daily(conn, 14)
    sleep = _recent_sleep(conn, 14)
    runs = _rows(conn,
        "SELECT * FROM activities WHERE type LIKE '%running%' AND date >= ? ORDER BY start_local DESC LIMIT 6",
        (TRAINING_START,))

    # Rising resting HR over the week
    rhr = [d["resting_hr"] for d in daily if d["resting_hr"]]
    if len(rhr) >= 6 and _median(rhr[-3:]) and _median(rhr[:3]):
        if _median(rhr[-3:]) - _median(rhr[:3]) >= 4:
            out.append({"kind": "recovery", "text":
                "Resting HR has drifted up over the past week or so. That usually means life-load or training-load is stacking up. A genuinely easy few days tends to bring it back down."})

    # Poor sleep streak → tie to anxiety, not training
    bad = [s for s in sleep[-3:] if s["score"] and s["score"] < 65]
    if len(bad) >= 2:
        out.append({"kind": "sleep", "text":
            "Two-plus rougher nights in a row. Since your sleep tracks anxiety more than training, treat this as a cue for a wind-down routine tonight rather than a reason to push harder tomorrow."})

    # Easy days running hot (keep easy easy)
    easy_runs = [r for r in runs if r["avg_hr"] and r["distance_m"]]
    hot = [r for r in easy_runs if r["avg_hr"] and r["avg_hr"] > 170]
    if hot:
        out.append({"kind": "pacing", "text":
            f"Your last easy/long efforts averaged high HR ({int(hot[0]['avg_hr'])}+). In summer heat some of that is the weather, but if you can talk in full sentences you're fine; if not, walk the hills and slow down. Easy days protect the hard ones."})

    # Positive reinforcement
    m = milestones(conn)
    if m["longest_run_mi"] and m["longest_run_mi"] >= 5:
        out.append({"kind": "win", "text":
            f"Longest run so far is {m['longest_run_mi']} mi, and you're building real endurance. {m['weeks_to_race']} weeks to the Nov 7 half."})
    if m["vo2max_change"] and m["vo2max_change"] > 0:
        out.append({"kind": "win", "text":
            f"VO2max is trending up (+{m['vo2max_change']}). Aerobic fitness is responding. The slow easy miles are working."})

    if not out:
        out.append({"kind": "win", "text": "Nothing flagged. Signals look steady. Keep doing what you're doing."})
    return out


# ---------- plan adjustments ----------
def _blocked_dates(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS conflicts (date TEXT PRIMARY KEY, note TEXT)")
    return {r["date"]: r["note"] for r in conn.execute("SELECT * FROM conflicts").fetchall()}


def adjustments(conn):
    """Concrete, data-driven suggestions to shuffle the plan."""
    out = []
    today = dt.date.today()
    ready = readiness(conn)
    plan = planmod.plan_with_actuals(conn)
    blocked = _blocked_dates(conn)

    upcoming = [w for w in plan if today.isoformat() <= w["date"] <= (today + dt.timedelta(days=7)).isoformat()]

    # 1) Low readiness + a hard session today/tomorrow
    if ready["score"] < 55:
        for w in upcoming[:2]:
            if w["type"] in HARD_TYPES:
                out.append({"severity": "high", "date": w["date"],
                    "text": f"Readiness is low ({ready['score']}) and {w['date']} is a {w['type']} day ({w['planned_miles']} mi). Consider swapping it for an easy run or rest, and moving the quality session later in the week."})
                break

    # 2) Scheduling conflicts (blocked dates)
    for w in upcoming:
        if w["date"] in blocked:
            # find an open day in the same week
            mon, sun = week_bounds(dt.date.fromisoformat(w["date"]))
            same_week = [x for x in plan if mon.isoformat() <= x["date"] <= sun.isoformat()]
            busy = {x["date"] for x in same_week} | set(blocked)
            slot = next((mon + dt.timedelta(days=i) for i in range(7)
                         if (mon + dt.timedelta(days=i)).isoformat() not in busy
                         and (mon + dt.timedelta(days=i)) >= today), None)
            move = f" Move it to {slot.strftime('%a %m-%d')}." if slot else " No open day this week, so fold it into an adjacent run or drop it (it's OK to miss one)."
            out.append({"severity": "med", "date": w["date"],
                "text": f"Conflict on {w['date']} ({blocked[w['date']]}) clashes with a {w['type']} run ({w['planned_miles']} mi).{move}"})

    # 3) Missed hard workouts recently
    recent_missed = [w for w in plan if w["status"] == "missed"
                     and w["type"] in HARD_TYPES
                     and w["date"] >= (today - dt.timedelta(days=4)).isoformat()]
    if recent_missed:
        w = recent_missed[-1]
        out.append({"severity": "low", "date": w["date"],
            "text": f"Missed the {w['date']} {w['type']} ({w['planned_miles']} mi). Don't cram it back in. Just resume the schedule; one missed session won't hurt the Nov 7 goal."})

    if not out:
        out.append({"severity": "low", "date": None,
            "text": "No adjustments needed. Plan and recovery are in sync. Follow the schedule as written."})
    return out


def series(conn, days=45):
    """Time series for the dashboard charts."""
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    daily = _rows(conn, "SELECT * FROM daily WHERE date >= ? ORDER BY date", (since,))
    sleep = _rows(conn, "SELECT * FROM sleep WHERE date >= ? ORDER BY date", (since,))
    sleep_by = {s["date"]: s for s in sleep}
    recovery = [{
        "date": d["date"],
        "resting_hr": d["resting_hr"],
        "stress": d["avg_stress"],
        "sleep_score": (sleep_by.get(d["date"]) or {}).get("score"),
        "sleep_hours": round((sleep_by.get(d["date"], {}).get("total_seconds") or 0) / 3600, 1) or None,
    } for d in daily]

    # Per-run HR + pace history (running only). Tag each run's *intended effort*
    # by matching its date to a planned workout: easy (shakeout/easy/long/quality)
    # vs hard (tempo/race). Off-plan runs default to easy.
    runs = _rows(conn,
        "SELECT * FROM activities WHERE type LIKE '%running%' AND date >= '" + TRAINING_START + "' ORDER BY start_local", ())
    match_type = {}
    for w in planmod.plan_with_actuals(conn):
        if w.get("actual"):
            match_type[w.get("moved_to") or w["date"]] = w["type"]
    run_hist = [{
        "date": r["date"],
        "miles": round((r["distance_m"] or 0) / MILE_M, 2),
        "avg_hr": r["avg_hr"],
        "max_hr": r["max_hr"],
        "pace_min_per_mi": round((r["avg_pace_s_per_km"] or 0) * (MILE_M / 1000) / 60, 2) if r["avg_pace_s_per_km"] else None,
        "vo2max": r["vo2max"],
        "intent": "hard" if match_type.get(r["date"]) in ("tempo", "interval", "race") else "easy",
        "planned_type": match_type.get(r["date"]),
    } for r in runs]

    # Weekly mileage planned vs actual across the whole plan
    plan = planmod.load_plan()
    weeks = {}
    for w in plan:
        wk = w["week"]
        if wk is None:
            continue
        weeks.setdefault(wk, {"week": wk, "planned": 0.0, "actual": 0.0, "start": w["date"]})
        weeks[wk]["planned"] += w["planned_miles"] or 0
    for r in runs:
        d = dt.date.fromisoformat(r["date"])
        for wk, info in weeks.items():
            ws = dt.date.fromisoformat(info["start"])
            wmon = ws - dt.timedelta(days=ws.weekday())
            if wmon <= d <= wmon + dt.timedelta(days=6):
                info["actual"] += (r["distance_m"] or 0) / MILE_M
                break
    weekly = [{"week": v["week"], "planned": round(v["planned"], 1), "actual": round(v["actual"], 1)}
              for v in sorted(weeks.values(), key=lambda x: x["week"])]

    # Easy-HR band derived from YOUR actual easy runs (median +/- 5), so it tracks
    # reality (heat now, cooler later) instead of the plan's theoretical <150.
    easy_hrs = sorted(r["avg_hr"] for r in run_hist if r["intent"] == "easy" and r["avg_hr"])
    if len(easy_hrs) >= 4:
        med = statistics.median(easy_hrs)
        easy_band = {"lo": round(med - 5), "hi": round(med + 5), "median": round(med)}
    else:
        easy_band = {"lo": 150, "hi": 160, "median": 155}

    return {"recovery": recovery, "runs": run_hist, "weekly": weekly, "easy_band": easy_band}


def add_conflict(conn, date, note=""):
    conn.execute("CREATE TABLE IF NOT EXISTS conflicts (date TEXT PRIMARY KEY, note TEXT)")
    conn.execute("INSERT OR REPLACE INTO conflicts (date, note) VALUES (?,?)", (date, note))
    conn.commit()


def remove_conflict(conn, date):
    conn.execute("CREATE TABLE IF NOT EXISTS conflicts (date TEXT PRIMARY KEY, note TEXT)")
    conn.execute("DELETE FROM conflicts WHERE date=?", (date,))
    conn.commit()


def add_reschedule(conn, orig_date, new_date, note=""):
    conn.execute("CREATE TABLE IF NOT EXISTS reschedules (orig_date TEXT PRIMARY KEY, new_date TEXT, note TEXT)")
    conn.execute("INSERT OR REPLACE INTO reschedules (orig_date, new_date, note) VALUES (?,?,?)",
                 (orig_date, new_date, note))
    conn.commit()


def remove_reschedule(conn, orig_date):
    conn.execute("CREATE TABLE IF NOT EXISTS reschedules (orig_date TEXT PRIMARY KEY, new_date TEXT, note TEXT)")
    conn.execute("DELETE FROM reschedules WHERE orig_date=?", (orig_date,))
    conn.commit()

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
# Readiness is scored against Lexie's OWN distribution, not textbook absolutes.
# The earlier version started at 100 and only subtracted on fixed cliffs
# (sleep score <70, RHR +3, stress > median+12). Because her sleep scores sit in
# the 80s and her stress never swings 12 points, those branches almost never
# fired: 20 of 22 days scored exactly 100, including a 5.5h night. Each signal
# now returns a continuous 0-1 quality vs her personal baseline, so a genuinely
# average day lands in the 70s and there is headroom in both directions.
WEIGHTS = {"sleep": 30, "debt": 15, "rhr": 25, "battery": 12, "stress": 8, "load": 10}


def _ramp(x, lo, hi):
    """Linear 0..1 as x goes lo->hi (works in either direction)."""
    if x is None:
        return None
    if lo == hi:
        return 1.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _spread(series, floor=1.0):
    """Robust spread (MAD-based sigma) of a personal series."""
    vals = [v for v in series if v is not None]
    if len(vals) < 4:
        return None
    m = statistics.median(vals)
    mad = statistics.median([abs(v - m) for v in vals])
    return max(floor, 1.4826 * mad)


def readiness(conn, asof=None):
    """0-100 readiness vs personal baselines, with plain-language reasons.

    `asof` (YYYY-MM-DD) scores a past day using only data available up to that
    date, so a completed run can be judged against the readiness she actually
    had that morning.
    """
    asof = asof or dt.date.today().isoformat()
    since = (dt.date.fromisoformat(asof) - dt.timedelta(days=60)).isoformat()
    daily = _rows(conn, "SELECT * FROM daily WHERE date >= ? AND date <= ? ORDER BY date", (since, asof))
    sleep = _rows(conn, "SELECT * FROM sleep WHERE date >= ? AND date <= ? ORDER BY date", (since, asof))

    parts, reasons, flags = {}, [], []
    today_row = daily[-1] if daily and daily[-1]["date"] == asof else None

    # --- Sleep last night: duration AND quality, both vs her own norm ---
    hist_hrs = [s["total_seconds"] / 3600 for s in sleep if s["total_seconds"]]
    last_sleep_row = sleep[-1] if sleep and sleep[-1]["date"] == asof else None
    last_sleep = last_sleep_row["score"] if last_sleep_row else None
    last_hrs = (last_sleep_row["total_seconds"] / 3600) if last_sleep_row and last_sleep_row["total_seconds"] else None

    # personal sleep need: her own typical night, held to a sane 7.0-8.0 window
    need = min(8.0, max(7.0, _median(hist_hrs[:-1]) or 7.5))
    dur_q = _ramp(last_hrs, need - 2.5, need)          # need-2.5h => 0, need => 1
    sc_med = _median([s["score"] for s in sleep[:-1] if s["score"]])
    sc_sd = _spread([s["score"] for s in sleep[:-1] if s["score"]], floor=4.0)
    if last_sleep is not None and sc_med and sc_sd:
        z = (last_sleep - sc_med) / sc_sd
        score_q = _ramp(z, -2.0, 0.5)                   # 2 sigma below her norm => 0
    else:
        score_q = _ramp(last_sleep, 50, 85) if last_sleep is not None else None

    if dur_q is not None or score_q is not None:
        qs = [q for q in (dur_q, score_q) if q is not None]
        ws = [0.55, 0.45][: len(qs)]
        parts["sleep"] = sum(q * w for q, w in zip(qs, ws)) / sum(ws)
        bits = []
        if last_hrs is not None:
            bits.append(f"{last_hrs:.1f}h")
        if last_sleep is not None:
            bits.append(f"score {last_sleep}")
        detail = ", ".join(bits)
        if parts["sleep"] < 0.45:
            reasons.append(
                f"Last night was short for you: {detail} (you typically get {need:.1f}h"
                + (f" at a score near {sc_med:.0f}" if sc_med else "")
                + "). Expect the legs and the head to feel it.")
            flags.append("short sleep")
        elif parts["sleep"] < 0.72:
            reasons.append(f"Sleep was a bit under your norm: {detail} vs your usual {need:.1f}h.")
        else:
            reasons.append(f"Slept well: {detail}. That is at or above your baseline.")

    # --- Cumulative sleep debt over the past week ---
    week = [s["total_seconds"] / 3600 for s in sleep[-7:] if s["total_seconds"]]
    if len(week) >= 3:
        debt = sum(max(0.0, need - h) for h in week)
        parts["debt"] = 1.0 - _ramp(debt, 1.0, 9.0)     # ~9h cumulative deficit => 0
        if debt >= 5:
            reasons.append(f"About {debt:.1f}h of sleep debt has stacked up over the last {len(week)} nights.")
            flags.append("sleep debt")

    # --- Resting HR vs personal baseline ---
    rhr_series = [d["resting_hr"] for d in daily if d["resting_hr"]]
    today_rhr = today_row["resting_hr"] if today_row else (rhr_series[-1] if rhr_series else None)
    base_rhr = _median(rhr_series[:-1]) if len(rhr_series) > 3 else _median(rhr_series)
    if today_rhr and base_rhr:
        delta = today_rhr - base_rhr
        parts["rhr"] = 1.0 - _ramp(delta, -1.0, 8.0)    # at baseline => ~0.9, +8 => 0
        if delta >= 5:
            reasons.append(f"Resting HR {today_rhr} is {delta:.0f} bpm over your baseline ({base_rhr:.0f}) -- a classic not-recovered flag.")
            flags.append("elevated RHR")
        elif delta >= 2:
            reasons.append(f"Resting HR {today_rhr} is mildly up ({delta:+.0f} vs {base_rhr:.0f}).")
        else:
            reasons.append(f"Resting HR {today_rhr} sits at or under baseline ({base_rhr:.0f}) -- well recovered.")

    # --- Morning body battery (was collected but never used) ---
    bb = today_row["body_battery_high"] if today_row else None
    if bb:
        parts["battery"] = _ramp(bb, 35, 90)
        if bb < 55:
            reasons.append(f"Body Battery only recharged to {bb} overnight.")
            flags.append("low battery")

    # --- Stress vs her own spread (not a fixed +12) ---
    stress_series = [d["avg_stress"] for d in daily if d["avg_stress"]]
    cur_stress = today_row["avg_stress"] if today_row else None
    s_med, s_sd = _median(stress_series[:-1]), _spread(stress_series[:-1], floor=3.0)
    if cur_stress and s_med and s_sd:
        z = (cur_stress - s_med) / s_sd
        parts["stress"] = 1.0 - _ramp(z, -0.5, 2.0)
        if z >= 1.0:
            reasons.append(f"All-day stress ({cur_stress}) is running above your norm ({s_med:.0f}).")
            flags.append("high stress")

    # --- Acute training load: last 3 days of running ---
    d0 = dt.date.fromisoformat(asof)
    recent_runs = _rows(conn,
        "SELECT * FROM activities WHERE type LIKE '%running%' AND date >= ? AND date <= ? ",
        ((d0 - dt.timedelta(days=2)).isoformat(), asof))
    load_mi = sum((r["distance_m"] or 0) for r in recent_runs) / MILE_M
    parts["load"] = 1.0 - _ramp(load_mi, 6.0, 18.0)
    if load_mi >= 9:
        reasons.append(f"You've covered {load_mi:.1f} mi in the last three days -- real load in the legs.")

    # --- Combine (weighted mean over whatever signals exist) ---
    used = {k: v for k, v in parts.items() if v is not None}
    if used:
        tw = sum(WEIGHTS[k] for k in used)
        score = round(sum(v * WEIGHTS[k] for k, v in used.items()) / tw * 100)
    else:
        score = 70  # no data: assume neutral rather than perfect

    # Acute constraint: a bad night is not merely one weighted input, it caps the
    # day. Lexie's felt experience tracks sleep more tightly than anything else,
    # so a 5.5h night must not get averaged away against an excellent resting HR.
    ceiling = 100
    if parts.get("sleep") is not None and parts["sleep"] < 0.55:
        ceiling = min(ceiling, round(50 + 60 * parts["sleep"]))
    if parts.get("rhr") is not None and parts["rhr"] < 0.45:
        ceiling = min(ceiling, round(45 + 70 * parts["rhr"]))
    if score > ceiling:
        score = ceiling
        reasons.append("Other signals look fine, but that is capped by how little you actually slept -- the rest of the body hasn't caught up with it yet.")
    score = max(0, min(100, score))

    if score >= 88:
        label, verdict = "Primed", "Everything is pointing up. A great day to take on the hardest thing on your schedule."
    elif score >= 72:
        label, verdict = "Ready", "Green light. Your body is up for a normal session today."
    elif score >= 55:
        label, verdict = "Moderate", "Slightly under. Fine to run, but keep easy days truly easy and don't force a hard one."
    elif score >= 40:
        label, verdict = "Back off", "Recovery signals are down. Cut the intensity or the distance -- an easy day here protects the whole week."
    else:
        label, verdict = "Rest", "Your body is asking for a break. Easy movement or a full rest day. This is the longevity play."

    return {
        "score": score,
        "label": label,
        "verdict": verdict,
        "reasons": reasons,
        "flags": flags,
        "components": {k: round(v * 100) for k, v in used.items()},
        "today_rhr": today_rhr,
        "baseline_rhr": round(base_rhr) if base_rhr else None,
        "last_sleep_score": last_sleep,
        "last_sleep_hours": round(last_hrs, 1) if last_hrs else None,
        "sleep_need": round(need, 1),
        "date": asof,
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
# Runs are judged against OTHER RUNS OF THE SAME KIND -- shakeouts vs shakeouts,
# long runs vs long runs -- because a 1 mi shakeout and a 5 mi long run have
# nothing useful to say to each other. Heart rate is heat-adjusted first (see
# weather.py) so a humid August long run is comparable to a cool September one,
# and the day's readiness is folded in so a good run on a depleted body reads as
# the win it actually is.

def _efficiency(a, heat_off):
    """Meters covered per heartbeat, at heat-normalised HR. Higher = fitter.

    Speed / HR is the standard efficiency index. Subtracting the estimated heat
    penalty from HR removes the weather so the trend reflects fitness.
    """
    dist, dur, hr = a.get("distance_m"), a.get("duration_s"), a.get("avg_hr")
    if not (dist and dur and hr):
        return None, None
    adj_hr = max(100.0, hr - (heat_off or 0.0))
    speed_m_min = dist / (dur / 60.0)
    return round(speed_m_min / adj_hr, 3), round(adj_hr, 1)


def evaluation_context(conn):
    """Shared inputs for evaluate(): conditions, per-day readiness, cohorts."""
    try:
        import weather
        cond = weather.by_activity(conn)
    except Exception:
        cond = {}

    done = [w for w in planmod.plan_with_actuals(conn) if w.get("status") == "done"]
    done.sort(key=lambda w: w["actual"].get("date") or w["date"])

    cohorts = {}
    for w in done:
        a = w["actual"]
        c = cond.get(a.get("activity_id")) or {}
        ei, adj_hr = _efficiency(a, c.get("hr_heat_offset"))
        cohorts.setdefault(w["type"], []).append({
            "date": a.get("date") or w["date"],
            "miles": a.get("distance_mi"),
            "pace": a.get("avg_pace_min_per_mi"),
            "hr": a.get("avg_hr"),
            "adj_hr": adj_hr,
            "ei": ei,
            "indoor": bool(c.get("indoor")),
            "label": c.get("label"),
        })
    return {"conditions": cond, "cohorts": cohorts, "readiness_cache": {}}


def _readiness_on(conn, ctx, date):
    if date not in ctx["readiness_cache"]:
        try:
            ctx["readiness_cache"][date] = readiness(conn, asof=date)
        except Exception:
            ctx["readiness_cache"][date] = None
    return ctx["readiness_cache"][date]


def _fmt_pace(p):
    return f"{int(p)}:{int(round((p - int(p)) * 60)):02d}/mi" if p else "n/a"


def evaluate(w, conn=None, ctx=None):
    """Assess one completed workout: vs the plan, vs its own cohort, in context."""
    a = w.get("actual")
    if not a:
        return None
    ptype = w["type"]
    planned = w.get("planned_miles")
    dist = a.get("distance_mi")
    hr = a.get("avg_hr")
    hard = ptype in ("tempo", "interval", "race")
    date = a.get("date") or w["date"]
    points = []
    rating = "solid"

    cond = (ctx or {}).get("conditions", {}).get(a.get("activity_id")) or {}
    heat_off = cond.get("hr_heat_offset") or 0.0
    ei, adj_hr = _efficiency(a, heat_off)

    # --- 1. distance vs plan ---
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

    # --- 2. conditions ---
    if cond.get("label"):
        if heat_off >= 3:
            points.append(f"Conditions: {cond['label']}. That alone is worth roughly +{heat_off:.0f} bpm at the same effort, so read the raw HR with that in mind.")
        else:
            points.append(f"Conditions: {cond['label']}.")

    # --- 3. effort vs the intent of the day, heat-adjusted ---
    if hr:
        cmp_hr = adj_hr if adj_hr else hr
        if hard:
            if cmp_hr >= 165:
                points.append(f"Effort on point: avg HR {hr:.0f} reached the tempo zone (165-175).")
            elif cmp_hr >= 158:
                points.append(f"A touch under the tempo zone: avg HR {hr:.0f} (target 165-175). A bit more push next time.")
            else:
                points.append(f"Stayed easy for a hard day: avg HR {hr:.0f}, below the tempo zone. Fine if the legs were cooked, but the quality stimulus was light.")
                rating = "easy-for-hard"
        else:
            if cmp_hr <= 158:
                points.append(f"Kept it genuinely easy: avg HR {hr:.0f}"
                              + (f" ({adj_hr:.0f} once the heat is backed out)" if heat_off >= 3 else "")
                              + ". Exactly what easy days are for.")
            elif cmp_hr <= 166:
                points.append(f"Easy effort, slightly elevated (avg HR {hr:.0f}"
                              + (f", ~{adj_hr:.0f} heat-adjusted" if heat_off >= 3 else "")
                              + "). Reasonable, not a problem.")
            else:
                points.append(f"Ran this easy day hot: avg HR {hr:.0f} even after adjusting for the weather (~{adj_hr:.0f}). Back off so the hard days have room.")
                rating = "ran-hot"

    # --- 4. like-for-like: this run vs previous runs of the SAME type ---
    cohort = [r for r in (ctx or {}).get("cohorts", {}).get(ptype, []) if r["date"] < date]
    peers = [r for r in cohort if r["ei"]]
    comparison = None
    if peers and ei:
        eis = sorted(r["ei"] for r in peers)
        med_ei = statistics.median(eis)
        better = sum(1 for v in eis if v < ei)
        rank = len(eis) + 1 - better
        pct = (ei - med_ei) / med_ei * 100
        kind = {"shakeout": "shakeout", "long": "long run", "tempo": "tempo",
                "interval": "interval session", "easy": "easy run",
                "quality": "quality run", "race": "race"}.get(ptype, ptype)
        paces = [r["pace"] for r in peers if r["pace"]]
        hrs = [r["adj_hr"] for r in peers if r["adj_hr"]]

        comparison = {
            "type": ptype, "n_peers": len(peers), "rank": rank,
            "ei": ei, "median_ei": round(med_ei, 3), "pct_vs_median": round(pct, 1),
            "median_pace": round(statistics.median(paces), 2) if paces else None,
            "median_adj_hr": round(statistics.median(hrs)) if hrs else None,
        }

        verdict = (f"Against your {len(peers)} previous {kind}"
                   f"{'s' if len(peers) != 1 else ''}: ")
        if pct >= 4:
            verdict += (f"this was {pct:.0f}% more efficient than your median "
                        f"({rank}{'st' if rank == 1 else 'nd' if rank == 2 else 'rd' if rank == 3 else 'th'} best). "
                        "More distance per heartbeat, weather backed out. Real fitness showing.")
            if rating == "solid":
                rating = "breakthrough" if rank == 1 else "strong"
        elif pct >= 1:
            verdict += f"slightly better than your median ({pct:+.0f}%). Quietly trending the right way."
            if rating == "solid":
                rating = "strong"
        elif pct >= -4:
            verdict += f"right in line with your median ({pct:+.0f}%). Consistent, which at this stage is exactly the goal."
        else:
            verdict += f"{abs(pct):.0f}% below your median for this kind of run."
            if rating == "solid":
                rating = "flat"
        points.append(verdict)

        if paces and a.get("avg_pace_min_per_mi"):
            mp = statistics.median(paces)
            dp = a["avg_pace_min_per_mi"] - mp
            points.append(f"Pace {_fmt_pace(a['avg_pace_min_per_mi'])} vs a {_fmt_pace(mp)} median for your {kind}s "
                          f"({'faster' if dp < 0 else 'slower'} by {abs(dp)*60:.0f}s).")
        if hrs and adj_hr:
            mh = statistics.median(hrs)
            points.append(f"Heat-adjusted HR {adj_hr:.0f} vs a {mh:.0f} median for your {kind}s "
                          f"({adj_hr - mh:+.0f} bpm).")
    elif ei:
        points.append("First run of this type in the plan, so there is nothing to compare it against yet. It becomes the baseline.")

    # --- 5. readiness she actually had that morning ---
    ready = _readiness_on(conn, ctx, date) if conn is not None and ctx is not None else None
    if ready:
        rs = ready["score"]
        if rs < 65 and rating in ("breakthrough", "strong", "solid"):
            points.append(f"Worth noting: readiness that morning was only {rs} ({ready['label']}) -- "
                          f"{', '.join(ready['flags']) if ready['flags'] else 'recovery signals were down'}. "
                          "Running this well on a depleted body counts for more, not less.")
            rating = "gutsy"
        elif rs < 65 and rating in ("flat", "short", "ran-hot"):
            points.append(f"Readiness was {rs} ({ready['label']}) that morning"
                          + (f" -- {', '.join(ready['flags'])}" if ready['flags'] else "")
                          + ". This is the honest reason the run felt like work, and it is not a fitness problem.")
        elif rs >= 85 and rating == "flat":
            points.append(f"Readiness was high that day ({rs}), so the dip is not recovery. "
                          "More likely conditions, fuelling, or simply a flat day -- everyone gets them.")
        elif rs >= 85:
            points.append(f"You went in fresh (readiness {rs}) and used it well.")

    if a.get("max_hr") and not hard and a["max_hr"] >= 186:
        points.append(f"Max HR touched {a['max_hr']:.0f}, likely a hill or a surge near the end.")

    headline = {
        "breakthrough": "Best run of this type yet.",
        "strong": "Strong session, trending ahead of your norm.",
        "gutsy": "Strong run on a body that wasn't fully recovered.",
        "solid": "Solid session, right on plan.",
        "flat": "Got it done, though flatter than your usual for this one.",
        "short": "Got it done, just short of the planned distance.",
        "ran-hot": "Good work, though this easy run ran warm.",
        "easy-for-hard": "Completed, but easier than the workout called for.",
    }.get(rating, "Nice work.")
    return {"rating": rating, "headline": headline, "points": points,
            "comparison": comparison, "conditions": cond or None,
            "readiness_that_day": ready["score"] if ready else None}


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
    # carry each week's Mon-Sun span so the dashboard can filter this chart to the
    # same time window as the others
    weekly = []
    for v in sorted(weeks.values(), key=lambda x: x["week"]):
        ws = dt.date.fromisoformat(v["start"])
        wmon = ws - dt.timedelta(days=ws.weekday())
        weekly.append({
            "week": v["week"],
            "planned": round(v["planned"], 1),
            "actual": round(v["actual"], 1),
            "start": wmon.isoformat(),
            "end": (wmon + dt.timedelta(days=6)).isoformat(),
        })

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

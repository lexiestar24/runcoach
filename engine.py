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


# ---------- within-day signals ----------
# Readiness used to be a morning verdict: overnight Body Battery peak and the
# whole day's average stress. But a 91 at 7am is not a 91 at 5:30pm: Body Battery
# here typically falls from ~80 at wake to the low 20s by the time of an evening run.
# These helpers read the 3-minute Body Battery / stress curve so the score can be
# asked for a specific moment: right now, or the hour she is actually going to run.
BASELINE_DAYS = 30          # history behind the hour-of-day profile
_PROFILE_CACHE = {}         # {asof: (built_at, profile)} -- a 30-day baseline is
_PROFILE_TTL = 600          # slow-moving, so a 10 min cache costs nothing


def _now_minute():
    n = dt.datetime.now()
    return n.hour * 60 + n.minute


def _hhmm(minute):
    h, m = divmod(int(minute), 60)
    ap = "am" if h < 12 else "pm"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d}{ap}"


def _to_minute(at):
    """Accept 'HH:MM', 'YYYY-MM-DD HH:MM:SS', a datetime, or minutes-past-midnight."""
    if at is None or isinstance(at, (int, float)):
        return int(at) if at is not None else None
    if isinstance(at, dt.datetime):
        return at.hour * 60 + at.minute
    s = str(at)
    if " " in s:
        s = s.split(" ", 1)[1]
    try:
        h, m = s.split(":")[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def _hour_profile(conn, asof):
    """Her typical Body Battery and stress for each hour of the day.

    Built from the 30 days before `asof` (never `asof` itself, so today cannot
    define its own normal). This is what makes an evening reading judgeable: a
    Body Battery of 24 is alarming at 8am and completely ordinary at 6pm.
    """
    hit = _PROFILE_CACHE.get(asof)
    if hit and (dt.datetime.now() - hit[0]).total_seconds() < _PROFILE_TTL:
        return hit[1]
    since = (dt.date.fromisoformat(asof) - dt.timedelta(days=BASELINE_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT date, minute, body_battery, stress FROM intraday WHERE date >= ? AND date < ?",
        (since, asof)).fetchall()

    # Sleeping minutes are not a fair yardstick for a waking one. Around her wake
    # time an hour is mostly sleep, where stress sits near 10 and Body Battery is
    # still charging, so a normal awake 5:45am was being marked as stressed and
    # under-charged against a baseline of being unconscious.
    asleep = {}
    for r in conn.execute("SELECT sleep_start, sleep_end FROM sleep WHERE date >= ? AND date <= ?",
                          (since, asof)).fetchall():
        s, e = r["sleep_start"], r["sleep_end"]
        if not (s and e):
            continue
        if s[:10] == e[:10]:
            asleep.setdefault(s[:10], []).append((_to_minute(s), _to_minute(e)))
        else:
            asleep.setdefault(s[:10], []).append((_to_minute(s), 24 * 60))
            asleep.setdefault(e[:10], []).append((0, _to_minute(e)))

    buckets = {}
    for r in rows:
        if any(lo <= r["minute"] <= hi for lo, hi in asleep.get(r["date"], ())):
            continue
        b = buckets.setdefault(r["minute"] // 60, {"bb": [], "stress": []})
        if r["body_battery"] is not None:
            b["bb"].append(r["body_battery"])
        if r["stress"] is not None:
            b["stress"].append(r["stress"])
    prof = {}
    for h, b in buckets.items():
        prof[h] = {
            "bb": _median(b["bb"]), "bb_sd": _spread(b["bb"], floor=6.0),
            "stress": _median(b["stress"]), "stress_sd": _spread(b["stress"], floor=8.0),
            "n": len(b["bb"]),
        }
    _PROFILE_CACHE[asof] = (dt.datetime.now(), prof)
    return prof


def _profile_at(prof, minute):
    """Profile for a minute, interpolating over an hour we happen to have no data for."""
    h = int(minute) // 60
    for step in range(0, 4):
        for cand in ((h - step) % 24, (h + step) % 24):
            p = prof.get(cand)
            if p and p["n"] >= 20:
                return p
    return None


def _wake_minute(conn, date, fallback=None):
    """When she actually got up, from Garmin's sleep record.

    Body Battery keeps charging for a while after you wake and peaks later, so
    using its peak as a stand-in put the morning up to an hour off.
    """
    row = conn.execute("SELECT sleep_end FROM sleep WHERE date = ?", (date,)).fetchone()
    if row and row["sleep_end"] and str(row["sleep_end"]).startswith(date):
        m = _to_minute(row["sleep_end"])
        if m is not None:
            return m
    return fallback


def _intraday_state(conn, date, at_minute):
    """Body Battery / stress as of `at_minute`, projecting forward if need be.

    The watch only syncs periodically, so the newest sample is usually behind the
    clock, and asking about a run later today means asking about a time that has
    not happened. Both cases are handled by carrying the last real reading along
    her typical shape for the hours in between, and both are flagged as such.
    """
    rows = conn.execute(
        "SELECT minute, body_battery, stress FROM intraday WHERE date = ? ORDER BY minute",
        (date,)).fetchall()
    if not rows:
        return None
    bb = [r for r in rows if r["body_battery"] is not None]
    if not bb:
        return None
    seen = [r for r in bb if r["minute"] <= at_minute] or [bb[0]]
    last, peak = seen[-1], max(seen, key=lambda r: r["body_battery"])
    st = [r["stress"] for r in rows
          if r["stress"] is not None and last["minute"] - 120 <= r["minute"] <= last["minute"]]

    out = {
        "level": last["body_battery"],
        "sample_minute": last["minute"],
        "peak": peak["body_battery"],
        "peak_minute": peak["minute"],
        "stress": round(statistics.mean(st)) if st else None,
        "gap_min": max(0, int(at_minute) - last["minute"]),
        "projected": False,
    }
    # More than 45 min of unobserved day: walk the level along her usual curve
    # rather than pretending the last reading still holds.
    if out["gap_min"] > 45:
        prof = _hour_profile(conn, date)
        a, b = _profile_at(prof, last["minute"]), _profile_at(prof, at_minute)
        if a and b and a["bb"] is not None and b["bb"] is not None:
            out["level"] = max(0, min(100, round(last["body_battery"] + (b["bb"] - a["bb"]))))
            out["stress"] = round(b["stress"]) if b["stress"] is not None else out["stress"]
            out["projected"] = True
    return out


# ---------- readiness ----------
# Readiness is scored against Lexie's OWN distribution, not textbook absolutes.
# The earlier version started at 100 and only subtracted on fixed cliffs
# (sleep score <70, RHR +3, stress > median+12). Because her sleep scores sit in
# the 80s and her stress never swings 12 points, those branches almost never
# fired: 20 of 22 days scored exactly 100, including a 5.5h night. Each signal
# now returns a continuous 0-1 quality vs her personal baseline, so a genuinely
# average day lands in the 70s and there is headroom in both directions.
#
# Weights: sleep and resting HR still lead, because they set what the day starts
# with, but the two time-of-day signals (what is left in the tank, how wound up
# she is right now) carry more than they used to -- they are the difference
# between a 7am score and a 5:30pm one.
WEIGHTS = {"sleep": 27, "debt": 12, "rhr": 21, "battery": 18, "stress": 12, "load": 10}


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


def readiness(conn, asof=None, at=None):
    """0-100 readiness vs personal baselines, with plain-language reasons.

    `asof` (YYYY-MM-DD) scores a past day using only data available up to that
    date, so a completed run can be judged against the readiness she actually
    had. `at` ('HH:MM', a timestamp, or minutes past midnight) scores a specific
    moment of that day: sleep and resting HR are fixed by morning, but Body
    Battery and stress are read as of that time, so the answer to "am I ready"
    changes between breakfast and an evening run the way the body does.
    """
    asof = asof or dt.date.today().isoformat()
    today_iso = dt.date.today().isoformat()
    at_min = _to_minute(at)
    if at_min is None:
        at_min = _now_minute() if asof == today_iso else 23 * 60 + 59
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

    # --- Body Battery: what is left in the tank AT `at_min`, not at wake ---
    # Two readings of the same number. Against her own curve for this hour, it
    # says whether the day has cost her more than usual; on an absolute scale it
    # says how much is actually there to spend. A normal evening is genuinely
    # less ready than a normal morning, and the absolute half is what keeps that
    # honest instead of grading every 6pm against other 6pms and calling it fine.
    prof = _hour_profile(conn, asof)
    state = _intraday_state(conn, asof, at_min)
    bb_overnight = today_row["body_battery_high"] if today_row else None
    if state:
        lvl = state["level"]
        pref = _profile_at(prof, at_min)
        absolute = _ramp(lvl, 5, 70)
        if pref and pref["bb"] is not None and pref["bb_sd"]:
            z = (lvl - pref["bb"]) / pref["bb_sd"]
            parts["battery"] = 0.55 * _ramp(z, -1.5, 1.0) + 0.45 * absolute
            typical = f", where you are usually near {pref['bb']:.0f} at this hour"
        else:
            parts["battery"] = absolute
            typical = ""
        drop = max(0, state["peak"] - lvl)
        # "Low" has to mean low FOR THIS HOUR. Being in the low 20s by an evening
        # run is routine here, so calling that empty every day would be noise; it
        # only earns a warning when it is also under her own curve.
        below = bool(pref and pref["bb"] is not None
                     and lvl < pref["bb"] - (pref["bb_sd"] or 8))
        if below and lvl < 25:
            reasons.append(f"Body Battery is down to {lvl} at {_hhmm(at_min)}, under your usual "
                           f"{pref['bb']:.0f} for this hour and {drop} off today's peak of {state['peak']}. "
                           "There is not much left to spend: make this an easy effort or move the session.")
            flags.append("battery spent")
        elif below:
            reasons.append(f"Body Battery {lvl} at {_hhmm(at_min)} is below your usual "
                           f"{pref['bb']:.0f} for this time of day: today has taken more out of you than most.")
            flags.append("drained day")
        elif lvl < 20:
            reasons.append(f"Body Battery reads {lvl} at {_hhmm(at_min)}{typical}, so the tank is "
                           "genuinely low, even if that is ordinary for you this late. "
                           "Fine for easy miles; a hard session would be running on fumes.")
        elif drop >= 45:
            reasons.append(f"Body Battery has fallen {drop} points since this morning's {state['peak']} "
                           f"and reads {lvl} at {_hhmm(at_min)}{typical}. Normal for the hour, but it is "
                           "not the body you had at breakfast.")
        else:
            reasons.append(f"Body Battery {lvl} at {_hhmm(at_min)}{typical}.")
    elif bb_overnight:
        # no within-day curve (watch not synced, or older history): fall back to
        # the overnight peak, which is all the daily summary knows
        parts["battery"] = _ramp(bb_overnight, 35, 90)
        if bb_overnight < 55:
            reasons.append(f"Body Battery only recharged to {bb_overnight} overnight.")
            flags.append("low battery")

    # --- Stress in the last couple of hours, vs her own norm for this hour ---
    cur_stress = state["stress"] if state else None
    pref = _profile_at(prof, at_min) if state else None
    if cur_stress is not None and pref and pref["stress"] is not None and pref["stress_sd"]:
        z = (cur_stress - pref["stress"]) / pref["stress_sd"]
        parts["stress"] = 1.0 - _ramp(z, -1.0, 2.0)
        if z >= 1.0:
            reasons.append(f"Stress has been running at {cur_stress} into {_hhmm(at_min)}, "
                           f"above your usual {pref['stress']:.0f} for this hour. "
                           "Warm up longer than feels necessary and let the first mile be slow.")
            flags.append("high stress")
        elif z <= -0.8:
            reasons.append(f"Stress is low for this time of day ({cur_stress} vs your usual {pref['stress']:.0f}).")
    else:
        # fall back to the day's average stress against her day-average baseline
        stress_series = [d["avg_stress"] for d in daily if d["avg_stress"]]
        day_stress = today_row["avg_stress"] if today_row else None
        s_med, s_sd = _median(stress_series[:-1]), _spread(stress_series[:-1], floor=3.0)
        if day_stress and s_med and s_sd:
            z = (day_stress - s_med) / s_sd
            parts["stress"] = 1.0 - _ramp(z, -0.5, 2.0)
            if z >= 1.0:
                reasons.append(f"All-day stress ({day_stress}) is running above your norm ({s_med:.0f}).")
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

    # Acute constraints. A signal this bad is not merely one input to average
    # away against an excellent resting HR: it caps the day.
    caps = []
    if parts.get("sleep") is not None and parts["sleep"] < 0.55:
        caps.append((round(50 + 60 * parts["sleep"]),
                     "Other signals look fine, but that is capped by how little you actually slept -- "
                     "the rest of the body hasn't caught up with it yet."))
    if parts.get("rhr") is not None and parts["rhr"] < 0.45:
        caps.append((round(45 + 70 * parts["rhr"]),
                     "That is capped by a resting heart rate this far over baseline, which is the "
                     "clearest signal there is that recovery is unfinished."))
    # A day already spent cannot read as a fresh one. Being above your own curve
    # for 5pm is genuinely good news, but it is still 5pm: five-sixths of the
    # day's Body Battery is gone and the morning's numbers cannot buy it back.
    # Without this, a great evening scored the same as a great breakfast.
    if state and state["level"] is not None:
        caps.append((round(70 + 30 * _ramp(state["level"], 10, 60)),
                     f"Scored for {_hhmm(at_min)}, not for this morning: with Body Battery at "
                     f"{state['level']} you are working with what the day has left you, however good "
                     "the overnight numbers were."))
    ceiling = min([c[0] for c in caps], default=100)
    if score > ceiling:
        reasons.append(min(caps, key=lambda c: c[0])[1])
        score = ceiling
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
        # which moment of the day this score describes
        "at_minute": at_min,
        "at_label": _hhmm(at_min),
        "body_battery": state["level"] if state else bb_overnight,
        "body_battery_peak": state["peak"] if state else bb_overnight,
        "stress_now": state["stress"] if state else None,
        "projected": bool(state and state["projected"]),
        "synced_at": _hhmm(state["sample_minute"]) if state else None,
        "stale_min": state["gap_min"] if state else None,
    }


def usual_run_minute(conn, date):
    """The time of day she actually runs on this weekday, from her own history.

    Her start times are strongly bimodal: weekday sessions go out around 5-6pm,
    weekend ones mid-morning. A single median across all runs would land in the
    early afternoon and describe no run she has ever done, so this matches the
    weekday first and only falls back to the weekday/weekend split.
    """
    rows = conn.execute(
        "SELECT start_local FROM activities WHERE type LIKE '%running%' AND start_local IS NOT NULL "
        "AND date >= ? ORDER BY start_local", (TRAINING_START,)).fetchall()
    same, group = [], []
    weekend = date.weekday() >= 5
    for r in rows:
        try:
            d = dt.datetime.strptime(r["start_local"], "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        m = d.hour * 60 + d.minute
        if d.weekday() == date.weekday():
            same.append(m)
        if (d.weekday() >= 5) == weekend:
            group.append(m)
    for series, need in ((same, 3), (group, 3)):
        if len(series) >= need:
            return int(statistics.median(series))
    return None


def day_outlook(conn):
    """How readiness moves across today: at wake, right now, and at run time.

    A morning number answers the wrong question when the run is twelve hours
    away. This scores the same day three times so the dashboard can show what
    the day has already cost and what is likely left by the time she laces up.
    """
    today = dt.date.today()
    iso = today.isoformat()
    now_min = _now_minute()
    out = {}

    state = _intraday_state(conn, iso, now_min)
    wake = _wake_minute(conn, iso, state["peak_minute"] if state else 7 * 60)
    if wake <= now_min - 45:
        m = readiness(conn, asof=iso, at=wake)
        out["morning"] = {"score": m["score"], "label": m["label"], "at_label": _hhmm(wake),
                          "body_battery": m["body_battery"]}

    # Today's session, honouring a reschedule that moved something onto today.
    due = next((w for w in planmod.plan_with_actuals(conn)
                if (w.get("moved_to") or w["date"]) == iso), None)
    if due and due.get("status") == "done":
        out["workout"] = {"summary": due["summary"], "status": "done"}
        return out
    if not due or not due.get("planned_miles"):
        return out

    run_min = usual_run_minute(conn, today)
    # only worth projecting if the run is still meaningfully ahead of the clock
    if run_min is not None and run_min > now_min + 30:
        r = readiness(conn, asof=iso, at=run_min)
        out["at_run"] = {
            "score": r["score"], "label": r["label"], "verdict": r["verdict"],
            "at_label": _hhmm(run_min), "body_battery": r["body_battery"],
            "flags": r["flags"], "projected": True,
        }
    out["workout"] = {"summary": due["summary"], "type": due["type"],
                      "miles": due["planned_miles"], "status": due.get("status"),
                      "at_label": _hhmm(run_min) if run_min is not None else None}
    return out


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


def _readiness_on(conn, ctx, date, at=None):
    """Readiness for a past run, scored at the hour that run actually started.

    An evening run judged against its own morning score was being credited with
    a freshness she no longer had by the time she went out.
    """
    key = (date, _to_minute(at))
    if key not in ctx["readiness_cache"]:
        try:
            r = readiness(conn, asof=date, at=at)
            # Also score that morning, so an evening run can be read against the
            # freshness it started from rather than against nothing.
            st = _intraday_state(conn, date, 23 * 60 + 59)
            wake = _wake_minute(conn, date, st["peak_minute"] if st else None)
            if wake is not None and _to_minute(at) and wake < _to_minute(at) - 45:
                m = readiness(conn, asof=date, at=wake)
                r["morning_score"], r["morning_at"] = m["score"], _hhmm(wake)
            ctx["readiness_cache"][key] = r
        except Exception:
            ctx["readiness_cache"][key] = None
    return ctx["readiness_cache"][key]


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

    # --- 0. rescheduled: credit the move, don't treat the shifted day as a slip ---
    if w.get("rescheduled") and w.get("moved_to"):
        orig = dt.date.fromisoformat(w["date"])
        new = dt.date.fromisoformat(w["moved_to"])
        points.append(
            f"Rescheduled from {orig.strftime('%a %m-%d')} to {new.strftime('%a %m-%d')} "
            f"and you got it done. Moving a run to protect it counts as consistency, not a missed session.")

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

    # --- 5. readiness she actually had when she started this run ---
    # Scored at the run's own start time, not at wake: a 6pm run begins on a body
    # that has already spent the day, and judging it against breakfast flattered it.
    started = a.get("start_local")
    ready = _readiness_on(conn, ctx, date, at=started) if conn is not None and ctx is not None else None
    when = f" at {ready['at_label']}, when you set off," if ready and started else " that day"
    # how much the day had already taken before the run: the point of scoring at
    # the start line rather than at breakfast
    fell = (f", down from {ready['morning_score']} at {ready['morning_at']}"
            if ready and ready.get("morning_score") and ready["morning_score"] - ready["score"] >= 4 else "")
    if ready:
        rs = ready["score"]
        if rs < 65 and rating in ("breakthrough", "strong", "solid"):
            points.append(f"Worth noting: readiness{when} was only {rs} ({ready['label']}){fell} -- "
                          f"{', '.join(ready['flags']) if ready['flags'] else 'recovery signals were down'}. "
                          "Running this well on a depleted body counts for more, not less.")
            rating = "gutsy"
        elif rs < 65 and rating in ("flat", "short", "ran-hot"):
            points.append(f"Readiness{when} was {rs} ({ready['label']}){fell}"
                          + (f" -- {', '.join(ready['flags'])}" if ready['flags'] else "")
                          + ". This is the honest reason the run felt like work, and it is not a fitness problem.")
        elif rs >= 85 and rating == "flat":
            points.append(f"Readiness{when} was still high ({rs}){fell}, so the dip is not recovery. "
                          "More likely conditions, fuelling, or simply a flat day -- everyone gets them.")
        elif rs >= 85:
            points.append(f"Readiness{when} was {rs}{fell}: "
                          + ("still plenty in the tank, and you used it well."
                             if fell else "you went in fresh and used it well."))
        elif rs >= 72:
            points.append(f"Readiness{when} was {rs} ({ready['label']}){fell}. A normal working body rather "
                          "than a fresh one, which is what most training actually happens on.")

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
            "readiness_that_day": ready["score"] if ready else None,
            "readiness_at": ready["at_label"] if (ready and started) else None,
            "readiness_morning": ready.get("morning_score") if ready else None}


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

    # 1) Low readiness + a hard session today/tomorrow. Today's session is judged
    #    at the hour she usually runs, since that is the body doing the workout.
    run_min = usual_run_minute(conn, today)
    at_run = readiness(conn, at=run_min) if run_min and run_min > _now_minute() + 30 else ready
    for w in upcoming[:2]:
        if w["type"] not in HARD_TYPES:
            continue
        r = at_run if w["date"] == today.isoformat() else ready
        if r["score"] < 55:
            when = (f"By {r['at_label']}, when you usually run, readiness projects to {r['score']}"
                    if r is at_run and r is not ready else f"Readiness is low ({r['score']})")
            out.append({"severity": "high", "date": w["date"],
                "text": f"{when} and {w['date']} is a {w['type']} day ({w['planned_miles']} mi). Consider swapping it for an easy run or rest, and moving the quality session later in the week."})
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

    # 3) Rescheduled workouts still ahead of you: acknowledge the move so a run you
    #    deliberately shifted never reads as a lapse.
    def _due(w):
        return w.get("moved_to") or w["date"]

    moved_ahead = [w for w in plan if w.get("rescheduled") and w["status"] == "upcoming"
                   and _due(w) <= (today + dt.timedelta(days=2)).isoformat()]
    if moved_ahead:
        w = moved_ahead[0]
        due = dt.date.fromisoformat(_due(w))
        when = "today" if due == today else "tomorrow" if due == today + dt.timedelta(days=1) \
            else f"on {due.strftime('%a %m-%d')}"
        out.append({"severity": "low", "date": _due(w),
            "text": f"Your {w['type']} ({w['planned_miles']} mi) moved from {dt.date.fromisoformat(w['date']).strftime('%a %m-%d')} to {due.strftime('%a %m-%d')} and is on for {when}. Nothing missed, just shifted. Run it as written."})

    # 4) Missed hard workouts recently. A rescheduled run is judged from the day it
    #    was moved to, so it only counts as missed once THAT day has passed.
    recent_missed = [w for w in plan if w["status"] == "missed"
                     and w["type"] in HARD_TYPES
                     and _due(w) >= (today - dt.timedelta(days=4)).isoformat()]
    if recent_missed:
        w = recent_missed[-1]
        moved = f" (already moved from {w['date']})" if w.get("rescheduled") else ""
        out.append({"severity": "low", "date": _due(w),
            "text": f"Missed the {_due(w)} {w['type']} ({w['planned_miles']} mi){moved}. Don't cram it back in. Just resume the schedule; one missed session won't hurt the Nov 7 goal."})

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

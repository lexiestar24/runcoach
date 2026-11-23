"""Freeze a finished plan into archive/<key>.json.

The snapshot holds everything the plan's tab shows: every workout with its
matched run and coaching evaluation, the race projection and race results
(when the plan ended in a race), and plan-level totals. It is built once, so
retuning race.py or the evaluation for the next race never rewrites what the
dashboard said about this one. Rebuild on purpose with --force.

    python archive.py madison-2026 [--force]
"""
import datetime as dt
import json
import os
import sys

import db
import engine
import plan as planmod
import plans as plansmod

MILE_M = 1609.344


def _stats(conn, workouts, meta):
    done = [w for w in workouts if w["status"] == "done"]
    runs = conn.execute(
        "SELECT * FROM activities WHERE type LIKE '%running%' AND date >= ? AND date <= ?",
        (meta["start"], meta["end"])).fetchall()
    miles = [(r["distance_m"] or 0) / MILE_M for r in runs]
    return {
        "weeks": max((w["week"] or 0 for w in workouts), default=0),
        "workouts_planned": len(workouts),
        "workouts_done": len(done),
        "consistency_pct": round(100 * len(done) / len(workouts)) if workouts else None,
        "planned_miles": round(sum(w["planned_miles"] or 0 for w in workouts), 1),
        "run_miles": round(sum(miles), 1),
        "runs": len(runs),
        "longest_run_mi": round(max(miles, default=0), 1),
    }


def build(key, force=False):
    meta = plansmod.get(key)
    if not meta:
        sys.exit(f"no plan '{key}' in plans.json")
    out_path = plansmod.archive_path(key)
    if os.path.exists(out_path) and not force:
        sys.exit(f"{out_path} exists; pass --force to rebuild it")

    conn = db.connect()
    try:
        workouts = planmod.plan_with_actuals(conn, meta["ics"])
        ctx = engine.evaluation_context(conn)
        for w in workouts:
            if w.get("actual"):
                w["evaluation"] = engine.evaluate(w, conn=conn, ctx=ctx)
            if w["status"] == "upcoming":   # a finished plan has nothing ahead
                w["status"] = "missed"
        snap = {
            "archived_at": dt.datetime.now().isoformat(timespec="seconds"),
            "plan": plansmod.public(meta),
            "stats": _stats(conn, workouts, meta),
            "workouts": workouts,
            "race": None,
            "results": [],
        }
        if meta.get("race"):
            import race as racemod
            import results as resultsmod
            snap["race"] = racemod.summary(conn, workouts)
            snap["results"] = resultsmod.summary(conn, snap["race"]["projection"])
    finally:
        conn.close()

    os.makedirs(plansmod.ARCHIVE_DIR, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1, default=str)
    return out_path, snap


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit(__doc__)
    path, snap = build(args[0], force="--force" in sys.argv)
    s = snap["stats"]
    print(f"wrote {path}: {s['workouts_done']}/{s['workouts_planned']} workouts, "
          f"{s['run_miles']} mi run, {len(snap['results'])} race result(s)")

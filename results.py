"""Race results: what happened on race day, mile by mile, next to what was projected.

Watch lap distances are not trusted for races: crowds, buildings and tight
corners make GPS drift, and a few percent long on every mile adds up to half a
mile by the finish. Splits are rebuilt from the GPS *track* instead (the sum of
the distances between track points), scaled to the distance the runner recorded
for the activity, and stored once per race in `race_results`.
"""
import datetime as dt
import json
import math
import os

MILE_M = 1609.344

# One entry per race, kept in races.json (gitignored: it holds personal notes;
# races.sample.json shows the shape). `recorded_mi` is the distance saved on the
# Garmin activity (edited by hand when the watch was off); `official_mi` is the
# certified course. `chip_time` is the official result once known ("h:mm:ss"),
# else the watch time is used.
RACES_PATH = os.environ.get("RUNCOACH_RACES",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)), "races.json"))


def load_races():
    try:
        with open(RACES_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return []


SCHEMA = """
CREATE TABLE IF NOT EXISTS race_results (
    activity_id  INTEGER PRIMARY KEY,
    splits_json  TEXT,
    track_mi     REAL,
    raw_mi       REAL,
    built_at     TEXT DEFAULT (datetime('now'))
);
"""


def _hav(a, b, c, d):
    p = math.radians
    return 2 * 6371000 * math.asin(math.sqrt(
        math.sin(p(c - a) / 2) ** 2 + math.cos(p(a)) * math.cos(p(c)) * math.sin(p(d - b) / 2) ** 2))


def build_splits(details, recorded_mi):
    """Per-mile splits from Garmin activity details (the per-second stream)."""
    idx = {m["key"]: m["metricsIndex"] for m in details["metricDescriptors"]}
    rows = [r["metrics"] for r in details["activityDetailMetrics"]]
    get = lambda r, k: r[idx[k]] if k in idx else None
    elev_key = "directCorrectedElevation" if "directCorrectedElevation" in idx else "directElevation"
    t0 = get(rows[0], "directTimestamp")
    pts, cum, prev = [], 0.0, None
    for r in rows:
        la, lo = get(r, "directLatitude"), get(r, "directLongitude")
        if la is not None and prev:
            cum += _hav(prev[0], prev[1], la, lo)
        if la is not None:
            prev = (la, lo)
        pts.append({"t": (get(r, "directTimestamp") - t0) / 1000.0, "d": cum,
                    "hr": get(r, "directHeartRate"), "v": get(r, "directSpeed"),
                    "el": get(r, elev_key)})
    track_m = pts[-1]["d"]
    raw_m = get(rows[-1], "sumDistance") or 0
    scale = recorded_mi * MILE_M / track_m if track_m else 1.0

    splits, start, mark = [], 0, 1.0
    for i, p in enumerate(pts):
        mi = p["d"] * scale / MILE_M
        last = i == len(pts) - 1
        if mi < mark and not last:
            continue
        seg = pts[start:i + 1]
        dist = mi - (mark - 1)
        dt_s = p["t"] - pts[start]["t"]
        hrs = [x["hr"] for x in seg if x["hr"]]
        slow = sum(b["t"] - a["t"] for a, b in zip(seg, seg[1:]) if (b["v"] or 0) < 1.9)
        gain = loss = 0.0
        for a, b in zip(seg, seg[1:]):
            if a["el"] is None or b["el"] is None:
                continue
            de = b["el"] - a["el"]
            gain, loss = (gain + de, loss) if de > 0 else (gain, loss - de)
        if dist > 0.05:
            splits.append({
                "mile": mark if mi >= mark else round(mi, 2),
                "distance_mi": round(dist, 2),
                "pace": round(dt_s / 60.0 / dist, 3),
                "seconds": round(dt_s),
                "avg_hr": round(sum(hrs) / len(hrs)) if hrs else None,
                "max_hr": max(hrs) if hrs else None,
                "slow_pct": round(100 * slow / dt_s) if dt_s else 0,   # time under ~14:07/mi
                "gain_ft": round(gain * 3.28084), "loss_ft": round(loss * 3.28084),
            })
        start, mark = i, mark + 1
    return splits, track_m / MILE_M, raw_m / MILE_M


def ensure(g, conn):
    """Build splits for any configured race that has none yet. Called from ingest."""
    conn.executescript(SCHEMA)
    built = 0
    for race in load_races():
        aid = race["activity_id"]
        if conn.execute("SELECT 1 FROM race_results WHERE activity_id=?", (aid,)).fetchone():
            continue
        if not conn.execute("SELECT 1 FROM activities WHERE activity_id=?", (aid,)).fetchone():
            continue
        splits, track_mi, raw_mi = build_splits(g.get_activity_details(aid, maxchart=5000), race["recorded_mi"])
        conn.execute("INSERT OR REPLACE INTO race_results (activity_id, splits_json, track_mi, raw_mi) "
                     "VALUES (?,?,?,?)", (aid, json.dumps(splits), track_mi, raw_mi))
        conn.commit()
        built += 1
    return built


def _fmt_pace(p):
    m = int(p)
    s = int(round((p - m) * 60))
    return f"{m + 1}:00" if s == 60 else f"{m}:{s:02d}"


def _fmt_hms(sec):
    sec = int(round(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def _parse_hms(s):
    h, m, sec = (int(x) for x in s.split(":"))
    return h * 3600 + m * 60 + sec


def _seg(splits, lo, hi):
    """Pace / HR over whole miles lo..hi (1-based, inclusive; hi may be the final partial)."""
    pick = [s for s in splits if lo <= math.ceil(s["mile"]) <= hi]
    if not pick:
        return None
    dist = sum(s["distance_mi"] for s in pick)
    sec = sum(s["seconds"] for s in pick)
    hr_t = [(s["avg_hr"], s["seconds"]) for s in pick if s["avg_hr"]]
    return {"pace": sec / 60.0 / dist, "pace_str": _fmt_pace(sec / 60.0 / dist),
            "avg_hr": round(sum(h * t for h, t in hr_t) / sum(t for _, t in hr_t)) if hr_t else None,
            "minutes": round(sec / 60.0, 1)}


def summary(conn, projection=None):
    """Everything the Race results section shows, newest race first."""
    conn.executescript(SCHEMA)
    out = []
    for race in reversed(load_races()):
        a = conn.execute("SELECT * FROM activities WHERE activity_id=?", (race["activity_id"],)).fetchone()
        rr = conn.execute("SELECT * FROM race_results WHERE activity_id=?", (race["activity_id"],)).fetchone()
        if not a:
            continue
        splits = json.loads(rr["splits_json"]) if rr else []
        watch_s = sum(s["seconds"] for s in splits) or a["duration_s"]
        chip = race.get("chip_time")
        finish_s = _parse_hms(chip) if chip else watch_s
        prior_max = conn.execute(
            "SELECT MAX(max_hr) FROM activities WHERE type LIKE '%running%' AND date < ?",
            (race["date"],)).fetchone()[0]
        cond = {}
        try:
            import weather
            c = weather.by_activity(conn).get(race["activity_id"]) or {}
            cond = {"temp_f": c.get("temp_f"), "temp_end_f": c.get("temp_end_f"),
                    "dew_f": c.get("dew_f"), "wind_mph": c.get("wind_mph")}
        except Exception:
            pass

        segments = []
        P = projection or {}
        rp_phases = {p["miles"]: p for p in (P.get("race_plan") or {}).get("phases", [])}
        last = math.ceil(splits[-1]["mile"]) if splits else 14
        for label, lo, hi in (("1-4", 1, 4), ("5-9", 5, 9), ("10-12", 10, 12), ("13", 13, last)):
            s = _seg(splits, lo, hi)
            if s:
                p = rp_phases.get(label)
                s.update({"miles": label, "projected_pace_str": p["pace_str"] if p else None,
                          "delta_s": round((s["pace"] - p["pace"]) * 60) if p else None})
                segments.append(s)
        first4, last4 = _seg(splits, 1, 4), _seg(splits, 10, 13)

        proj = None
        if P.get("available"):
            pub = P.get("published") or {}
            proj = {
                "published_finish": pub.get("finish"), "published_band": pub.get("band"),
                "recal_finish": P["finish_central"], "recal_band": P["finish_band"],
                "recal_in_band": P["finish_lo_min"] <= finish_s / 60.0 <= P["finish_hi_min"],
                "recal_miss_min": round(finish_s / 60.0 - P["finish_min"], 1),
                "published_miss_min": round(finish_s / 60.0 - _parse_hms(pub["finish"] + ":00") / 60.0, 1)
                if pub.get("finish") else None,
            }
        out.append({
            "key": race["key"], "name": race["name"], "date": race["date"],
            "first": race.get("first", False),
            "finish": _fmt_hms(finish_s), "finish_is_chip": bool(chip),
            "recorded_mi": race["recorded_mi"], "official_mi": race["official_mi"],
            "pace_official_str": _fmt_pace(finish_s / 60.0 / race["official_mi"]),
            "pace_recorded_str": _fmt_pace(finish_s / 60.0 / race["recorded_mi"]),
            "avg_hr": a["avg_hr"], "max_hr": a["max_hr"],
            "new_max_hr": bool(prior_max and a["max_hr"] and a["max_hr"] > prior_max),
            "prior_max_hr": prior_max,
            "elevation_gain_ft": round((a["elevation_gain"] or 0) * 3.28084),
            "raw_watch_mi": round(rr["raw_mi"], 2) if rr else None,
            "track_mi": round(rr["track_mi"], 2) if rr else None,
            "conditions": cond,
            "splits": [dict(s, pace_str=_fmt_pace(s["pace"])) for s in splits],
            "segments": segments,
            "negative_split_s": round((first4["pace"] - last4["pace"]) * 60) if first4 and last4 else None,
            "goals": [{"label": g, "met": m} for g, m in race["goals"]],
            "notes": race["notes"],
            "split_note": race.get("split_note"),
            "unmodelled": race.get("unmodelled"),
            "data_note": race.get("data_note"),
            "projection": proj,
        })
    return out


if __name__ == "__main__":
    import db
    conn = db.connect()
    print(json.dumps(summary(conn), indent=2))

"""Training-plan registry: which plan is active and which are archived.

The registry lives in a gitignored plans.json (personal paths and notes);
plans.sample.json shows the shape. Without a registry, RunCoach runs in
single-plan mode on RUNCOACH_ICS / ./plan.ics, exactly as before.

Each plan: key, name, short (tab label), status ("active" | "archived"),
ics (path, ~ expanded), start/end (ISO dates), optional race
({key, name, date}), optional blocks ([{name, start, end, focus}]) and notes.
An archived plan is served from its frozen snapshot (archive/<key>.json),
built once by archive.py, so later model changes never rewrite history.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
PLANS_PATH = os.environ.get("RUNCOACH_PLANS", os.path.join(HERE, "plans.json"))
ARCHIVE_DIR = os.path.join(HERE, "archive")


def _default():
    return [{
        "key": "default", "name": "Training plan", "short": "Plan", "status": "active",
        "ics": os.environ.get("RUNCOACH_ICS", os.path.join(HERE, "plan.ics")),
    }]


def load():
    try:
        with open(PLANS_PATH, encoding="utf-8") as f:
            plans = json.load(f)
    except FileNotFoundError:
        return _default()
    for p in plans:
        if p.get("ics"):
            p["ics"] = os.path.expanduser(p["ics"])
    return plans


def active():
    plans = load()
    return next((p for p in plans if p.get("status") == "active"), plans[0])


def get(key):
    return next((p for p in load() if p["key"] == key), None)


def archive_path(key):
    return os.path.join(ARCHIVE_DIR, f"{key}.json")


def public(p):
    """What the dashboard needs about a plan (no file paths)."""
    keep = ("key", "name", "short", "status", "start", "end", "race", "blocks", "notes")
    out = {k: p[k] for k in keep if k in p}
    out["has_archive"] = p.get("status") == "archived" and os.path.exists(archive_path(p["key"]))
    return out

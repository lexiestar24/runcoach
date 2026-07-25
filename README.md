# RunCoach

A self-hosted running dashboard that pairs **Garmin Connect** data with a
half-marathon training plan, then turns it into readiness scores, coaching tips,
milestone tracking, and data-driven plan adjustments. Runs on a Raspberry Pi and
is reached from phone and desktop over Tailscale. No cloud, no app store.

## What it does

- **Readiness** score from resting-HR trend vs your personal baseline, sleep
  score, sleep debt, and stress.
- **Milestones** — weeks to race, plan week, total miles, longest run, VO2max
  trend, consistency.
- **Coaching tips** and **plan adjustments** driven by your recovery signals and
  scheduling conflicts (recovery-first philosophy: keep easy days easy).
- **Charts** — weekly mileage planned vs actual, resting HR, sleep duration, and
  an effort chart whose "easy" HR band is derived from your own easy runs.
- **Per-run detail** with a coaching evaluation of each completed workout and the
  note you wrote on the Garmin activity.

## Architecture

| File | Role |
|------|------|
| `db.py` | SQLite schema + connection (tables: daily, sleep, activities, conflicts, reschedules) |
| `ingest.py` | Pulls Garmin stats / sleep / activities into SQLite |
| `plan.py` | Parses the training plan `.ics` into workouts and matches actual runs |
| `engine.py` | Readiness, milestones, tips, adjustments, per-run evaluation, chart series |
| `app.py` | FastAPI API + serves the dashboard |
| `static/index.html` | Self-contained dashboard (vanilla JS, inline-SVG charts, dark theme) |
| `deploy/` | systemd user units for the app + a daily Garmin sync timer |

## Setup

```bash
python -m venv venv && . venv/bin/activate
pip install -r requirements.txt

# One-time Garmin login (stores a ~1-year token under ~/.garminconnect)
python -c "from garminconnect import Garmin; import getpass; \
  g=Garmin(input('email: '), getpass.getpass()); g.login(); g.garth.dump('~/.garminconnect')"

# Point at your plan's .ics (or set RUNCOACH_ICS)
export RUNCOACH_ICS=/path/to/your-plan.ics

python ingest.py --days 45 --activities 50   # backfill history
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Open `http://<host>:8000`.

## Deployment (systemd, user services)

```bash
cp deploy/*.service deploy/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now runcoach.service        # the dashboard
systemctl --user enable --now runcoach-ingest.timer   # daily 06:15 Garmin sync
loginctl enable-linger "$USER"                          # survive logout/reboot
```

## Notes

- `runcoach.db` holds personal health data and is git-ignored.
- The training plan is expected as an `.ics` of dated workouts; effort types
  (easy / quality / tempo / interval / long / race) are inferred from each
  event's description.

Built with [Claude Code](https://claude.com/claude-code).

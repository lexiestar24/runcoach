"""RunCoach API + dashboard server (FastAPI)."""
import os
import threading
import datetime as dt
import mimetypes

mimetypes.add_type("application/manifest+json", ".webmanifest")  # PWA manifest

from fastapi import FastAPI, Body
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

import db
import engine
import plan as planmod

HERE = os.path.dirname(os.path.abspath(__file__))
app = FastAPI(title="RunCoach")


@app.get("/api/dashboard")
def dashboard():
    conn = db.connect()
    try:
        data = {
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "readiness": engine.readiness(conn),
            "weekly": engine.weekly_load(conn),
            "milestones": engine.milestones(conn),
            "tips": engine.tips(conn),
            "adjustments": engine.adjustments(conn),
            "series": engine.series(conn),
            "plan": planmod.plan_with_actuals(conn),
        }
        ctx = engine.evaluation_context(conn)
        for w in data["plan"]:
            if w.get("actual"):
                w["evaluation"] = engine.evaluate(w, conn=conn, ctx=ctx)
    finally:
        conn.close()
    return JSONResponse(data)


@app.post("/api/conflict")
def add_conflict(payload: dict = Body(...)):
    conn = db.connect()
    try:
        engine.add_conflict(conn, payload["date"], payload.get("note", ""))
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/conflict/{date}")
def del_conflict(date: str):
    conn = db.connect()
    try:
        engine.remove_conflict(conn, date)
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/reschedule")
def add_reschedule(payload: dict = Body(...)):
    conn = db.connect()
    try:
        engine.add_reschedule(conn, payload["orig_date"], payload["new_date"], payload.get("note", ""))
    finally:
        conn.close()
    return {"ok": True}


@app.delete("/api/reschedule/{orig_date}")
def del_reschedule(orig_date: str):
    conn = db.connect()
    try:
        engine.remove_reschedule(conn, orig_date)
    finally:
        conn.close()
    return {"ok": True}


_refreshing = {"busy": False}


def _do_refresh():
    try:
        import ingest
        ingest.run(days=3, activities_count=30)
    finally:
        _refreshing["busy"] = False


@app.post("/api/refresh")
def refresh():
    if not _refreshing["busy"]:
        _refreshing["busy"] = True
        threading.Thread(target=_do_refresh, daemon=True).start()
    return {"ok": True, "refreshing": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(HERE, "static", "index.html"))


app.mount("/", StaticFiles(directory=os.path.join(HERE, "static")), name="static")

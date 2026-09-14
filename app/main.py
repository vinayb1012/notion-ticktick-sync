import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, FileResponse
from starlette.staticfiles import StaticFiles

from app.models import SyncResult, TasksResponse, Task, WeeklyItem
from app.sync import sync_date, sync_week
from app.ticktick import get_tasks_for_date
from app.config import load_config
from app.weekly import (
    get_week_bounds,
    list_weekly_items,
    create_weekly_item,
    set_weekly_item_done,
    update_weekly_item_priority,
    delete_weekly_item,
)

import httpx
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ticktick-notion-sync")

app = FastAPI(title="TickTick → Notion Sync", version="1.0.0")

DIST_DIR = Path(__file__).parent.parent / "frontend" / "dist"

# Serve static assets (JS, CSS, etc.)
app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="static")

# In-memory job store (with history)
_jobs: dict[str, dict] = {}
_job_counter = 0
_job_history: list[dict] = []
_MAX_HISTORY = 50


@app.get("/api/health")
async def health():
    cfg = load_config()
    return {
        "status": "ok",
        "ticktick_configured": bool(cfg.get("ticktick_access_token")),
        "notion_configured": bool(cfg.get("notion_token")),
    }


@app.get("/api/tasks/{date_str}")
async def get_tasks(date_str: str) -> TasksResponse:
    """Fetch TickTick tasks for a specific date (YYYY-MM-DD)."""
    # Validate date format
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date format. Use YYYY-MM-DD."})

    cfg = load_config()
    async with httpx.AsyncClient(timeout=30) as client:
        tasks = await get_tasks_for_date(cfg, client, date_str)

    return TasksResponse(
        date=date_str,
        tasks=[Task(**t) for t in tasks],
        total=len(tasks),
    )


@app.post("/api/sync/{date_str}")
async def start_sync(date_str: str, background_tasks: BackgroundTasks):
    """Start a sync job for the given date. Returns immediately with a job ID."""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "Invalid date format. Use YYYY-MM-DD."})

    global _job_counter
    _job_counter += 1
    job_id = f"job-{_job_counter}"
    _jobs[job_id] = {"status": "running", "date": date_str, "result": None, "error": None}

    async def _run(job_id: str, date_str: str):
        try:
            result = await sync_date(date_str)
            _jobs[job_id].update(status="done", result=result)
            _remember(job_id)
        except Exception as e:
            log.error(f"Sync failed for {date_str}: {e}")
            _jobs[job_id].update(status="error", error=str(e))
            _remember(job_id)

    background_tasks.add_task(_run, job_id, date_str)
    return {"job_id": job_id, "status": "running", "date": date_str}


@app.get("/api/sync/status/{job_id}")
async def sync_status(job_id: str):
    """Check the status of a sync job."""
    if job_id not in _jobs:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return _jobs[job_id]


@app.get("/api/sync/history")
async def sync_history():
    """Recent sync job history (newest first)."""
    return {"history": list(reversed(_job_history[-_MAX_HISTORY:]))}


def _remember(job_id: str):
    job = dict(_jobs[job_id])
    job["job_id"] = job_id
    job["finished_at"] = datetime.now(timezone.utc).isoformat()
    _job_history.append(job)


# ---- Weekly items ----


def _week_or_400(date_str: str) -> tuple[str, str]:
    try:
        return get_week_bounds(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")


@app.get("/api/week/{date_str}")
async def get_week(date_str: str):
    """Week bounds + all weekly items for the week containing date_str."""
    week_start, week_end = _week_or_400(date_str)
    cfg = load_config()
    async with httpx.AsyncClient(timeout=30) as client:
        items = await list_weekly_items(cfg, client, week_start)
    return {
        "week_start": week_start,
        "week_end": week_end,
        "items": [WeeklyItem(**i) for i in items],
    }


@app.post("/api/week/{date_str}/items")
async def add_item(date_str: str, body: dict):
    """Add a weekly item. Body: {name, priority?}. Week derived from date_str."""
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    priority = body.get("priority") or "2. Medium"
    if priority not in ("1. High", "2. Medium", "3. Low"):
        raise HTTPException(status_code=400, detail=f"invalid priority: {priority}")
    week_start, _ = _week_or_400(date_str)
    cfg = load_config()
    async with httpx.AsyncClient(timeout=30) as client:
        item = await create_weekly_item(cfg, client, name, week_start, priority)
    return WeeklyItem(**item)


@app.patch("/api/items/{page_id}/done")
async def toggle_item_done(page_id: str, body: dict):
    cfg = load_config()
    async with httpx.AsyncClient(timeout=30) as client:
        await set_weekly_item_done(cfg, client, page_id, bool(body.get("done", False)))
    return {"ok": True}


@app.patch("/api/items/{page_id}/priority")
async def change_item_priority(page_id: str, body: dict):
    priority = body.get("priority") or ""
    if priority not in ("1. High", "2. Medium", "3. Low"):
        raise HTTPException(status_code=400, detail=f"invalid priority: {priority}")
    cfg = load_config()
    async with httpx.AsyncClient(timeout=30) as client:
        await update_weekly_item_priority(cfg, client, page_id, priority)
    return {"ok": True}


@app.delete("/api/items/{page_id}")
async def remove_item(page_id: str):
    cfg = load_config()
    async with httpx.AsyncClient(timeout=30) as client:
        await delete_weekly_item(cfg, client, page_id)
    return {"ok": True}


@app.post("/api/sync-week/{date_str}")
async def start_week_sync(date_str: str, background_tasks: BackgroundTasks):
    """Start a weekly-items sync job for the week containing date_str."""
    week_start, week_end = _week_or_400(date_str)
    global _job_counter
    _job_counter += 1
    job_id = f"week-{_job_counter}"
    _jobs[job_id] = {"status": "running", "date": week_start, "kind": "week"}

    async def _run(job_id: str, ws: str, we: str):
        try:
            result = await sync_week(ws, we)
            _jobs[job_id].update(status="done", result=result)
            _remember(job_id)
        except Exception as e:
            log.error(f"Week sync failed for {ws}: {e}")
            _jobs[job_id].update(status="error", error=str(e))
            _remember(job_id)

    background_tasks.add_task(_run, job_id, week_start, week_end)
    return {"job_id": job_id, "status": "running", "week_start": week_start}


@app.api_route("/api/run-sync", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
async def run_sync(request: Request):
    """Run today's sync (for external cron services like cron-job.org).

    Accepts any HTTP method — free cron services send inconsistent methods.
    Protected by CRON_SECRET env var: pass it as `Authorization: Bearer <secret>`.
    Respects the same hour window as the script (SYNC_START_HOUR/SYNC_END_HOUR).
    """
    secret = os.environ.get("CRON_SECRET", "")
    if secret:
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {secret}":
            return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    start_hour = int(os.environ.get("SYNC_START_HOUR", "0"))
    end_hour = int(os.environ.get("SYNC_END_HOUR", "24"))
    hour = datetime.now(timezone.utc).hour
    if not (start_hour <= hour < end_hour):
        return {"status": "skipped", "reason": f"hour {hour} UTC outside window {start_hour}-{end_hour}"}

    global _job_counter
    _job_counter += 1
    job_id = f"cron-{_job_counter}"
    date_str = today_local()
    _jobs[job_id] = {"status": "running", "date": date_str, "kind": "cron"}

    async def _run(job_id: str, date_str: str):
        try:
            result = await sync_date(date_str)
            _jobs[job_id].update(status="done", result=result)
            _remember(job_id)
        except Exception as e:
            log.error(f"Cron sync failed for {date_str}: {e}")
            _jobs[job_id].update(status="error", error=str(e))
            _remember(job_id)

    import asyncio
    asyncio.get_running_loop().create_task(_run(job_id, date_str))
    return {"job_id": job_id, "status": "running", "date": date_str}


def today_local() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


# Catch-all: serve React app for any non-API route
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    index = DIST_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse(status_code=404, content={"error": "Frontend not built. Run: cd frontend && npm run build"})

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import JSONResponse, FileResponse
from starlette.staticfiles import StaticFiles

from app.models import SyncResult, TasksResponse, Task
from app.sync import sync_date
from app.ticktick import get_tasks_for_date
from app.config import load_config

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("ticktick-notion-sync")

app = FastAPI(title="TickTick → Notion Sync", version="1.0.0")

DIST_DIR = Path(__file__).parent.parent / "frontend" / "dist"

# Serve static assets (JS, CSS, etc.)
app.mount("/assets", StaticFiles(directory=DIST_DIR / "assets"), name="static")

# In-memory job store
_jobs: dict[str, dict] = {}
_job_counter = 0


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
        except Exception as e:
            log.error(f"Sync failed for {date_str}: {e}")
            _jobs[job_id].update(status="error", error=str(e))

    background_tasks.add_task(_run, job_id, date_str)
    return {"job_id": job_id, "status": "running", "date": date_str}


@app.get("/api/sync/status/{job_id}")
async def sync_status(job_id: str):
    """Check the status of a sync job."""
    if job_id not in _jobs:
        return JSONResponse(status_code=404, content={"error": "Job not found"})
    return _jobs[job_id]


# Catch-all: serve React app for any non-API route
@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    index = DIST_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse(status_code=404, content={"error": "Frontend not built. Run: cd frontend && npm run build"})

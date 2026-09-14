import httpx
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("ticktick-notion-sync")


def get_local_tz():
    """Timezone used for all date comparisons.

    Defaults to the system timezone; TZ_NAME env var overrides (e.g.
    TZ_NAME=Europe/Stockholm on cloud hosts that run in UTC).
    """
    name = os.environ.get("TZ_NAME", "")
    return ZoneInfo(name) if name else datetime.now(timezone.utc).astimezone().tzinfo


class TickTickAuthError(Exception):
    """Raised when TickTick auth fails and token refresh is impossible."""

    def __init__(self, message: str = "TickTick auth failed — update tokens"):
        super().__init__(message)


TICKTICK_TOKEN_URL = "https://ticktick.com/oauth/token"
TICKTICK_API_BASE = "https://api.ticktick.com/open/v1"


async def refresh_access_token(cfg: dict, client: httpx.AsyncClient) -> dict:
    """Refresh the TickTick access token."""
    if not cfg.get("ticktick_refresh_token"):
        raise ValueError("No refresh token available")

    resp = await client.post(
        TICKTICK_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "refresh_token": cfg["ticktick_refresh_token"],
        },
        auth=(cfg["ticktick_client_id"], cfg["ticktick_client_secret"]),
    )
    resp.raise_for_status()
    tokens = resp.json()

    cfg["ticktick_access_token"] = tokens["access_token"]
    if "refresh_token" in tokens:
        cfg["ticktick_refresh_token"] = tokens["refresh_token"]
    return cfg


async def _api_call(cfg: dict, client: httpx.AsyncClient, method: str, url: str, **kwargs) -> httpx.Response:
    """Make an API call with automatic token refresh on 401."""
    headers = {**kwargs.pop("headers", {}), "Authorization": f"Bearer {cfg['ticktick_access_token']}"}
    resp = await client.request(method, url, headers=headers, **kwargs)
    if resp.status_code == 401:
        try:
            cfg = await refresh_access_token(cfg, client)
            headers["Authorization"] = f"Bearer {cfg['ticktick_access_token']}"
            resp = await client.request(method, url, headers=headers, **kwargs)
        except (ValueError, httpx.HTTPStatusError) as e:
            log.error(f"Token refresh failed: {e} — update TICKTICK_ACCESS_TOKEN/REFRESH_TOKEN")
            raise TickTickAuthError() from e
    return resp


async def get_projects(cfg: dict, client: httpx.AsyncClient) -> dict[str, str]:
    """Fetch all projects. Returns {project_id: project_name}."""
    resp = await _api_call(cfg, client, "GET", f"{TICKTICK_API_BASE}/project")
    resp.raise_for_status()
    return {p["id"]: p["name"] for p in resp.json()}


async def get_tasks_for_date(cfg: dict, client: httpx.AsyncClient, date_str: str) -> list[dict]:
    """Fetch tasks due on a specific date (YYYY-MM-DD).

    Uses /project/{pid}/data to get all tasks (including recurring ones)
    and filters locally by dueDate. This handles recurring tasks correctly
    because /project/{pid}/data returns tasks with their current due dates.
    """
    local_tz = get_local_tz()

    projects = await get_projects(cfg, client)

    # Fetch all tasks from each project (handles recurring tasks)
    all_tasks = []
    for pid, pname in projects.items():
        resp = await _api_call(
            cfg, client, "GET",
            f"{TICKTICK_API_BASE}/project/{pid}/data",
        )
        if resp.status_code == 200:
            tasks = resp.json().get("tasks", [])
            for t in tasks:
                t["projectName"] = pname
            all_tasks.extend(tasks)

    # Also fetch Inbox (not listed by /project endpoint)
    inbox_resp = await _api_call(
        cfg, client, "GET",
        f"{TICKTICK_API_BASE}/project/inbox/data",
    )
    if inbox_resp.status_code == 200:
        inbox_tasks = inbox_resp.json().get("tasks", [])
        for t in inbox_tasks:
            t["projectName"] = "Inbox"
        all_tasks.extend(inbox_tasks)

    # Fetch completed tasks
    completed_resp = await _api_call(
        cfg, client, "POST",
        f"{TICKTICK_API_BASE}/task/completed",
        json={},
        headers={"Content-Type": "application/json"},
    )
    if completed_resp.status_code == 200:
        for t in completed_resp.json():
            ct = t.get("completedTime", "")
            if not ct:
                continue
            try:
                utc_dt = datetime.fromisoformat(ct.replace("+0000", "+00:00"))
                local_dt = utc_dt.astimezone(local_tz)
                completed_date = local_dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                completed_date = ct[:10]
            if completed_date == date_str:
                t["projectName"] = projects.get(t.get("projectId", ""), "Inbox")
                all_tasks.append(t)

    # Filter: due today/overdue OR completed today
    result = []
    for t in all_tasks:
        due = t.get("dueDate", "")
        ct = t.get("completedTime", "")

        # Parse due date
        due_date_local = None
        if due:
            try:
                utc_dt = datetime.fromisoformat(due.replace("+0000", "+00:00"))
                local_dt = utc_dt.astimezone(local_tz)
                due_date_local = local_dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                due_date_local = due[:10]

        # Parse completed date
        completed_date_local = None
        if ct:
            try:
                utc_dt = datetime.fromisoformat(ct.replace("+0000", "+00:00"))
                local_dt = utc_dt.astimezone(local_tz)
                completed_date_local = local_dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                completed_date_local = ct[:10]

        # Include if: due today/overdue OR completed today
        include = False
        if due_date_local and due_date_local <= date_str:
            include = True
        if completed_date_local and completed_date_local == date_str:
            include = True

        if include:
            result.append(t)

    # Deduplicate recurring tasks: a completed recurring task spawns the next
    # occurrence with a new ID, so both the completed and the new occurrence
    # can match the filter. When the completed occurrence was completed on the
    # synced date, KEEP it (it's the historical record of what was done) and
    # drop the future incomplete twin. Otherwise (completed on an earlier day,
    # e.g. an overdue item completed today), keep the incomplete occurrence and
    # drop the stale completed one.
    deduped = []
    incomplete_titles = set()
    completed_today_titles = set()
    for t in result:
        if t.get("status") != 2:
            incomplete_titles.add(t.get("title", "").strip().lower())
        else:
            ct = t.get("completedTime", "")
            try:
                cdate = datetime.fromisoformat(ct.replace("+0000", "+00:00")).astimezone(local_tz).strftime("%Y-%m-%d") if ct else ""
            except (ValueError, TypeError):
                cdate = ct[:10]
            if cdate == date_str:
                completed_today_titles.add(t.get("title", "").strip().lower())
    for t in result:
        title = t.get("title", "").strip().lower()
        if t.get("status") != 2 and title in completed_today_titles:
            log.info(f"Dropping future occurrence of recurring task completed today: '{t.get('title')}'")
            continue
        if t.get("status") == 2 and title in incomplete_titles and title not in completed_today_titles:
            log.info(f"Dropping completed duplicate of recurring task: '{t.get('title')}'")
            continue
        deduped.append(t)

    return deduped

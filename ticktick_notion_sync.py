#!/Library/Frameworks/Python.framework/Versions/3.10/bin/python3
"""
TickTick → Notion Daily Task Sync

Syncs tasks due today from TickTick to your Notion daily journal page.
Runs via launchd every 2 hours.
"""

import json
import logging
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
from threading import Thread

import requests

# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def retry_request(method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """Make an HTTP request with exponential backoff retry."""
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code < 500:
                return resp
            # Server error — retry
        except requests.exceptions.ConnectionError:
            pass  # Network error — retry
        if attempt < max_retries - 1:
            delay = 2 ** attempt  # 1s, 2s, 4s
            log.warning(f"Request failed (attempt {attempt + 1}/{max_retries}), retrying in {delay}s...")
            time.sleep(delay)
    # Final attempt
    return requests.request(method, url, **kwargs)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
LOG_DIR = Path.home() / "logs"
LOG_PATH = LOG_DIR / "ticktick-notion-sync.log"

# Cloud deployment: hour guard via env vars (Render cron runs 24/7)
SYNC_START_HOUR = int(os.environ.get("SYNC_START_HOUR", "0"))
SYNC_END_HOUR = int(os.environ.get("SYNC_END_HOUR", "24"))

TICKTICK_AUTH_URL = "https://ticktick.com/oauth/authorize"
TICKTICK_TOKEN_URL = "https://ticktick.com/oauth/token"
TICKTICK_API_BASE = "https://api.ticktick.com/open/v1"

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# Weekly items sync constants
WEEKLY_ITEMS_DB_ID = "dde14ec29d684a72aa2e39e9341747b5"       # Weekly items database
WEEKLY_TOGGLE_NAME = "Weekly items"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
    ],
)
log = logging.getLogger("ticktick-notion-sync")

# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

# Env var keys that override config.json values (cloud deployment)
ENV_OVERRIDES = {
    "ticktick_client_id": "TICKTICK_CLIENT_ID",
    "ticktick_client_secret": "TICKTICK_CLIENT_SECRET",
    "ticktick_access_token": "TICKTICK_ACCESS_TOKEN",
    "ticktick_refresh_token": "TICKTICK_REFRESH_TOKEN",
    "notion_token": "NOTION_TOKEN",
    "notion_database_id": "NOTION_DATABASE_ID",
    "notion_template_id": "NOTION_TEMPLATE_ID",
    "weekly_items_database_id": "WEEKLY_ITEMS_DATABASE_ID",
    "redirect_uri": "REDIRECT_URI",
}


def load_config() -> dict:
    cfg: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    else:
        log.info("No config.json found — using environment variables")
    for key, env in ENV_OVERRIDES.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    return cfg

def save_config(cfg: dict) -> None:
    """Persist config (token rotation). Warn when the filesystem is ephemeral."""
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        log.warning(f"Cannot persist config.json ({e}) — token rotation will be lost on restart")

# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------

class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Tiny HTTP server that captures the OAuth callback code."""

    auth_code = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "code" in params:
            OAuthCallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorization successful! You can close this tab.</h1>")
        else:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Missing code parameter")

    def log_message(self, format, *args):
        pass  # Suppress request logs


def run_oauth_flow(cfg: dict) -> dict:
    """Run the full OAuth authorization code flow."""
    redirect_uri = cfg["redirect_uri"]
    scope = "tasks:read tasks:write"

    # Build authorization URL
    auth_params = urlencode({
        "client_id": cfg["ticktick_client_id"],
        "scope": scope,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    })
    auth_url = f"{TICKTICK_AUTH_URL}?{auth_params}"

    print("\n" + "=" * 60)
    print("STEP 1: Open this URL in your browser to authorize the app:")
    print("=" * 60)
    print(f"\n{auth_url}\n")
    print("=" * 60)
    print("Waiting for authorization...\n")

    # Start local server to capture callback
    port = int(urlparse(redirect_uri).port or 8080)
    server = HTTPServer(("localhost", port), OAuthCallbackHandler)
    server.timeout = 300  # 5 minutes to complete auth

    # Try to open browser automatically
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    # Wait for callback
    while OAuthCallbackHandler.auth_code is None:
        server.handle_request()

    code = OAuthCallbackHandler.auth_code
    server.server_close()

    print("Authorization code received! Exchanging for tokens...")

    # Exchange code for tokens
    token_data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": scope,
    }
    resp = requests.post(
        TICKTICK_TOKEN_URL,
        data=token_data,
        auth=(cfg["ticktick_client_id"], cfg["ticktick_client_secret"]),
    )
    resp.raise_for_status()
    tokens = resp.json()

    cfg["ticktick_access_token"] = tokens["access_token"]
    cfg["ticktick_refresh_token"] = tokens.get("refresh_token", "")
    save_config(cfg)

    print("Tokens saved to config.json!")
    return cfg


def refresh_access_token(cfg: dict) -> dict:
    """Refresh the TickTick access token using the refresh token."""
    if not cfg.get("ticktick_refresh_token"):
        log.error("No refresh token available. Run setup first.")
        sys.exit(1)

    resp = retry_request(
        "POST",
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
    save_config(cfg)

    log.info("Access token refreshed successfully")
    return cfg

# ---------------------------------------------------------------------------
# TickTick API
# ---------------------------------------------------------------------------

def ticktick_headers(cfg: dict) -> dict:
    return {"Authorization": f"Bearer {cfg['ticktick_access_token']}"}


def get_ticktick_tasks_due_today(cfg: dict) -> list[dict]:
    """Fetch all incomplete tasks due today or overdue from TickTick.

    Uses per-project endpoint to avoid the 200-task limit of /task/filter.
    """
    # Timezone for local date comparison (TZ_NAME env var overrides system tz)
    tz_name = os.environ.get("TZ_NAME", "")
    local_tz = ZoneInfo(tz_name) if tz_name else datetime.now(timezone.utc).astimezone().tzinfo
    today_local = datetime.now(local_tz).strftime("%Y-%m-%d")

    # Get all projects
    projects_resp = requests.get(
        f"{TICKTICK_API_BASE}/project",
        headers=ticktick_headers(cfg),
    )
    if projects_resp.status_code == 401:
        cfg = refresh_access_token(cfg)
        projects_resp = requests.get(
            f"{TICKTICK_API_BASE}/project",
            headers=ticktick_headers(cfg),
        )
    projects_resp.raise_for_status()
    projects = {p["id"]: p["name"] for p in projects_resp.json()}

    # Get incomplete tasks from each project
    all_tasks = []
    for pid, pname in projects.items():
        resp = requests.get(
            f"{TICKTICK_API_BASE}/project/{pid}/data",
            headers=ticktick_headers(cfg),
        )
        if resp.status_code == 401:
            cfg = refresh_access_token(cfg)
            resp = requests.get(
                f"{TICKTICK_API_BASE}/project/{pid}/data",
                headers=ticktick_headers(cfg),
            )
        if resp.status_code == 200:
            tasks = resp.json().get("tasks", [])
            for t in tasks:
                t["_project_name"] = pname
            all_tasks.extend(tasks)

    # Also get completed tasks (separate endpoint)
    resp = retry_request(
        "POST",
        f"{TICKTICK_API_BASE}/task/completed",
        headers={**ticktick_headers(cfg), "Content-Type": "application/json"},
        json={},
    )
    if resp.status_code == 401:
        cfg = refresh_access_token(cfg)
        resp = retry_request(
            "POST",
            f"{TICKTICK_API_BASE}/task/completed",
            headers={**ticktick_headers(cfg), "Content-Type": "application/json"},
            json={},
        )
    if resp.status_code == 200:
        completed_tasks = resp.json()
        project_map = {p["id"]: p["name"] for p in projects_resp.json()}
        # Only include tasks completed TODAY
        for t in completed_tasks:
            ct = t.get("completedTime", "")
            if not ct:
                continue
            try:
                utc_dt = datetime.fromisoformat(ct.replace("+0000", "+00:00"))
                local_dt = utc_dt.astimezone(local_tz)
                completed_date = local_dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                completed_date = ct[:10]
            if completed_date == today_local:
                t["_project_name"] = project_map.get(t.get("projectId", ""), "Inbox")
                all_tasks.append(t)
        log.info(f"Fetched completed tasks (completed today only)")

    # Filter: incomplete tasks due today/overdue OR completed today
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
        if due_date_local and due_date_local <= today_local:
            include = True
        if completed_date_local and completed_date_local == today_local:
            include = True

        if include:
            result.append(t)

    # Deduplicate recurring tasks: a completed recurring task spawns the next
    # occurrence with a new ID, so both the completed and the new occurrence
    # can match the filter. When the completed occurrence was completed today,
    # KEEP it (it's the historical record of what was done) and drop the
    # future incomplete twin. Otherwise keep the incomplete occurrence and
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
            if cdate == today_local:
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

    log.info(f"Found {len(deduped)} tasks due today/overdue or completed today (out of {len(all_tasks)} total)")
    return deduped


def get_ticktick_projects(cfg: dict) -> dict:
    """Fetch project list and return {project_id: project_name} map."""
    resp = requests.get(
        f"{TICKTICK_API_BASE}/project",
        headers=ticktick_headers(cfg),
    )
    if resp.status_code == 401:
        cfg = refresh_access_token(cfg)
        resp = requests.get(
            f"{TICKTICK_API_BASE}/project",
            headers=ticktick_headers(cfg),
        )
    resp.raise_for_status()
    return {p["id"]: p["name"] for p in resp.json()}

# ---------------------------------------------------------------------------
# Notion API
# ---------------------------------------------------------------------------

def notion_headers(cfg: dict) -> dict:
    return {
        "Authorization": f"Bearer {cfg['notion_token']}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def find_today_journal_page(cfg: dict, date_str: str) -> str | None:
    """Find today's journal page in the database. Returns page_id or None."""
    resp = requests.post(
        f"{NOTION_API_BASE}/databases/{cfg['notion_database_id']}/query",
        headers=notion_headers(cfg),
        json={
            "filter": {
                "property": "Name",
                "title": {"equals": date_str},
            }
        },
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if results:
        page_id = results[0]["id"]
        log.info(f"Found existing journal page: {date_str} ({page_id})")
        return page_id
    log.info(f"No journal page found for {date_str}")
    return None


def create_journal_page(cfg: dict, date_str: str) -> str:
    """Create journal page using the Notion template. Returns page_id."""
    # Convert DD-Mon-YYYY to YYYY-MM-DD for the Date property
    date_prop = datetime.strptime(date_str, "%d-%b-%Y").strftime("%Y-%m-%d")
    template_id = cfg.get("notion_template_id", "")

    page_payload = {
        "parent": {"database_id": cfg["notion_database_id"]},
        "properties": {
            "Name": {"title": [{"text": {"content": date_str}}]},
            "Date": {
                "date": {
                    "start": date_prop,
                }
            },
        },
    }

    if template_id:
        page_payload["template"] = {"type": "template_id", "template_id": template_id}

    resp = requests.post(
        f"{NOTION_API_BASE}/pages",
        headers=notion_headers(cfg),
        json=page_payload,
    )
    resp.raise_for_status()
    page = resp.json()
    page_id = page["id"]
    log.info(f"Created journal page: {date_str} ({page_id})")

    # Wait for template content to appear (poll up to 10s)
    if template_id:
        _wait_for_template(cfg, page_id)

    # Add the initial structure with "Tasks for the day" section
    add_journal_structure(cfg, page_id)

    return page_id


def _wait_for_template(cfg: dict, page_id: str, max_wait: int = 15) -> None:
    """Poll the page until template content fully appears or timeout.

    Waits for multiple blocks to appear (template applies async)
    and adds a settling delay to avoid adding tasks mid-template.
    """
    headers = notion_headers(cfg)
    prev_count = 0
    stable_count = 0
    for i in range(max_wait):
        resp = requests.get(
            f"{NOTION_API_BASE}/blocks/{page_id}/children",
            headers=headers,
        )
        resp.raise_for_status()
        blocks = resp.json().get("results", [])
        count = len(blocks)

        # Wait for at least 5 blocks (template sections)
        if count >= 5:
            # Wait until block count stabilizes (no new blocks for 2s)
            if count == prev_count:
                stable_count += 1
                if stable_count >= 2:
                    log.info(f"Template fully applied after {i + 1}s ({count} blocks)")
                    return
            else:
                stable_count = 0
            prev_count = count

        time.sleep(1)
    log.warning(f"Template did not stabilize within {max_wait}s, proceeding anyway")


def add_journal_structure(cfg: dict, page_id: str) -> None:
    """Add the basic journal structure including 'Tasks for the day' heading."""
    headers = notion_headers(cfg)

    # First, get existing blocks to see what's already there
    resp = requests.get(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=headers,
    )
    resp.raise_for_status()
    existing_blocks = resp.json().get("results", [])

    # Check if "Tasks for the day" already exists
    has_tasks_section = False
    for block in existing_blocks:
        if block.get("type") == "heading_3":
            rich_text = block["heading_3"].get("rich_text", [])
            if rich_text and rich_text[0].get("plain_text") == "Tasks for the day":
                has_tasks_section = True
                break

    if not has_tasks_section:
        # Append "Tasks for the day" heading at the end
        resp = requests.patch(
            f"{NOTION_API_BASE}/blocks/{page_id}/children",
            headers=headers,
            json={
                "children": [
                    {
                        "object": "block",
                        "type": "heading_3",
                        "heading_3": {
                            "rich_text": [{"type": "text", "text": {"content": "Tasks for the day"}}]
                        },
                    },
                ]
            },
        )
        resp.raise_for_status()
        log.info("Added 'Tasks for the day' heading")


def find_tasks_heading_block(cfg: dict, page_id: str) -> str | None:
    """Find the block_id of the 'Tasks for the day' heading."""
    resp = requests.get(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=notion_headers(cfg),
    )
    resp.raise_for_status()

    for block in resp.json().get("results", []):
        if block.get("type") == "heading_3":
            rich_text = block["heading_3"].get("rich_text", [])
            if rich_text and rich_text[0].get("plain_text") == "Tasks for the day":
                return block["id"]
    return None


def find_ticktick_toggle(cfg: dict, page_id: str) -> str | None:
    """Find the existing 'Todo items (from ticktick)' toggle block."""
    resp = requests.get(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=notion_headers(cfg),
    )
    resp.raise_for_status()

    for block in resp.json().get("results", []):
        if block.get("type") == "toggle":
            rich_text = block["toggle"].get("rich_text", [])
            if rich_text and "ticktick" in rich_text[0].get("plain_text", "").lower():
                return block["id"]
    return None


def get_priority_emoji(priority: int) -> str:
    """Map TickTick priority to emoji indicator.
    
    TickTick priority: 0=none, 1=low, 3=medium, 5=high
    """
    if priority >= 5:
        return "🔴 "
    elif priority >= 3:
        return "🟡 "
    elif priority >= 1:
        return "🟢 "
    return ""


def is_task_overdue(task: dict) -> bool:
    """Check if a task is overdue (due date is before today).

    Completed tasks are never overdue — they belong in the Today section
    with a checked box, even if their due date has passed.
    """
    if task.get("status") == 2:
        return False

    tz_name = os.environ.get("TZ_NAME", "")
    local_tz = ZoneInfo(tz_name) if tz_name else datetime.now(timezone.utc).astimezone().tzinfo
    today_local = datetime.now(local_tz).strftime("%Y-%m-%d")
    
    due = task.get("dueDate", "")
    if not due:
        return False
    
    try:
        utc_dt = datetime.fromisoformat(due.replace("+0000", "+00:00"))
        local_dt = utc_dt.astimezone(local_tz)
        due_date_local = local_dt.strftime("%Y-%m-%d")
        return due_date_local < today_local
    except (ValueError, TypeError):
        return due[:10] < today_local


def build_task_blocks(tasks: list[dict]) -> tuple[list[dict], int, int]:
    """Build Notion block children for tasks grouped into a toggle.
    
    Returns (blocks, completed_count, total_count).
    
    Features:
    - Priority indicators (🔴🟡🟢)
    - Task descriptions as indented text
    - Overdue vs today sections
    - Links back to TickTick
    - Completion summary
    """
    local_tz = datetime.now(timezone.utc).astimezone().tzinfo
    today_local = datetime.now(local_tz).strftime("%Y-%m-%d")
    
    # Separate overdue and today tasks
    overdue_tasks = []
    today_tasks = []
    completed_count = 0
    total_count = len(tasks)
    
    for task in tasks:
        if task.get("status") == 2:
            completed_count += 1
        
        if is_task_overdue(task):
            overdue_tasks.append(task)
        else:
            today_tasks.append(task)
    
    # Group by project within each category
    def group_by_project(task_list):
        grouped = {}
        for task in task_list:
            project_name = task.get("_project_name", "Inbox")
            grouped.setdefault(project_name, []).append(task)
        return grouped
    
    today_grouped = group_by_project(today_tasks)
    
    all_blocks = []
    
    def render_task(task):
        title = task.get("title", "Untitled")
        is_checked = task.get("status") == 2
        priority_emoji = get_priority_emoji(task.get("priority", 0))
        task_id = task.get("id", "")
        ticktick_url = f"https://ticktick.com/webapp/#q/all/tasks/{task_id}" if task_id else ""
        
        rich_text = []
        if priority_emoji:
            rich_text.append({"type": "text", "text": {"content": priority_emoji}})
        
        if ticktick_url:
            rich_text.append({"type": "text", "text": {"content": title, "link": {"url": ticktick_url}}})
        else:
            rich_text.append({"type": "text", "text": {"content": title}})
        
        all_blocks.append({
            "object": "block",
            "type": "to_do",
            "to_do": {"rich_text": rich_text, "checked": is_checked},
        })
        
        description = task.get("desc", "")
        if description:
            all_blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text",
                                    "text": {"content": f"    {description}"},
                                    "annotations": {"italic": True, "color": "gray"}}]
                },
            })
    
    # --- Overdue Section (flat, no project grouping) ---
    if overdue_tasks:
        all_blocks.append({
            "object": "block",
            "type": "heading_4",
            "heading_4": {
                "rich_text": [{"type": "text", "text": {"content": "⚠️ Overdue"}}],
            },
        })
        for task in overdue_tasks:
            render_task(task)
    
    # --- Today Section (grouped by project) ---
    if today_tasks:
        if overdue_tasks:
            all_blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}})
        
        all_blocks.append({
            "object": "block",
            "type": "heading_4",
            "heading_4": {
                "rich_text": [{"type": "text", "text": {"content": "📅 Today"}}],
            },
        })
        
        for project_name, project_tasks in today_grouped.items():
            if len(today_grouped) > 1:
                all_blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": f"📁 {project_name}"},
                                "annotations": {"bold": True},
                            }
                        ]
                    },
                })
            
            for task in project_tasks:
                render_task(task)
    
    
    # --- Completion Summary ---
    all_blocks.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": []},  # Spacer
    })
    
    summary_text = f"✅ {completed_count} of {total_count} tasks completed"
    if completed_count == total_count and total_count > 0:
        summary_text += " 🎉"
    
    all_blocks.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": summary_text},
                    "annotations": {"bold": True, "color": "green" if completed_count == total_count else "default"},
                }
            ]
        },
    })
    
    return all_blocks, completed_count, total_count


def _progress_bar_segments(completed: int, total: int, width: int = 10) -> list[dict]:
    """Generate colored progress bar segments."""
    if total == 0:
        return [{"text": "░" * width, "color": "gray"}]
    filled = round(completed / total * width)
    segments = []
    if filled > 0:
        segments.append({"text": "▓" * filled, "color": "green"})
    if filled < width:
        segments.append({"text": "░" * (width - filled), "color": "gray"})
    return segments


def _get_toggle_name_segments(completed: int, total: int) -> list[dict]:
    """Return toggle name as colored rich_text segments."""
    if total == 0:
        return [{"text": "Todo items (from ticktick) — No tasks", "color": "default"}]
    pct = round(completed / total * 100)
    bar_segments = _progress_bar_segments(completed, total)
    suffix = " 🎉" if completed == total else ""
    segments = [
        {"text": f"Todo items (from ticktick) — {pct}% ", "color": "default"},
        *bar_segments,
        {"text": f" {completed}/{total}{suffix}", "color": "default"},
    ]
    return segments


def write_tasks_to_journal(cfg: dict, page_id: str, tasks: list[dict]) -> None:
    """Write tasks to the 'Tasks for the day' section in a toggle block."""
    headers = notion_headers(cfg)

    # Find the "Tasks for the day" heading block
    heading_block_id = find_tasks_heading_block(cfg, page_id)
    if not heading_block_id:
        log.warning("'Tasks for the day' heading not found, adding it")
        add_journal_structure(cfg, page_id)
        heading_block_id = find_tasks_heading_block(cfg, page_id)
        if not heading_block_id:
            log.error("Failed to find or create 'Tasks for the day' heading")
            return

    # Check for existing ticktick toggle
    existing_toggle_id = find_ticktick_toggle(cfg, page_id)

    if existing_toggle_id:
        # Delete existing toggle and its children
        log.info("Removing existing ticktick toggle block")
        requests.delete(
            f"{NOTION_API_BASE}/blocks/{existing_toggle_id}",
            headers=headers,
        ).raise_for_status()
        time.sleep(0.5)  # Notion rate limit

    if not tasks:
        log.info("No tasks to write")
        return

    # Find the index of the heading block to insert after it
    resp = requests.get(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=headers,
    )
    resp.raise_for_status()
    blocks = resp.json().get("results", [])
    heading_index = None
    for i, block in enumerate(blocks):
        if block["id"] == heading_block_id:
            heading_index = i + 1  # insert after the heading
            break

    if heading_index is None:
        heading_index = len(blocks)  # fallback: append at end

    # Step 1: Create empty toggle block at the correct position
    toggle_resp = requests.patch(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=headers,
        json={
            "children": [
                {
                    "object": "block",
                    "type": "toggle",
                    "toggle": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {"content": "Todo items (from ticktick)"},
                            }
                        ],
                    },
                }
            ],
            "after": heading_block_id,
        },
    )
    toggle_resp.raise_for_status()
    toggle_id = toggle_resp.json()["results"][0]["id"]
    log.info(f"Created toggle block: {toggle_id}")

    # Step 2: Append task checkboxes inside the toggle
    task_blocks, completed_count, total_count = build_task_blocks(tasks)
    if task_blocks:
        children_resp = requests.patch(
            f"{NOTION_API_BASE}/blocks/{toggle_id}/children",
            headers=headers,
            json={"children": task_blocks},
        )
        children_resp.raise_for_status()

    # Update toggle name with colored progress
    name_segments = _get_toggle_name_segments(completed_count, total_count)
    requests.patch(
        f"{NOTION_API_BASE}/blocks/{toggle_id}",
        headers=headers,
        json={
            "toggle": {
                "rich_text": [
                    {"type": "text", "text": {"content": s["text"]}, "annotations": {"color": s["color"]}}
                    for s in name_segments
                ]
            }
        },
    ).raise_for_status()

    log.info(f"Successfully wrote {len(tasks)} tasks to journal")

# ---------------------------------------------------------------------------
# Weekly items sync (Weekly items DB -> daily journal snapshot + two-way checks)
# ---------------------------------------------------------------------------

def get_week_start(date_obj: datetime) -> str:
    """Return ISO date string for the Monday of the week containing date_obj."""
    monday = date_obj - timedelta(days=date_obj.weekday())
    return monday.strftime("%Y-%m-%d")


def get_week_end(date_obj: datetime) -> str:
    """Return ISO date string for the Sunday of the week containing date_obj."""
    monday = date_obj - timedelta(days=date_obj.weekday())
    sunday = monday + timedelta(days=6)
    return sunday.strftime("%Y-%m-%d")


def query_weekly_items(cfg: dict, week_start: str, week_end: str) -> list[dict]:
    """Query the Weekly items DB for items whose Week overlaps this week.

    Returns list of {id, name, done, priority}.
    """
    resp = retry_request(
        "POST",
        f"{NOTION_API_BASE}/databases/{WEEKLY_ITEMS_DB_ID}/query",
        headers=notion_headers(cfg),
        json={
            "filter": {
                "and": [
                    {"property": "Week", "date": {"on_or_before": week_end}},
                    {"property": "Week", "date": {"on_or_after": week_start}},
                ]
            },
            "sorts": [{"property": "Priority", "direction": "ascending"}],
        },
    )
    if resp.status_code == 404:
        log.warning("Weekly items database not found or not shared with integration")
        return []
    resp.raise_for_status()

    items = []
    for r in resp.json().get("results", []):
        props = r.get("properties", {})
        name_prop = props.get("Name", {})
        name = "".join(t.get("plain_text", "") for t in name_prop.get("title", []))
        done = props.get("Done", {}).get("checkbox", False)
        priority_prop = props.get("Priority", {})
        priority = priority_prop.get("select", {}).get("name", "") if priority_prop.get("select") else ""
        items.append({
            "id": r["id"],
            "name": name,
            "done": done,
            "priority": priority,
        })
    log.info(f"Found {len(items)} weekly items for {week_start}..{week_end}")
    return items


def find_weekly_toggle(cfg: dict, page_id: str) -> str | None:
    """Find the existing 'Weekly items' toggle block on a journal page."""
    resp = requests.get(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=notion_headers(cfg),
    )
    resp.raise_for_status()
    for block in resp.json().get("results", []):
        if block.get("type") == "toggle":
            rich_text = block["toggle"].get("rich_text", [])
            if rich_text and rich_text[0].get("plain_text", "") == WEEKLY_TOGGLE_NAME:
                return block["id"]
    return None


def read_weekly_toggle_items(cfg: dict, toggle_id: str) -> list[dict]:
    """Read checkbox items from the Weekly items toggle on a journal page.

    Returns list of {block_id, text, checked}.
    """
    items = []
    has_more = True
    cursor = None
    while has_more:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = requests.get(
            f"{NOTION_API_BASE}/blocks/{toggle_id}/children",
            headers=notion_headers(cfg),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        for block in data.get("results", []):
            if block.get("type") == "to_do":
                rich_text = block["to_do"].get("rich_text", [])
                text = "".join(t.get("plain_text", "") for t in rich_text)
                items.append({
                    "block_id": block["id"],
                    "text": text,
                    "checked": block["to_do"].get("checked", False),
                })
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return items


def sync_weekly_items_to_journal(cfg: dict, page_id: str, items: list[dict]) -> None:
    """Sync weekly items into a journal page's 'Weekly items' toggle.

    - Creates the toggle (after 'Tasks for the day' heading) if missing
    - Adds new items as static checkboxes (snapshot)
    - Reconciles check state both ways: journal checkbox <-> DB Done
    - Never removes existing snapshot items (history preservation)
    """
    if not items:
        log.info("No weekly items to sync")
        return

    headers = notion_headers(cfg)

    # Two-way check reconciliation when toggle already exists
    toggle_id = find_weekly_toggle(cfg, page_id)
    if toggle_id:
        journal_items = read_weekly_toggle_items(cfg, toggle_id)
        db_by_name = {it["name"].strip(): it for it in items}

        # 1) Journal -> DB: user checked an item on the journal page
        for ji in journal_items:
            match = db_by_name.get(ji["text"].strip())
            if match and ji["checked"] and not match["done"]:
                log.info(f"Journal check -> DB: '{ji['text']}'")
                requests.patch(
                    f"{NOTION_API_BASE}/pages/{match['id']}",
                    headers=headers,
                    json={"properties": {"Done": {"checkbox": True}}},
                ).raise_for_status()
                match["done"] = True

        # 2) DB -> Journal: user checked an item in the DB/Weekly list
        for ji in journal_items:
            match = db_by_name.get(ji["text"].strip())
            if match and match["done"] and not ji["checked"]:
                log.info(f"DB check -> Journal: '{ji['text']}'")
                requests.patch(
                    f"{NOTION_API_BASE}/blocks/{ji['block_id']}",
                    headers=headers,
                    json={"to_do": {"checked": True}},
                ).raise_for_status()

        # 3) Append items added to the DB after this page's snapshot was taken.
        #    Only ever affects today's page (sync never rewrites past pages),
        #    so this cannot corrupt the historical record.
        existing_names = {ji["text"].strip().lower() for ji in journal_items}
        new_items = [it for it in items if it["name"].strip().lower() not in existing_names]
        if new_items:
            children = [
                {
                    "object": "block",
                    "type": "to_do",
                    "to_do": {
                        "rich_text": [{"type": "text", "text": {"content": it["name"]}}],
                        "checked": it["done"],
                    },
                }
                for it in new_items
            ]
            resp = retry_request(
                "PATCH",
                f"{NOTION_API_BASE}/blocks/{toggle_id}/children",
                headers=headers,
                json={"children": children},
            )
            resp.raise_for_status()
            for it in new_items:
                log.info(f"Appended new weekly item: '{it['name']}'")

        return

    # Create the toggle after the 'Tasks for the day' heading
    tasks_heading_id = find_tasks_heading_block(cfg, page_id)
    if not tasks_heading_id:
        add_journal_structure(cfg, page_id)
        tasks_heading_id = find_tasks_heading_block(cfg, page_id)
    if not tasks_heading_id:
        log.error("Cannot place 'Weekly items' toggle: no 'Tasks for the day' heading")
        return

    toggle_resp = retry_request(
        "PATCH",
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=headers,
        json={
            "children": [
                {
                    "object": "block",
                    "type": "toggle",
                    "toggle": {
                        "rich_text": [
                            {"type": "text", "text": {"content": WEEKLY_TOGGLE_NAME}}
                        ]
                    },
                }
            ],
            "after": tasks_heading_id,
        },
    )
    toggle_resp.raise_for_status()
    toggle_id = toggle_resp.json()["results"][0]["id"]
    log.info(f"Created 'Weekly items' toggle: {toggle_id}")

    # Append snapshot checkboxes (static copy - this is the historical record)
    priority_rank = {"1. High": 0, "2. Medium": 1, "3. Low": 2}
    ordered = sorted(items, key=lambda it: priority_rank.get(it["priority"], 3))
    children = []
    for it in ordered:
        children.append({
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [{"type": "text", "text": {"content": it["name"]}}],
                "checked": it["done"],
            },
        })
    if children:
        resp = retry_request(
            "PATCH",
            f"{NOTION_API_BASE}/blocks/{toggle_id}/children",
            headers=headers,
            json={"children": children},
        )
        resp.raise_for_status()
        log.info(f"Wrote {len(children)} weekly items to journal")


# ---------------------------------------------------------------------------
# Main sync logic
# ---------------------------------------------------------------------------

def sync() -> None:
    """Run the full TickTick → Notion sync."""
    # Hour guard for always-on cron schedulers (no-op outside the window)
    if SYNC_START_HOUR > 0 or SYNC_END_HOUR < 24:
        hour = datetime.now().hour
        if not (SYNC_START_HOUR <= hour < SYNC_END_HOUR):
            log.info(f"Hour {hour} outside sync window {SYNC_START_HOUR}-{SYNC_END_HOUR} — skipping")
            return

    log.info("=" * 40)
    log.info("Starting TickTick → Notion sync")

    cfg = load_config()

    # Check for required tokens
    if not cfg.get("ticktick_access_token"):
        log.error("No TickTick access token. Run: python ticktick_notion_sync.py --setup")
        sys.exit(1)

    if not cfg.get("notion_token"):
        log.error("No Notion token in config. Add 'notion_token' to config.json")
        sys.exit(1)

    # Get today's date string in local timezone (matching your journal format)
    local_tz = datetime.now(timezone.utc).astimezone().tzinfo
    date_str = datetime.now(local_tz).strftime("%d-%b-%Y")  # e.g., "05-Sep-2026"
    log.info(f"Today's date: {date_str}")

    # Fetch tasks from TickTick
    try:
        tasks = get_ticktick_tasks_due_today(cfg)
    except requests.exceptions.RequestException as e:
        log.error(f"Failed to fetch TickTick tasks: {e}")
        sys.exit(1)

    # Find or create today's journal page
    try:
        page_id = find_today_journal_page(cfg, date_str)
        if not page_id:
            page_id = create_journal_page(cfg, date_str)
    except requests.exceptions.RequestException as e:
        log.error(f"Failed to find/create Notion page: {e}")
        sys.exit(1)

    # Write tasks to journal
    try:
        write_tasks_to_journal(cfg, page_id, tasks)
    except requests.exceptions.RequestException as e:
        log.error(f"Failed to write tasks to Notion: {e}")
        sys.exit(1)

    # Sync weekly items into the journal (snapshot + two-way check sync)
    try:
        local_dt = datetime.now(local_tz)
        week_start = get_week_start(local_dt)
        week_end = get_week_end(local_dt)
        weekly_items = query_weekly_items(cfg, week_start, week_end)
        sync_weekly_items_to_journal(cfg, page_id, weekly_items)
    except requests.exceptions.RequestException as e:
        log.error(f"Failed to sync weekly items: {e}")
        log.warning("Continuing without weekly items sync")

    log.info("Sync completed successfully!")


def setup() -> None:
    """Run the OAuth setup flow."""
    cfg = load_config()
    run_oauth_flow(cfg)
    print("\nSetup complete! You can now run the sync.")
    print("Test it with: python ticktick_notion_sync.py")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        setup()
    else:
        sync()


if __name__ == "__main__":
    main()

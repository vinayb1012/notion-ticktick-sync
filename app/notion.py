import httpx
import time
import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("ticktick-notion-sync")

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"


def _headers(cfg: dict) -> dict:
    return {
        "Authorization": f"Bearer {cfg['notion_token']}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }



# ---------------------------------------------------------------------------
# Resilient Notion block writes
# ---------------------------------------------------------------------------

RETRY_STATUSES = {409, 429, 500, 502, 503, 504}


async def _block_request(
    cfg: dict,
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    json: dict | None = None,
    max_retries: int = 5,
) -> httpx.Response:
    """Notion request with exponential backoff for transient conflicts.

    Notion returns 409 Conflict when the parent block is concurrently modified
    (parallel sync, user editing in the UI, or a just-deleted sibling). Retrying
    with backoff resolves it in nearly all cases.
    """
    delay = 0.5
    resp = None
    for attempt in range(max_retries):
        resp = await client.request(method, url, headers=_headers(cfg), json=json)
        if resp.status_code not in RETRY_STATUSES:
            return resp
        if attempt == max_retries - 1:
            break
        log.warning(
            "Notion %s %s -> %s (attempt %d/%d), retrying in %.1fs",
            method, url.rsplit("/v1", 1)[-1], resp.status_code, attempt + 1, max_retries, delay,
        )
        await _async_sleep(delay)
        delay = min(delay * 2, 4.0)
    return resp


async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


def _format_date_for_notion(date_str: str) -> str:
    """Convert YYYY-MM-DD to DD-Mon-YYYY (e.g., 08-Sep-2026) for Notion page titles."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return dt.strftime("%d-%b-%Y")


async def find_journal_page(cfg: dict, client: httpx.AsyncClient, date_str: str) -> str | None:
    """Find a journal page by its title. Returns page_id or None. READ-ONLY."""
    title = _format_date_for_notion(date_str)
    resp = await client.post(
        f"{NOTION_API_BASE}/databases/{cfg['notion_database_id']}/query",
        headers=_headers(cfg),
        json={"filter": {"property": "Name", "title": {"equals": title}}},
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    if results:
        return results[0]["id"]
    return None


async def create_journal_page(cfg: dict, client: httpx.AsyncClient, date_str: str) -> str:
    """Create a new journal page using the Notion template. Returns page_id.

    Uses the configured template ID to apply your custom journal format.
    Polls until template content appears before returning.
    """
    title = _format_date_for_notion(date_str)
    template_id = cfg.get("notion_template_id", "")

    # Build the page creation payload
    page_payload = {
        "parent": {"database_id": cfg["notion_database_id"]},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Date": {
                "date": {
                    "start": date_str,  # Use the synced date, not today
                }
            },
        },
    }

    # Add template if configured
    if template_id:
        page_payload["template"] = {"type": "template_id", "template_id": template_id}

    resp = await client.post(
        f"{NOTION_API_BASE}/pages",
        headers=_headers(cfg),
        json=page_payload,
    )
    resp.raise_for_status()
    page_id = resp.json()["id"]
    log.info(f"Created journal page: {title} ({page_id})")

    # Poll until template content appears (max 10 seconds)
    if template_id:
        await _wait_for_template(cfg, client, page_id)

    await _add_journal_structure(cfg, client, page_id)
    return page_id


async def _wait_for_template(cfg: dict, client: httpx.AsyncClient, page_id: str, max_wait: int = 15) -> None:
    """Poll the page until template content fully appears or timeout.

    Waits for multiple blocks to appear (template applies async)
    and adds a settling delay to avoid adding tasks mid-template.
    """
    prev_count = 0
    stable_count = 0
    for i in range(max_wait):
        resp = await client.get(
            f"{NOTION_API_BASE}/blocks/{page_id}/children",
            headers=_headers(cfg),
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


async def _add_journal_structure(cfg: dict, client: httpx.AsyncClient, page_id: str) -> None:
    """Add the 'Tasks for the day' heading to a new page."""
    resp = await client.get(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=_headers(cfg),
    )
    resp.raise_for_status()
    existing = resp.json().get("results", [])

    has_section = any(
        b.get("type") == "heading_3"
        and b["heading_3"].get("rich_text", [])
        and b["heading_3"]["rich_text"][0].get("plain_text") == "Tasks for the day"
        for b in existing
    )

    if not has_section:
        resp = await client.patch(
            f"{NOTION_API_BASE}/blocks/{page_id}/children",
            headers=_headers(cfg),
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


async def _find_tasks_heading_block(cfg: dict, client: httpx.AsyncClient, page_id: str) -> str | None:
    """Find the block_id of 'Tasks for the day' heading."""
    resp = await client.get(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=_headers(cfg),
    )
    resp.raise_for_status()
    for block in resp.json().get("results", []):
        if block.get("type") == "heading_3":
            rich_text = block["heading_3"].get("rich_text", [])
            if rich_text and rich_text[0].get("plain_text") == "Tasks for the day":
                return block["id"]
    return None


async def _find_ticktick_toggle(cfg: dict, client: httpx.AsyncClient, page_id: str) -> str | None:
    """Find existing 'Todo items (from ticktick)' toggle block."""
    resp = await client.get(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=_headers(cfg),
    )
    resp.raise_for_status()
    for block in resp.json().get("results", []):
        if block.get("type") == "toggle":
            rich_text = block["toggle"].get("rich_text", [])
            if rich_text and "ticktick" in rich_text[0].get("plain_text", "").lower():
                return block["id"]
    return None


def _get_priority_emoji(priority: int) -> str:
    if priority >= 5:
        return "🔴 "
    elif priority >= 3:
        return "🟡 "
    elif priority >= 1:
        return "🟢 "
    return ""


def _get_local_tz():
    """Timezone for date comparisons; TZ_NAME env var overrides system tz."""
    name = os.environ.get("TZ_NAME", "")
    return ZoneInfo(name) if name else datetime.now(timezone.utc).astimezone().tzinfo


def _build_task_blocks(tasks: list[dict]) -> tuple[list[dict], int, int]:
    """Build Notion blocks from a list of TickTick tasks.

    Returns (blocks, completed_count, total_count).
    """
    local_tz = _get_local_tz()
    today_local = datetime.now(local_tz).strftime("%Y-%m-%d")

    overdue_tasks = []
    today_tasks = []
    completed_count = 0

    for task in tasks:
        if task.get("status") == 2:
            completed_count += 1

        due = task.get("dueDate", "")
        is_overdue = False
        if task.get("status") != 2 and due:  # completed tasks are never overdue
            try:
                utc_dt = datetime.fromisoformat(due.replace("+0000", "+00:00"))
                due_date = utc_dt.astimezone(local_tz).strftime("%Y-%m-%d")
                is_overdue = due_date < today_local
            except (ValueError, TypeError):
                is_overdue = due[:10] < today_local

        (overdue_tasks if is_overdue else today_tasks).append(task)

    def group_by_project(task_list):
        grouped = {}
        for task in task_list:
            pname = task.get("projectName", "Inbox")
            grouped.setdefault(pname, []).append(task)
        return grouped

    today_grouped = group_by_project(today_tasks)
    all_blocks = []

    def render_task(task):
        t = task.get("title", "Untitled")
        checked = task.get("status") == 2
        emoji = _get_priority_emoji(task.get("priority", 0))
        tid = task.get("id", "")
        url = f"https://ticktick.com/webapp/#q/all/tasks/{tid}" if tid else ""

        rt = []
        if emoji:
            rt.append({"type": "text", "text": {"content": emoji}})
        if url:
            rt.append({"type": "text", "text": {"content": t, "link": {"url": url}}})
        else:
            rt.append({"type": "text", "text": {"content": t}})

        all_blocks.append({
            "object": "block",
            "type": "to_do",
            "to_do": {"rich_text": rt, "checked": checked},
        })
        desc = task.get("desc", "")
        if desc:
            all_blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text",
                                    "text": {"content": f"    {desc}"},
                                    "annotations": {"italic": True, "color": "gray"}}]
                },
            })

    if overdue_tasks:
        all_blocks.append({
            "object": "block",
            "type": "heading_4",
            "heading_4": {"rich_text": [{"type": "text", "text": {"content": "⚠️ Overdue"}}]},
        })
        for task in overdue_tasks:
            render_task(task)

    if today_tasks:
        if overdue_tasks:
            all_blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}})
        all_blocks.append({
            "object": "block",
            "type": "heading_4",
            "heading_4": {"rich_text": [{"type": "text", "text": {"content": "📅 Today"}}]},
        })
        for pname, ptasks in today_grouped.items():
            if len(today_grouped) > 1:
                all_blocks.append({
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": f"📁 {pname}"},
                                        "annotations": {"bold": True}}]
                    },
                })
            for task in ptasks:
                render_task(task)

    all_blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": []}})
    total = len(tasks)
    summary = f"✅ {completed_count} of {total} tasks completed"
    if completed_count == total and total > 0:
        summary += " 🎉"
    all_blocks.append({
        "object": "block",
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{"type": "text", "text": {"content": summary},
                            "annotations": {"bold": True,
                                            "color": "green" if completed_count == total else "default"}}]
        },
    })

    return all_blocks, completed_count, total


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


async def write_tasks_to_journal(cfg: dict, client: httpx.AsyncClient, page_id: str, tasks: list[dict]) -> None:
    """Write tasks to the journal page's 'Tasks for the day' section."""
    heading_block_id = await _find_tasks_heading_block(cfg, client, page_id)
    if not heading_block_id:
        await _add_journal_structure(cfg, client, page_id)
        heading_block_id = await _find_tasks_heading_block(cfg, client, page_id)
        if not heading_block_id:
            log.error("Failed to find or create 'Tasks for the day' heading")
            return

    existing_toggle_id = await _find_ticktick_toggle(cfg, client, page_id)

    if not tasks:
        # No tasks: only remove a stale toggle, nothing to add
        if existing_toggle_id:
            resp = await _block_request(
                cfg, client, "DELETE", f"{NOTION_API_BASE}/blocks/{existing_toggle_id}"
            )
            if resp.status_code >= 400:
                log.warning("Could not delete empty tasks toggle: %s", resp.status_code)
        return

    task_blocks, completed_count, total_count = _build_task_blocks(tasks)

    # Create the NEW toggle first, then delete the old one. If any write fails
    # mid-way the previous snapshot stays intact (no data loss on 409s).
    toggle_resp = await _block_request(
        cfg,
        client,
        "PATCH",
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        json={
            "children": [
                {
                    "object": "block",
                    "type": "toggle",
                    "toggle": {
                        "rich_text": [{"type": "text", "text": {"content": "Todo items (from ticktick)"}}],
                    },
                }
            ],
            "after": heading_block_id,
        },
    )
    toggle_resp.raise_for_status()
    toggle_id = toggle_resp.json()["results"][0]["id"]

    if task_blocks:
        blocks_resp = await _block_request(
            cfg,
            client,
            "PATCH",
            f"{NOTION_API_BASE}/blocks/{toggle_id}/children",
            json={"children": task_blocks},
        )
        blocks_resp.raise_for_status()

    # Update toggle name with colored progress
    name_segments = _get_toggle_name_segments(completed_count, total_count)
    name_resp = await _block_request(
        cfg,
        client,
        "PATCH",
        f"{NOTION_API_BASE}/blocks/{toggle_id}",
        json={
            "toggle": {
                "rich_text": [
                    {"type": "text", "text": {"content": s["text"]}, "annotations": {"color": s["color"]}}
                    for s in name_segments
                ]
            }
        },
    )
    name_resp.raise_for_status()

    # Old snapshot is superseded — remove it now that the new one is complete
    if existing_toggle_id and existing_toggle_id != toggle_id:
        del_resp = await _block_request(
            cfg, client, "DELETE", f"{NOTION_API_BASE}/blocks/{existing_toggle_id}"
        )
        if del_resp.status_code >= 400 and del_resp.status_code != 404:
            log.warning("Could not delete old tasks toggle: %s", del_resp.status_code)

    log.info(f"Wrote {len(tasks)} tasks to journal page")


# ---------------------------------------------------------------------------
# Weekly items sync (ported from ticktick_notion_sync.py, async version)
# ---------------------------------------------------------------------------

WEEKLY_TOGGLE_NAME = "Weekly items"


def _plain_text(rich_text: list) -> str:
    return "".join(rt.get("plain_text", "") for rt in rich_text)


async def find_weekly_toggle(cfg: dict, client: httpx.AsyncClient, page_id: str) -> str | None:
    """Find the 'Weekly items' toggle block on a journal page."""
    resp = await client.get(
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
        headers=_headers(cfg),
    )
    resp.raise_for_status()
    for block in resp.json().get("results", []):
        if block.get("type") == "toggle":
            if _plain_text(block["toggle"].get("rich_text", [])) == WEEKLY_TOGGLE_NAME:
                return block["id"]
    return None


async def read_weekly_toggle_items(cfg: dict, client: httpx.AsyncClient, toggle_id: str) -> list[dict]:
    """Read to_do blocks inside the Weekly items toggle.

    Returns {block_id, text, checked, last_edited} per item
    (last_edited = block's last_edited_time, ISO timestamp).
    """
    items = []
    has_more = True
    cursor = None
    while has_more:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = await client.get(
            f"{NOTION_API_BASE}/blocks/{toggle_id}/children",
            headers=_headers(cfg),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        for block in data.get("results", []):
            if block.get("type") == "to_do":
                items.append({
                    "block_id": block["id"],
                    "text": _plain_text(block["to_do"].get("rich_text", [])),
                    "checked": block["to_do"].get("checked", False),
                    "last_edited": block.get("last_edited_time", ""),
                })
        has_more = data.get("has_more", False)
        cursor = data.get("next_cursor")
    return items


async def sync_weekly_items_to_journal(cfg: dict, client: httpx.AsyncClient, page_id: str, items: list[dict]) -> dict:
    """Sync weekly items into a journal page's 'Weekly items' toggle.

    - Creates the toggle (after 'Tasks for the day' heading) if missing
    - Adds new items as static checkboxes (snapshot)
    - Reconciles check state both ways: journal checkbox <-> DB Done
    - Never removes existing snapshot items (history preservation)
    """
    out = {"checked_journal_to_db": 0, "checked_db_to_journal": 0, "appended": 0, "created_toggle": False}

    if not items:
        return out

    # Two-way check reconciliation when toggle already exists.
    # Conflict rule: whichever side was edited more recently wins.
    # Journal block last_edited_time > DB page last_edited_time -> journal
    # state propagates (both tick AND untick); otherwise DB state wins.
    toggle_id = await find_weekly_toggle(cfg, client, page_id)
    if toggle_id:
        journal_items = await read_weekly_toggle_items(cfg, client, toggle_id)
        db_by_name = {it["name"].strip(): it for it in items}

        # Fetch DB page last_edited timestamps for conflict resolution
        db_edited = {}
        for it in items:
            try:
                pr = await client.get(
                    f"{NOTION_API_BASE}/pages/{it['id']}",
                    headers=_headers(cfg),
                )
                pr.raise_for_status()
                db_edited[it["id"]] = pr.json().get("last_edited_time", "")
            except Exception:
                db_edited[it["id"]] = ""

        def journal_wins(ji: dict, match: dict) -> bool:
            """True when the journal checkbox is newer than the DB page."""
            je = ji.get("last_edited", "")
            de = db_edited.get(match["id"], "")
            if not je or not de:
                return False
            return je > de

        # 1) Journal -> DB: journal checkbox newer than DB (tick or untick)
        for ji in journal_items:
            match = db_by_name.get(ji["text"].strip())
            if not match:
                continue
            if ji["checked"] != match["done"] and journal_wins(ji, match):
                direction = "tick" if ji["checked"] else "untick"
                log.info(f"Journal {direction} -> DB: '{ji['text']}'")
                resp = await client.patch(
                    f"{NOTION_API_BASE}/pages/{match['id']}",
                    headers=_headers(cfg),
                    json={"properties": {"Done": {"checkbox": ji["checked"]}}},
                )
                resp.raise_for_status()
                match["done"] = ji["checked"]
                out["checked_journal_to_db"] += 1

        # 2) DB -> Journal: DB state newer than journal block (tick or untick)
        for ji in journal_items:
            match = db_by_name.get(ji["text"].strip())
            if not match:
                continue
            if match["done"] != ji["checked"] and not journal_wins(ji, match):
                log.info(f"DB -> Journal {match['done']} : '{ji['text']}'")
                resp = await client.patch(
                    f"{NOTION_API_BASE}/blocks/{ji['block_id']}",
                    headers=_headers(cfg),
                    json={"to_do": {"checked": match["done"]}},
                )
                resp.raise_for_status()
                out["checked_db_to_journal"] += 1

        # 3) Append items added to the DB after this page's snapshot was taken.
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
            resp = await _block_request(
                cfg,
                client,
                "PATCH",
                f"{NOTION_API_BASE}/blocks/{toggle_id}/children",
                json={"children": children},
            )
            resp.raise_for_status()
            out["appended"] = len(new_items)

        return out

    # Create the toggle after the 'Tasks for the day' heading
    tasks_heading_id = await _find_tasks_heading_block(cfg, client, page_id)
    if not tasks_heading_id:
        await _add_journal_structure(cfg, client, page_id)
        tasks_heading_id = await _find_tasks_heading_block(cfg, client, page_id)
    if not tasks_heading_id:
        log.error("Cannot place 'Weekly items' toggle: no 'Tasks for the day' heading")
        return out

    toggle_resp = await _block_request(
        cfg,
        client,
        "PATCH",
        f"{NOTION_API_BASE}/blocks/{page_id}/children",
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
    out["created_toggle"] = True

    # Append snapshot checkboxes (static copy - this is the historical record)
    priority_rank = {"1. High": 0, "2. Medium": 1, "3. Low": 2}
    ordered = sorted(items, key=lambda it: priority_rank.get(it["priority"], 3))
    children = [
        {
            "object": "block",
            "type": "to_do",
            "to_do": {
                "rich_text": [{"type": "text", "text": {"content": it["name"]}}],
                "checked": it["done"],
            },
        }
        for it in ordered
    ]
    if children:
        resp = await _block_request(
            cfg,
            client,
            "PATCH",
            f"{NOTION_API_BASE}/blocks/{toggle_id}/children",
            json={"children": children},
        )
        resp.raise_for_status()
        out["appended"] = len(children)

    return out

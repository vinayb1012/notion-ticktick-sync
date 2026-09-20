import httpx
import logging
from datetime import datetime, timedelta, timezone

log = logging.getLogger("ticktick-notion-sync")

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

PRIORITY_RANK = {"1. High": 0, "2. Medium": 1, "3. Low": 2}


def _headers(cfg: dict) -> dict:
    return {
        "Authorization": f"Bearer {cfg['notion_token']}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def get_week_bounds(date_str: str) -> tuple[str, str]:
    """Monday-based week bounds for a given date (YYYY-MM-DD)."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    monday = dt - timedelta(days=dt.weekday())
    sunday = monday + timedelta(days=6)
    return monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d")


def _week_end(week_start: str) -> str:
    """Sunday for a Monday week start (YYYY-MM-DD) — used as date range end."""
    from datetime import datetime, timedelta
    return (datetime.strptime(week_start, "%Y-%m-%d") + timedelta(days=6)).strftime("%Y-%m-%d")


def _page_to_item(page: dict) -> dict:
    props = page.get("properties", {})
    name = "".join(
        rt.get("plain_text", "") for rt in props.get("Name", {}).get("title", [])
    )
    done = props.get("Done", {}).get("checkbox", False)
    week = props.get("Week", {}).get("date", {})
    week_start = week.get("start", "") if week else ""
    prio_obj = props.get("Priority", {}).get("select")
    priority = prio_obj.get("name", "") if prio_obj else ""
    return {
        "id": page["id"],
        "name": name,
        "done": done,
        "week": week_start,
        "priority": priority,
    }


async def list_weekly_items(cfg: dict, client: httpx.AsyncClient, week_start: str) -> list[dict]:
    """List all weekly items for the week starting on week_start (YYYY-MM-DD).

    Uses a Mon-Sun range filter (not `equals week_start`) so items created
    with a mid-week Week date are included, matching the standalone script.
    """
    from datetime import datetime, timedelta
    start = datetime.strptime(week_start, "%Y-%m-%d")
    week_end = (start + timedelta(days=6)).strftime("%Y-%m-%d")
    resp = await client.post(
        f"{NOTION_API_BASE}/databases/{cfg['weekly_items_database_id']}/query",
        headers=_headers(cfg),
        json={
            "filter": {
                "and": [
                    {"property": "Week", "date": {"on_or_after": week_start}},
                    {"property": "Week", "date": {"on_or_before": week_end}},
                ]
            },
            "sorts": [{"property": "Priority", "direction": "ascending"}],
        },
    )
    resp.raise_for_status()
    return [_page_to_item(p) for p in resp.json().get("results", [])]


async def create_weekly_item(
    cfg: dict, client: httpx.AsyncClient, name: str, week_start: str, priority: str
) -> dict:
    resp = await client.post(
        f"{NOTION_API_BASE}/pages",
        headers=_headers(cfg),
        json={
            "parent": {"database_id": cfg["weekly_items_database_id"]},
            "properties": {
                "Name": {"title": [{"text": {"content": name}}]},
                "Week": {"date": {"start": week_start, "end": _week_end(week_start)}},
                "Priority": {"select": {"name": priority}},
            },
        },
    )
    resp.raise_for_status()
    return _page_to_item(resp.json())


async def set_weekly_item_done(cfg: dict, client: httpx.AsyncClient, page_id: str, done: bool) -> None:
    resp = await client.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_headers(cfg),
        json={"properties": {"Done": {"checkbox": done}}},
    )
    resp.raise_for_status()


async def update_weekly_item_priority(
    cfg: dict, client: httpx.AsyncClient, page_id: str, priority: str
) -> None:
    resp = await client.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_headers(cfg),
        json={"properties": {"Priority": {"select": {"name": priority}}}},
    )
    resp.raise_for_status()


async def move_weekly_item_to_next_week(cfg: dict, client: httpx.AsyncClient, page_id: str) -> dict:
    """Move a weekly item to the week after the one it currently belongs to."""
    resp = await client.get(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_headers(cfg),
    )
    resp.raise_for_status()
    item = _page_to_item(resp.json())
    if not item["week"]:
        raise ValueError("Item has no Week date set")
    from datetime import datetime, timedelta
    current = datetime.strptime(item["week"][:10], "%Y-%m-%d")
    next_week = (current + timedelta(days=7)).strftime("%Y-%m-%d")
    resp = await client.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_headers(cfg),
        json={"properties": {"Week": {"date": {"start": next_week, "end": _week_end(next_week)}}}},
    )
    resp.raise_for_status()
    return _page_to_item(resp.json())


async def carry_over_incomplete_items(cfg: dict, client: httpx.AsyncClient, current_monday: str) -> int:
    """Roll unfinished items from past weeks into the week starting current_monday.

    Queries items with Week < current_monday and Done == false, then sets each
    item's Week to current_monday. Completed items stay in their original week.
    Returns the number of items moved.
    """
    from datetime import datetime, timedelta
    monday = datetime.strptime(current_monday, "%Y-%m-%d")
    day_before = (monday - timedelta(days=1)).strftime("%Y-%m-%d")
    resp = await client.post(
        f"{NOTION_API_BASE}/databases/{cfg['weekly_items_database_id']}/query",
        headers=_headers(cfg),
        json={
            "filter": {
                "and": [
                    {"property": "Week", "date": {"on_or_before": day_before}},
                    {"property": "Done", "checkbox": {"equals": False}},
                ]
            },
        },
    )
    resp.raise_for_status()
    moved = 0
    for page in resp.json().get("results", []):
        page_id = page["id"]
        patch = await client.patch(
            f"{NOTION_API_BASE}/pages/{page_id}",
            headers=_headers(cfg),
            json={"properties": {"Week": {"date": {"start": current_monday, "end": _week_end(current_monday)}}}},
        )
        if patch.status_code == 200:
            moved += 1
            name = _page_to_item(page)["name"]
            log.info("Carried over unfinished weekly item: %s", name)
        else:
            log.warning("Failed to carry over item %s: %s", page_id, patch.text)
    return moved


async def delete_weekly_item(cfg: dict, client: httpx.AsyncClient, page_id: str) -> None:
    resp = await client.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_headers(cfg),
        json={"archived": True},
    )
    resp.raise_for_status()

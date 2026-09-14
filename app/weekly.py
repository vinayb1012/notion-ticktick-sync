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
    """List all weekly items for the week starting on week_start (YYYY-MM-DD)."""
    resp = await client.post(
        f"{NOTION_API_BASE}/databases/{cfg['weekly_items_database_id']}/query",
        headers=_headers(cfg),
        json={
            "filter": {
                "property": "Week",
                "date": {"equals": week_start},
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
                "Week": {"date": {"start": week_start}},
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


async def delete_weekly_item(cfg: dict, client: httpx.AsyncClient, page_id: str) -> None:
    resp = await client.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=_headers(cfg),
        json={"archived": True},
    )
    resp.raise_for_status()

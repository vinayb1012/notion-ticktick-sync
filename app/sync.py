import logging
import httpx

from app.config import load_config
from app.ticktick import get_tasks_for_date
from app.notion import find_journal_page, create_journal_page, write_tasks_to_journal

log = logging.getLogger("ticktick-notion-sync")


async def sync_date(date_str: str) -> dict:
    """Sync TickTick tasks for a given date (YYYY-MM-DD) to Notion.

    READS existing Notion pages — only creates new ones if none exist.
    Never modifies existing page content beyond the tasks toggle.
    """
    cfg = load_config()

    async with httpx.AsyncClient(timeout=30) as client:
        tasks = await get_tasks_for_date(cfg, client, date_str)
        log.info(f"Fetched {len(tasks)} tasks for {date_str}")

        page_id = await find_journal_page(cfg, client, date_str)
        created = False
        if not page_id:
            page_id = await create_journal_page(cfg, client, date_str)
            created = True

        await write_tasks_to_journal(cfg, client, page_id, tasks)

    page_url = f"https://notion.so/{page_id.replace('-', '')}"
    return {
        "date": date_str,
        "page_id": page_id,
        "page_url": page_url,
        "tasks_synced": len(tasks),
        "created": created,
    }


async def sync_week(week_start: str, week_end: str) -> dict:
    """Sync weekly items snapshot into each daily journal page of the week.

    Only touches TODAY's page (past/future pages are frozen history).
    """
    cfg = load_config()

    from datetime import datetime, timedelta
    from app.weekly import list_weekly_items, carry_over_incomplete_items

    start = datetime.strptime(week_start, "%Y-%m-%d")
    end = datetime.strptime(week_end, "%Y-%m-%d")
    today_local = datetime.now().astimezone().strftime("%Y-%m-%d")

    async with httpx.AsyncClient(timeout=30) as client:
        carried = await carry_over_incomplete_items(cfg, client, week_start)
        if carried:
            log.info("Carried over %d unfinished weekly item(s) into week %s", carried, week_start)
        items = await list_weekly_items(cfg, client, week_start)

        results = []
        d = start
        while d <= end:
            date_str = d.strftime("%Y-%m-%d")
            page_id = await find_journal_page(cfg, client, date_str)
            if page_id:
                from app.notion import sync_weekly_items_to_journal
                synced = await sync_weekly_items_to_journal(cfg, client, page_id, items)
                results.append({"date": date_str, "page_id": page_id, **synced})
            d += timedelta(days=1)

    return {
        "week_start": week_start,
        "week_end": week_end,
        "items": len(items),
        "today": today_local,
        "pages": results,
    }

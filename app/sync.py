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

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent.parent / "config.json"

_ENV_KEYS = {
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
    # Environment variables override config.json (for cloud deployment)
    for key, env in _ENV_KEYS.items():
        val = os.environ.get(env)
        if val:
            cfg[key] = val
    return cfg

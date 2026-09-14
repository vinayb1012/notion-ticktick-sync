# TickTick → Notion Sync

Syncs TickTick tasks and weekly checklist items into a Notion daily journal, with a web dashboard for manual control.

## What It Does

**Daily task sync** (`ticktick_notion_sync.py`):
- Runs every 2 hours (7am–9pm) via macOS launchd
- Fetches incomplete tasks due today (plus tasks completed today) from TickTick
- Creates today's journal page from your Notion template if it doesn't exist
- Writes tasks as checkboxes inside a "Todo items (from ticktick)" toggle, grouped by project with priority indicators (🔴🟡🟢) and a completion progress bar
- Deduplicates recurring tasks (a completed recurrence and its next occurrence appear as one entry)

**Weekly items sync** (same script):
- Reads the week's checklist from a "Weekly items" Notion database (Mon–Sun)
- Snapshots it as static checkboxes into a "Weekly items" toggle on each day's journal page — each page keeps a permanent record of its week
- Two-way check sync: checking an item on a journal page marks it Done in the database, and vice versa (applied on each sync run)
- Appends items added mid-week to today's page on the next run; past pages are never rewritten

**Web dashboard** (`app/` + `frontend/`):
- FastAPI backend + React/Vite frontend
- Browse tasks for any date, trigger syncs manually, view sync results

## Project Structure

```
ticktick_notion_sync.py   # Main sync script (daily + weekly sync)
app/                      # FastAPI backend
  main.py                 #   API endpoints + static file serving
  ticktick.py             #   TickTick API client
  notion.py               #   Notion API client
  sync.py                 #   Sync orchestration
  config.py               #   Config loading
  models.py               #   Pydantic models
frontend/                 # React + Vite dashboard
config.example.json       # Template for config.json (secrets — never committed)
start.sh                  # Local dev server launcher
```

## Setup

### 1. Notion Integration Token

1. Go to https://www.notion.so/my-integrations → "New integration"
2. Name it (e.g. "Ticktick sync"), select your workspace, copy the token (`ntn_…`)
3. Share your databases with the integration: open each database → **•••** → **Connections** → add your integration. Required for:
   - **Daily journal** database
   - **Weekly items** database (if using weekly sync)

### 2. TickTick OAuth App

1. Create a TickTick Open API app at https://developer.ticktick.com/
2. Set the redirect URI to `http://localhost:8080/`
3. Copy the client ID and secret

### 3. Configure

```bash
cp config.example.json config.json
# Edit config.json with your real values
```

| Key | Description |
|-----|-------------|
| `ticktick_client_id/secret` | From the TickTick developer app |
| `notion_token` | Notion integration token |
| `notion_database_id` | Daily journal database ID (from its URL) |
| `notion_template_id` | Daily journal template page ID |
| `redirect_uri` | OAuth redirect (default `http://localhost:8080/`) |

### 4. Run OAuth Setup

```bash
python3 ticktick_notion_sync.py --setup
```

A browser window opens for TickTick authorization; tokens are saved to `config.json`.

### 5. Test the Sync

```bash
python3 ticktick_notion_sync.py
```

Check your Notion journal for today — tasks appear in "Tasks for the day", weekly items in "Weekly items".

### 6. Install the Scheduler (macOS)

```bash
launchctl load ~/Library/LaunchAgents/com.ticktick.notion-sync.plist
launchctl list | grep ticktick   # verify
```

Schedule: 7:00–21:00 every 2 hours. Edit `StartCalendarInterval` in the plist to change.

### 7. Web Dashboard (optional)

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
./start.sh                 # serves API + built frontend at http://localhost:8000

# Frontend development:
cd frontend && npm install && npm run dev
```

## Logs & Troubleshooting

```bash
tail -f ~/logs/ticktick-notion-sync.log        # sync log
tail -f ~/logs/ticktick-notion-sync.error.log  # errors
```

| Problem | Solution |
|---------|----------|
| "No TickTick access token" | Run `python3 ticktick_notion_sync.py --setup` |
| 401 from TickTick | Tokens expired — re-run setup |
| 401/404 from Notion | Check token; share the database with the integration |
| Weekly items missing | Share the Weekly items DB with the integration; check `Found N weekly items` in logs |
| Jobs not running | `launchctl unload` then reload the plist |

## Security

- `config.json` (contains tokens) is gitignored — never commit it
- For cloud deployment, move secrets to environment variables and keep the repo private

## License

MIT

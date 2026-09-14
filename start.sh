#!/bin/bash
# Start the TickTick → Notion web app
cd "$(dirname "$0")"
source venv/bin/activate
echo "Starting server at http://localhost:8000"
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

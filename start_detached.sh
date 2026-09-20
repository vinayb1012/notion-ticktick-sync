#!/bin/bash
# Start the web app detached (no reload), logging to logs/webapp.log
cd "$(dirname "$0")"
mkdir -p logs

if [ -f logs/webapp.pid ] && kill -0 "$(cat logs/webapp.pid)" 2>/dev/null; then
    echo "Already running (PID $(cat logs/webapp.pid))"
    exit 1
fi

source venv/bin/activate
nohup uvicorn app.main:app --host 0.0.0.0 --port 8000 >> logs/webapp.log 2>&1 &
echo $! > logs/webapp.pid
echo "Started (PID $(cat logs/webapp.pid)) — http://localhost:8000 — logs: logs/webapp.log"

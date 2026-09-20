#!/bin/bash
# Stop the detached web app
cd "$(dirname "$0")"

if [ -f logs/webapp.pid ]; then
    PID=$(cat logs/webapp.pid)
    if kill "$PID" 2>/dev/null; then
        echo "Stopped (PID $PID)"
    else
        echo "PID $PID not running"
    fi
    rm -f logs/webapp.pid
else
    # Fallback: kill anything listening on port 8000
    PID=$(lsof -ti:8000)
    if [ -n "$PID" ]; then
        kill $PID
        echo "Stopped (PID $PID, found via port 8000)"
    else
        echo "Not running"
    fi
fi

#!/bin/zsh
# Restart the dashboard/worker the moment no job is running (polls 5x/sec), so a live job is never killed.
cd "$(dirname "$0")/.."
while true; do
  r=$(sqlite3 data/osc.db "SELECT COUNT(*) FROM jobs WHERE status='running'")
  if [ "$r" = "0" ]; then
    pkill -f "python osc.py serve"; sleep 1
    nohup .venv/bin/python osc.py serve --port 8791 >> logs/server.log 2>&1 &
    sleep 3; echo "restarted $(date +%H:%M:%S)"; exit 0
  fi
  sleep 0.2
done

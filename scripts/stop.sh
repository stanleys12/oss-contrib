#!/bin/zsh
# Stop the dashboard. It runs under launchd (KeepAlive), so unload that first or it comes straight back.
P=~/Library/LaunchAgents/com.stanleyshen.osc-dashboard.plist
launchctl list | grep -q com.stanleyshen.osc-dashboard && launchctl unload "$P"
if ! pgrep -f "osc.py serve" >/dev/null; then echo "stopped"; exit 0; fi
pkill -f "osc.py serve"
for i in 1 2 3 4 5; do pgrep -f "osc.py serve" >/dev/null || { echo "stopped"; exit 0; }; sleep 1; done
pkill -9 -f "osc.py serve" && echo "stopped (forced)"

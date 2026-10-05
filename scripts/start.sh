#!/bin/zsh
# Start the osc dashboard + GitHub poller on http://127.0.0.1:8791 under launchd (restarts itself, starts at login).
P=~/Library/LaunchAgents/com.stanleyshen.osc-dashboard.plist
launchctl list | grep -q com.stanleyshen.osc-dashboard || launchctl load "$P"
sleep 3 && echo "osc dashboard: http://127.0.0.1:8791"

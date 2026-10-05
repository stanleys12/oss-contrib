#!/bin/zsh
# Disk watchdog for the OSS contribution engine: every 30 min, reclaims space inside ~/oss-contrib only.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$HOME/oss-contrib"
cd "$HOME/oss-contrib" || exit 1
mkdir -p logs
exec "$HOME/oss-contrib/.venv/bin/python" -m osc.housekeeping --guard >> "logs/diskguard-$(date +%Y-%m).log" 2>&1

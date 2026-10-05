#!/bin/zsh
# Dashboard + GitHub update poller, kept alive by ~/Library/LaunchAgents/com.stanleyshen.osc-dashboard.plist
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$HOME/oss-contrib" || exit 1
exec "$HOME/oss-contrib/.venv/bin/python" osc.py serve --port 8791 >> logs/server.log 2>&1

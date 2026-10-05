#!/bin/zsh
# Unattended daily run of the OSS contribution engine. Scheduled by ~/Library/LaunchAgents/com.stanleyshen.osc-daily.plist
export PATH="$HOME/oss-contrib/.tools/node_modules/.bin:/opt/homebrew/bin:/usr/local/bin:$HOME/.nvm/versions/node/v22.23.3/bin:$HOME/.local/bin:$HOME/.cargo/bin:/usr/bin:/bin:/usr/sbin:/sbin:/Applications/Docker.app/Contents/Resources/bin"
export PYTHONPATH="$HOME/oss-contrib"
export HOME="$HOME"
cd "$HOME/oss-contrib" || exit 1
mkdir -p logs
exec /usr/bin/caffeinate -i -s "$HOME/oss-contrib/.venv/bin/python" -m osc.daily >> "logs/daily-$(date +%Y-%m-%d).log" 2>&1

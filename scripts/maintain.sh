#!/bin/zsh
# Daily maintenance of one of the owner's public repos. Scheduled by ~/Library/LaunchAgents/com.stanleyshen.osc-maintain.plist
export PATH="$HOME/oss-contrib/.tools/node_modules/.bin:/opt/homebrew/bin:/usr/local/bin:$HOME/.nvm/versions/node/v22.23.3/bin:$HOME/.local/bin:$HOME/.cargo/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$HOME/oss-contrib"
cd "$HOME/oss-contrib" || exit 1
mkdir -p logs
exec /usr/bin/caffeinate -i -s "$HOME/oss-contrib/.venv/bin/python" -m osc.maintain >> "logs/maintain-$(date +%Y-%m).log" 2>&1

#!/bin/zsh
# Daily advisory-database corrections (Security advisory credit badge). launchd: com.stanleyshen.osc-advisory
export PATH="$HOME/oss-contrib/.tools/node_modules/.bin:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$HOME/oss-contrib"
cd "$HOME/oss-contrib" || exit 1
mkdir -p logs
exec /usr/bin/caffeinate -i -s "$HOME/oss-contrib/.venv/bin/python" -m osc.advisory --daily >> "logs/advisory-$(date +%Y-%m).log" 2>&1

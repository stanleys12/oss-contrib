#!/bin/zsh
# Daily vulnerability research (coordinated disclosure). launchd: com.stanleyshen.osc-vulnscan. Reports are GATED.
export PATH="$HOME/oss-contrib/.tools/node_modules/.bin:/opt/homebrew/bin:/usr/local/bin:$HOME/.nvm/versions/node/v22.23.3/bin:$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/go/bin:/opt/homebrew/opt/go/bin:$HOME/go/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export PYTHONPATH="$HOME/oss-contrib"
cd "$HOME/oss-contrib" || exit 1
mkdir -p logs
exec /usr/bin/caffeinate -i -s "$HOME/oss-contrib/.venv/bin/python" -m osc.vulnscan --scan 1 >> "logs/vulnscan-$(date +%Y-%m).log" 2>&1

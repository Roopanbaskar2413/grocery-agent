#!/bin/bash
# Restarts the agent (mock store, public in-app chat) and republishes it on the internet via
# Tailscale Funnel. Run this after a reboot, a crash, or any time the chat URL stops responding.
# Safe to re-run: it kills any previous instance on the same port first.
#
#   ./start_funnel.sh
#
# Secrets (chat tokens, fridge key, VAPID keys) live in .env.funnel, kept out of git. Do not
# regenerate them casually -- the chat tokens were already handed to two other people, and a new
# fridge key would need to be re-entered on the fridge.
set -e
cd "$(dirname "$0")"

TAILSCALE=/Applications/Tailscale.app/Contents/MacOS/Tailscale
PORT=8001
FUNNEL_PORT=8443
LOG=/tmp/grocery_agent_run.log

if [ ! -f .env.funnel ]; then
  echo ".env.funnel not found. It holds the chat tokens, fridge key and VAPID keys -- without it" >&2
  echo "a restart would hand out brand new tokens and break everyone's sign-in." >&2
  exit 1
fi
set -a
source .env.funnel
set +a

source .venv/bin/activate
pkill -f "uvicorn run_dev:app --host 0.0.0.0 --port $PORT" 2>/dev/null || true
sleep 1
nohup uvicorn run_dev:app --host 0.0.0.0 --port "$PORT" > "$LOG" 2>&1 &
sleep 2

"$TAILSCALE" funnel --https="$FUNNEL_PORT" --bg "$PORT" > /dev/null

HOSTNAME=$("$TAILSCALE" status --json 2>/dev/null | python3 -c "import json,sys; print(json.load(sys.stdin)['Self']['DNSName'].rstrip('.'))" 2>/dev/null || echo "<this-device>.ts.net")
echo
echo "Chat: https://$HOSTNAME:$FUNNEL_PORT/chat"
echo "Logs: $LOG"

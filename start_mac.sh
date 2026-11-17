#!/bin/bash
# One-command launcher for the WhatsApp dry run on macOS. Secrets are typed here, hidden, and never written to disk.
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "python3 not found. Install it (brew install python) and re-run."; exit 1; }
python3 -m pip install -q -r requirements.txt 2>&1 | tail -1 || true
command -v ngrok >/dev/null || { echo "ngrok not found. Run: brew install ngrok   then: ngrok config add-authtoken <your token from dashboard.ngrok.com>"; exit 1; }

ask()  { local v; read -r -p "$1: " v; echo "$v"; }
asks() { local v; read -r -s -p "$1 (hidden): " v; echo >&2; echo "$v"; }

export WHATSAPP_APP_SECRET=$(asks "Meta App secret")
export WHATSAPP_TOKEN=$(asks "WhatsApp long-lived token")
export WHATSAPP_PHONE_NUMBER_ID=$(ask "Phone number ID (digits)")
export PHONE_1=$(ask "Your phone, like +91XXXXXXXXXX")
export PHONE_2=$(ask "Second phone (or press Enter to reuse the first)"); [ -z "$PHONE_2" ] && PHONE_2=$PHONE_1
export PHONE_3=$(ask "Third phone (or press Enter to reuse the first)");  [ -z "$PHONE_3" ] && PHONE_3=$PHONE_1
G=$(asks "Gemini API key (press Enter to skip)"); [ -n "$G" ] && export GEMINI_API_KEY=$G
export WHATSAPP_VERIFY_TOKEN=${WHATSAPP_VERIFY_TOKEN:-verify-$(python3 -c 'import secrets;print(secrets.token_hex(6))')}
export FRIDGE_API_KEY=${FRIDGE_API_KEY:-fridge-$(python3 -c 'import secrets;print(secrets.token_hex(8))')}
export QUIET_START=${QUIET_START:-22} QUIET_END=${QUIET_END:-7}
H=$(date +%H); if [ "$H" -ge 22 ] || [ "$H" -lt 7 ]; then export QUIET_START=24 QUIET_END=0; echo "Night time: quiet hours switched off for this test."; fi

python3 dryrun.py env || true
echo; echo "Your webhook verify token (paste into Meta): $WHATSAPP_VERIFY_TOKEN"
echo "Your fridge key (for dryrun.py push/status):      $FRIDGE_API_KEY"
echo; echo "Starting the agent on port 8000 and ngrok. Ctrl+C stops both."
python3 -m uvicorn run_dev:app --port 8000 &
UV=$!; trap "kill $UV 2>/dev/null" EXIT
sleep 2
ngrok http 8000

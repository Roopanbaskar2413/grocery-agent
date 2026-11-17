#!/bin/bash
# Chat-only launcher (no Meta, no ngrok). Starts the agent with the in-app chat on port 8000.
#   ./start_chat.sh          reachable from your phone on the same Wi-Fi
#   ./start_chat.sh local    this computer only
# Tokens are made once and kept in .chat_tokens (private to you). Delete that file to issue new ones.
set -e
cd "$(dirname "$0")"
command -v python3 >/dev/null || { echo "python3 not found. Install it (brew install python) and re-run."; exit 1; }
python3 -m pip install -q -r requirements.txt 2>&1 | tail -1 || true

if [ ! -f .chat_tokens ]; then
  umask 077
  for i in 1 2 3; do python3 -c 'import secrets;print(secrets.token_urlsafe(18))'; done > .chat_tokens
fi
chmod 600 .chat_tokens
export CHAT_TOKEN_1=$(sed -n 1p .chat_tokens) CHAT_TOKEN_2=$(sed -n 2p .chat_tokens) CHAT_TOKEN_3=$(sed -n 3p .chat_tokens)
export FRIDGE_API_KEY=${FRIDGE_API_KEY:-$(python3 -c 'import secrets;print(secrets.token_hex(8))')}

if [ -z "$GEMINI_API_KEY" ]; then
  read -r -s -p "Gemini API key (hidden, press Enter to skip and use the simple parser): " G; echo
  [ -n "$G" ] && export GEMINI_API_KEY=$G
fi
H=$(date +%H); if [ "$H" -ge 22 ] || [ "$H" -lt 7 ]; then export QUIET_START=24 QUIET_END=0; echo "Night time: quiet hours switched off for this test."; fi

HOST=0.0.0.0; [ "$1" = "local" ] && HOST=127.0.0.1
IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | cut -d' ' -f1)
echo
echo "Open the chat:"
echo "  this computer:  http://localhost:8000/chat"
[ "$HOST" = "0.0.0.0" ] && [ -n "$IP" ] && echo "  your phone:     http://$IP:8000/chat   (same Wi-Fi)"
echo "Sign-in tokens (paste on the sign-in screen):"
echo "  you:      $CHAT_TOKEN_1"
echo "  person 2: $CHAT_TOKEN_2"
echo "  person 3: $CHAT_TOKEN_3"
[ "$HOST" = "0.0.0.0" ] && echo "Note: plain http on your home network. Use only at home, and use 'local' on shared Wi-Fi."
echo
echo "Fridge key (for testing):  $FRIDGE_API_KEY"
echo "Start a test cart in another Terminal window:"
echo "  curl -X POST http://localhost:8000/fridge/list -H 'X-Fridge-Key: $FRIDGE_API_KEY' -H 'content-type: application/json' \\"
echo "    -d '{\"items\":[{\"name\":\"Tomato\"},{\"name\":\"Onion\"},{\"name\":\"Cucumber\"},{\"name\":\"Mushroom\"}]}'"
echo
exec python3 -m uvicorn run_dev:app --host "$HOST" --port 8000

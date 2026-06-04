#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# pb-ask — Send a question to the Privileged Brain daemon and stream the answer
#
# Usage:
#   pb-ask "list all listening TCP ports"
#   pb-ask                  # interactive mode (reads from stdin)
#   echo "question" | pb-ask
# ─────────────────────────────────────────────────────────────────────────────

SOCKET="/run/pb-daemon.sock"
OLLAMA_URL="http://localhost:11434/api/generate"
MODEL="privileged-brain"

# ── Fallback: query Ollama directly if daemon socket isn't up ────────────────
query_ollama_direct() {
  local prompt="$1"
  if command -v curl &>/dev/null; then
    curl -s "$OLLAMA_URL" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"$MODEL\",\"prompt\":$(printf '%s' "$prompt" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))'),\"stream\":true}" \
    | while IFS= read -r line; do
        token=$(echo "$line" | python3 -c 'import json,sys; d=json.loads(sys.stdin.read()); print(d.get("response",""),end="")' 2>/dev/null)
        printf "%s" "$token"
        done
    echo
  else
    echo "[pb-ask] curl not found. Install: sudo apt install curl"
    exit 1
  fi
}

# ── Build the prompt ─────────────────────────────────────────────────────────
if [ $# -gt 0 ]; then
  PROMPT="$*"
elif [ ! -t 0 ]; then
  # stdin pipe
  PROMPT=$(cat)
else
  # Interactive
  printf "Ask Privileged Brain: "
  read -r PROMPT
fi

if [ -z "$PROMPT" ]; then
  echo "Usage: pb-ask \"your question\""
  exit 1
fi

# ── Route to daemon or direct Ollama ─────────────────────────────────────────
if [ -S "$SOCKET" ]; then
  # Daemon is running — stream via socket (socat if available, else Python)
  if command -v socat &>/dev/null; then
    printf '%s\n' "$PROMPT" | socat - "UNIX-CONNECT:$SOCKET"
  else
    python3 - "$PROMPT" "$SOCKET" << 'PYEOF'
import socket, sys
prompt = sys.argv[1]
path   = sys.argv[2]
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
    s.connect(path)
    s.sendall((prompt + "\n").encode())
    s.shutdown(socket.SHUT_WR)
    while True:
        chunk = s.recv(256)
        if not chunk:
            break
        sys.stdout.write(chunk.decode(errors="replace"))
        sys.stdout.flush()
PYEOF
  fi
else
  # Daemon not running — fallback to direct Ollama query
  echo "[pb-ask] pb-daemon not running — querying Ollama directly..."
  query_ollama_direct "$PROMPT"
fi

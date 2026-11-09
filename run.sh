#!/usr/bin/env bash
# Ask the App
#   ./run.sh                     start with the projects in ~/.config/ask-the-app/config.json
#   ./run.sh /path/to/program    start answering questions about that folder (no settings needed)
#   ./run.sh stop                stop the copy this script started
set -euo pipefail
cd "$(dirname "$0")"
PIDFILE="${XDG_RUNTIME_DIR:-/tmp}/ask-the-app-$(id -u).pid"
PORT="${ASKAPP_PORT:-8795}"

if [[ "${1:-}" == "stop" ]]; then
  if [[ -f "$PIDFILE" ]] && kill "$(cat "$PIDFILE")" 2>/dev/null; then echo "Stopped."; else echo "It wasn't running from here."; fi
  rm -f "$PIDFILE"
  exit 0
fi

ARGS=(--port "$PORT")
[[ -n "${1:-}" ]] && ARGS+=(--root "$1")
python3 serve.py "${ARGS[@]}" &
echo $! > "$PIDFILE"
sleep 1
URL="http://127.0.0.1:$PORT/"
(command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1) || echo "Open $URL"
wait

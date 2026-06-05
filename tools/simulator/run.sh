#!/usr/bin/env bash
# kckit simulator — start/restart/stop
# Usage: ./tools/simulator/run.sh [start|stop|restart|status]

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PIDFILE="$ROOT/.kckit_server.pid"
LOG="$ROOT/temp/server.log"
CMD="python3 -m tools.simulator.server"

mkdir -p "$ROOT/temp"

_pid() { [ -f "$PIDFILE" ] && cat "$PIDFILE"; }

_running() {
  local p=$(_pid)
  [ -n "$p" ] && kill -0 "$p" 2>/dev/null
}

start() {
  if _running; then
    echo "already running (pid $(_pid))"
    return
  fi
  cd "$ROOT"
  nohup $CMD >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  echo "started (pid $!) → log: $LOG"
}

stop() {
  local p=$(_pid)
  if [ -z "$p" ]; then
    # fallback: kill by port
    p=$(lsof -ti :8765 2>/dev/null | head -1)
  fi
  if [ -n "$p" ]; then
    kill "$p" 2>/dev/null && echo "stopped (pid $p)" || echo "pid $p not found"
    rm -f "$PIDFILE"
  else
    echo "not running"
  fi
}

status() {
  if _running; then
    echo "running (pid $(_pid))"
    curl -s http://localhost:8765/api/bridge/status 2>/dev/null || echo "(server not yet ready)"
  else
    echo "stopped"
  fi
}

case "${1:-start}" in
  start)   start ;;
  stop)    stop ;;
  restart) stop; sleep 0.5; start ;;
  status)  status ;;
  *)       echo "usage: $0 [start|stop|restart|status]" ;;
esac

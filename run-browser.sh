#!/usr/bin/env bash
# Start the local realtime voice backend for the native macOS app.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

# Run this checkout's sources. Without this the backend imports whichever
# `chatbot` the venv has installed -- which can be another checkout's -- and
# then /health reports that path, the app sees a service from a foreign root,
# and restarts it forever.
export PYTHONPATH="$HERE/src${PYTHONPATH:+:$PYTHONPATH}"

CHATBOT_ENV="${CHATBOT_ENV:-$HOME/.config/chatbot/env}"
if [[ -f "$CHATBOT_ENV" ]]; then
  saved_openrouter="${OPENROUTER_API_KEY:-}"
  # shellcheck disable=SC1090
  source "$CHATBOT_ENV"
  [[ -n "$saved_openrouter" ]] && OPENROUTER_API_KEY="$saved_openrouter"
  export OPENROUTER_API_KEY
fi

PORT="${PORT:-8766}"
SERVER_LOG="${SERVER_LOG:-/tmp/chatbot-server.log}"
MODEL="${MODEL:-anthropic/claude-haiku-5.5}"
export MODEL

listener() { lsof -ti "TCP:$1" -sTCP:LISTEN 2>/dev/null | head -1 || true; }
reuse_running=false
for arg in "$@"; do
  case "$arg" in
    --reuse-running) reuse_running=true ;;
    # Accepted and ignored: there is no sidecar to start on its own any more.
    --sidecar-only) ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done
ports=("$PORT")

# --reuse-running keeps a service only while it runs this checkout's current
# code. Each service reports the fingerprint of the sources it loaded and
# whether disk has since changed; anything older than that contract, from
# this checkout may be replaced. Unverified or foreign listeners are refused.
HERE_PHYSICAL="$(pwd -P)"
PYTHON=""
if [[ -x .venv/bin/python3 ]]; then
  PYTHON=.venv/bin/python3
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
fi

health_url() {
  echo "http://127.0.0.1:$1/health"
}

# current | stale | foreign | unknown | unreachable, from one health probe.
service_state() {
  if [[ -z "$PYTHON" ]]; then
    echo unreachable
    return
  fi
  "$PYTHON" scripts/service_state.py "$1" "$HERE_PHYSICAL" 2>/dev/null || echo unreachable
}

# Only this project's own service may be stopped by this launcher.
ours() {
  local cmd
  cmd="$(ps -o command= -p "$1" 2>/dev/null || true)"
  [[ "$cmd" == *"chatbot serve"* ]]
}

# current | stale | foreign | unknown | hung | other for the service on port $1
# (pid $2). A listener that never answers gets a moment to finish binding
# before it counts as hung; both services serve their health route from the
# instant they listen, so a longer wait only delays the restart.
probe_service() {
  local port="$1" pid="$2" state=unreachable attempt
  for attempt in $(seq 1 5); do
    state="$(service_state "$(health_url "$port")")"
    [[ "$state" != unreachable ]] && break
    sleep 0.2
  done
  if [[ "$state" == unreachable ]]; then
    if ours "$pid"; then echo hung; else echo other; fi
  elif [[ "$state" == unknown ]] && ! ours "$pid"; then
    echo other
  else
    echo "$state"
  fi
}

# The launcher supervising pid $1, else $1 itself. Stopping a launcher runs
# its EXIT trap, which stops every service it owns without leaving orphans.
stop_target() {
  local pid="$1" parent cmd
  parent="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
  if [[ -n "$parent" && "$parent" != 1 ]]; then
    cmd="$(ps -o command= -p "$parent" 2>/dev/null || true)"
    if [[ "$cmd" == *run-browser.sh* ]]; then
      echo "$parent"
      return
    fi
  fi
  echo "$pid"
}

wait_port_free() {
  local port="$1" pid="$2"
  for _ in $(seq 1 50); do
    [[ -z "$(listener "$port")" ]] && return 0
    sleep 0.2
  done
  kill -KILL "$pid" 2>/dev/null || true
  for _ in $(seq 1 25); do
    [[ -z "$(listener "$port")" ]] && return 0
    sleep 0.2
  done
  echo "Error: could not free port $port (pid $pid)." >&2
  return 1
}

held_ports=()
held_pids=()
held_targets=()
held_states=()
for port in "${ports[@]}"; do
  pid="$(listener "$port")"
  [[ -z "$pid" ]] && continue
  if [[ "$reuse_running" == false ]]; then
    echo "Error: port $port is already in use by pid $pid." >&2
    exit 1
  fi
  held_ports+=("$port")
  held_pids+=("$pid")
  held_targets+=("$(stop_target "$pid")")
  held_states+=("$(probe_service "$port" "$pid")")
done

stop_targets=""
for i in ${held_ports[@]+"${!held_ports[@]}"}; do
  case "${held_states[$i]}" in
    current) ;;
    stale)
      echo "The service on port ${held_ports[$i]} (pid ${held_pids[$i]}) is ${held_states[$i]}; restarting it from $HERE."
      stop_targets="$stop_targets ${held_targets[$i]} "
      ;;
    *)
      echo "Error: port ${held_ports[$i]} is ${held_states[$i]}; ownership is not verified. Leaving pid ${held_pids[$i]} alone." >&2
      exit 1
      ;;
  esac
done
if [[ -n "$stop_targets" ]]; then
  for target in $(printf '%s\n' $stop_targets | sort -u); do
    kill -TERM "$target" 2>/dev/null || true
  done
  # A stopped launcher takes every service it owns with it, current or not,
  # so wait for each port that is about to clear rather than reuse a listener
  # that is already on its way out.
  for i in ${held_ports[@]+"${!held_ports[@]}"}; do
    [[ "$stop_targets" == *" ${held_targets[$i]} "* ]] || continue
    if [[ "${held_states[$i]}" == current ]]; then
      echo "The service on port ${held_ports[$i]} shares that launcher and restarts with it."
    fi
    wait_port_free "${held_ports[$i]}" "${held_pids[$i]}"
  done
fi

server_pid=""
cleanup() {
  [[ -n "$server_pid" ]] && kill "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -z "$(listener "$PORT")" ]]; then
  echo "Starting the Chatbot voice service..."
  PORT="$PORT" ./run-openrouter.sh >"$SERVER_LOG" 2>&1 &
  server_pid=$!
fi
echo "Voice backend on port $PORT."
echo "Ctrl+C stops the services started by this launcher."
if [[ -n "$server_pid" ]]; then
  for _ in $(seq 1 600); do
    [[ -n "$(listener "$PORT")" ]] && break
    if ! kill -0 "$server_pid" 2>/dev/null; then
      tail -20 "$SERVER_LOG" >&2
      exit 1
    fi
    sleep 0.2
  done
  if [[ -z "$(listener "$PORT")" ]]; then
    echo "The model server did not start. See $SERVER_LOG." >&2
    exit 1
  fi
fi
# Supervise only a child this launcher owns. An existing external service
# must not be killed when this launcher exits.
while [[ -n "$server_pid" ]]; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    echo "The Chatbot voice service stopped. Check $SERVER_LOG." >&2
    exit 1
  fi
  sleep 1
done

#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091 # repo-relative path computed from this script location
source "${repo_root}/scripts/live-stack/env.sh"

pid_file="${KDIVE_STACK_PID_FILE:-${repo_root}/.live-stack.pid}"
log_dir="${KDIVE_STACK_LOG_DIR:-${repo_root}/.live-stack-logs}"
mode="${1:-foreground}"

mkdir -p "${log_dir}"

# shellcheck disable=SC2329 # invoked by trap below
cleanup() {
  if [[ -f "${pid_file}" ]]; then
    while read -r pid; do
      [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true
    done <"${pid_file}"
    rm -f "${pid_file}"
  fi
}
trap cleanup EXIT INT TERM

start_one() {
  local name="$1"
  shift
  "$@" >"${log_dir}/${name}.log" 2>&1 &
  echo "$!" >>"${pid_file}"
}

children_running() {
  local running
  local pid
  running="$(jobs -pr)"
  while read -r pid; do
    [[ -n "${pid}" ]] || continue
    if ! grep -qx -- "${pid}" <<<"${running}"; then
      return 1
    fi
  done <"${pid_file}"
}

rm -f "${pid_file}"
start_one server uv run python -m kdive server
start_one worker uv run python -m kdive worker
start_one reconciler uv run python -m kdive reconciler

if [[ "${mode}" == "--daemon" ]]; then
  sleep 0.2
  if ! children_running; then
    echo "KDIVE MCP stack failed during startup; see ${log_dir}" >&2
    exit 1
  fi
fi

echo "KDIVE MCP stack started"
echo "MCP URL: ${KDIVE_STACK_BASE_URL}"
echo "Logs: ${log_dir}"

if [[ "${mode}" == "--daemon" ]]; then
  trap - EXIT INT TERM
  exit 0
fi

set +e
wait -n
rc="$?"
set -e
exit "${rc}"

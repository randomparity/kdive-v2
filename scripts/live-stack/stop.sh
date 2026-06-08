#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
pid_file="${KDIVE_STACK_PID_FILE:-${repo_root}/.live-stack.pid}"

if [[ ! -f "${pid_file}" ]]; then
  echo "no KDIVE stack pid file at ${pid_file}"
  exit 0
fi

while read -r pid; do
  [[ -n "${pid}" ]] && kill "${pid}" 2>/dev/null || true
done <"${pid_file}"
rm -f "${pid_file}"

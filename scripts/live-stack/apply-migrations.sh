#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091 # repo-relative path computed from this script location
source "${repo_root}/scripts/live-stack/env.sh"

uv run python - <<'PY'
import os

import psycopg

from kdive.db.migrate import apply_migrations

conn = psycopg.connect(os.environ["KDIVE_DATABASE_URL"], autocommit=True)
try:
    applied = apply_migrations(conn)
finally:
    conn.close()

print(f"applied {len(applied)} migration(s)")
PY

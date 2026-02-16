#!/usr/bin/env bash
set -euo pipefail

echo "[calendo] starting portal…"

# Make the portal package importable even when Render starts from repo root.
export PYTHONPATH="portal${PYTHONPATH:+:$PYTHONPATH}"

# Run migrations (recommended). You can disable by setting SKIP_MIGRATIONS=1.
if [ "${SKIP_MIGRATIONS:-0}" != "1" ]; then
  echo "[calendo] running migrations…"
  attempts="${MIGRATION_RETRY_ATTEMPTS:-20}"
  sleep_s="${MIGRATION_RETRY_SLEEP_SECONDS:-2}"

  i=1
  while [ "$i" -le "$attempts" ]; do
    if alembic -c portal/alembic.ini upgrade head; then
      echo "[calendo] migrations complete."
      break
    fi
    echo "[calendo] alembic failed (attempt ${i}/${attempts}) — retrying in ${sleep_s}s…" >&2
    i=$((i + 1))
    sleep "$sleep_s"
  done

  if [ "$i" -gt "$attempts" ]; then
    echo "[calendo] WARNING: migrations kept failing; starting app anyway." >&2
  fi
fi

exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8010}"
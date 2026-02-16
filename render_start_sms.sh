#!/usr/bin/env bash
set -euo pipefail

echo "[calendo-sms] starting SMS Calendar (Streamlit)…"

# By default we keep the SMS app under /sms so the portal can link to it.
# If you deploy the SMS service on its own domain and want it at /, set:
#   SMS_BASE_PATH=
BASE_PATH="${SMS_BASE_PATH:-sms}"

port="${PORT:-8501}"

cmd=(streamlit run sms/apps/fullcalendar_app.py \
  --server.address 0.0.0.0 \
  --server.port "$port" \
  --server.headless true \
  --browser.gatherUsageStats false)

if [ -n "$BASE_PATH" ]; then
  cmd+=(--server.baseUrlPath "/${BASE_PATH}")
fi

exec "${cmd[@]}"
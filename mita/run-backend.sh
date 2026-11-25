#!/usr/bin/env bash
set -euo pipefail

export MITA_CONFIG_JSON_FILE=/data/mita-server.json
export MITA_LOG_NO_TIMESTAMP=true
export MITA_UDS_PATH=/data/mita.sock
export MITA_INSECURE_UDS=1

python3 /app/webapp.py --init
exec python3 /app/webapp.py

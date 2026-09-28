#!/usr/bin/with-contenv bashio
set -euo pipefail

CONFIG_PATH="/data/mita-server.json"

export MITA_CONFIG_JSON_FILE="${CONFIG_PATH}"
export MITA_LOG_NO_TIMESTAMP="true"
export MITA_UDS_PATH="/data/mita.sock"
export MITA_INSECURE_UDS="1"

python3 /app/webapp.py --init

# Mita stores rolling per-user metrics in /var/lib/mita/metrics.pb.
# Home Assistant add-on updates may recreate the container, so keep that
# directory on /data and link Mita's standard state path to it.
mkdir -p /data/mita-state
if [ -e /var/lib/mita ] && [ ! -L /var/lib/mita ]; then
  rm -rf /var/lib/mita
fi
ln -sfn /data/mita-state /var/lib/mita

USER_COUNT="$(python3 - <<'PY'
import json
with open("/data/users.json", "r", encoding="utf-8") as f:
    print(len(json.load(f)))
PY
)"

bashio::log.info "Starting mita on TCP port 2022"
bashio::log.info "Configured users: ${USER_COUNT}"
bashio::log.info "Admin UI is available through Home Assistant Ingress"
bashio::log.info "Subscription server is listening on TCP port 8099"

touch /data/mita.log
mita run > >(tee -a /data/mita.log) 2>&1 &
MITA_PID=$!

python3 /app/webapp.py &
WEB_PID=$!

cleanup() {
  kill "${WEB_PID}" "${MITA_PID}" 2>/dev/null || true
  wait "${WEB_PID}" "${MITA_PID}" 2>/dev/null || true
}
trap cleanup SIGTERM SIGINT EXIT

while kill -0 "${MITA_PID}" 2>/dev/null && kill -0 "${WEB_PID}" 2>/dev/null; do
  sleep 2
done

bashio::log.error "mita or admin/subscription server exited unexpectedly"
exit 1

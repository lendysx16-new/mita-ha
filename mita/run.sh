#!/usr/bin/with-contenv bashio
set -euo pipefail

CONFIG_PATH="/data/mita-server.json"
OPTIONS_PATH="/data/options.json"

if [[ ! -s "${OPTIONS_PATH}" ]]; then
  bashio::log.fatal "Home Assistant options file is missing"
  exit 1
fi

USER_COUNT="$(jq '.users | length' "${OPTIONS_PATH}")"
if [[ "${USER_COUNT}" -lt 1 ]]; then
  bashio::log.fatal "Configure at least one user"
  exit 1
fi

if jq -e '.users[] | select(.username == "change-me" or .password == "change-this-password")' "${OPTIONS_PATH}" >/dev/null; then
  bashio::log.warning "Default credentials are still configured. Change them before exposing the server to the Internet."
fi

LOG_LEVEL="$(jq -r '.log_level // "INFO"' "${OPTIONS_PATH}")"
PREFER_IPV4="$(jq -r '.prefer_ipv4 // true' "${OPTIONS_PATH}")"
ALLOW_PRIVATE_IP="$(jq -r '.allow_private_ip // false' "${OPTIONS_PATH}")"
ALLOW_LOOPBACK_IP="$(jq -r '.allow_loopback_ip // false' "${OPTIONS_PATH}")"

if [[ "${PREFER_IPV4}" == "true" ]]; then
  DNS_POLICY="PREFER_IPv4"
else
  DNS_POLICY="USE_FIRST_IP"
fi

jq --arg log_level "${LOG_LEVEL}" --arg dns_policy "${DNS_POLICY}" --argjson allow_private "${ALLOW_PRIVATE_IP}" --argjson allow_loopback "${ALLOW_LOOPBACK_IP}" '
{
  portBindings: [
    {
      port: 2022,
      protocol: "TCP"
    }
  ],
  users: [
    .users[] | {
      name: .username,
      password: .password,
      allowPrivateIP: $allow_private,
      allowLoopbackIP: $allow_loopback
    }
  ],
  loggingLevel: $log_level,
  dns: {
    dualStack: $dns_policy
  }
}
' "${OPTIONS_PATH}" > "${CONFIG_PATH}"

chmod 600 "${CONFIG_PATH}"

bashio::log.info "Starting mita on TCP port 2022"
bashio::log.info "Configured users: ${USER_COUNT}"

export MITA_CONFIG_JSON_FILE="${CONFIG_PATH}"
export MITA_LOG_NO_TIMESTAMP="true"
export MITA_UDS_PATH="/data/mita.sock"
export MITA_INSECURE_UDS="1"

exec /usr/bin/mita run

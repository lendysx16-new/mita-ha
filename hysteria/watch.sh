#!/bin/sh
# Hysteria does not hot-reload ACL rules. Restart only when their config changes.
set -eu

config=/hysteria-config/hysteria2.yaml
generation=/run/hysteria2/generation
pid=

stop_server() {
  if [ -n "$pid" ]; then
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    pid=
  fi
}
trap 'stop_server; exit 0' INT TERM

previous=
while :; do
  if [ -s "$config" ]; then
    current=$(sha256sum "$config" | cut -d ' ' -f 1)
    if [ "$current" != "$previous" ] || [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
      stop_server
      # Notify the backend of each new counter epoch. Hysteria's mounts contain
      # only its own generated config, certificate chain and this marker.
      cat /proc/sys/kernel/random/uuid > "$generation.tmp"
      mv "$generation.tmp" "$generation"
      hysteria server -c "$config" &
      pid=$!
      previous=$current
    fi
  else
    stop_server
    previous=
  fi
  sleep 2 & wait $!
done

# Changelog

## 3.38.0-3

- Added a separate Traffic tab to the Home Assistant Ingress UI.
- Added per-user 24-hour, 7-day, and 30-day download/upload counters from `mita get users`.
- Added automatic traffic refresh every 30 seconds while the Traffic tab is open.
- Persisted Mita metrics under `/data/mita-state`.

## 3.38.0-2

- Added Home Assistant Ingress admin UI.
- Added user creation/deletion without app restart.
- Added per-user private/LAN and loopback permissions.
- Added per-user subscription tokens.
- Added ClashMi YAML subscription server on TCP 8099.
- Added editable public Mieru endpoint and subscription base URL.

## 3.38.0-1

- Initial Home Assistant packaging.
- Mita 3.38.0.

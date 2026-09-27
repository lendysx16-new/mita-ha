# Changelog

## 3.38.0-9

- Switched traffic timestamps to `Intl.DateTimeFormat` using the browser locale.
- Collapsed per-device traffic under expandable user rows.
- Added clearer spacing between traffic user groups.

## 3.38.0-8

- Reworked the subscription landing page as a mobile-first layout.
- Added a client selector for Clash Mi, Karing, FlClash, Clash Verge Rev, and generic Clash/Mihomo clients.
- Download links now change with the selected client and platform support.
- Added verified app deep links for Clash Mi, Karing, and FlClash.
- HWID is enabled automatically only for clients that support it.

## 3.38.0-7

- Hardened HTML/YAML negotiation: browser landing pages are detected using Fetch Metadata instead of Accept alone.
- Added `?web=1` to explicitly force the landing page and kept `?raw=1` for raw YAML.
- Clash/Mihomo deep links now use the raw subscription URL explicitly.

## 3.38.0-6

- Added a browser-friendly subscription landing page at the existing `/sub/<token>` URL.
- Added Open in Clash Mi and Open in Clash deep links with X-HWID enabled automatically.
- Added Copy link and Clash Mi download links for macOS, Windows, iOS, and Android.
- Kept subscription clients backward-compatible through Accept-based HTML/YAML content negotiation.
- Added `?raw=1` to force raw YAML in a browser.
- Bundled the frontend as one SSR HTML file using Vue 3 from jsDelivr.

## 3.38.0-5

- Format Last active timestamps using the browser locale instead of showing raw ISO strings.

## 3.38.0-4

- Added HWID-aware device registration behind the existing per-user subscription URL.
- Added a Devices tab with per-user device limits, device metadata, and device removal.
- Added backward-compatible legacy credentials so existing imported profiles continue working during migration.
- Added the option to disable legacy access after at least one HWID device has registered.
- Added separate Mieru credentials per device and grouped device traffic under the parent user.
- Added Remnawave-compatible HWID response headers for unsupported clients and reached device limits.

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

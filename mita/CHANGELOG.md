# Changelog

## 3.38.0.17

- Added explicit `GEOSITE,google-deepmind` routing for Gemini, AI Studio, and the Google Generative Language API.
- Added explicit `GEOSITE,anthropic` routing for Claude and Anthropic domains.
- These rules are evaluated before the general `ru-blocked` rules so related API/CDN domains also use Mieru.

## 3.38.0.16

- Added a first-pass Rule-mode routing test using Runet Freedom `geosite.dat` and `geoip.dat`.
- `GEOSITE,ru-blocked` and `GEOIP,ru-blocked` now route through Mieru; unmatched traffic goes `DIRECT`.
- Enabled Mihomo geodata auto-update every 6 hours with the memory-conservative loader.
- Removed `DIRECT` from the selectable PROXY group so the Mieru route cannot be accidentally changed there.

## 3.38.0.15

- Fixed legacy compatibility with Clash Mi: `x-hwid-not-supported` is now returned only when legacy access is disabled and HWID is actually required.
- Manual subscription imports keep working while **Legacy credential enabled** is on.
- HWID-capable deep links still register separate devices normally.

## 3.38.0.14

- Refined the public subscription page using the emerald palette #092328 / #12544F / #2A835F / #8BBB92.
- Increased emerald gradient transitions while removing heavy shadows and glow.
- Simplified card radius and hierarchy to keep the client UI minimal.

## 3.38.0.13

- Switched the add-on revision format from `3.38.0-N` to `3.38.0.N` so Home Assistant's AwesomeVersion comparison recognizes new revisions as updates.
- Keeps the bundled Mita upstream version at 3.38.0 while using the fourth numeric component as the add-on revision.

## 3.38.0-12

- Added a dark emerald gradient theme to the public subscription landing page.
- Added subtle glass/blur, emerald accents, and stronger mobile visual hierarchy.

## 3.38.0-11

- Detect the subscription-page visitor OS from the browser User-Agent.
- Show download links for the detected platform first.
- Move installers for other operating systems under an expandable Other platforms section.
- Keep architecture-specific choices visible when the browser cannot reliably identify CPU architecture.

## 3.38.0-10

- Added direct latest GitHub Release downloads for Clash Mi, Karing, FlClash, and Clash Verge Rev where App Store links are not available.
- Added release-asset resolution on the subscription server, with a short cache and redirect to the matching GitHub asset.
- Activity timestamps now default to localized relative time via `Intl.RelativeTimeFormat`.
- Clicking an activity timestamp toggles between relative and exact localized date/time.

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

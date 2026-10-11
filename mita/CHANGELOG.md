## 3.38.0.28

- Restrict the public app selector to HWID-capable clients only (Clash Mi, Karing, FlClashX, Koala Clash), retaining old download endpoints for compatibility.
- Distinguish the two setup steps with separate compact rectangular borders and pink accent markers/buttons in light and dark modes.
- Add Cyrillic-capable Press Start 2P pixel titles with Manrope body text and monospace fallback.
- Make the unselected "Платформа" placeholder disabled and hidden from the selection list.

- Condensed the public onboarding page for iPhone: two steps and device/client selectors fit in the first screen on typical Safari viewport sizes, with advanced copy and token controls collapsed.
- Show device-compatible clients first and hide unsupported apps on the selected platform, while allowing manual device switching.
- Added verified HWID-capable Mihomo clients: FlClashX (Android/Windows/macOS/Linux) and Koala Clash (Windows/macOS/Linux).
- Added deep links for `flclashx://install-config` and `koala-clash://install-config`, preserving automatic native HWID headers.
- Extended protected GitHub Release downloads and UI/backend regression coverage for both clients.

## 3.38.0.27

- Rebuilt the public subscription landing page as a simple two-step setup: install the selected client, then open the personalized profile.
- Preserved existing client download URLs and xhwid-aware deep links, with manual copy for clients without import links.
- Removed Vue/CDN dependency from the public subscription page and added browser-behavior regression tests.
- Added Manrope typography (with local font fallback), automatic system dark/light colors, and staggered CSS entrance animations with reduced-motion support.
- Simplified the subscription design with flat, shadow-free sections, 5px controls, thin dividers and unobtrusive step numbers.

- Serialize user, token, device and settings mutations to prevent lost updates under concurrent API requests.
- Roll back persistent state and retry the previous configuration when Mita rejects a reload; report HTTP 503 rather than false success.
- Avoid logging subscription tokens in HTTP access logs.
- Validate JSON request bodies and Content-Length before processing API mutations.
- Keep newly generated user passwords visible until explicitly dismissed in the admin UI.
- Extract Mita config generation into a pure function and reject malformed public subscription base URLs.
- Add regression tests for API operations, device registration, concurrency, and rollback behavior.

## 3.38.0.26

- Redesigned Home Assistant Ingress admin panel with shadcn-inspired colors, layout, cards, forms, badges, and responsive navigation.
- Added account filtering and overview counters for users, HWID devices, and legacy credentials.
- Kept all existing management APIs and browser interactions without adding CDN or JavaScript dependencies.
- Extracted the admin page to `admin.html` for easier future UI updates.

## 3.38.0.25

- Subscription configs now use global proxy mode for every client.
- Removed per-domain/IP routing rule providers and routing rules from generated configs.

# Changelog

## 3.38.0.21

- Fixed broken mobile table layout in Users and Devices by hiding desktop headers and rendering rows as full-width stacked cards.
- Added distinct card surfaces across the admin UI so sections stand out from the page background.
- Added dedicated mobile backgrounds for Traffic parent and device cards while keeping the desktop table layout unchanged.

## 3.38.0.20

- Legacy credentials can now be disabled before any HWID device has registered; an HWID-capable client can create its device credential on the first subscription refresh.
- New users can be created with legacy credentials disabled from the start.
- Reworked the admin UI for mobile-first use with larger tap targets, stacked forms, and responsive user/device tables.
- Rebuilt the Traffic view as compact mobile cards while preserving the desktop table layout and expandable per-device counters.

## 3.38.0.19

- Removed the temporary `/sub/rules/ru-blocked-domain.list` endpoint; the Home Assistant server no longer proxies or transforms domain rules.
- Switched `ru-blocked-domain` to the ready-made Mihomo `geosite-ru-blocked.mrs` from `igorkass/mihomo-rule-sets`.
- Switched `ru-blocked-ip` to the matching `geoip-ru-blocked.mrs` from the same repository.
- Those MRS files are generated from Runet Freedom's `russia-v2ray-rules-dat` ru-blocked rule sets and updated automatically.

## 3.38.0.18

- Switched routing from `GEOSITE` / `GEOIP` rules to explicit Mihomo `RULE-SET` providers so Clash Mi no longer rewrites `ru-blocked` to missing MetaCubeX files.
- `ru-blocked-domain` is generated from Runet Freedom's current `ru-blocked.txt` geosite category and served as Mihomo text-domain rules.
- `ru-blocked-ip` uses Runet Freedom's native `mrs/ru-blocked.mrs` provider.
- Gemini/Google DeepMind and Anthropic/Claude use existing MetaCubeX MRS providers.
- All provider rules update every 6 hours; unmatched traffic remains `DIRECT`.

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

# Mita (mieru server)

This app runs `mita`, provides a Home Assistant Ingress admin UI, and serves per-user ClashMi subscriptions.

## Admin UI

Open the app and click **Open Web UI**.

The admin UI is not published as a normal host port. It is served through Home Assistant Ingress and lets you:

- add users;
- delete users;
- choose per-user LAN/loopback access;
- rotate subscription tokens;
- copy subscription URLs;
- configure the public Mieru hostname and port;
- view per-user traffic in a separate **Traffic** tab (24 hours, 7 days, and 30 days, split into download/upload);
- automatically register HWID-capable clients as separate devices behind the same subscription URL;
- set a per-user device limit and remove individual devices;
- keep legacy credentials enabled during migration, then disable them after all needed devices have registered.

Traffic data comes from Mita's native per-user metrics. Device-specific Mieru credentials are grouped back under the parent user in the Traffic tab. Activity timestamps are shown as localized relative time (for example, “1 hour ago”); clicking a timestamp toggles the exact localized date/time. The Web UI refreshes it on demand and every 30 seconds while the Traffic tab is open. Metrics are stored under `/data/mita-state` so they survive normal add-on restarts and future container updates.

User changes are applied with `mita reload`; the app does not need to be restarted.

## Mieru port

The container listens on `2022/TCP`.

Example router forwarding:

```text
Internet TCP 8443 -> Home Assistant IP TCP 2022
```

Or set the app's host port for `2022/tcp` to `8443` and forward:

```text
Internet TCP 8443 -> Home Assistant IP TCP 8443
```

## Subscription server

The subscription HTTP server listens on host port `8099/TCP`.

Do **not** forward 8099 directly from the router. Expose only `/sub/*` through your existing Caddy HTTPS endpoint.

Example:

```caddy
me.lendysx16.ru {
  handle /sub/* {
    reverse_proxy http://HOME_ASSISTANT_IP:8099
  }
}
```

Then each user gets a URL like:

```text
https://me.lendysx16.ru/sub/<random-token>
```

Opening the subscription URL in a browser shows a guided, mobile-first **two-step setup page**:

1. **Install the app:** choose Clash Mi, Karing, FlClash or another client; choose the target OS if needed. The page displays the direct App Store or current GitHub Release installer for that platform.
2. **Open the profile in the app:** use the client's import deep link, preserving `xhwid=true` for Clash Mi and Karing. If a client does not have an import URL scheme, copy the subscription URL and import it manually.

Additional release variants, manual copy, and the raw subscription token URL are placed in optional controls so first-time users only see the two steps. The page detects the visitor's OS from User-Agent, but allows manual platform selection. The landing page now uses bundled HTML/CSS/JavaScript with **no Vue or CDN dependency**, so it works independently of third-party script hosting.

The public `/sub/<token>/download/<client>/<target>` URLs still resolve current GitHub Release installers through the server, and the private subscription URL remains hidden until the visitor chooses to reveal it.

The same `/sub/<token>` URL still returns YAML to ClashMi and other clients because they request it as a subscription instead of HTML. Add `?raw=1` to force the YAML response in a browser.

The landing page is served from the bundled `subscription.html` template; it requires no frontend build step or external JavaScript dependencies.

ClashMi can also use the URL directly in **Add Profile Link**.

### Device-aware subscriptions

The subscription endpoint understands the Remnawave/Happ HWID headers:

```text
x-hwid
x-device-os
x-ver-os
x-device-model
user-agent
```

When a valid `x-hwid` is present, the add-on creates a private Mieru credential for that device while keeping the same public subscription URL. Device limits are enforced when a new HWID is registered.

For existing installations, **Legacy credential enabled** is on by default. Requests without HWID continue to receive the old username/password, so existing imported profiles keep working. After supported clients refresh their subscription and appear under **Devices**, legacy access can be disabled for that user.

HWID support depends on the client. Some clients, including ClashMi, can have HWID sending disabled by default.

The public subscription endpoint serves only `/sub/<token>`; it has no admin API.

## Existing installations

On first start after upgrading, existing users from the Home Assistant app configuration are migrated to `/data/users.json`. After that, the Web UI becomes the user database.

## Development and regression tests

The add-on uses Python's standard library for API tests. Run from the repository root:

```bash
python3 -m unittest discover -s tests -v
```

Tests use temporary files and mock the Mita reload command. GitHub Actions also checks Python syntax, shell syntax, YAML, and the admin page JavaScript. No live Home Assistant or Mita process is required for the regression suite.

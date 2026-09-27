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

Traffic data comes from Mita's native per-user metrics. Device-specific Mieru credentials are grouped back under the parent user in the Traffic tab. The Web UI refreshes it on demand and every 30 seconds while the Traffic tab is open. Metrics are stored under `/data/mita-state` so they survive normal add-on restarts and future container updates.

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

Opening the subscription URL in a browser shows a mobile-first landing page. It has:

- a client picker for Clash Mi, Karing, FlClash, Clash Verge Rev, and a generic Clash/Mihomo client;
- **Open in client** deep links where the client exposes a verified URL scheme;
- automatic `xhwid=true` for Clash Mi and Karing, which support HWID subscription headers;
- a **Copy link** button;
- download links that change with the selected client and only show the platforms that client publishes.

The same `/sub/<token>` URL still returns YAML to ClashMi and other clients because they request it as a subscription instead of HTML. Add `?raw=1` to force the YAML response in a browser.

The landing page is a single server-rendered HTML file bundled with the add-on. Vue 3 is loaded from jsDelivr; there is no frontend build step.

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

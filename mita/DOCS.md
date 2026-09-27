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
- view per-user traffic in a separate **Traffic** tab (24 hours, 7 days, and 30 days, split into download/upload).

Traffic data comes from Mita's native per-user metrics. The Web UI refreshes it on demand and every 30 seconds while the Traffic tab is open. Metrics are stored under `/data/mita-state` so they survive normal add-on restarts and future container updates.

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

ClashMi can use this URL in **Add Profile Link**.

The public subscription endpoint serves only `/sub/<token>`; it has no admin API.

## Existing installations

On first start after upgrading, existing users from the Home Assistant app configuration are migrated to `/data/users.json`. After that, the Web UI becomes the user database.

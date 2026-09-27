# Mita Home Assistant Add-on

Home Assistant app/add-on repository for **mita**, the server component of the [mieru](https://github.com/enfein/mieru) proxy protocol.

Features:

- Mita 3.38.0 for amd64/aarch64
- TCP transport
- Home Assistant Ingress admin UI
- add/delete users without restarting the app
- per-user LAN access settings
- per-user ClashMi subscription links
- public subscription endpoint separated from the private admin UI

## Install

1. Add this repository to the Home Assistant App/Add-on Store.
2. Install **Mita (mieru server)**.
3. Start the app.
4. Open its Web UI and manage users there.
5. Forward the Mieru TCP port on your router.
6. Reverse proxy only `/sub/*` from HTTPS to the subscription server on port 8099.

See the app documentation for the Caddy example.

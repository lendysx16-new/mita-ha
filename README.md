# Mita Home Assistant Add-on

Home Assistant app/add-on repository for **mita**, the server component of the [mieru](https://github.com/enfein/mieru) proxy protocol.

Features:

- Mita 3.40.0 for amd64/aarch64
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

## Run on Linux

Docker Engine with the Compose plugin builds the same Mita server and web UI
for Linux amd64 or arm64. The Linux service uses host networking. Mita listens on
TCP 2022, subscriptions on 8099, and the admin UI only on 127.0.0.1:8098.
Access the admin UI from another machine through an SSH tunnel:

```sh
ssh -L 8098:127.0.0.1:8098 user@linux-server
```

Open http://127.0.0.1:8098 through that tunnel. Put an HTTPS reverse proxy in
front of the subscription endpoint and publish only `/sub/*`. Set
`MITA_PUBLIC_HOST`, `MITA_PUBLIC_PORT` and `MITA_SUBSCRIPTION_BASE_URL` in
Compose's local `.env` for fresh installs, or edit Public connection settings
in the admin UI. Set `MITA_POSTGRES_DSN` to enable PostgreSQL backups on a fresh install.
The connection string contains credentials. Keep `.env` private.

For a fresh install:

```sh
docker compose up -d --build
```

### Move from Home Assistant using the same PostgreSQL database

1. On Home Assistant, confirm a successful PostgreSQL backup in the admin UI.
   Starting the add-on writes a fresh snapshot. Stop the old instance once the
   snapshot is complete and before starting the replacement.
2. On Linux, set `MITA_POSTGRES_DSN` in `.env` to a connection string for the same
   database and schema. If Linux cannot resolve the HA database hostname, use an
   address reachable from Linux.
3. Restore before the first start. For an existing Linux instance, stop it and
   set `MITA_DATA_VOLUME` to a new volume name in `.env` to retain the old data.
   The restore command refuses a nonempty volume.

```sh
docker compose build
docker compose run --rm --entrypoint python3 mita /app/webapp.py --restore-postgres latest
docker compose up -d
```

Replace `latest` with the numeric `id` from `mita_backups` to select a specific
snapshot. Restore brings over users, passwords, subscription tokens, registered
devices, settings and saved Mita metrics. The supplied DSN is saved so the new
instance continues backing up to the same database. Snapshot archives exclude
local logs and sockets. Database log history stays in PostgreSQL.

To keep existing subscription URLs working, point their original hostname and
proxy routes at the Linux server. The database stores snapshots, so use it for
migration or recovery with one active instance. Running HA and Linux against the
same tables does not synchronize changes and mixes their snapshot histories.

## Route selected destinations through an outbound proxy

Open **Settings → Outbound** in the Mita admin UI on Home Assistant or Linux.
Save the JSON configuration, then restart the HA add-on or run
`docker compose restart mita` on Linux. Changes take effect after restart.
Use empty `proxies` and `rules` arrays to remove all custom routing.

Rules are evaluated in order. `domainNames` entries match the domain and its
subdomains when the client sends a hostname. If the client sends an IP, use
`ipRanges` with CIDRs. `"*"` matches all entries of that kind. A rule's
`action` is `PROXY`, `DIRECT` or `REJECT`. A `PROXY` rule references one or
more configured `proxyNames`. Unmatched destinations go directly.

Mita connects to a SOCKS5 proxy defined by `name`, `host` and `port`.
For authentication, add `"socks5Authentication": {"user": "...", "password": "..."}`
to the proxy. PostgreSQL snapshots include the outbound settings and their
credentials. The external sing-box configuration is a separate file.

### Optional sing-box service on Linux

Copy `sing-box/config.example.json` to `sing-box/config.json`. Keep its SOCKS5
inbound bound to `127.0.0.1:1080`. The example uses placeholder VLESS settings. Replace them with your provider
settings or use a different sing-box outbound such as Shadowsocks, Trojan or
Hysteria2. Set `route.final` to that outbound’s tag. Keep endpoint credentials in this local file.

```sh
docker compose --profile sing-box run --rm --no-deps sing-box check -c /etc/sing-box/config.json
docker compose --profile sing-box up -d
```

Use the sing-box example in the Mita Outbound editor to route specific domains
through that SOCKS5 listener. On Home Assistant, point Mita at a reachable
SOCKS5 listener from a separately installed sing-box instance. The add-on's
loopback address reaches only the Mita container. If the listener is remote,
require SOCKS5 authentication and limit network access to the Mita host.
SOCKS5 credentials alone do not encrypt the connection.

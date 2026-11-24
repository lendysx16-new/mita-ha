# Mita server

A Docker Compose deployment of **mita**, the server of the
[mieru](https://github.com/enfein/mieru) proxy protocol, with a local admin
panel, per-user subscription links and sing-box based routing through your
provider servers.

- Mita 3.40.0 (built from source with a per-account outbound patch) for Linux amd64 / arm64
- admin panel: users, HWID devices, traffic, settings, outbounds and routing
- add, change and delete users without restarting Mita
- per-user subscription page and Mihomo YAML for Clash Mi, Karing, FlClashX and other clients
- outbounds from share links (Hysteria2, VLESS REALITY, Trojan, …) with health checks
- routing policies with GeoSite / GeoIP categories, applied by sing-box
- automatic PostgreSQL backups and restore

## Install

Needs Docker Engine with the Compose plugin. Create `.env` next to
`compose.yaml`:

```sh
MITA_ADMIN_PASSWORD=...          # at least 24 characters, e.g. openssl rand -hex 24
MITA_PUBLIC_HOST=vpn.example.com # host clients connect to
MITA_PUBLIC_PORT=2022            # public TCP port of Mita (after port forwarding)
MITA_SUBSCRIPTION_BASE_URL=https://vpn.example.com
MITA_POSTGRES_DSN=               # optional: postgresql://user:password@host:5432/db for backups
```

The public settings are only defaults for a fresh install; later change them
in the admin panel. `.env` contains secrets, keep it private.

```sh
docker compose up -d --build
```

The service uses host networking: Mita listens on TCP 2022, the subscription
server on 8099 and the admin panel only on `127.0.0.1:8098`. Mita users can
never reach the host's loopback addresses.

### Admin panel

Open it through an SSH tunnel and sign in as `admin` with
`MITA_ADMIN_PASSWORD`:

```sh
ssh -L 8098:127.0.0.1:8098 user@server
```

then open http://127.0.0.1:8098. The **Users** tab lists accounts; open one for
its subscription link, password and token actions, HWID devices, device limit,
LAN access and outbound. **Devices** lists every registered device,
**Traffic** shows per-user and per-device totals (24 hours, 7 and 30 days), and
**Settings** holds the public connection, contact email, log level and
PostgreSQL. User changes are applied with `mita reload`.

The first start creates the account `change-me` with a random password; rename
or delete it after adding your own users. Accounts that still use the old
public placeholder password are flagged with **Default password**.

### Subscriptions

Block external access to TCP 8099 with the host firewall and publish only
`/sub/*` through your HTTPS reverse proxy, for example Caddy:

```caddy
vpn.example.com {
  handle /sub/* {
    reverse_proxy http://127.0.0.1:8099
  }
}
```

Each user gets `https://vpn.example.com/sub/<token>`. Clients receive Mihomo
YAML from it; a browser gets a two-step setup page (install a client, then
import the profile with its deep link). Add `?raw=1` to see the YAML in a
browser. An optional **Contact email** from Settings is shown on that page.

Clients that send `x-hwid` (Clash Mi and Karing with `xhwid=true`, FlClashX,
Koala Clash, SlothClash, ClashFest) get a private credential per device behind
the same URL, limited by the user's device limit. **Legacy credential enabled**
keeps the shared username/password working for clients without HWID; turn it
off once all devices have registered. Clients, their HWID support, deep links
and release assets are listed in `mita/clients.json`, which the backend, the
setup page and `mita/install-linux.sh` (Linux terminal install on the setup
page) all read.

## Route traffic through outbounds

The bundled sing-box decides where traffic goes. Mita
sends every connection to sing-box's loopback router port; sing-box applies
your routing policies and sends each connection to one of your outbounds,
directly, or blocks it. Routing stays off (Mita connects directly)
until you add an outbound or a policy.

### Outbounds tab

Add your provider servers. Paste a share link (`hysteria2://` / `hy2://`,
`vless://` including REALITY, `vmess://`, `trojan://`, `ss://`, `tuic://`,
`socks5://`, `http://`) or one sing-box outbound JSON object, and give it a
name (the link's own name is used if you leave it empty). sing-box checks the
outbound before it is saved. Outbounds cannot be renamed, and one that a
policy, **Everything else** or an account still uses cannot be deleted.

The **Status** column shows whether each outbound actually works: Mita fetches
`https://www.gstatic.com/generate_204` through the outbound and shows the
latency or the error. It checks on save, every five minutes, and when you press
**Test**; a failed check does not block saving. Set `MITA_OUTBOUND_PROBE_URL`
in `.env` to probe a different http(s) URL. If sing-box exits, it is restarted
with increasing delays up to one minute; while it is down, routed traffic fails.

### Routing tab

Each **policy** sends its domains, CIDRs and GeoSite / GeoIP categories to one
saved outbound, directly, or blocks them. Policies are checked top to bottom
(reorder them with the arrows); the first match wins. **Everything else**
decides what happens to traffic no policy matched (direct by default). Saving
a policy restarts only sing-box, so it applies within a second without touching
Mita; active connections through sing-box are reset.

- Domains match the domain and its subdomains. A policy holds up to 4,096
  domains and 4,096 networks of its own.
- Categories are written `geosite:<name>` (domains) or `geoip:<name>` (IP
  networks), up to eight per policy, for example `geosite:youtube`,
  `geosite:telegram` + `geoip:telegram`, `geosite:category-ads-all`. Names come
  from [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat/tree/sing/geo)
  (`sing` branch); a GitHub link to any sing-box source `.json` works too. A
  wrong name fails on save with "HTTP 404 (no such category?)".
- Categories are stored as local sing-box rule sets without losing anything:
  exact domains, suffixes, keywords and regular expressions all apply, and
  there is no entry limit. They are downloaded on save and refreshed daily;
  sing-box picks up refreshed files without a restart.
- **Geo sources** can point `geosite:` and/or `geoip:` at a v2ray `.dat` file
  on GitHub (release downloads work) or jsDelivr instead, for example Runet
  Freedom's
  `https://github.com/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geosite.dat`
  for `geosite:ru-blocked`; `geosite:<name>@<attribute>` keeps only entries with
  that attribute. Files are cached in `/data/geo-cache` (not in backups).
- **Import GeoSite / GeoIP file** copies the suffixes and CIDRs of a local
  sing-box source file into the policy's own fields once.
- Clients that connect by IP are sniffed (TLS SNI / HTTP Host) so domain rules
  still apply. **Generated sing-box route** shows the rules sing-box runs.

### Restart Mita and per-account outbounds

Policy changes never need a Mita restart. Mita itself only needs one when
outbounds are added or removed, so that they can be chosen for an account, or
when routing is turned on for the first time. The **Restart Mita** button at
the top of both tabs then turns prominent; it runs `mita stop` and `mita start`
inside the container, so connections drop for a few seconds while the admin
panel and sing-box keep running.

In a user's settings, **Outbound for PROXY** makes every policy that sends
traffic to an outbound use the chosen outbound instead for that account and its
HWID devices; direct, blocked and **Everything else = direct** traffic is
unchanged. Client subscription URLs and configs stay the same.

Outbound configs, policies and their credentials are included in PostgreSQL
backups; treat them as confidential. Settings from earlier versions are moved
automatically on start: the single built-in sing-box becomes the outbound
`managed-sing-box`, SOCKS5 proxies from the old JSON editor become `socks`
outbounds, and a catch-all `*` policy becomes **Everything else**.

An external sing-box (the `sing-box` Compose profile with
`sing-box/config.example.json`) still works: keep its SOCKS5 inbound on
`127.0.0.1:1080` with a password and add it in the Outbounds tab as
`socks5://user:password@127.0.0.1:1080`.

## PostgreSQL backups and restore

With a PostgreSQL connection set, Mita stores a compressed snapshot in the
table `mita_backups` on every start and every 24 hours (retrying every five
minutes on errors) and keeps the newest 30. A snapshot contains `users.json`
(credentials, devices, subscription tokens), `settings.json` (including
outbounds and routing), `mita-server.json` and the traffic metrics, but no logs.
Treat the database as confidential.

To restore into a new installation, set `MITA_POSTGRES_DSN` in `.env` and run,
before the first start:

```sh
docker compose build
docker compose run --rm --entrypoint python3 mita /app/webapp.py --restore-postgres latest
docker compose up -d
```

Replace `latest` with an `id` from `mita_backups` to pick a snapshot. The
restore refuses a non-empty data volume; set `MITA_DATA_VOLUME` in `.env` to a
new volume name to keep an old one. An interrupted restore blocks startup until
you rerun it. Point the original hostname and routes at the new server to keep
subscription URLs working, and run only one instance against the same tables.

## Development

```sh
python3 -m unittest discover -s tests -v
node --test tests/*.mjs
```

The tests use temporary files and mock Mita and sing-box. CI also builds the
image, which compiles the patched Mita core and runs its test.

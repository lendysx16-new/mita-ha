# Mita + Hysteria 2 server

A Docker Compose deployment of **mita**, the server of the
[mieru](https://github.com/enfein/mieru) proxy protocol, with a local admin
panel, per-user subscription links and awgm-box based routing through your
provider servers.

- Mita 3.40.0 (built from source with a per-account outbound patch) for Linux amd64 / arm64
- admin panel: users, HWID devices, traffic, settings, outbounds and routing
- add, change and delete users without restarting Mita
- per-user subscription page and Mihomo YAML for Clash Mi, Karing, FlClashX and other clients
- optional Hysteria 2.14 server alongside Mieru (same HWIDs, subscription, outbounds and traffic view)
- outbounds from share links (Hysteria2, VLESS REALITY, Trojan, …) with health checks
- routing policies with GeoSite / GeoIP categories, applied by awgm-box
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
# MITA_HY2_HOST=vpn.example.com  # optional Hysteria 2 defaults, editable in the panel
# MITA_HY2_PORT=443
```

The public settings are only defaults for a fresh install; later change them
in the admin panel. `.env` contains secrets, keep it private.

```sh
docker compose up -d --build
```

The deployment has separate `backend` and `mita` containers. The backend runs
the Python admin/subscription servers and the outbound-only awgm-box router;
the `mita` container runs only the Mieru server. Both share the existing named
`mita-data` volume. Mita listens on TCP 2022, subscriptions on TCP 8099 and the
admin panel on `127.0.0.1:8098`. The backend controls Mita through an internal
Unix socket; its **Restart Mita** action restarts only the Mita process.
Existing volumes, accounts and subscription URLs are preserved when upgrading
from the old single-container deployment.

All services use host networking because awgm-box's authenticated SOCKS5
listeners and Mita's Unix socket are local to the VPS. Keep TCP 8097 (Hysteria
authorization), TCP 8098 (admin), TCP 9090 (Hysteria stats) and the awgm-box
SOCKS5 ports bound to loopback. Restrict public access to TCP 8099 with the
firewall, and publish only `/sub/*` via your HTTPS reverse proxy.

### Optional Hysteria 2 inbound

Hysteria 2 is a **second server**, not an awgm-box outbound. It is disabled by
default so existing Mieru subscribers are unaffected. To enable it:

1. Put a valid TLS certificate and its private key in `hysteria/certs/` as
   `fullchain.pem` and `privkey.pem`. Set `HYSTERIA_CERT_DIR` in `.env` if you
   already manage the certificate in another directory. The server's hostname
   (or the optional SNI name) must match the certificate.
2. In **Settings → Public connection**, enable **Add Hysteria 2 to subscriptions**.
   Set the hostname, UDP port (443 by default) and optional SNI, then save.
3. Start the additional container:

   ```sh
   docker compose --profile hysteria2 up -d
   ```

4. Allow the chosen UDP port in the VPS firewall. Users only need to **update
   their existing subscription** in Mihomo; it gains a selectable Hysteria 2
   node alongside Mieru, with device-specific credentials when HWID is used.

For Mieru-only operation use `docker compose up -d --build` as before. Hysteria
2 reads an automatically generated config from the existing data volume. Its
local auth service is at `127.0.0.1:8097` and the statistics API is at
`127.0.0.1:9090`, both protected by random secrets stored in settings. Do not
forward these ports. Hysteria restarts when the generated configuration changes
(for example when an account's outbound changes); active QUIC connections then
reconnect. New and revoked credentials are checked dynamically through the
backend and revoked credentials are also kicked from active sessions.

When routing is enabled Hysteria uses the **same awgm-box policies** as Mieru,
including individual account outbound overrides; the rule selection is based
on Hysteria 2.14's authenticated client ID. Localhost destinations are always
blocked. Private-network access follows the account's LAN setting. Hysteria
traffic is polled every minute and added to Mieru's 24h/7d/30d totals in the
Traffic tab (hourly granularity). The Hysteria traffic history is included in
PostgreSQL backups. If the backend stops, *new* Hysteria sessions are denied
until it returns. The backend still owns awgm-box, so restarting it also
temporarily interrupts traffic that uses routed outbounds. Direct Hysteria
sessions do not depend on the router once authenticated.

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

The backend image bundles [awgm-box release 42](https://github.com/hoaxisr/awgm-box/releases/tag/42)
for amd64 and arm64. It uses the existing outbound settings and backups.
The bundled awgm-box decides where traffic goes. Mita
sends every connection to awgm-box's loopback router port; awgm-box applies
your routing policies and sends each connection to one of your outbounds,
directly, or blocks it. Routing stays off (Mita connects directly)
until you add an outbound or a policy.

### Outbounds tab

Add your provider servers. Paste a share link (`hysteria2://` / `hy2://`,
`vless://` including REALITY, `vmess://`, `trojan://`, `ss://`, `tuic://`,
`socks5://`, `http://`) or one sing-box outbound JSON object, and give it a
name (the link's own name is used if you leave it empty). awgm-box checks the
outbound before it is saved. Outbounds cannot be renamed, and one that a
policy, **Everything else** or an account still uses cannot be deleted.

VLESS XHTTP links and Clash `xhttp-opts` are supported. The importer maps the
path, host, mode and common XHTTP options to awgm-box. For advanced layouts
such as a separate download transport, use native awgm-box JSON.
Native JSON also supports Mieru, TrustTunnel, AmneziaWG (`type: awg`), and
MASQUE (`type: masque-client`). AmneziaWG and MASQUE use the userspace network
stack. Mieru can also be imported from Clash subscriptions.

For REALITY, awgm-box 42 leaves X25519MLKEM768 enabled by default with
compatible uTLS fingerprints. The importer preserves an explicit
`support-x25519mlkem768` flag from Clash `reality-opts` or a share-link query.
In native outbound JSON the field is `tls.reality.support_x25519mlkem768`.
This controls the TLS key share.

To choose a server from a provider subscription, open **Add outbound → Import
from subscription URL**. If your provider requires custom request headers,
paste them as one `Name: value` pair per line before clicking **Load servers**.
Mita drops custom headers if redirected to another host or port. Filter by a named group
when the subscription includes groups, choose a server, then save it as an
outbound. The importer supports public HTTPS subscriptions with plain or
base64-encoded share links, sing-box JSON outbounds, compatible Clash proxy
entries, and Happ/INCY subscriptions that return Xray JSON profiles. For Xray
profiles Mita imports individual proxy servers and uses its own DNS and
routing. Groups are filters; the saved outbound points to the server you chose.
Use **Test selected server** to check a candidate before saving. To switch an
existing outbound, edit it, load servers from its saved subscription, and save
the new selection. Routing policies and account assignments keep the same
outbound name.

Leave **Refresh the selected server from its subscription daily** enabled to
keep tracking that server by the provider's name and protocol. If it is
missing, ambiguous, or the new configuration cannot be applied, Mita keeps
the previous config and retries in about an hour. A successful change
restarts awgm-box so active connections through it may reconnect.
The subscription URL and request headers are kept in settings and backups
even when daily refresh is disabled, so you can still switch servers manually.

The **Status** column shows whether each outbound actually works: Mita fetches
`https://www.gstatic.com/generate_204` through the outbound and shows the
latency or the error. It checks on save, every five minutes, and when you press
**Test**; a failed check does not block saving. Set `MITA_OUTBOUND_PROBE_URL`
in `.env` to probe a different http(s) URL. If awgm-box exits, it is restarted
with increasing delays up to one minute; while it is down, routed traffic fails.

### Routing tab

Each **policy** sends its domains, CIDRs and GeoSite / GeoIP categories to one
saved outbound, directly, or blocks them. Policies are checked top to bottom
(reorder them with the arrows); the first match wins. **Everything else**
decides what happens to traffic no policy matched (direct by default). Saving
a policy restarts only awgm-box, so it applies within a second without touching
Mita; active connections through awgm-box are reset.

- Domains match the domain and its subdomains. A policy holds up to 4,096
  domains and 4,096 networks of its own.
- Categories are written `geosite:<name>` (domains) or `geoip:<name>` (IP
  networks), up to eight per policy, for example `geosite:youtube`,
  `geosite:telegram` + `geoip:telegram`, `geosite:category-ads-all`. Names come
  from [MetaCubeX/meta-rules-dat](https://github.com/MetaCubeX/meta-rules-dat/tree/sing/geo)
  (`sing` branch); a GitHub link to any sing-box source `.json` works too. A
  wrong name fails on save with "HTTP 404 (no such category?)".
- Categories are stored as local awgm-box rule sets without losing anything:
  exact domains, suffixes, keywords and regular expressions all apply, and
  there is no entry limit. They are downloaded on save and refreshed daily;
  awgm-box picks up refreshed files without a restart.
- **Geo sources** can point `geosite:` and/or `geoip:` at a v2ray `.dat` file
  on GitHub (release downloads work) or jsDelivr instead, for example Runet
  Freedom's
  `https://github.com/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geosite.dat`
  for `geosite:ru-blocked`; `geosite:<name>@<attribute>` keeps only entries with
  that attribute. Files are cached in `/data/geo-cache` (not in backups).
- **Import GeoSite / GeoIP file** copies the suffixes and CIDRs of a local
  sing-box source file into the policy's own fields once.
- Clients that connect by IP are sniffed (TLS SNI / HTTP Host) so domain rules
  still apply. **Generated awgm-box route** shows the rules awgm-box runs.

### Restart Mita and per-account outbounds

Policy changes never need a Mita restart. Mita itself only needs one when
outbounds are added or removed, so that they can be chosen for an account, or
when routing is turned on for the first time. The **Restart Mita** button at
the top of both tabs then turns prominent; it requests a Mita process restart
over a Unix socket in the `mita` container. Connections drop for a few seconds
while the admin panel and awgm-box keep running.

In a user's settings, **Outbound for PROXY** makes every policy that sends
traffic to an outbound use the chosen outbound instead for that account and its
HWID devices; direct, blocked and **Everything else = direct** traffic is
unchanged. Client subscription URLs and configs stay the same.

Outbound configs, policies and their credentials are included in PostgreSQL
backups; treat them as confidential. Settings from earlier versions are moved
automatically on start: the single built-in sing-box becomes the outbound
`managed-sing-box`, SOCKS5 proxies from the old JSON editor become `socks`
outbounds, and a catch-all `*` policy becomes **Everything else**.

If you previously enabled the external `sing-box` Compose profile, stop that
service before enabling the new `awgm-box` profile. Move its local
`sing-box/config.json` to `awgm-box/config.json`.

An external awgm-box (the `awgm-box` Compose profile with
`awgm-box/config.example.json`) still works: keep its SOCKS5 inbound on
`127.0.0.1:1080` with a password and add it in the Outbounds tab as
`socks5://user:password@127.0.0.1:1080`.

## PostgreSQL backups and restore

With a PostgreSQL connection set, Mita stores a compressed snapshot in the
table `mita_backups` on every start and every 24 hours (retrying every five
minutes on errors) and keeps the newest 30. A snapshot contains `users.json`
(credentials, devices, subscription tokens), `settings.json` (including
outbounds and routing), `mita-server.json` and the traffic metrics, but no logs.
Treat the database as confidential.

**Connect via** under Settings sends backups and log forwarding through one of
your outbounds instead of directly, for example when the direct path to the
database drops large packets (small queries work, backups hang). awgm-box then
forwards `127.0.0.1:1179` to the database host and port from the connection
string through that outbound; TLS to the database still verifies its real host
name. This needs a single TCP host in the connection string, and an outbound in
use there cannot be deleted. A stalled connection gives up after about a minute,
and the backup status shows the database error.

To restore into a new installation, set `MITA_POSTGRES_DSN` in `.env` and run,
before the first start:

```sh
docker compose build
docker compose run --rm --entrypoint python3 backend /app/webapp.py --restore-postgres latest
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

The tests use temporary files and mock Mita and awgm-box. CI also builds the
image, which compiles the patched Mita core and runs its test.

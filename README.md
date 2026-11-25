# Mita + Hysteria 2 server

Docker Compose deployment of a [mieru](https://github.com/enfein/mieru) proxy server (Mita) with an
admin panel, per-user subscription links, optional Hysteria 2, and routing through provider servers
(awgm-box, a sing-box fork). Linux amd64/arm64.

Services in `compose.yaml` (all use host networking):

| Service | What it runs | Started by |
|---|---|---|
| `backend` | admin panel, subscription server, awgm-box router | `docker compose up -d` |
| `mita` | the Mieru server | `docker compose up -d` |
| `hysteria2` | Hysteria 2 server | `docker compose --profile hysteria2 up -d` (optional) |

## Deploy

Run every step on the server as root, in order. Replace `vpn.example.com` with the real domain.

1. **Install Docker** Engine with the Compose plugin (`docker compose version` must work).

2. **Get the code**
   ```sh
   git clone https://github.com/lendysx16-new/mita-ha.git /opt/mita-ha
   cd /opt/mita-ha
   ```

3. **Create `.env`** in `/opt/mita-ha` (keep it private, it holds secrets):
   ```sh
   cat > .env <<EOF
   MITA_ADMIN_PASSWORD=$(openssl rand -hex 24)
   MITA_PUBLIC_HOST=vpn.example.com
   MITA_PUBLIC_PORT=2022
   MITA_SUBSCRIPTION_BASE_URL=https://vpn.example.com
   EOF
   ```
   `MITA_PUBLIC_*` are only defaults for the first start; later they are edited in the panel.
   Optional variables are listed in [Environment variables](#environment-variables).

4. **Start**
   ```sh
   docker compose up -d --build
   ```
   The first build compiles Mita and takes a few minutes.

5. **Check**
   ```sh
   docker compose ps                 # backend is "healthy", mita is "running"
   docker compose logs --tail 30 backend mita
   ```
   `backend` exits immediately if `MITA_ADMIN_PASSWORD` is missing or shorter than 24 characters.

6. **Open the firewall**: allow TCP `2022` (Mieru) and TCP `80`/`443` (reverse proxy). Do **not**
   expose TCP `8099`, `8098`, `8097`, `9090` or the loopback SOCKS ports (see [Ports](#ports)).

7. **Publish subscriptions** through an HTTPS reverse proxy. Only `/sub/*` may be public. Caddy:
   ```caddy
   vpn.example.com {
     handle /sub/* {
       reverse_proxy http://127.0.0.1:8099
     }
   }
   ```

8. **Open the admin panel** from your computer through an SSH tunnel, then browse to
   http://127.0.0.1:8098 and sign in as `admin` with `MITA_ADMIN_PASSWORD`:
   ```sh
   ssh -L 8098:127.0.0.1:8098 root@vpn.example.com
   ```

9. **Create users** in **Users → + Add user**. The first start creates a user `change-me` with a
   random password; delete it. Each user's subscription link (`https://vpn.example.com/sub/<token>`)
   is on the user's page; clients import it, a browser shows a setup page.

## Optional: Hysteria 2

1. **TLS certificate** for the Hysteria hostname. Either put `fullchain.pem` and `privkey.pem` into
   `/opt/mita-ha/hysteria/certs/`, or reuse Certbot/Let's Encrypt certificates by adding to `.env`:
   ```dotenv
   HYSTERIA_CERT_DIR=/etc/letsencrypt
   MITA_HY2_TLS_CERT=/certs/live/vpn.example.com/fullchain.pem
   MITA_HY2_TLS_KEY=/certs/live/vpn.example.com/privkey.pem
   ```
   `HYSTERIA_CERT_DIR` is mounted at `/certs` inside the container; the two paths are container
   paths. Renewed certificates are picked up without a restart.
2. **Pick the UDP port.** If Caddy runs on the same IP, it uses UDP 443 for HTTP/3: either use another
   port (e.g. `8443`) or disable HTTP/3 in the Caddyfile global options:
   ```caddy
   {
     servers :443 {
       protocols h1 h2
     }
   }
   ```
3. **Apply** `.env` changes and start Hysteria:
   ```sh
   docker compose up -d
   docker compose --profile hysteria2 up -d
   ```
4. **Enable it in the panel**: **Settings → Public connection → Add Hysteria 2 to subscriptions**, set
   hostname (must match the certificate), UDP port, optional SNI, **Save connection**.
5. **Check**: the panel shows "Hysteria 2 is running" under the checkbox. If it shows an error, read
   `docker compose logs --tail 50 hysteria2`.
6. **Open the UDP port** in the firewall. Users refresh their subscription and get a second node.

## Optional: route traffic through provider servers

All in the admin panel; changes apply without restarting the server unless the panel asks for it.

1. **Outbounds → + Add outbound**: paste a share link (`vless://` incl. REALITY/XHTTP, `hysteria2://`,
   `trojan://`, `ss://`, `tuic://`, `vmess://`, `socks5://`), awgm-box JSON, or **Import from
   subscription URL**. The **Status** column must show `OK · N ms`.
2. **Routing → + Add policy**: domains, CIDRs and categories (`geosite:youtube`, `geoip:ru`, …) →
   an outbound, Direct or Block. Set **Everything else** at the bottom. First match wins.
3. If a yellow **Restart Mita** bar appears, press it (needed after adding/removing outbounds).
4. Optional per user: **Users → user → Outbound for PROXY**.

## Optional: PostgreSQL backups

Set the connection in **Settings → Server settings** (or `MITA_POSTGRES_DSN` in `.env` before the
first start). Snapshots are written on start and every 24 h (newest 30 kept) to table
`mita_backups`. If the direct path to the database is unreliable, choose an outbound in
**Connect via**.

Restore into a new, empty installation (before its first start):
```sh
echo 'MITA_POSTGRES_DSN=postgresql://user:password@host:5432/db' >> .env
docker compose build
docker compose run --rm --entrypoint python3 backend /app/webapp.py --restore-postgres latest
docker compose up -d
```
`latest` can be replaced with an `id` from `mita_backups`. Restore refuses a non-empty volume; set
`MITA_DATA_VOLUME=<new-name>` in `.env` to restore next to old data.

## Update

```sh
cd /opt/mita-ha
git pull
docker compose up -d --build
docker compose --profile hysteria2 up -d   # only if Hysteria is used
```
Data, users and subscription links are kept in the `mita-data` volume.

## Reference

### Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `MITA_ADMIN_PASSWORD` | yes | admin panel password, ≥ 24 characters |
| `MITA_PUBLIC_HOST` | first start | public host of the Mieru server |
| `MITA_PUBLIC_PORT` | first start | public TCP port of Mieru (default `2022`) |
| `MITA_SUBSCRIPTION_BASE_URL` | first start | public HTTPS base of subscription links |
| `MITA_POSTGRES_DSN` | no | PostgreSQL connection for backups and logs |
| `MITA_HY2_HOST`, `MITA_HY2_PORT` | no | Hysteria defaults (editable in the panel) |
| `HYSTERIA_CERT_DIR` | no | host directory mounted at `/certs` for Hysteria (default `./hysteria/certs`) |
| `MITA_HY2_TLS_CERT`, `MITA_HY2_TLS_KEY` | no | certificate/key paths inside `/certs` |
| `MITA_OUTBOUND_PROBE_URL` | no | URL used to test outbounds (default `https://www.gstatic.com/generate_204`) |
| `MITA_DATA_VOLUME` | no | name of the data volume (default `mita-linux-data`) |

### Ports

| Port | Who listens | Expose publicly? |
|---|---|---|
| TCP 2022 | Mieru | yes |
| UDP 443 (or chosen) | Hysteria 2 | yes, if Hysteria is used |
| TCP 8099 | subscription server | no, only `/sub/*` via the reverse proxy |
| TCP 8098 | admin panel (127.0.0.1) | no, use an SSH tunnel |
| TCP 8097, 9090 | Hysteria auth / stats (127.0.0.1) | no |
| TCP 1081+, 1179, 1180, 2081+ | awgm-box SOCKS and forwards (127.0.0.1) | no |

### Troubleshooting

| Symptom | Check / fix |
|---|---|
| `backend` keeps restarting | `docker compose logs backend`; usually `MITA_ADMIN_PASSWORD` missing or short |
| `mita` not starting | it waits for `backend` to be healthy; `docker compose logs mita` |
| subscription link 404 | the reverse proxy must forward `/sub/*` to `127.0.0.1:8099` |
| Hysteria error in the panel | `docker compose --profile hysteria2 up -d`; `docker compose logs hysteria2` (certificate path, UDP port busy) |
| outbound Status shows an error | **Test** in Outbounds; the error text comes from awgm-box |
| backup error in Settings | the status shows the database error; try **Connect via** an outbound |

## Development

```sh
python3 -m unittest discover -s tests -v
node --test tests/*.mjs
```
CI also builds the images and checks generated awgm-box and Hysteria configs with the real binaries.

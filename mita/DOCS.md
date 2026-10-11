# Mita (mieru server)

This app runs `mita`, provides a Home Assistant Ingress admin UI, and serves per-user ClashMi subscriptions.

## Admin UI

Open the app and click **Open Web UI**.

The **Users** tab is a compact directory. Select an account to open its dedicated profile with its subscription URL, copy/rotate actions, delete control, HWID devices and limits. The add-user form is collapsed until **+ Add user** is selected, and public connection settings have moved to **Settings**. **Devices** now shows a searchable-sized compact table of registered devices with direct links to the owning account. This keeps secrets and destructive actions off the main list.

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

1. **Install the app:** choose an available client for the detected platform (Clash Mi and Karing on iOS, plus FlClashX, Koala Clash and other Mihomo clients on supported platforms). The page displays direct App Store or GitHub Release installers.
2. **Open the profile in the app:** use the client's import deep link, preserving `xhwid=true` for Clash Mi and Karing. If a client does not have an import URL scheme, copy the subscription URL and import it manually.

The iPhone layout prioritizes the selector and both buttons within a typical Safari viewport. Steps are separated by thin rectangular borders and pink accents. Section titles use the Cyrillic-capable pixel-style Press Start 2P font with a local monospace fallback; body copy remains in Manrope. Both light and dark themes follow the device setting. The disabled, hidden `Платформа` option is a placeholder rather than a selectable device. Manual copy, HWID information, and the raw subscription URL are hidden under **Other connection methods**; alternate release variants stay in a separate collapsed disclosure. The page detects the visitor's OS from User-Agent, but allows manual platform selection. The landing page now uses bundled HTML/CSS/JavaScript with **no Vue or CDN dependency**, so it works independently of third-party script hosting.

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

### HWID client compatibility

- **Clash Mi**, **Karing**: support `x-hwid` when the `xhwid=true` flag is passed to the `install-config` deep link (disabled by default in the client).
- **FlClashX**: supports automatic `x-hwid` during subscription fetches; install links use `flclashx://install-config` and GitHub Release assets for Android and desktop. Note: there is no iOS release.
- **Koala Clash**: sends `x-hwid` when fetching subscriptions; deep link uses `koala-clash://install-config`. Desktop only.
The public app picker lists **only these four HWID-capable clients**. Previously supported FlClash and Clash Verge Rev download routes remain available for existing links, but they are not advertised as HWID-compatible.

HWID support refers to the subscription request headers. The generated YAML remains standard Mihomo/Mieru; clients must also use a Mihomo core capable of the Mieru protocol.

## Linux terminal installation

When **Linux** is selected on the subscription page, step 1 includes an optional **Install via terminal** disclosure. It shows a copyable command for the currently selected client:

```bash
curl -fsSLo ./mita-install-linux.sh https://raw.githubusercontent.com/lendysx16-new/mita-ha/main/mita/install-linux.sh && bash ./mita-install-linux.sh clashmi
```

The script is versioned in this repository (`mita/install-linux.sh`). Inspect the downloaded file before running `bash` if desired. It uses Python 3 and curl to look up the latest GitHub Release from the client's **official** repository and download an exact matching installer; it does not run remotely downloaded scripts from that release. On Debian/Ubuntu it invokes `sudo apt-get install` on the DEB, and on Fedora/openSUSE it uses DNF/Zypper to install an RPM. If neither manager exists, it installs an AppImage into `~/.local/bin` when the client publishes one. Root access is only needed for package installation, not the AppImage.

To override package detection, run `bash ./mita-install-linux.sh koala rpm`, `... deb`, or `... appimage`. Some clients publish only AMD64 packages or do not publish AppImages; unsupported architecture/format combinations fail with an explicit message. Test compatibility without downloading or installing using `bash ./mita-install-linux.sh koala deb --dry-run`.

**Privacy:** the Linux terminal command references only the public installation script and client ID; it never includes the user's subscription URL or token. After installing the application, use step 2 on the page to add the private subscription in the app.

## Public support email

Under **Public connection** in the admin UI, set **Contact email (optional)**. It is validated and stored in `/data/settings.json`. When non-empty, the public subscription setup page displays a tappable `mailto:` address beneath the connection steps. Clearing the field removes it. Updating this metadata does not restart Mita.

## Automatic PostgreSQL backups

If the **PostgreSQL connection** field in the admin UI is configured, a background worker stores compressed snapshots in the PostgreSQL table `mita_backups`:

- **After every startup of the admin backend**, then **every 24 hours** after a successful snapshot. This includes app restarts and reboots; changing the DSN also triggers a new snapshot.
- On errors or temporary PostgreSQL outages, the worker retries every **5 minutes**, without stopping the VPN.
- Includes `users.json` (user credentials, HWIDs and subscription tokens), `settings.json`, `mita-server.json`, `options.json` if present, and the files under `mita-state/` (including rolling traffic metrics).
- Excludes `mita.log`, log-forwarder offsets, Unix sockets, other temporary data and the database itself. Logging to the `mita_logs` table is configured separately.
- Uses a gzip-compressed tar archive inside PostgreSQL `BYTEA`, a SHA-256 digest, the archive's file list, the backup reason and creation timestamp. The newest **30 snapshots** are retained; older records are deleted in the same transaction.
- Rejects symbolic links and snapshots larger than **64 MiB uncompressed**. Backup errors appear in the **Config** section; the worker retries automatically.

**Security:** these backups contain plaintext credentials, subscription tokens, and potentially the PostgreSQL connection string. Protect database accounts, backups and transfers. Use a dedicated PostgreSQL account with restricted access; backups are not a substitute for an independent encrypted off-site backup.

To inspect available backups using `psql`:

```sql
SELECT id, created_at, reason, file_names, checksum_sha256, octet_length(payload) AS bytes
FROM mita_backups ORDER BY created_at DESC, id DESC;
```

To **export** a particular snapshot (for example id 12) from a trusted host with `psql` and `base64` available:

```bash
psql "$DATABASE_URL" -Atc "SELECT encode(payload, 'base64') FROM mita_backups WHERE id = 12" | base64 -d > mita-backup.tar.gz
sha256sum mita-backup.tar.gz
tar -tzf mita-backup.tar.gz
```

Compare the SHA-256 value with the corresponding database record before restoring. Extract only into a trusted, **stopped** add-on's data directory after making a backup of its current contents. Never run a restore over a running add-on, and never expose the backup archive to the public subscription site.

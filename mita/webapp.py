#!/usr/bin/env python3
import argparse
import copy
import html
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    import psycopg2
except ImportError:
    psycopg2 = None
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
USERS_PATH = DATA_DIR / "users.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
MITA_CONFIG_PATH = DATA_DIR / "mita-server.json"
SUBSCRIPTION_PAGE_PATH = Path("/app/subscription.html")
MITA_LOG_PATH = DATA_DIR / "mita.log"
LOG_FORWARD_STATE_PATH = DATA_DIR / "log-forwarder-state.json"

ADMIN_PORT = 8098
SUB_PORT = 8099
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HWID_RE = re.compile(r"^[A-Za-z0-9=-]{10,64}$")
DEVICE_ID_RE = re.compile(r"^[A-Fa-f0-9]{16}$")
LOCK = threading.RLock()
RELEASE_CACHE_LOCK = threading.RLock()
RELEASE_CACHE = {}
RELEASE_CACHE_TTL = 900
LOG_DB_STATUS_LOCK = threading.RLock()
LOG_DB_STATUS = {
    "enabled": False,
    "connected": False,
    "last_error": "",
    "last_write": None,
    "rows_written": 0,
}


GITHUB_DOWNLOADS = {
    "clashmi": {
        "repo": "KaringX/clashmi",
        "targets": {
            "macos": r"^clashmi_.*_macos_universal\.dmg$",
            "windows": r"^clashmi_.*_windows_x64\.exe$",
            "android-arm64": r"^clashmi_.*_android_arm64-v8a\.apk$",
            "android-armv7": r"^clashmi_.*_android_armeabi-v7a\.apk$",
            "linux-amd64-appimage": r"^clashmi_.*_linux_amd64\.AppImage$",
            "linux-amd64-deb": r"^clashmi_.*_linux_amd64\.deb$",
            "linux-amd64-rpm": r"^clashmi_.*_linux_amd64\.rpm$",
        },
    },
    "karing": {
        "repo": "KaringX/karing",
        "targets": {
            "macos": r"^karing_.*_macos_universal\.dmg$",
            "windows": r"^karing_.*_windows_x64\.exe$",
            "android-arm64": r"^karing_.*_android_arm64-v8a\.apk$",
            "android-armv7": r"^karing_.*_android_armeabi-v7a\.apk$",
            "linux-amd64-appimage": r"^karing_.*_linux_amd64\.AppImage$",
            "linux-amd64-deb": r"^karing_.*_linux_amd64\.deb$",
        },
    },
    "flclash": {
        "repo": "chen08209/FlClash",
        "targets": {
            "macos-arm64": r"^FlClash-.*-macos-arm64\.dmg$",
            "macos-amd64": r"^FlClash-.*-macos-amd64\.dmg$",
            "windows-amd64": r"^FlClash-.*-windows-amd64-setup\.exe$",
            "windows-arm64": r"^FlClash-.*-windows-arm64-setup\.exe$",
            "android-arm64": r"^FlClash-.*-android-arm64-v8a\.apk$",
            "android-armv7": r"^FlClash-.*-android-armeabi-v7a\.apk$",
            "linux-amd64-appimage": r"^FlClash-.*-linux-amd64\.AppImage$",
            "linux-amd64-deb": r"^FlClash-.*-linux-amd64\.deb$",
            "linux-amd64-rpm": r"^FlClash-.*-linux-amd64\.rpm$",
            "linux-arm64-appimage": r"^FlClash-.*-linux-arm64\.AppImage$",
            "linux-arm64-deb": r"^FlClash-.*-linux-arm64\.deb$",
            "linux-arm64-rpm": r"^FlClash-.*-linux-arm64\.rpm$",
        },
    },
    "verge": {
        "repo": "clash-verge-rev/clash-verge-rev",
        "targets": {
            "macos-arm64": r"^Clash\.Verge_.*_aarch64\.dmg$",
            "macos-amd64": r"^Clash\.Verge_.*_x64\.dmg$",
            "windows-amd64": r"^Clash\.Verge_.*_x64-setup\.exe$",
            "windows-arm64": r"^Clash\.Verge_.*_arm64-setup\.exe$",
            "linux-amd64-deb": r"^Clash Verge_.*_amd64\.deb$",
            "linux-amd64-rpm": r"^Clash Verge-.*-1\.x86_64\.rpm$",
            "linux-arm64-deb": r"^Clash Verge_.*_arm64\.deb$",
            "linux-arm64-rpm": r"^Clash\.Verge-.*-1\.aarch64\.rpm$",
        },
    },
}


def resolve_github_download(client_id, target_id):
    client = GITHUB_DOWNLOADS.get(client_id)
    if not client:
        raise ValueError("unknown client")
    pattern = client["targets"].get(target_id)
    if not pattern:
        raise ValueError("unknown download target")

    repo = client["repo"]
    now = time.monotonic()
    with RELEASE_CACHE_LOCK:
        cached = RELEASE_CACHE.get(repo)
        if cached and now - cached["time"] < RELEASE_CACHE_TTL:
            assets = cached["assets"]
        else:
            request = urllib.request.Request(
                f"https://api.github.com/repos/{repo}/releases/latest",
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "mita-ha",
                },
            )
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.load(response)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"GitHub release lookup failed: {exc}") from exc

            assets = {
                str(asset.get("name", "")): str(asset.get("browser_download_url", ""))
                for asset in payload.get("assets", [])
                if asset.get("name") and asset.get("browser_download_url")
            }
            RELEASE_CACHE[repo] = {"time": now, "assets": assets}

    regex = re.compile(pattern)
    for name, url in assets.items():
        if regex.fullmatch(name):
            return url
    raise RuntimeError("matching release asset not found")


def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path, value):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_options():
    return load_json(OPTIONS_PATH, {})


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def set_log_db_status(**values):
    with LOG_DB_STATUS_LOCK:
        LOG_DB_STATUS.update(values)


def get_log_db_status():
    with LOG_DB_STATUS_LOCK:
        return dict(LOG_DB_STATUS)


def parse_log_level(line):
    match = re.match(r"^(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL)\b", line.strip(), re.IGNORECASE)
    if not match:
        return None
    level = match.group(1).upper()
    return "WARN" if level == "WARNING" else level


def ensure_log_table(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS mita_logs (
                id BIGSERIAL PRIMARY KEY,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                level TEXT,
                message TEXT NOT NULL
            )
            """
        )
    conn.commit()


def read_log_forward_state():
    state = load_json(LOG_FORWARD_STATE_PATH, {})
    try:
        return max(0, int(state.get("offset", 0)))
    except (TypeError, ValueError):
        return 0


def write_log_forward_state(offset):
    write_json(LOG_FORWARD_STATE_PATH, {"offset": int(offset)})


def log_db_worker():
    if psycopg2 is None:
        set_log_db_status(last_error="psycopg2 is not installed")
        return

    offset = read_log_forward_state()
    conn = None
    active_dsn = None

    while True:
        settings = get_settings()
        dsn = str(settings.get("postgres_dsn", "") or "").strip()
        set_log_db_status(enabled=bool(dsn))

        if not dsn:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None
            active_dsn = None
            set_log_db_status(connected=False, last_error="")
            time.sleep(2)
            continue

        if active_dsn != dsn:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            conn = None
            active_dsn = dsn

        try:
            if conn is None or conn.closed:
                conn = psycopg2.connect(dsn, connect_timeout=8)
                conn.autocommit = False
                ensure_log_table(conn)
                set_log_db_status(connected=True, last_error="")

            if not MITA_LOG_PATH.exists():
                time.sleep(1)
                continue

            size = MITA_LOG_PATH.stat().st_size
            if offset > size:
                offset = 0

            batch = []
            next_offset = offset
            with MITA_LOG_PATH.open("rb") as f:
                f.seek(offset)
                while len(batch) < 200:
                    raw = f.readline()
                    if not raw:
                        break
                    next_offset = f.tell()
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line:
                        batch.append((parse_log_level(line), line))

            if not batch:
                time.sleep(1)
                continue

            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO mita_logs (level, message) VALUES (%s, %s)",
                    batch,
                )
            conn.commit()
            offset = next_offset
            write_log_forward_state(offset)

            status = get_log_db_status()
            set_log_db_status(
                connected=True,
                last_error="",
                last_write=utc_now(),
                rows_written=int(status.get("rows_written", 0)) + len(batch),
            )
        except Exception as exc:
            set_log_db_status(connected=False, last_error=str(exc))
            if conn is not None:
                try:
                    conn.rollback()
                    conn.close()
                except Exception:
                    pass
            conn = None
            time.sleep(5)


def migrate_users():
    users = get_users()
    changed = False
    for user in users:
        if "legacy_enabled" not in user:
            user["legacy_enabled"] = True
            changed = True
        if "device_limit" not in user:
            user["device_limit"] = 0
            changed = True
        if "devices" not in user or not isinstance(user.get("devices"), list):
            user["devices"] = []
            changed = True
        for device in user["devices"]:
            if "first_seen" not in device:
                device["first_seen"] = device.get("last_seen") or utc_now()
                changed = True
            if "last_seen" not in device:
                device["last_seen"] = device["first_seen"]
                changed = True
    if changed:
        write_json(USERS_PATH, users)


def normalize_base_url(value):
    value = (value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("subscription_base_url must be an http(s) URL")
    return value


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    options = load_options()

    if not USERS_PATH.exists():
        default_private = bool(options.get("allow_private_ip", False))
        default_loopback = bool(options.get("allow_loopback_ip", False))
        users = []
        for item in options.get("users", []):
            username = str(item.get("username", "")).strip()
            password = str(item.get("password", ""))
            if not username or not password:
                continue
            users.append({
                "username": username,
                "password": password,
                "allow_private_ip": default_private,
                "allow_loopback_ip": default_loopback,
                "token": secrets.token_urlsafe(24),
                "legacy_enabled": True,
                "device_limit": 0,
                "devices": [],
            })
        if not users:
            users = [{
                "username": "change-me",
                "password": "change-this-password",
                "allow_private_ip": default_private,
                "allow_loopback_ip": default_loopback,
                "token": secrets.token_urlsafe(24),
                "legacy_enabled": True,
                "device_limit": 0,
                "devices": [],
            }]
        write_json(USERS_PATH, users)

    migrate_users()

    if not SETTINGS_PATH.exists():
        settings = {
            "public_mieru_host": str(options.get("public_mieru_host", "me.lendysx16.ru")).strip(),
            "public_mieru_port": int(options.get("public_mieru_port", 8443)),
            "subscription_base_url": str(options.get("subscription_base_url", "https://me.lendysx16.ru")).rstrip("/"),
            "log_level": str(options.get("log_level", "INFO")),
            "prefer_ipv4": bool(options.get("prefer_ipv4", True)),
            "default_allow_private_ip": bool(options.get("allow_private_ip", False)),
            "default_allow_loopback_ip": bool(options.get("allow_loopback_ip", False)),
            "postgres_dsn": str(options.get("postgres_dsn", "")).strip(),
        }
        write_json(SETTINGS_PATH, settings)

    rebuild_mita_config()


class MitaReloadError(RuntimeError):
    """Raised when a configuration change could not be applied to mita."""


def persist_and_reload(path, updated, previous):
    """Commit state only if mita accepts the corresponding configuration.

    Caller must hold LOCK through the full read/modify/write/reload sequence.
    """
    write_json(path, updated)
    ok, output = reload_mita()
    if ok:
        return
    write_json(path, previous)
    # Restore the on-disk mita configuration and retry applying the old state.
    reload_mita()
    raise MitaReloadError(output or "mita reload failed")


def get_users():
    return load_json(USERS_PATH, [])


def get_settings():
    return load_json(SETTINGS_PATH, {})


def rebuild_mita_config():
    with LOCK:
        users = get_users()
        settings = get_settings()
        dns_policy = "PREFER_IPv4" if settings.get("prefer_ipv4", True) else "USE_FIRST_IP"
        mita_users = []
        for user in users:
            access = {
                "allowPrivateIP": bool(user.get("allow_private_ip", False)),
                "allowLoopbackIP": bool(user.get("allow_loopback_ip", False)),
            }
            if user.get("legacy_enabled", True):
                mita_users.append({
                    "name": user["username"],
                    "password": user["password"],
                    **access,
                })
            for device in user.get("devices", []):
                mita_users.append({
                    "name": device["mita_username"],
                    "password": device["password"],
                    **access,
                })

        config = {
            "portBindings": [{"port": 2022, "protocol": "TCP"}],
            "users": mita_users,
            "loggingLevel": settings.get("log_level", "INFO"),
            "dns": {"dualStack": dns_policy},
        }
        write_json(MITA_CONFIG_PATH, config)


def reload_mita():
    rebuild_mita_config()
    try:
        proc = subprocess.run(
            ["/usr/bin/mita", "reload"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
            env=os.environ.copy(),
        )
        return proc.returncode == 0, proc.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def parse_mita_users_output(output):
    lines = []
    for raw in output.splitlines():
        line = raw.strip()
        if line.startswith("INFO "):
            line = line[5:].lstrip()
        if line:
            lines.append(line)

    header_index = next(
        (i for i, line in enumerate(lines) if line.startswith("User") and "LastActive" in line),
        None,
    )
    if header_index is None:
        return []

    header = re.split(r"\s{2,}", lines[header_index])
    aliases = {
        "User": "username",
        "LastActive": "last_active",
        "1DayDown": "day_down",
        "1DayDownload": "day_down",
        "1DayUp": "day_up",
        "1DayUpload": "day_up",
        "7DaysDown": "week_down",
        "7DaysDownload": "week_down",
        "7DaysUp": "week_up",
        "7DaysUpload": "week_up",
        "30DaysDown": "month_down",
        "30DaysDownload": "month_down",
        "30DaysUp": "month_up",
        "30DaysUpload": "month_up",
    }

    result = []
    for line in lines[header_index + 1:]:
        fields = re.split(r"\s{2,}", line)
        if len(fields) != len(header):
            continue
        row = {
            "username": "",
            "last_active": "-",
            "day_down": "-",
            "day_up": "-",
            "week_down": "-",
            "week_up": "-",
            "month_down": "-",
            "month_up": "-",
        }
        for key, value in zip(header, fields):
            target = aliases.get(key)
            if target:
                row[target] = value
        if row["username"]:
            result.append(row)
    return result


def get_mita_user_traffic():
    proc = subprocess.run(
        ["/usr/bin/mita", "get", "users"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        env=os.environ.copy(),
    )
    output = proc.stdout.strip()
    if proc.returncode != 0:
        raise RuntimeError(output or "mita get users failed")
    return parse_mita_users_output(output)


def parse_iec_bytes(value):
    value = str(value or "").strip()
    if not value or value == "-":
        return 0
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(B|KiB|MiB|GiB|TiB|PiB)", value)
    if not match:
        return 0
    scale = {
        "B": 1,
        "KiB": 1024,
        "MiB": 1024 ** 2,
        "GiB": 1024 ** 3,
        "TiB": 1024 ** 4,
        "PiB": 1024 ** 5,
    }[match.group(2)]
    return int(float(match.group(1)) * scale)


def format_iec_bytes(value):
    value = int(value)
    if value < 1024:
        return f"{value}B"
    units = ["KiB", "MiB", "GiB", "TiB", "PiB"]
    size = float(value)
    for unit in units:
        size /= 1024.0
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.1f}{unit}"
    return f"{value}B"


def latest_activity(values):
    best_text = "-"
    best_dt = None
    for value in values:
        value = str(value or "").strip()
        if not value or value == "-":
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if best_dt is None or parsed > best_dt:
            best_dt = parsed
            best_text = value
    return best_text


def summarize_traffic_rows(rows):
    fields = ("day_down", "day_up", "week_down", "week_up", "month_down", "month_up")
    result = {key: format_iec_bytes(sum(parse_iec_bytes(row.get(key)) for row in rows)) for key in fields}
    result["last_active"] = latest_activity(row.get("last_active") for row in rows)
    return result


def get_traffic_view():
    raw_rows = get_mita_user_traffic()
    raw_by_username = {row["username"]: row for row in raw_rows}
    result = []

    for user in get_users():
        children = []
        legacy_row = raw_by_username.get(user["username"])
        if legacy_row:
            children.append({"name": "Legacy", "kind": "legacy", **legacy_row})

        for device in user.get("devices", []):
            row = raw_by_username.get(device.get("mita_username"))
            if row:
                children.append({
                    "name": device_display_name(device),
                    "kind": "device",
                    "device_id": device.get("id", ""),
                    **row,
                })
            else:
                children.append({
                    "name": device_display_name(device),
                    "kind": "device",
                    "device_id": device.get("id", ""),
                    "username": device.get("mita_username", ""),
                    "last_active": "-",
                    "day_down": "0B",
                    "day_up": "0B",
                    "week_down": "0B",
                    "week_up": "0B",
                    "month_down": "0B",
                    "month_up": "0B",
                })

        totals = summarize_traffic_rows(children)
        result.append({
            "username": user["username"],
            **totals,
            "children": children,
        })

    return result


def yaml_q(value):
    return json.dumps(str(value), ensure_ascii=False)


def device_display_name(device):
    model = str(device.get("model", "")).strip()
    os_name = str(device.get("os", "")).strip()
    os_version = str(device.get("os_version", "")).strip()
    if model:
        return model
    if os_name and os_version:
        return f"{os_name} {os_version}"
    if os_name:
        return os_name
    return "Device " + str(device.get("id", ""))[:6]


def subscription_yaml(user, credential=None):
    settings = get_settings()
    credential = credential or {
        "username": user["username"],
        "password": user["password"],
        "label": user["username"],
    }
    name = "Mieru " + user["username"]
    label = str(credential.get("label", "")).strip()
    if label and label != user["username"]:
        name += " · " + label
    host = settings["public_mieru_host"]
    port = int(settings["public_mieru_port"])
    username = credential["username"]
    password = credential["password"]
    return f"""mixed-port: 7890
allow-lan: false
mode: global
log-level: info
ipv6: true

proxies:
  - name: {yaml_q(name)}
    type: mieru
    server: {yaml_q(host)}
    port: {port}
    transport: TCP
    udp: true
    username: {yaml_q(username)}
    password: {yaml_q(password)}
    multiplexing: MULTIPLEXING_HIGH

proxy-groups:
  - name: PROXY
    type: select
    proxies:
      - {yaml_q(name)}
"""


def find_user_by_token(token):
    return next(
        (
            user
            for user in get_users()
            if secrets.compare_digest(str(user.get("token", "")), token)
        ),
        None,
    )


def detect_platform_from_user_agent(user_agent):
    ua = str(user_agent or "").lower()
    if "android" in ua:
        return "android"
    if any(token in ua for token in ("iphone", "ipad", "ipod")):
        return "ios"
    if "windows" in ua:
        return "windows"
    if "macintosh" in ua or "mac os x" in ua:
        return "macos"
    if "linux" in ua:
        return "linux"
    return "unknown"


def render_subscription_page(user, request_headers=None):
    template = SUBSCRIPTION_PAGE_PATH.read_text(encoding="utf-8")
    subscription_url = public_user(user)["subscription_url"]
    user_agent = ""
    if request_headers is not None:
        user_agent = str(request_headers.get("User-Agent", "") or "")
    bootstrap = json.dumps(
        {
            "username": user["username"],
            "url": subscription_url,
            "platform": detect_platform_from_user_agent(user_agent),
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    return (
        template
        .replace("@@PAGE_TITLE@@", html.escape("Mieru · " + user["username"]))
        .replace("@@USERNAME_HTML@@", html.escape(user["username"]))
        .replace("@@SUBSCRIPTION_URL_HTML@@", html.escape(subscription_url))
        .replace("@@BOOTSTRAP_JSON@@", bootstrap)
    )


def public_device(device):
    return {
        "id": device.get("id", ""),
        "hwid": device.get("hwid", ""),
        "name": device_display_name(device),
        "os": device.get("os", ""),
        "os_version": device.get("os_version", ""),
        "model": device.get("model", ""),
        "user_agent": device.get("user_agent", ""),
        "first_seen": device.get("first_seen", ""),
        "last_seen": device.get("last_seen", ""),
    }


def public_user(user):
    base = get_settings().get("subscription_base_url", "").rstrip("/")
    return {
        "username": user["username"],
        "allow_private_ip": bool(user.get("allow_private_ip", False)),
        "allow_loopback_ip": bool(user.get("allow_loopback_ip", False)),
        "subscription_url": base + "/sub/" + user["token"],
        "legacy_enabled": bool(user.get("legacy_enabled", True)),
        "device_limit": int(user.get("device_limit", 0) or 0),
        "devices": [public_device(d) for d in user.get("devices", [])],
    }


def _header(headers, name, max_len=256):
    return str(headers.get(name, "") or "").strip()[:max_len]


def _new_device(user, hwid, headers):
    device_id = secrets.token_hex(8)
    return {
        "id": device_id,
        "hwid": hwid,
        "mita_username": "dev_" + secrets.token_hex(12),
        "password": secrets.token_urlsafe(24),
        "os": _header(headers, "x-device-os", 80),
        "os_version": _header(headers, "x-ver-os", 80),
        "model": _header(headers, "x-device-model", 120),
        "user_agent": _header(headers, "user-agent", 200),
        "first_seen": utc_now(),
        "last_seen": utc_now(),
    }


def resolve_subscription(token, headers):
    response_headers = {"x-hwid-active": "true"}
    raw_hwid = _header(headers, "x-hwid", 128)
    hwid = raw_hwid if HWID_RE.fullmatch(raw_hwid) else ""

    with LOCK:
        users = get_users()
        user = next(
            (u for u in users if secrets.compare_digest(str(u.get("token", "")), token)),
            None,
        )
        if not user:
            return None, None, {}, HTTPStatus.NOT_FOUND, "subscription not found"

        if not hwid:
            if user.get("legacy_enabled", True):
                credential = {
                    "username": user["username"],
                    "password": user["password"],
                    "label": user["username"],
                }
                return user, credential, response_headers, None, None

            # Only advertise "HWID not supported" when HWID is actually
            # required. Clash Mi treats this header pair as a hard error.
            response_headers["x-hwid-not-supported"] = "true"
            return None, None, response_headers, HTTPStatus.NOT_FOUND, "HWID is required for this subscription"

        devices = user.setdefault("devices", [])
        device = next((d for d in devices if secrets.compare_digest(str(d.get("hwid", "")), hwid)), None)
        if device:
            changed = False
            metadata = {
                "os": _header(headers, "x-device-os", 80),
                "os_version": _header(headers, "x-ver-os", 80),
                "model": _header(headers, "x-device-model", 120),
                "user_agent": _header(headers, "user-agent", 200),
            }
            for key, value in metadata.items():
                if value and device.get(key) != value:
                    device[key] = value
                    changed = True
            device["last_seen"] = utc_now()
            changed = True
            if changed:
                write_json(USERS_PATH, users)
            credential = {
                "username": device["mita_username"],
                "password": device["password"],
                "label": device_display_name(device),
            }
            return user, credential, response_headers, None, None

        limit = int(user.get("device_limit", 0) or 0)
        if limit > 0 and len(devices) >= limit:
            response_headers["x-hwid-max-devices-reached"] = "true"
            response_headers["x-hwid-limit"] = "true"
            return None, None, response_headers, HTTPStatus.NOT_FOUND, "device limit reached"

        previous = copy.deepcopy(users)
        device = _new_device(user, hwid, headers)
        devices.append(device)
        try:
            persist_and_reload(USERS_PATH, users, previous)
        except MitaReloadError as exc:
            return None, None, response_headers, HTTPStatus.SERVICE_UNAVAILABLE, str(exc)

        credential = {
            "username": device["mita_username"],
            "password": device["password"],
            "label": device_display_name(device),
        }
        return user, credential, response_headers, None, None


PAGE = Path(__file__).with_name("admin.html").read_text(encoding="utf-8")

class CommonHandler(BaseHTTPRequestHandler):
    server_version = "mita-ha/1"

    def log_message(self, fmt, *args):
        # Subscription tokens are credentials; never persist them in HTTP logs.
        message = re.sub(r"(/sub/)[A-Za-z0-9_-]{16,}", r"\1[redacted]", fmt % args)
        print("[web] %s - %s" % (self.address_string(), message), flush=True)

    def send_bytes(self, status, content_type, data, headers=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, str(value))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, value, headers=None):
        self.send_bytes(
            status,
            "application/json; charset=utf-8",
            json.dumps(value, ensure_ascii=False),
            headers=headers,
        )

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > 65536:
            raise ValueError("request size is invalid")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body


class AdminHandler(CommonHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", ""):
            self.send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", PAGE)
            return
        if path == "/api/state":
            self.send_json(HTTPStatus.OK, {"users": [public_user(u) for u in get_users()], "settings": get_settings()})
            return
        if path == "/api/traffic":
            try:
                self.send_json(HTTPStatus.OK, {"users": get_traffic_view()})
            except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return
        if path == "/api/log-db-status":
            self.send_json(HTTPStatus.OK, {"status": get_log_db_status()})
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/users":
            try:
                body = self.read_json()
                username = str(body.get("username", "")).strip()
                if not USERNAME_RE.fullmatch(username):
                    raise ValueError("username must contain only A-Z, a-z, 0-9, _, . or -")
                password = str(body.get("password", "")) or secrets.token_urlsafe(24)
                generated = password if not body.get("password") else ""
                with LOCK:
                    users = get_users()
                    if any(u["username"] == username for u in users):
                        raise ValueError("user already exists")
                    previous = copy.deepcopy(users)
                    user = {
                        "username": username,
                        "password": password,
                        "allow_private_ip": bool(body.get("allow_private_ip", False)),
                        "allow_loopback_ip": bool(body.get("allow_loopback_ip", False)),
                        "token": secrets.token_urlsafe(24),
                        "legacy_enabled": bool(body.get("legacy_enabled", True)),
                        "device_limit": 0,
                        "devices": [],
                    }
                    users.append(user)
                    persist_and_reload(USERS_PATH, users, previous)
                    result = public_user(user)
                self.send_json(HTTPStatus.CREATED, {
                    "user": result,
                    "generated_password": generated or None,
                    "mita_reloaded": True,
                    "mita_output": "",
                })
            except ValueError as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except MitaReloadError as exc:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return

        m = re.fullmatch(r"/api/users/([^/]+)/rotate-token", path)
        if m:
            username = urllib.parse.unquote(m.group(1))
            with LOCK:
                users = get_users()
                for user in users:
                    if user["username"] == username:
                        user["token"] = secrets.token_urlsafe(24)
                        write_json(USERS_PATH, users)
                        result = public_user(user)
                        break
                else:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "user not found"})
                    return
            self.send_json(HTTPStatus.OK, {"user": result})
            return

        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_PUT(self):
        path = urllib.parse.urlparse(self.path).path

        m = re.fullmatch(r"/api/users/([^/]+)/device-settings", path)
        if m:
            username = urllib.parse.unquote(m.group(1))
            try:
                body = self.read_json()
                with LOCK:
                    users = get_users()
                    user = next((u for u in users if u["username"] == username), None)
                    if not user:
                        self.send_json(HTTPStatus.NOT_FOUND, {"error": "user not found"})
                        return

                    limit = int(body.get("device_limit", user.get("device_limit", 0) or 0))
                    if limit < 0 or limit > 100:
                        raise ValueError("device_limit must be between 0 and 100")
                    legacy_enabled = bool(body.get("legacy_enabled", user.get("legacy_enabled", True)))
                    if limit > 0 and len(user.get("devices", [])) > limit:
                        raise ValueError("device_limit is lower than the number of registered devices")

                    previous = copy.deepcopy(users)
                    old_legacy = bool(user.get("legacy_enabled", True))
                    user["legacy_enabled"] = legacy_enabled
                    user["device_limit"] = limit
                    if old_legacy != legacy_enabled:
                        persist_and_reload(USERS_PATH, users, previous)
                    else:
                        write_json(USERS_PATH, users)
                    result = public_user(user)

                self.send_json(HTTPStatus.OK, {"user": result})
            except ValueError as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except MitaReloadError as exc:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            return

        if path != "/api/settings":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            body = self.read_json()
            with LOCK:
                settings = get_settings()
                previous = copy.deepcopy(settings)
                host = str(body.get("public_mieru_host", settings.get("public_mieru_host", ""))).strip()
                if not host or "/" in host or " " in host:
                    raise ValueError("invalid public_mieru_host")
                port = int(body.get("public_mieru_port", settings.get("public_mieru_port", 8443)))
                if port < 1 or port > 65535:
                    raise ValueError("invalid public_mieru_port")
                base = normalize_base_url(body.get("subscription_base_url", settings.get("subscription_base_url", "")))
                old_log_level = str(settings.get("log_level", "INFO"))
                if "log_level" in body:
                    log_level = str(body["log_level"]).upper().strip()
                    if log_level not in {"FATAL", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"}:
                        raise ValueError("invalid log_level")
                    settings["log_level"] = log_level
                if "postgres_dsn" in body:
                    postgres_dsn = str(body.get("postgres_dsn", "") or "").strip()
                    if len(postgres_dsn) > 4096:
                        raise ValueError("postgres_dsn is too long")
                    settings["postgres_dsn"] = postgres_dsn

                settings["public_mieru_host"] = host
                settings["public_mieru_port"] = port
                settings["subscription_base_url"] = base
                if settings.get("log_level", "INFO") != old_log_level:
                    persist_and_reload(SETTINGS_PATH, settings, previous)
                else:
                    write_json(SETTINGS_PATH, settings)

            self.send_json(HTTPStatus.OK, {
                "settings": settings,
                "mita_reloaded": True,
                "mita_output": "",
            })
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except MitaReloadError as exc:
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path

        m = re.fullmatch(r"/api/users/([^/]+)/devices/([A-Fa-f0-9]{16})", path)
        if m:
            username = urllib.parse.unquote(m.group(1))
            device_id = m.group(2)
            with LOCK:
                users = get_users()
                user = next((u for u in users if u["username"] == username), None)
                if not user:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "user not found"})
                    return
                devices = user.get("devices", [])
                device = next((d for d in devices if d.get("id") == device_id), None)
                if not device:
                    self.send_json(HTTPStatus.NOT_FOUND, {"error": "device not found"})
                    return
                if not user.get("legacy_enabled", True) and len(devices) == 1:
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "enable legacy access before removing the last device"},
                    )
                    return
                previous = copy.deepcopy(users)
                user["devices"] = [d for d in devices if d.get("id") != device_id]
                try:
                    persist_and_reload(USERS_PATH, users, previous)
                except MitaReloadError as exc:
                    self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                    return
                result = public_user(user)
            self.send_json(HTTPStatus.OK, {"deleted": device_id, "user": result})
            return

        m = re.fullmatch(r"/api/users/([^/]+)", path)
        if not m:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        username = urllib.parse.unquote(m.group(1))
        with LOCK:
            users = get_users()
            new_users = [u for u in users if u["username"] != username]
            if len(new_users) == len(users):
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "user not found"})
                return
            if not new_users:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "at least one user must remain"})
                return
            try:
                persist_and_reload(USERS_PATH, new_users, users)
            except MitaReloadError as exc:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
        self.send_json(HTTPStatus.OK, {"deleted": username, "mita_reloaded": True, "mita_output": ""})


class SubscriptionHandler(CommonHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        download_match = re.fullmatch(
            r"/sub/([A-Za-z0-9_-]{16,})/download/([a-z0-9-]+)/([a-z0-9-]+)",
            path,
        )
        if download_match:
            token, client_id, target_id = download_match.groups()
            if not find_user_by_token(token):
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "subscription not found"})
                return
            try:
                location = resolve_github_download(client_id, target_id)
            except ValueError as exc:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            except RuntimeError as exc:
                self.send_json(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
                return

            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", location)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        m = re.fullmatch(r"/sub/([A-Za-z0-9_-]{16,})", path)
        if not m:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        token = m.group(1)
        query = urllib.parse.parse_qs(parsed.query)
        force_raw = query.get("raw", ["0"])[0] == "1"
        force_web = query.get("web", ["0"])[0] == "1"
        accept = self.headers.get("Accept", "").lower()
        fetch_mode = self.headers.get("Sec-Fetch-Mode", "").lower()
        fetch_dest = self.headers.get("Sec-Fetch-Dest", "").lower()

        # Do not use Accept alone here. Many HTTP clients send */*, and some
        # embedded clients may advertise text/html. Browser top-level
        # navigations are much more reliably identified by Fetch Metadata.
        browser_navigation = fetch_mode == "navigate" or fetch_dest == "document"
        wants_html = force_web or (
            not force_raw
            and browser_navigation
            and "text/html" in accept
        )

        if wants_html:
            user = find_user_by_token(token)
            if not user:
                self.send_bytes(
                    HTTPStatus.NOT_FOUND,
                    "text/html; charset=utf-8",
                    "<!doctype html><title>Not found</title><h1>Subscription not found</h1>",
                )
                return
            try:
                page = render_subscription_page(user, self.headers)
            except OSError as exc:
                self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
            self.send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", page)
            return

        user, credential, response_headers, error_status, error_message = resolve_subscription(
            token,
            self.headers,
        )
        if error_status is not None:
            self.send_json(error_status, {"error": error_message}, headers=response_headers)
            return

        self.send_bytes(
            HTTPStatus.OK,
            "text/yaml; charset=utf-8",
            subscription_yaml(user, credential),
            headers=response_headers,
        )


def serve():
    admin = ThreadingHTTPServer(("0.0.0.0", ADMIN_PORT), AdminHandler)
    sub = ThreadingHTTPServer(("0.0.0.0", SUB_PORT), SubscriptionHandler)

    def shutdown_handler(signum, frame):
        threading.Thread(target=admin.shutdown, daemon=True).start()
        threading.Thread(target=sub.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    threading.Thread(target=sub.serve_forever, name="subscription-server", daemon=True).start()
    threading.Thread(target=log_db_worker, name="postgres-log-forwarder", daemon=True).start()
    print("[web] admin UI listening on 8098 (Ingress only)", flush=True)
    print("[web] subscription server listening on 8099", flush=True)
    admin.serve_forever()
    admin.server_close()
    sub.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    args = parser.parse_args()
    initialize()
    if not args.init:
        serve()

#!/usr/bin/env python3
import argparse
import base64
import ipaddress
import contextlib
import copy
import html
import hashlib
import io
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import tarfile
import tempfile
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
PASSWORD_MAX_LENGTH = 256
# Placeholder shipped in config.yaml. It is public, so it must never become a live credential.
PLACEHOLDER_PASSWORD = "change-this-password"
# Home Assistant Ingress proxies every request from the Supervisor at this address.
# Extra addresses (e.g. for local development) can be added with MITA_ADMIN_ALLOWED_IPS.
INGRESS_PROXY_IP = "172.30.32.2"
CONTACT_EMAIL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+$")
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
BACKUP_INTERVAL_SECONDS = 24 * 60 * 60
BACKUP_RETRY_SECONDS = 5 * 60
BACKUP_KEEP_COUNT = 30
BACKUP_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
BACKUP_STATUS_LOCK = threading.RLock()
BACKUP_STATUS = {
    "enabled": False,
    "connected": False,
    "last_error": "",
    "last_backup": None,
    "last_reason": None,
    "backup_count": 0,
}


CLIENT_CATALOG_PATH = Path(__file__).with_name("clients.json")


def load_client_catalog(path=None):
    """Read the subscription client catalog shared with subscription.html and tests."""
    with (path or CLIENT_CATALOG_PATH).open("r", encoding="utf-8") as f:
        clients = json.load(f)["clients"]
    for client in clients:
        for download in client["downloads"]:
            if ("asset" in download) == ("url" in download):
                raise ValueError(f"{client['id']}: download needs exactly one of asset/url")
            if "asset" in download:
                re.compile(download["asset"])
                if not client.get("repo") or not download.get("target"):
                    raise ValueError(f"{client['id']}: GitHub download needs repo and target")
    return clients


CLIENTS = load_client_catalog()
# Allowlist for /sub/<token>/download/<client>/<target>: only these release assets are resolved.
GITHUB_DOWNLOADS = {
    client["id"]: {
        "repo": client["repo"],
        "targets": {d["target"]: d["asset"] for d in client["downloads"] if "asset" in d},
    }
    for client in CLIENTS
    if client.get("repo")
}


def public_client_catalog():
    """Catalog for the browser: no regexes or repository internals."""
    return [
        {
            "id": client["id"],
            "name": client["name"],
            "scheme": client.get("scheme"),
            "hwid": bool(client.get("hwid")),
            "xhwid_param": bool(client.get("xhwid_param")),
            "downloads": [
                {key: d[key] for key in ("os", "label", "note", "target", "url") if key in d}
                for d in client["downloads"]
            ],
        }
        for client in CLIENTS
    ]


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


class StateError(RuntimeError):
    """Persisted state is unreadable; mutations must stop until it is restored."""


def load_json(path, default, strict=False):
    """Read JSON state. With strict=True a corrupt file raises instead of acting empty."""
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        if strict:
            raise StateError(f"{path.name} is corrupt ({exc}); restore it from a backup") from exc
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


def set_backup_status(**values):
    with BACKUP_STATUS_LOCK:
        BACKUP_STATUS.update(values)


def get_backup_status():
    with BACKUP_STATUS_LOCK:
        return dict(BACKUP_STATUS)


def build_backup_archive():
    """Snapshot persistent operational data, excluding logs and Unix sockets.

    The archive stores credentials and must be treated as confidential.
    """
    names = ("users.json", "settings.json", "mita-server.json", "options.json")
    files = []
    total = 0
    with LOCK:
        for name in names:
            path = DATA_DIR / name
            if not path.exists():
                if name in ("users.json", "settings.json"):
                    raise RuntimeError(f"backup required file missing: {name}")
                continue
            if not path.is_file() or path.is_symlink():
                raise RuntimeError(f"unsafe backup path: {name}")
            data = path.read_bytes()
            total += len(data)
            if total > BACKUP_MAX_UNCOMPRESSED_BYTES:
                raise RuntimeError("backup exceeds maximum uncompressed size")
            files.append((name, data))

        metrics_root = DATA_DIR / "mita-state"
        if metrics_root.is_symlink():
            raise RuntimeError("backup cannot include symlinked metrics")
        if metrics_root.is_dir():
            for path in sorted(metrics_root.rglob("*")):
                if path.is_symlink():
                    raise RuntimeError("backup cannot include symlinks")
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise RuntimeError("backup contains unsupported file")
                name = path.relative_to(DATA_DIR).as_posix()
                data = path.read_bytes()
                total += len(data)
                if total > BACKUP_MAX_UNCOMPRESSED_BYTES:
                    raise RuntimeError("backup exceeds maximum uncompressed size")
                files.append((name, data))

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files:
            info = tarfile.TarInfo(name=name)
            info.mode = 0o600
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    payload = buffer.getvalue()
    return payload, [name for name, _ in files], hashlib.sha256(payload).hexdigest()


def save_postgres_backup(dsn, reason="scheduled"):
    """Persist an atomic compressed snapshot and enforce bounded retention."""
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed")
    payload, names, digest = build_backup_archive()
    with contextlib.closing(psycopg2.connect(dsn, connect_timeout=8)) as connection:
        with connection:
            with connection.cursor() as cur:
                cur.execute("""
                    CREATE TABLE IF NOT EXISTS mita_backups (
                        id BIGSERIAL PRIMARY KEY,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        reason TEXT NOT NULL,
                        file_names JSONB NOT NULL,
                        checksum_sha256 TEXT NOT NULL,
                        payload BYTEA NOT NULL
                    )
                """)
                cur.execute(
                    """
                    INSERT INTO mita_backups (reason, file_names, checksum_sha256, payload)
                    VALUES (%s, %s::jsonb, %s, %s)
                    """,
                    (reason, json.dumps(names), digest, psycopg2.Binary(payload)),
                )
                cur.execute(
                    """
                    DELETE FROM mita_backups
                    WHERE id NOT IN (
                        SELECT id FROM mita_backups ORDER BY created_at DESC, id DESC LIMIT %s
                    )
                    """,
                    (BACKUP_KEEP_COUNT,),
                )
                cur.execute("SELECT COUNT(*) FROM mita_backups")
                count = int(cur.fetchone()[0])
    return {"reason": reason, "file_names": names, "sha256": digest, "count": count}


def restore_backup_archive(payload, checksum, dsn=None):
    """Restore into an unused data directory while the server is stopped."""
    if hashlib.sha256(payload).hexdigest() != checksum:
        raise ValueError("backup checksum mismatch")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if any(DATA_DIR.iterdir()):
        raise ValueError("restore requires an empty data directory")

    json_types = {
        "users.json": list,
        "settings.json": dict,
        "mita-server.json": dict,
        "options.json": dict,
    }
    with tempfile.TemporaryDirectory(prefix=".restore-", dir=DATA_DIR) as staging:
        root = Path(staging)
        seen = set()
        total = 0
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r|gz") as archive:
            for member in archive:
                name = member.name
                parts = name.split("/")
                if (name in seen or not member.isfile()
                        or any(part in ("", ".", "..") for part in parts)
                        or (name not in json_types
                            and not (parts[0] == "mita-state" and len(parts) > 1))):
                    raise ValueError("backup contains an unsupported or duplicate path")
                seen.add(name)
                total += member.size
                if total > BACKUP_MAX_UNCOMPRESSED_BYTES:
                    raise ValueError("backup exceeds maximum uncompressed size")
                data = archive.extractfile(member).read()
                if name in json_types:
                    value = json.loads(data)
                    if not isinstance(value, json_types[name]):
                        raise ValueError("backup contains invalid JSON state")
                    if name == "settings.json" and dsn is not None:
                        value["postgres_dsn"] = dsn
                        data = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as file:
                    file.write(data)
                path.chmod(0o600)
        if not {"users.json", "settings.json"} <= seen:
            raise ValueError("backup is missing required state")

        installed = []
        try:
            for path in root.iterdir():
                destination = DATA_DIR / path.name
                path.rename(destination)
                installed.append(destination)
        except OSError:
            for path in installed:
                path.rename(root / path.name)
            raise


def restore_postgres_backup(dsn, backup_id):
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed")
    with contextlib.closing(psycopg2.connect(dsn, connect_timeout=8)) as connection:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, checksum_sha256, payload FROM mita_backups
                WHERE (%s::bigint IS NULL OR id = %s)
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (backup_id, backup_id),
            )
            record = cur.fetchone()
    if record is None:
        raise ValueError("no matching PostgreSQL backup")
    identifier, checksum, payload = record
    restore_backup_archive(bytes(payload), checksum, dsn)
    return identifier


def backup_db_worker():
    """Back up on every server start, then every 24h; retry failed jobs."""
    if psycopg2 is None:
        set_backup_status(last_error="psycopg2 is not installed")
        return
    active_dsn = None
    next_backup_at = 0.0
    reason = "startup"
    while True:
        dsn = str(get_settings().get("postgres_dsn", "") or "").strip()
        set_backup_status(enabled=bool(dsn))
        if not dsn:
            active_dsn = None
            next_backup_at = 0.0
            reason = "startup"
            set_backup_status(connected=False, last_error="")
            time.sleep(30)
            continue
        if dsn != active_dsn:
            active_dsn = dsn
            next_backup_at = 0.0
            reason = "startup"
            set_backup_status(connected=False, last_error="")
        remaining = next_backup_at - time.monotonic()
        if remaining > 0:
            time.sleep(min(60.0, remaining))
            continue
        try:
            result = save_postgres_backup(dsn, reason)
            set_backup_status(
                connected=True,
                last_error="",
                last_backup=utc_now(),
                last_reason=result["reason"],
                backup_count=result["count"],
            )
            next_backup_at = time.monotonic() + BACKUP_INTERVAL_SECONDS
            reason = "scheduled"
        except Exception as exc:
            # Do not print exception details or the DSN: backup contains secrets.
            set_backup_status(connected=False, last_error=type(exc).__name__)
            next_backup_at = time.monotonic() + BACKUP_RETRY_SECONDS
        time.sleep(1)


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
    value = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in value)
    ):
        raise ValueError("subscription_base_url must be an http(s) URL without credentials, query or fragment")
    try:
        _ = parsed.port  # Reject malformed port values before persisting.
    except ValueError as exc:
        raise ValueError("invalid subscription_base_url port") from exc
    return value


def normalize_contact_email(value):
    """Validate the optional public contact address before saving or displaying it."""
    if not isinstance(value, str):
        raise ValueError("invalid contact_email")
    address = value.strip()
    if not address:
        return ""
    if len(address) > 254 or not CONTACT_EMAIL_RE.fullmatch(address):
        raise ValueError("invalid contact_email")
    local, domain = address.rsplit("@", 1)
    if (
        len(local) > 64
        or local.startswith(".")
        or local.endswith(".")
        or ".." in local
        or len(domain) > 253
    ):
        raise ValueError("invalid contact_email")
    labels = domain.split(".")
    if len(labels) < 2 or any(
        not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
        for label in labels
    ) or not labels[-1].isalpha() or len(labels[-1]) < 2:
        raise ValueError("invalid contact_email")
    return address


def is_standalone():
    return os.environ.get("MITA_STANDALONE") == "1"


def initialize():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    options = load_options()
    if is_standalone():
        options.setdefault("public_mieru_host", os.environ.get("MITA_PUBLIC_HOST", "localhost"))
        options.setdefault("public_mieru_port", int(os.environ.get("MITA_PUBLIC_PORT", "2022")))
        options.setdefault("subscription_base_url", os.environ.get(
            "MITA_SUBSCRIPTION_BASE_URL", "http://localhost:8099"))
        options.setdefault("postgres_dsn", os.environ.get("MITA_POSTGRES_DSN", ""))

    if not USERS_PATH.exists():
        default_private = bool(options.get("allow_private_ip", False))
        default_loopback = bool(options.get("allow_loopback_ip", False))
        users = []
        for item in options.get("users", []):
            username = str(item.get("username", "")).strip()
            password = str(item.get("password", ""))
            if not username or not password:
                continue
            if password == PLACEHOLDER_PASSWORD:
                password = secrets.token_urlsafe(24)
                print(
                    f"[init] replaced the placeholder password of '{username}' with a random one; "
                    "share its subscription link or reset the password in the admin UI",
                    flush=True,
                )
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
                "password": secrets.token_urlsafe(24),
                "allow_private_ip": default_private,
                "allow_loopback_ip": default_loopback,
                "token": secrets.token_urlsafe(24),
                "legacy_enabled": True,
                "device_limit": 0,
                "devices": [],
            }]
        write_json(USERS_PATH, users)

    migrate_users()
    for user in get_users():
        if user.get("password") == PLACEHOLDER_PASSWORD:
            print(
                f"[init] WARNING: user '{user['username']}' still uses the public placeholder password; "
                "reset it in the admin UI",
                flush=True,
            )

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
            "contact_email": "",
        }
        write_json(SETTINGS_PATH, settings)

    rebuild_mita_config()


class MitaReloadError(RuntimeError):
    """Raised when a configuration change could not be applied to mita."""


def persist_and_reload(path, updated, previous):
    """Commit state only if mita accepts the corresponding configuration.

    Caller must hold LOCK through the full read/modify/write/reload sequence.
    """
    try:
        write_json(path, updated)
    except OSError as exc:  # os.replace is atomic, so the previous file is intact.
        raise MitaReloadError(f"unable to save {path.name}: {exc}") from exc
    ok, output = reload_mita()
    if ok:
        return output
    try:
        write_json(path, previous)
    except OSError as exc:
        raise MitaReloadError(
            f"{output or 'mita reload failed'}; rollback of {path.name} also failed: {exc}"
        ) from exc
    # Restore the on-disk mita configuration and retry applying the old state.
    reload_mita()
    raise MitaReloadError(output or "mita reload failed")


def get_users():
    users = load_json(USERS_PATH, [], strict=True)
    if not isinstance(users, list) or not all(
        isinstance(u, dict) and isinstance(u.get("username"), str) for u in users
    ):
        raise StateError(f"{USERS_PATH.name} has an invalid structure; restore it from a backup")
    return users


def get_settings():
    settings = load_json(SETTINGS_PATH, {}, strict=True)
    if not isinstance(settings, dict):
        raise StateError(f"{SETTINGS_PATH.name} has an invalid structure; restore it from a backup")
    return settings


def parse_bool(body, key, default):
    """Accept only real JSON booleans so "false" can never enable a permission."""
    if key not in body:
        return default
    value = body[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false")
    return value


def parse_int(body, key, default, minimum, maximum):
    if key not in body:
        return default
    value = body[key]
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{key} must be between {minimum} and {maximum}")
    return value


def parse_password(body):
    """Return (password, generated) from an optional "password" field."""
    value = body.get("password")
    if value is None or value == "":
        password = secrets.token_urlsafe(24)
        return password, password
    if not isinstance(value, str):
        raise ValueError("password must be a string")
    if value == PLACEHOLDER_PASSWORD:
        raise ValueError("this placeholder password is public; choose another one")
    if len(value) > PASSWORD_MAX_LENGTH or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"password must be at most {PASSWORD_MAX_LENGTH} printable characters")
    return value, ""


def normalize_egress(value, *, destination_limit=256):
    """Validate the supported subset of Mita's native egress schema."""
    def object_fields(item, allowed, label):
        if not isinstance(item, dict) or set(item) - allowed:
            raise ValueError(f"{label}: invalid object or unknown field")

    def items(item, key, limit):
        values = item.get(key, [])
        if not isinstance(values, list) or len(values) > limit:
            raise ValueError(f"{key} must be an array with at most {limit} entries")
        return values

    def domain(value, wildcard=False):
        if not isinstance(value, str):
            raise ValueError("domain or proxy host must be a string")
        if wildcard and value == "*":
            return value
        value = value.lower().removesuffix(".")
        labels = value.split(".")
        if len(value) > 253 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in labels
        ):
            raise ValueError("invalid domain or proxy host (use ASCII or punycode)")
        return value

    object_fields(value, {"proxies", "rules"}, "Outbound")
    proxies = []
    names = set()
    for proxy in items(value, "proxies", 32):
        object_fields(proxy, {"name", "protocol", "host", "port", "socks5Authentication"}, "Proxy")
        name = proxy.get("name")
        if not isinstance(name, str) or not USERNAME_RE.fullmatch(name) or name in names:
            raise ValueError("proxy names must be valid and unique")
        names.add(name)
        protocol = "SOCKS5_PROXY_PROTOCOL"
        if proxy.get("protocol", protocol) != protocol:
            raise ValueError("Mita outbound supports only SOCKS5")
        port = proxy.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("proxy port must be an integer between 1 and 65535")
        host = proxy.get("host")
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            host = domain(host)
        entry = {"name": name, "protocol": protocol, "host": host, "port": port}
        if "socks5Authentication" in proxy:
            auth = proxy["socks5Authentication"]
            object_fields(auth, {"user", "password"}, "SOCKS5 authentication")
            for key in ("user", "password"):
                field = auth.get(key)
                if not isinstance(field, str) or not field or len(field.encode("utf-8", errors="replace")) > 255 or any(
                    ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in field
                ):
                    raise ValueError("SOCKS5 credentials must be 1 to 255 UTF-8 bytes without control characters")
            entry["socks5Authentication"] = auth.copy()
        proxies.append(entry)

    rules = []
    for rule in items(value, "rules", 128):
        object_fields(rule, {"domainNames", "ipRanges", "action", "proxyNames"}, "Rule")
        domains = [domain(v, wildcard=True) for v in items(rule, "domainNames", destination_limit)]
        ranges = []
        for value in items(rule, "ipRanges", destination_limit):
            if not isinstance(value, str) or (value != "*" and "/" not in value):
                raise ValueError("IP ranges must be CIDR strings or *")
            ranges.append("*" if value == "*" else str(ipaddress.ip_network(value, strict=False)))
        if not domains and not ranges:
            raise ValueError("each rule needs a domain or IP range")
        action = rule.get("action")
        if action not in ("PROXY", "DIRECT", "REJECT"):
            raise ValueError("rule action must be PROXY, DIRECT, or REJECT")
        references = items(rule, "proxyNames", 32)
        if action == "PROXY":
            if not references or any(not isinstance(ref, str) or ref not in names for ref in references):
                raise ValueError("PROXY rule must reference defined proxies")
        elif references:
            raise ValueError("only PROXY rules may reference proxies")
        rules.append({"domainNames": domains, "ipRanges": ranges, "action": action, "proxyNames": references})
    return {"proxies": proxies, "rules": rules}


SING_BOX_BINARY = Path("/usr/local/bin/sing-box")
SING_BOX_PORT = 1081
SING_BOX_PROXY_NAME = "managed-sing-box"
SING_BOX_TYPES = {"vless", "shadowsocks", "trojan", "hysteria2", "socks"}


def sing_box_available():
    return is_standalone() and os.access(SING_BOX_BINARY, os.X_OK)


def validate_sing_box_outbound(outbound):
    if not isinstance(outbound, dict) or not isinstance(outbound.get("type"), str):
        raise ValueError("provide one sing-box outbound JSON object")
    protocol = outbound["type"]
    if protocol not in SING_BOX_TYPES:
        raise ValueError("supported outbound types: VLESS, Shadowsocks, Trojan, Hysteria2 and SOCKS")
    server = outbound.get("server")
    if not isinstance(server, str) or not server.strip() or any(c.isspace() for c in server):
        raise ValueError("sing-box outbound requires a server hostname or IP")
    port = outbound.get("server_port")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("sing-box outbound requires server_port between 1 and 65535")
    required = {
        "vless": ("uuid",),
        "shadowsocks": ("method", "password"),
        "trojan": ("password",),
        "hysteria2": ("password",),
        "socks": (),
    }
    for key in required[protocol]:
        if not isinstance(outbound.get(key), str) or not outbound[key].strip():
            raise ValueError(f"sing-box outbound requires {key}")


def sing_box_config(settings):
    validate_sing_box_outbound(settings["outbound"])
    return {
        "inbounds": [{
            "type": "socks",
            "listen": "127.0.0.1",
            "listen_port": SING_BOX_PORT,
            "users": [{"username": "mita", "password": settings["password"]}],
        }],
        "outbounds": [dict(settings["outbound"], tag="upstream")],
        "route": {"final": "upstream"},
    }


def managed_egress(settings):
    egress = normalize_egress(settings.get("egress", {}))
    bridge = settings.get("sing_box")
    if bridge is None:
        return egress
    if any(proxy["name"] == SING_BOX_PROXY_NAME for proxy in egress["proxies"]):
        raise ValueError(f"{SING_BOX_PROXY_NAME} is reserved while built-in sing-box is enabled")
    managed = normalize_egress({
        "proxies": [{
            "name": SING_BOX_PROXY_NAME,
            "host": "127.0.0.1",
            "port": SING_BOX_PORT,
            "socks5Authentication": {"user": "mita", "password": bridge["password"]},
        }],
        "rules": [{
            "domainNames": bridge["domains"],
            "ipRanges": bridge.get("ip_ranges", []),
            "action": "PROXY",
            "proxyNames": [SING_BOX_PROXY_NAME],
        }],
    }, destination_limit=16384)
    egress["proxies"].extend(managed["proxies"])
    egress["rules"] = managed["rules"] + egress["rules"]
    return egress


class SingBoxBridge:
    """Manage the bundled SOCKS5 listener within the Linux container."""

    def __init__(self):
        self.process = None

    def running(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        if self.running():
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None

    def start(self, settings):
        try:
            with socket.socket() as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", SING_BOX_PORT))
        except OSError as exc:
            raise ValueError(f"sing-box needs loopback port {SING_BOX_PORT} to be free") from exc

        with tempfile.TemporaryDirectory(prefix="mita-sing-box-") as directory:
            config = Path(directory) / "config.json"
            write_json(config, sing_box_config(settings))
            args = [str(SING_BOX_BINARY), "run", "-c", str(config)]
            self.process = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            for _ in range(30):
                if not self.running():
                    break
                try:
                    with socket.create_connection(("127.0.0.1", SING_BOX_PORT), timeout=0.1):
                        return
                except OSError:
                    time.sleep(0.1)
        self.stop()
        raise ValueError("sing-box could not start its local SOCKS5 listener")

    def replace(self, updated, previous, persist):
        if updated is not None:
            payload = json.dumps(sing_box_config(updated))
            try:
                result = subprocess.run(
                    [str(SING_BOX_BINARY), "check", "-c", "/dev/stdin"],
                    input=payload, text=True, capture_output=True, timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError("could not validate the sing-box configuration") from exc
            if result.returncode:
                raise ValueError("sing-box rejected the outbound configuration; check the provider JSON")

        self.stop()
        try:
            if updated is not None:
                self.start(updated)
            persist()
        except (ValueError, OSError):
            self.stop()
            if previous is not None:
                self.start(previous)
            raise


SING_BOX = SingBoxBridge()


def admin_settings(settings):
    settings = settings.copy()
    if not is_standalone():
        settings.pop("egress", None)
        settings.pop("sing_box", None)
    elif settings.get("sing_box"):
        settings["sing_box"] = {
            key: settings["sing_box"][key] for key in ("domains", "outbound", "ip_ranges")
            if key in settings["sing_box"]
        }
    return settings


def build_mita_config(users, settings):
    """Construct the Mita config without filesystem or subprocess side effects."""
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
    if is_standalone() and ("egress" in settings or settings.get("sing_box")):
        config["egress"] = managed_egress(settings)
    return config


def rebuild_mita_config():
    with LOCK:
        config = build_mita_config(get_users(), get_settings())
        write_json(MITA_CONFIG_PATH, config)


def reload_mita():
    try:
        rebuild_mita_config()
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
    # iOS browsers may request a desktop site and report Macintosh/Mac OS X.
    # Firefox (FxiOS) and other iOS-specific tokens must win over the Mac hint.
    if (
        any(token in ua for token in ("iphone", "ipad", "ipod", "fxios/", "crios/", "edgios/", "opios/"))
        or ("macintosh" in ua and "mobile/" in ua)
    ):
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
    try:
        contact_email = normalize_contact_email(get_settings().get("contact_email", ""))
    except ValueError:
        contact_email = ""
    contact_link = ""
    if contact_email:
        escaped_email = html.escape(contact_email, quote=True)
        mailto_url = html.escape(
            "mailto:" + urllib.parse.quote(contact_email, safe="@._+-"), quote=True
        )
        contact_link = (
            '<p class="contact-info">Связаться: '
            f'<a href="{mailto_url}">{escaped_email}</a></p>'
        )
    user_agent = ""
    if request_headers is not None:
        user_agent = str(request_headers.get("User-Agent", "") or "")
    bootstrap = json.dumps(
        {
            "username": user["username"],
            "url": subscription_url,
            "platform": detect_platform_from_user_agent(user_agent),
            # Without legacy access the subscription is only served to clients sending x-hwid.
            "hwid_required": not user.get("legacy_enabled", True),
            "clients": public_client_catalog(),
        },
        ensure_ascii=False,
    ).replace("</", "<\\/")

    return (
        template
        .replace("@@PAGE_TITLE@@", html.escape("Mieru · " + user["username"]))
        .replace("@@USERNAME_HTML@@", html.escape(user["username"]))
        .replace("@@SUBSCRIPTION_URL_HTML@@", html.escape(subscription_url))
        .replace("@@CONTACT_LINK_HTML@@", contact_link)
        .replace("@@BOOTSTRAP_JSON@@", bootstrap)
    )


def subscription_page_headers(page):
    """CSP pinned to the page's own inline scripts (the bootstrap differs per user)."""
    hashes = " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii") + "'"
        for script in re.findall(r"<script>(.*?)</script>", page, re.S)
    )
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; script-src {hashes}; "
            "style-src 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
            "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Frame-Options": "DENY",
    }


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
        "default_password": user.get("password") == PLACEHOLDER_PASSWORD,
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

    def read_json(self, max_bytes=65536):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid Content-Length") from exc
        if length < 0 or length > max_bytes:
            raise ValueError("request size is invalid")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid JSON body") from exc
        if not isinstance(body, dict):
            raise ValueError("JSON body must be an object")
        return body


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def admin_allowed_clients():
    extra = os.environ.get("MITA_ADMIN_ALLOWED_IPS", "")
    allowed = {INGRESS_PROXY_IP, "127.0.0.1", "::1"}
    allowed.update(item.strip() for item in extra.split(",") if item.strip())
    return allowed


def is_allowed_admin_client(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    return str(ip) in admin_allowed_clients()


def find_user(users, username):
    user = next((u for u in users if u["username"] == username), None)
    if not user:
        raise ApiError(HTTPStatus.NOT_FOUND, "user not found")
    return user


ADMIN_PAGE_HEADERS = {
    # Ingress embeds the panel in a same-origin iframe; inline code is the page itself.
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
USER = r"/api/users/(?P<username>[^/]+)"
ADMIN_ROUTES = [
    ("GET", r"/?", "page"),
    ("GET", r"/api/state", "get_state"),
    ("GET", r"/api/traffic", "get_traffic"),
    ("GET", r"/api/log-db-status", "get_log_db"),
    ("GET", r"/api/backup-status", "get_backup"),
    ("POST", r"/api/users", "create_user"),
    ("POST", USER + r"/rotate-token", "rotate_token"),
    ("POST", USER + r"/password", "reset_password"),
    ("PATCH", USER, "update_user"),
    ("PUT", USER + r"/device-settings", "update_user"),  # kept for older UI builds
    ("PUT", r"/api/settings", "update_settings"),
    ("PUT", r"/api/sing-box", "sing_box"),
    ("DELETE", USER + r"/devices/(?P<device_id>[A-Fa-f0-9]{16})", "delete_device"),
    ("DELETE", USER, "delete_user"),
]
ADMIN_ROUTES = [(method, re.compile(pattern), name) for method, pattern, name in ADMIN_ROUTES]


class AdminHandler(CommonHandler):
    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        if not is_allowed_admin_client(self.client_address[0]):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "admin UI is only available through Home Assistant Ingress"})
            return
        path = urllib.parse.urlparse(self.path).path
        matches = [(m, r.fullmatch(path), name) for m, r, name in ADMIN_ROUTES]
        matches = [(m, match, name) for m, match, name in matches if match]
        route = next(((match, name) for m, match, name in matches if m == method), None)
        if not route:
            if matches:
                allow = ", ".join(sorted({m for m, _, _ in matches}))
                self.send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method not allowed"}, headers={"Allow": allow})
            else:
                self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if method != "GET":
            # A JSON media type cannot be sent cross-site without a CORS preflight,
            # which this server never approves; this blocks form-based CSRF.
            content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if content_type != "application/json":
                self.send_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "Content-Type must be application/json"})
                return
        match, name = route
        params = {key: urllib.parse.unquote(value) for key, value in match.groupdict().items()}
        try:
            result = getattr(self, "route_" + name)(**params)
        except ApiError as exc:
            self.send_json(exc.status, {"error": exc.message})
        except ValueError as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (MitaReloadError, StateError) as exc:
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
        except Exception as exc:  # Never drop the connection without a response.
            print(f"[web] {method} {path} failed: {exc!r}", flush=True)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})
        else:
            if result is not None:
                status, payload = result
                self.send_json(status, payload)

    # ----- read-only -----
    def route_page(self):
        self.send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", PAGE, headers=ADMIN_PAGE_HEADERS)

    def route_get_state(self):
        return HTTPStatus.OK, {
            "users": [public_user(u) for u in get_users()],
            "settings": admin_settings(get_settings()),
            "outbound_enabled": is_standalone(),
            "sing_box_available": sing_box_available(),
            "sing_box_running": sing_box_available() and SING_BOX.running(),
        }

    def route_get_traffic(self):
        try:
            return HTTPStatus.OK, {"users": get_traffic_view()}
        except (RuntimeError, subprocess.SubprocessError, OSError) as exc:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, str(exc)) from exc

    def route_get_log_db(self):
        return HTTPStatus.OK, {"status": get_log_db_status()}

    def route_get_backup(self):
        return HTTPStatus.OK, {"status": get_backup_status()}

    # ----- users -----
    def route_create_user(self):
        body = self.read_json()
        username = body.get("username", "")
        username = username.strip() if isinstance(username, str) else ""
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("username must contain only A-Z, a-z, 0-9, _, . or -")
        password, generated = parse_password(body)
        with LOCK:
            settings = get_settings()
            users = get_users()
            if any(u["username"] == username for u in users):
                raise ValueError("user already exists")
            previous = copy.deepcopy(users)
            user = {
                "username": username,
                "password": password,
                "allow_private_ip": parse_bool(body, "allow_private_ip", bool(settings.get("default_allow_private_ip", False))),
                "allow_loopback_ip": parse_bool(body, "allow_loopback_ip", bool(settings.get("default_allow_loopback_ip", False))),
                "token": secrets.token_urlsafe(24),
                "legacy_enabled": parse_bool(body, "legacy_enabled", True),
                "device_limit": 0,
                "devices": [],
            }
            users.append(user)
            reload_output = persist_and_reload(USERS_PATH, users, previous)
            result = public_user(user)
        return HTTPStatus.CREATED, {
            "user": result,
            "generated_password": generated or None,
            "mita_reloaded": True,
            "mita_output": reload_output,
        }

    def route_rotate_token(self, username):
        with LOCK:
            users = get_users()
            user = find_user(users, username)
            user["token"] = secrets.token_urlsafe(24)
            write_json(USERS_PATH, users)  # Tokens are not part of the mita config.
            result = public_user(user)
        return HTTPStatus.OK, {"user": result}

    def route_reset_password(self, username):
        body = self.read_json()
        password, generated = parse_password(body)
        with LOCK:
            users = get_users()
            user = find_user(users, username)
            previous = copy.deepcopy(users)
            user["password"] = password
            if user.get("legacy_enabled", True):
                persist_and_reload(USERS_PATH, users, previous)
            else:  # Password is only used by the legacy credential.
                write_json(USERS_PATH, users)
            result = public_user(user)
        return HTTPStatus.OK, {"user": result, "generated_password": generated or None}

    def route_update_user(self, username):
        body = self.read_json()
        with LOCK:
            users = get_users()
            user = find_user(users, username)
            previous = copy.deepcopy(users)
            before = {k: bool(user.get(k, d)) for k, d in
                      (("allow_private_ip", False), ("allow_loopback_ip", False), ("legacy_enabled", True))}
            after = {k: parse_bool(body, k, v) for k, v in before.items()}
            limit = parse_int(body, "device_limit", int(user.get("device_limit", 0) or 0), 0, 100)
            if limit > 0 and len(user.get("devices", [])) > limit:
                raise ValueError("device_limit is lower than the number of registered devices")
            user.update(after)
            user["device_limit"] = limit
            if after != before:  # Access flags and legacy credentials live in the mita config.
                persist_and_reload(USERS_PATH, users, previous)
            else:
                write_json(USERS_PATH, users)
            result = public_user(user)
        return HTTPStatus.OK, {"user": result}

    def route_delete_device(self, username, device_id):
        with LOCK:
            users = get_users()
            user = find_user(users, username)
            devices = user.get("devices", [])
            if not any(d.get("id") == device_id for d in devices):
                raise ApiError(HTTPStatus.NOT_FOUND, "device not found")
            if not user.get("legacy_enabled", True) and len(devices) == 1:
                raise ValueError("enable legacy access before removing the last device")
            previous = copy.deepcopy(users)
            user["devices"] = [d for d in devices if d.get("id") != device_id]
            persist_and_reload(USERS_PATH, users, previous)
            result = public_user(user)
        return HTTPStatus.OK, {"deleted": device_id, "user": result}

    def route_delete_user(self, username):
        with LOCK:
            users = get_users()
            find_user(users, username)
            new_users = [u for u in users if u["username"] != username]
            if not new_users:
                raise ValueError("at least one user must remain")
            reload_output = persist_and_reload(USERS_PATH, new_users, users)
        return HTTPStatus.OK, {"deleted": username, "mita_reloaded": True, "mita_output": reload_output}

    # ----- settings -----
    def route_sing_box(self):
        if not sing_box_available():
            raise ApiError(HTTPStatus.NOT_FOUND, "built-in sing-box is only available in the Linux image")
        body = self.read_json(max_bytes=1024 * 1024)
        with LOCK:
            settings = get_settings()
            previous = settings.get("sing_box")
            outbound = body.get("outbound")
            if outbound is None and body.get("enabled") is False:
                updated = None
                settings.pop("sing_box", None)
            else:
                validate_sing_box_outbound(outbound)
                rule = normalize_egress({"rules": [{
                    "domainNames": body.get("domains", []),
                    "ipRanges": body.get("ip_ranges", []),
                    "action": "DIRECT",
                }]}, destination_limit=16384)["rules"][0]
                domains = list(dict.fromkeys(rule["domainNames"]))
                ip_ranges = list(dict.fromkeys(rule["ipRanges"]))
                if "*" in domains or any(
                    value == "*" or ipaddress.ip_network(value).prefixlen == 0
                    for value in ip_ranges
                ):
                    raise ValueError("enter explicit domains or IP ranges for the managed sing-box rule")
                password = previous["password"] if previous else secrets.token_urlsafe(24)
                updated = {
                    "outbound": outbound, "domains": domains,
                    "ip_ranges": ip_ranges, "password": password,
                }
                settings["sing_box"] = updated
                managed_egress(settings)
            SING_BOX.replace(updated, previous, lambda: write_json(SETTINGS_PATH, settings))
        return HTTPStatus.OK, {"restart_required": True}

    def route_update_settings(self):
        body = self.read_json()
        with LOCK:
            settings = get_settings()
            previous = copy.deepcopy(settings)
            host = body.get("public_mieru_host", settings.get("public_mieru_host", ""))
            host = host.strip() if isinstance(host, str) else ""
            if not host or "/" in host or any(c.isspace() for c in host) or len(host) > 253:
                raise ValueError("invalid public_mieru_host")
            port = parse_int(body, "public_mieru_port", int(settings.get("public_mieru_port", 8443)), 1, 65535)
            base = normalize_base_url(body.get("subscription_base_url", settings.get("subscription_base_url", "")))
            old_log_level = str(settings.get("log_level", "INFO"))
            if "log_level" in body:
                log_level = str(body["log_level"]).upper().strip()
                if log_level not in {"FATAL", "ERROR", "WARN", "INFO", "DEBUG", "TRACE"}:
                    raise ValueError("invalid log_level")
                settings["log_level"] = log_level
            if "postgres_dsn" in body:
                postgres_dsn = body["postgres_dsn"]
                if postgres_dsn is None:
                    postgres_dsn = ""
                if not isinstance(postgres_dsn, str):
                    raise ValueError("postgres_dsn must be a string")
                postgres_dsn = postgres_dsn.strip()
                if len(postgres_dsn) > 4096:
                    raise ValueError("postgres_dsn is too long")
                settings["postgres_dsn"] = postgres_dsn
            if "contact_email" in body:
                settings["contact_email"] = normalize_contact_email(body["contact_email"])

            if "egress" in body:
                if not is_standalone():
                    raise ValueError("outbound is only available on standalone Linux")
                settings["egress"] = normalize_egress(body["egress"])
                managed_egress(settings)
            egress_changed = settings.get("egress") != previous.get("egress")

            settings["public_mieru_host"] = host
            settings["public_mieru_port"] = port
            settings["subscription_base_url"] = base
            reload_output = ""
            if settings.get("log_level", "INFO") != old_log_level:
                reload_output = persist_and_reload(SETTINGS_PATH, settings, previous)
            else:
                write_json(SETTINGS_PATH, settings)

        return HTTPStatus.OK, {
            "settings": admin_settings(settings),
            "restart_required": egress_changed,
            "mita_reloaded": settings.get("log_level", "INFO") != old_log_level,
            "mita_output": reload_output,
        }


class SubscriptionHandler(CommonHandler):
    def do_GET(self):
        try:
            self.handle_subscription()
        except StateError as exc:
            print(f"[web] subscription unavailable: {exc}", flush=True)
            self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "subscription service unavailable"})
        except Exception as exc:
            print(f"[web] subscription request failed: {exc!r}", flush=True)
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    def handle_subscription(self):
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
            self.send_bytes(HTTPStatus.OK, "text/html; charset=utf-8", page,
                            headers=subscription_page_headers(page))
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


class Server(ThreadingHTTPServer):
    # The stdlib default backlog of 5 resets connections during short bursts.
    request_queue_size = 64
    daemon_threads = True


def serve():
    admin_host = "127.0.0.1" if is_standalone() else "0.0.0.0"
    admin = Server((admin_host, ADMIN_PORT), AdminHandler)
    sub = Server(("0.0.0.0", SUB_PORT), SubscriptionHandler)

    if sing_box_available():
        bridge = get_settings().get("sing_box")
        if bridge is not None:
            try:
                SING_BOX.start(bridge)
            except (ValueError, OSError):
                print("[web] built-in sing-box failed to start; check the Outbound settings", flush=True)

    def shutdown_handler(signum, frame):
        threading.Thread(target=admin.shutdown, daemon=True).start()
        threading.Thread(target=sub.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    threading.Thread(target=sub.serve_forever, name="subscription-server", daemon=True).start()
    threading.Thread(target=log_db_worker, name="postgres-log-forwarder", daemon=True).start()
    threading.Thread(target=backup_db_worker, name="postgres-backup-worker", daemon=True).start()
    print(f"[web] admin UI listening on {admin_host}:{ADMIN_PORT}", flush=True)
    print("[web] subscription server listening on 8099", flush=True)
    try:
        admin.serve_forever()
    finally:
        with LOCK:
            SING_BOX.stop()
        admin.server_close()
        sub.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--restore-postgres", metavar="BACKUP", help="latest or a numeric backup ID")
    args = parser.parse_args()
    if args.restore_postgres:
        dsn = os.environ.get("MITA_POSTGRES_DSN", "").strip()
        if not dsn:
            parser.error("set MITA_POSTGRES_DSN to the database containing the backup")
        try:
            backup_id = None if args.restore_postgres == "latest" else int(args.restore_postgres)
            identifier = restore_postgres_backup(dsn, backup_id)
        except (ValueError, OSError, RuntimeError, tarfile.TarError) as exc:
            parser.exit(1, f"Restore failed: {exc}\n")
        except Exception:
            parser.exit(1, "Restore failed while reading PostgreSQL. Check the DSN and table access.\n")
        print(f"Restored PostgreSQL backup {identifier}")
        raise SystemExit(0)
    initialize()
    if not args.init:
        serve()

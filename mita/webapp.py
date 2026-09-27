#!/usr/bin/env python3
import argparse
import html
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import urllib.parse
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_DIR = Path("/data")
OPTIONS_PATH = DATA_DIR / "options.json"
USERS_PATH = DATA_DIR / "users.json"
SETTINGS_PATH = DATA_DIR / "settings.json"
MITA_CONFIG_PATH = DATA_DIR / "mita-server.json"
SUBSCRIPTION_PAGE_PATH = Path("/app/subscription.html")

ADMIN_PORT = 8098
SUB_PORT = 8099
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
HWID_RE = re.compile(r"^[A-Za-z0-9=-]{10,64}$")
DEVICE_ID_RE = re.compile(r"^[A-Fa-f0-9]{16}$")
LOCK = threading.RLock()


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
        }
        write_json(SETTINGS_PATH, settings)

    rebuild_mita_config()


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
mode: rule
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
      - DIRECT

rules:
  - MATCH,PROXY
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


def render_subscription_page(user):
    template = SUBSCRIPTION_PAGE_PATH.read_text(encoding="utf-8")
    subscription_url = public_user(user)["subscription_url"]
    bootstrap = json.dumps(
        {"username": user["username"], "url": subscription_url},
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
            response_headers["x-hwid-not-supported"] = "true"
            if user.get("legacy_enabled", True):
                credential = {
                    "username": user["username"],
                    "password": user["password"],
                    "label": user["username"],
                }
                return user, credential, response_headers, None, None
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

        device = _new_device(user, hwid, headers)
        devices.append(device)
        write_json(USERS_PATH, users)
        ok, output = reload_mita()
        if not ok:
            devices[:] = [d for d in devices if d.get("id") != device["id"]]
            write_json(USERS_PATH, users)
            rebuild_mita_config()
            return None, None, response_headers, HTTPStatus.SERVICE_UNAVAILABLE, output or "failed to register device"

        credential = {
            "username": device["mita_username"],
            "password": device["password"],
            "label": device_display_name(device),
        }
        return user, credential, response_headers, None, None


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mita users</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
body{margin:0;padding:20px;background:Canvas;color:CanvasText}main{max-width:980px;margin:0 auto}
h1{font-size:24px;margin:0 0 6px}.muted{opacity:.65}.card{border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:14px;padding:16px;margin:16px 0}
.tabs{display:flex;gap:8px;margin:18px 0 4px}.tab{background:color-mix(in srgb,CanvasText 10%,Canvas);color:CanvasText}.tab.active{background:#03a9f4;color:white}.tab-panel{display:none}.tab-panel.active{display:block}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}label{display:flex;flex-direction:column;gap:6px;font-size:13px}
input{font:inherit;padding:10px 12px;border:1px solid color-mix(in srgb,CanvasText 22%,transparent);border-radius:9px;background:Canvas}
.check{display:flex;flex-direction:row;align-items:center;gap:8px}button{font:inherit;padding:9px 12px;border:0;border-radius:9px;cursor:pointer;background:#03a9f4;color:white}
button:disabled{opacity:.55;cursor:default}button.secondary{background:color-mix(in srgb,CanvasText 12%,Canvas)}button.danger{background:#d64b4b}.actions{display:flex;gap:8px;flex-wrap:wrap}
.section-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.section-head h2{margin-right:auto}.traffic-pair{white-space:nowrap}.traffic-pair span{display:block}
.device-user{border-top:1px solid color-mix(in srgb,CanvasText 12%,transparent);padding:16px 0}.device-user:first-child{border-top:0}.device-settings{display:flex;gap:12px;align-items:end;flex-wrap:wrap;margin:10px 0}.device-settings label{min-width:150px}.device-list{margin-top:12px}.device-meta{font-size:12px;opacity:.7}.legacy-note{font-size:12px;opacity:.7}
.traffic-parent-row td{padding-top:14px;padding-bottom:14px}.traffic-parent-row td:first-child{white-space:nowrap}.traffic-toggle{display:inline-flex;align-items:center;gap:7px;background:transparent;color:CanvasText;padding:0;border:0;font-weight:700}.traffic-toggle .chevron{display:inline-block;width:14px;transition:transform .15s ease}.traffic-toggle.open .chevron{transform:rotate(90deg)}.traffic-count{font-size:12px;opacity:.55;font-weight:500}.child-row td{background:color-mix(in srgb,CanvasText 3%,Canvas)}.child-row td:first-child{padding-left:36px}.traffic-spacer td{height:18px;padding:0;border:0;background:Canvas}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent)}
code{font-size:12px;word-break:break-all}@media(max-width:700px){.grid{grid-template-columns:1fr}thead{display:none}tr{display:block;padding:10px 0}td{display:block;border:0;padding:5px 0}.traffic-table thead{display:table-header-group}.traffic-table tr{display:table-row}.traffic-table td{display:table-cell;border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent);padding:9px 6px}.traffic-table{font-size:12px}.traffic-table th{padding:9px 6px}}
</style>
</head>
<body><main>
<h1>Mita</h1>
<div class="muted">Admin UI is available through Home Assistant Ingress.</div>
<nav class="tabs">
<button class="tab active" data-tab="usersTab">Users</button>
<button class="tab" data-tab="devicesTab">Devices</button>
<button class="tab" data-tab="trafficTab">Traffic</button>
</nav>

<div id="usersTab" class="tab-panel active">
<section class="card"><h2>Add user</h2><div class="grid">
<label>Username<input id="username" autocomplete="off"></label>
<label>Password<input id="password" type="password" placeholder="Leave empty to generate"></label>
<label class="check"><input id="private" type="checkbox">Allow private/LAN IPs</label>
<label class="check"><input id="loopback" type="checkbox">Allow loopback</label>
</div><p><button id="add">Add user</button></p></section>

<section class="card"><h2>Public settings</h2><div class="grid">
<label>Mieru hostname<input id="host"></label>
<label>Mieru public TCP port<input id="port" type="number" min="1" max="65535"></label>
<label style="grid-column:1/-1">Subscription base URL<input id="base"></label>
</div><p><button id="saveSettings">Save settings</button></p></section>

<section class="card"><h2>Users</h2><table>
<thead><tr><th>User</th><th>Access</th><th>Devices</th><th>Subscription</th><th></th></tr></thead>
<tbody id="users"></tbody>
</table></section>
</div>

<div id="devicesTab" class="tab-panel">
<section class="card">
<div class="section-head"><h2>Devices</h2></div>
<div class="muted">The same subscription link can register separate HWID devices. Legacy access stays enabled until you turn it off for a user.</div>
<div id="deviceUsers"></div>
</section>
</div>

<div id="trafficTab" class="tab-panel">
<section class="card">
<div class="section-head"><h2>Traffic</h2><button id="refreshTraffic" class="secondary">Refresh</button></div>
<div id="trafficUpdated" class="muted">Traffic counters are reported by mita per authenticated user.</div>
<table class="traffic-table">
<thead><tr><th>User</th><th>Last active</th><th>24 hours</th><th>7 days</th><th>30 days</th></tr></thead>
<tbody id="traffic"></tbody>
</table>
</section>
</div>

<div id="msg" class="muted"></div>
</main>
<script>
const $=s=>document.querySelector(s);
const api=p=>new URL(p.replace(/^\//,''),location.href.endsWith('/')?location.href:location.href+'/').toString();
async function request(path,opts={}){
  const r=await fetch(api(path),{headers:{'Content-Type':'application/json'},...opts});
  const data=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(data.error||r.statusText);
  return data;
}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function message(s){$('#msg').textContent=s;setTimeout(()=>{$('#msg').textContent=''},4000)}
let trafficTimer=null;
const openTrafficUsers=new Set();

function setTab(id){
  document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.tab===id));
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('active',p.id===id));
  if(trafficTimer){clearInterval(trafficTimer);trafficTimer=null}
  if(id==='trafficTab'){
    loadTraffic();
    trafficTimer=setInterval(loadTraffic,30000);
  }
}

function trafficPair(down,up){
  return '<div class="traffic-pair"><span>↓ '+esc(down||'-')+'</span><span>↑ '+esc(up||'-')+'</span></div>';
}

const dateTimeFormatter=new Intl.DateTimeFormat(navigator.languages||navigator.language,{
  dateStyle:'medium',
  timeStyle:'medium'
});
const timeFormatter=new Intl.DateTimeFormat(navigator.languages||navigator.language,{
  timeStyle:'medium'
});

function displayTime(value){
  if(!value||value==='-')return '-';
  const d=new Date(value);
  return Number.isNaN(d.getTime())?value:dateTimeFormatter.format(d);
}

function displayClock(value=new Date()){
  const d=value instanceof Date?value:new Date(value);
  return Number.isNaN(d.getTime())?'-':timeFormatter.format(d);
}

function renderDevices(users){
  $('#deviceUsers').innerHTML=users.map(u=>{
    const rows=u.devices.length?'<table class="device-list"><thead><tr><th>Device</th><th>HWID</th><th>Last subscription refresh</th><th></th></tr></thead><tbody>'+
      u.devices.map(d=>'<tr>'+
        '<td><strong>'+esc(d.name)+'</strong><div class="device-meta">'+esc([d.os,d.os_version].filter(Boolean).join(' '))+'</div></td>'+
        '<td><code>'+esc(d.hwid)+'</code></td>'+
        '<td>'+esc(displayTime(d.last_seen))+'</td>'+
        '<td><button class="danger" data-remove-device="'+esc(d.id)+'" data-user="'+esc(u.username)+'">Remove</button></td></tr>'
      ).join('')+'</tbody></table>':'<div class="muted" style="margin-top:12px">No HWID devices registered yet. Refresh the subscription from a supported client to register one.</div>';
    return '<div class="device-user" data-device-card="'+esc(u.username)+'">'+
      '<div class="section-head"><h3>'+esc(u.username)+'</h3><span class="muted">'+u.devices.length+' registered</span></div>'+
      '<div class="device-settings">'+
        '<label>Device limit<input data-device-limit type="number" min="0" max="100" value="'+esc(u.device_limit)+'"><span class="legacy-note">0 = unlimited</span></label>'+
        '<label class="check"><input data-legacy type="checkbox" '+(u.legacy_enabled?'checked':'')+'>Legacy credential enabled</label>'+
        '<button data-save-devices="'+esc(u.username)+'">Save</button>'+
      '</div>'+
      '<div class="legacy-note">Legacy mode keeps already imported profiles working. Disable it only after all needed devices have appeared here.</div>'+
      rows+'</div>';
  }).join('');
  document.querySelectorAll('[data-save-devices]').forEach(b=>b.onclick=()=>saveDeviceSettings(b.dataset.saveDevices));
  document.querySelectorAll('[data-remove-device]').forEach(b=>b.onclick=()=>removeDevice(b.dataset.user,b.dataset.removeDevice));
}

async function saveDeviceSettings(username){
  const card=[...document.querySelectorAll('[data-device-card]')].find(x=>x.dataset.deviceCard===username);
  if(!card)return;
  try{
    await request('api/users/'+encodeURIComponent(username)+'/device-settings',{method:'PUT',body:JSON.stringify({
      device_limit:Number(card.querySelector('[data-device-limit]').value||0),
      legacy_enabled:card.querySelector('[data-legacy]').checked
    })});
    await load();message('Device settings saved');
  }catch(e){alert(e.message)}
}

async function removeDevice(username,deviceId){
  if(!confirm('Remove this device? Its current device credential will stop working.'))return;
  try{
    await request('api/users/'+encodeURIComponent(username)+'/devices/'+encodeURIComponent(deviceId),{method:'DELETE'});
    await load();message('Device removed');
  }catch(e){alert(e.message)}
}

function toggleTrafficUser(username){
  if(openTrafficUsers.has(username))openTrafficUsers.delete(username);
  else openTrafficUsers.add(username);

  document.querySelectorAll('[data-traffic-child]').forEach(row=>{
    if(row.dataset.trafficChild===username)row.hidden=!openTrafficUsers.has(username);
  });
  document.querySelectorAll('[data-traffic-toggle]').forEach(button=>{
    if(button.dataset.trafficToggle===username){
      const open=openTrafficUsers.has(username);
      button.classList.toggle('open',open);
      button.setAttribute('aria-expanded',open?'true':'false');
    }
  });
}

async function loadTraffic(){
  const button=$('#refreshTraffic');
  button.disabled=true;
  try{
    const data=await request('api/traffic');
    $('#traffic').innerHTML=data.users.length?data.users.map((u,index)=>{
      const username=String(u.username);
      const open=openTrafficUsers.has(username);
      const childCount=(u.children||[]).length;
      const spacer=index?'<tr class="traffic-spacer"><td colspan="5"></td></tr>':'';
      const parent='<tr class="traffic-parent-row">'+
        '<td><button class="traffic-toggle '+(open?'open':'')+'" data-traffic-toggle="'+esc(username)+'" aria-expanded="'+(open?'true':'false')+'">'+
          '<span class="chevron">›</span><span>'+esc(username)+'</span><span class="traffic-count">'+childCount+'</span>'+
        '</button></td>'+
        '<td>'+esc(displayTime(u.last_active))+'</td>'+
        '<td>'+trafficPair(u.day_down,u.day_up)+'</td>'+
        '<td>'+trafficPair(u.week_down,u.week_up)+'</td>'+
        '<td>'+trafficPair(u.month_down,u.month_up)+'</td></tr>';
      const children=(u.children||[]).map(d=>'<tr class="child-row" data-traffic-child="'+esc(username)+'" '+(open?'':'hidden')+'>'+
        '<td>'+esc(d.name)+(d.kind==='legacy'?' <span class="muted">(legacy)</span>':'')+'</td>'+
        '<td>'+esc(displayTime(d.last_active))+'</td>'+
        '<td>'+trafficPair(d.day_down,d.day_up)+'</td>'+
        '<td>'+trafficPair(d.week_down,d.week_up)+'</td>'+
        '<td>'+trafficPair(d.month_down,d.month_up)+'</td></tr>').join('');
      return spacer+parent+children;
    }).join(''):'<tr><td colspan="5" class="muted">No traffic data yet</td></tr>';

    document.querySelectorAll('[data-traffic-toggle]').forEach(b=>{
      b.onclick=()=>toggleTrafficUser(b.dataset.trafficToggle);
    });
    $('#trafficUpdated').textContent='Updated '+displayClock()+'. Rolling counters from mita.';
  }catch(e){
    $('#trafficUpdated').textContent='Unable to load traffic: '+e.message;
  }finally{
    button.disabled=false;
  }
}

async function load(){
  const data=await request('api/state');
  $('#host').value=data.settings.public_mieru_host||'';
  $('#port').value=data.settings.public_mieru_port||8443;
  $('#base').value=data.settings.subscription_base_url||'';
  $('#private').checked=!!data.settings.default_allow_private_ip;
  $('#loopback').checked=!!data.settings.default_allow_loopback_ip;
  $('#users').innerHTML=data.users.map(u=>'<tr>'+
    '<td><strong>'+esc(u.username)+'</strong></td>'+
    '<td>'+(u.allow_private_ip?'LAN ':'')+(u.allow_loopback_ip?'Loopback':'')+'</td>'+
    '<td>'+u.devices.length+(u.device_limit?' / '+u.device_limit:'')+(u.legacy_enabled?' + legacy':'')+'</td>'+
    '<td><code>'+esc(u.subscription_url)+'</code><div class="actions" style="margin-top:6px">'+
    '<button class="secondary" data-copy="'+esc(u.subscription_url)+'">Copy link</button>'+
    '<button class="secondary" data-rotate="'+esc(u.username)+'">Rotate token</button></div></td>'+
    '<td><button class="danger" data-delete="'+esc(u.username)+'">Delete</button></td></tr>'
  ).join('');
  document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>copyLink(b.dataset.copy));
  document.querySelectorAll('[data-rotate]').forEach(b=>b.onclick=()=>rotate(b.dataset.rotate));
  document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>removeUser(b.dataset.delete));
  renderDevices(data.users);
}
async function copyLink(v){await navigator.clipboard.writeText(v);message('Subscription link copied')}
async function rotate(username){await request('api/users/'+encodeURIComponent(username)+'/rotate-token',{method:'POST',body:'{}'});await load();message('Token rotated')}
async function removeUser(username){if(!confirm('Delete '+username+'?'))return;await request('api/users/'+encodeURIComponent(username),{method:'DELETE'});await load();message('User deleted')}
$('#add').onclick=async()=>{
  try{
    const data=await request('api/users',{method:'POST',body:JSON.stringify({
      username:$('#username').value.trim(),password:$('#password').value,
      allow_private_ip:$('#private').checked,allow_loopback_ip:$('#loopback').checked
    })});
    $('#username').value='';$('#password').value='';await load();
    message(data.generated_password?'User added. Generated password: '+data.generated_password:'User added');
  }catch(e){alert(e.message)}
};
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>setTab(b.dataset.tab));
$('#refreshTraffic').onclick=loadTraffic;

$('#saveSettings').onclick=async()=>{
  try{
    await request('api/settings',{method:'PUT',body:JSON.stringify({
      public_mieru_host:$('#host').value.trim(),public_mieru_port:Number($('#port').value),
      subscription_base_url:$('#base').value.trim()
    })});
    await load();message('Settings saved');
  }catch(e){alert(e.message)}
};
load().catch(e=>alert(e.message));
</script>
</body></html>
"""


class CommonHandler(BaseHTTPRequestHandler):
    server_version = "mita-ha/1"

    def log_message(self, fmt, *args):
        print("[web] %s - %s" % (self.address_string(), fmt % args), flush=True)

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
        length = int(self.headers.get("Content-Length", "0"))
        if length > 65536:
            raise ValueError("request too large")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))


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
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/users":
            try:
                body = self.read_json()
                username = str(body.get("username", "")).strip()
                if not USERNAME_RE.fullmatch(username):
                    raise ValueError("username must contain only A-Z, a-z, 0-9, _, . or -")
                users = get_users()
                if any(u["username"] == username for u in users):
                    raise ValueError("user already exists")
                password = str(body.get("password", ""))
                generated = ""
                if not password:
                    password = secrets.token_urlsafe(24)
                    generated = password
                user = {
                    "username": username,
                    "password": password,
                    "allow_private_ip": bool(body.get("allow_private_ip", False)),
                    "allow_loopback_ip": bool(body.get("allow_loopback_ip", False)),
                    "token": secrets.token_urlsafe(24),
                    "legacy_enabled": True,
                    "device_limit": 0,
                    "devices": [],
                }
                users.append(user)
                write_json(USERS_PATH, users)
                ok, output = reload_mita()
                self.send_json(HTTPStatus.CREATED, {
                    "user": public_user(user),
                    "generated_password": generated or None,
                    "mita_reloaded": ok,
                    "mita_output": output,
                })
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        m = re.fullmatch(r"/api/users/([^/]+)/rotate-token", path)
        if m:
            username = urllib.parse.unquote(m.group(1))
            users = get_users()
            for user in users:
                if user["username"] == username:
                    user["token"] = secrets.token_urlsafe(24)
                    write_json(USERS_PATH, users)
                    self.send_json(HTTPStatus.OK, {"user": public_user(user)})
                    return
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "user not found"})
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
                    if not legacy_enabled and not user.get("devices"):
                        raise ValueError("register at least one HWID device before disabling legacy access")
                    if limit > 0 and len(user.get("devices", [])) > limit:
                        raise ValueError("device_limit is lower than the number of registered devices")

                    old_legacy = bool(user.get("legacy_enabled", True))
                    old_limit = int(user.get("device_limit", 0) or 0)
                    user["legacy_enabled"] = legacy_enabled
                    user["device_limit"] = limit
                    write_json(USERS_PATH, users)

                    if old_legacy != legacy_enabled:
                        ok, output = reload_mita()
                        if not ok:
                            user["legacy_enabled"] = old_legacy
                            user["device_limit"] = old_limit
                            write_json(USERS_PATH, users)
                            rebuild_mita_config()
                            self.send_json(
                                HTTPStatus.SERVICE_UNAVAILABLE,
                                {"error": output or "mita reload failed"},
                            )
                            return

                    self.send_json(HTTPStatus.OK, {"user": public_user(user)})
            except (ValueError, json.JSONDecodeError) as exc:
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        if path != "/api/settings":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            body = self.read_json()
            settings = get_settings()
            host = str(body.get("public_mieru_host", settings.get("public_mieru_host", ""))).strip()
            if not host or "/" in host or " " in host:
                raise ValueError("invalid public_mieru_host")
            port = int(body.get("public_mieru_port", settings.get("public_mieru_port", 8443)))
            if port < 1 or port > 65535:
                raise ValueError("invalid public_mieru_port")
            base = normalize_base_url(body.get("subscription_base_url", settings.get("subscription_base_url", "")))
            settings["public_mieru_host"] = host
            settings["public_mieru_port"] = port
            settings["subscription_base_url"] = base
            write_json(SETTINGS_PATH, settings)
            self.send_json(HTTPStatus.OK, {"settings": settings})
        except (ValueError, json.JSONDecodeError) as exc:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

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
                original_devices = list(devices)
                user["devices"] = [d for d in devices if d.get("id") != device_id]
                if not user.get("legacy_enabled", True) and not user["devices"]:
                    self.send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "enable legacy access before removing the last device"},
                    )
                    return
                write_json(USERS_PATH, users)
                ok, output = reload_mita()
                if not ok:
                    user["devices"] = original_devices
                    write_json(USERS_PATH, users)
                    rebuild_mita_config()
                    self.send_json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": output or "mita reload failed"},
                    )
                    return
                self.send_json(HTTPStatus.OK, {"deleted": device_id, "user": public_user(user)})
            return

        m = re.fullmatch(r"/api/users/([^/]+)", path)
        if not m:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        username = urllib.parse.unquote(m.group(1))
        users = get_users()
        new_users = [u for u in users if u["username"] != username]
        if len(new_users) == len(users):
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "user not found"})
            return
        if not new_users:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": "at least one user must remain"})
            return
        write_json(USERS_PATH, new_users)
        ok, output = reload_mita()
        self.send_json(HTTPStatus.OK, {"deleted": username, "mita_reloaded": ok, "mita_output": output})


class SubscriptionHandler(CommonHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
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
                page = render_subscription_page(user)
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

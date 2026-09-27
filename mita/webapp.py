#!/usr/bin/env python3
import argparse
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


def yaml_q(value):
    return json.dumps(str(value), ensure_ascii=False)


def subscription_yaml(user):
    settings = get_settings()
    name = "Mieru " + user["username"]
    host = settings["public_mieru_host"]
    port = int(settings["public_mieru_port"])
    username = user["username"]
    password = user["password"]

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


def public_user(user):
    base = get_settings().get("subscription_base_url", "").rstrip("/")
    return {
        "username": user["username"],
        "allow_private_ip": bool(user.get("allow_private_ip", False)),
        "allow_loopback_ip": bool(user.get("allow_loopback_ip", False)),
        "subscription_url": base + "/sub/" + user["token"],
    }


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
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent)}
code{font-size:12px;word-break:break-all}@media(max-width:700px){.grid{grid-template-columns:1fr}thead{display:none}tr{display:block;padding:10px 0}td{display:block;border:0;padding:5px 0}.traffic-table thead{display:table-header-group}.traffic-table tr{display:table-row}.traffic-table td{display:table-cell;border-bottom:1px solid color-mix(in srgb,CanvasText 12%,transparent);padding:9px 6px}.traffic-table{font-size:12px}.traffic-table th{padding:9px 6px}}
</style>
</head>
<body><main>
<h1>Mita</h1>
<div class="muted">Admin UI is available through Home Assistant Ingress.</div>
<nav class="tabs">
<button class="tab active" data-tab="usersTab">Users</button>
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
<thead><tr><th>User</th><th>Access</th><th>Subscription</th><th></th></tr></thead>
<tbody id="users"></tbody>
</table></section>
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

async function loadTraffic(){
  const button=$('#refreshTraffic');
  button.disabled=true;
  try{
    const data=await request('api/traffic');
    $('#traffic').innerHTML=data.users.length?data.users.map(u=>'<tr>'+
      '<td><strong>'+esc(u.username)+'</strong></td>'+
      '<td>'+esc(u.last_active||'-')+'</td>'+
      '<td>'+trafficPair(u.day_down,u.day_up)+'</td>'+
      '<td>'+trafficPair(u.week_down,u.week_up)+'</td>'+
      '<td>'+trafficPair(u.month_down,u.month_up)+'</td></tr>'
    ).join(''):'<tr><td colspan="5" class="muted">No traffic data yet</td></tr>';
    $('#trafficUpdated').textContent='Updated '+new Date().toLocaleTimeString()+'. Rolling counters from mita.';
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
    '<td><code>'+esc(u.subscription_url)+'</code><div class="actions" style="margin-top:6px">'+
    '<button class="secondary" data-copy="'+esc(u.subscription_url)+'">Copy link</button>'+
    '<button class="secondary" data-rotate="'+esc(u.username)+'">Rotate token</button></div></td>'+
    '<td><button class="danger" data-delete="'+esc(u.username)+'">Delete</button></td></tr>'
  ).join('');
  document.querySelectorAll('[data-copy]').forEach(b=>b.onclick=()=>copyLink(b.dataset.copy));
  document.querySelectorAll('[data-rotate]').forEach(b=>b.onclick=()=>rotate(b.dataset.rotate));
  document.querySelectorAll('[data-delete]').forEach(b=>b.onclick=()=>removeUser(b.dataset.delete));
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

    def send_bytes(self, status, content_type, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, status, value):
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(value, ensure_ascii=False))

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
                self.send_json(HTTPStatus.OK, {"users": get_mita_user_traffic()})
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
        if urllib.parse.urlparse(self.path).path != "/api/settings":
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
        path = urllib.parse.urlparse(self.path).path
        m = re.fullmatch(r"/sub/([A-Za-z0-9_-]{16,})", path)
        if not m:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        token = m.group(1)
        user = next((u for u in get_users() if secrets.compare_digest(str(u.get("token", "")), token)), None)
        if not user:
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "subscription not found"})
            return
        self.send_bytes(HTTPStatus.OK, "text/yaml; charset=utf-8", subscription_yaml(user))


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

"""Render a router config like the one Mita runs, for `awgm-box check` in CI.

Covers every outbound kind the panel accepts: share links (incl. XHTTP) and
native JSON (mieru, AmneziaWG endpoint).

Usage: python3 tests/render_router_config.py OUT_DIR

Writes OUT_DIR/config.json plus the local rule-set file it references (under
OUT_DIR/geo-cache/rules). The config comes from webapp.sing_box_config() with
outbounds created by webapp's own share-link parser, so CI checks exactly what
the admin panel would hand to the bundled sing-box fork. Mount OUT_DIR at the
same absolute path inside the container, because the rule-set path is absolute.
"""
import base64
import importlib.util
import json
import sys
import urllib.parse
import uuid
from pathlib import Path

WEBAPP = Path(__file__).resolve().parents[1] / "mita" / "webapp.py"

# A syntactically valid REALITY public key (32 bytes, base64url without padding).
REALITY_KEY = base64.urlsafe_b64encode(bytes(range(1, 33))).decode().rstrip("=")
UUID = str(uuid.UUID(int=0x11111111111141118111111111111111))

SHARE_LINKS = {
    "hy2": "hysteria2://pass@hy.example:8443/?sni=hy.example&obfs=salamander&obfs-password=ob#hy2",
    "reality": (f"vless://{UUID}@reality.example:443?security=reality&sni=www.example.com"
                f"&fp=chrome&pbk={REALITY_KEY}&sid=ab12&flow=xtls-rprx-vision&type=tcp#reality"),
    "vless-ws": f"vless://{UUID}@ws.example:443?security=tls&sni=ws.example&type=ws&path=%2Fws&host=cdn.example#ws",
    "vless-grpc": f"vless://{UUID}@grpc.example:443?security=tls&sni=grpc.example&type=grpc&serviceName=gs#grpc",
    "trojan": "trojan://secret@trojan.example:443?sni=trojan.example&type=ws&path=%2Ft#trojan",
    "ss": "ss://" + base64.urlsafe_b64encode(b"chacha20-ietf-poly1305:sspass").decode().rstrip("=")
          + "@ss.example:8388#ss",
    "tuic": f"tuic://{UUID}:tpass@tuic.example:443?congestion_control=bbr&alpn=h3&sni=tuic.example#tuic",
    "vmess": "vmess://" + base64.b64encode(json.dumps({
        "v": "2", "ps": "vmess", "add": "vmess.example", "port": "443", "id": UUID, "aid": "0",
        "net": "ws", "host": "cdn.example", "path": "/vm", "tls": "tls", "sni": "vmess.example",
    }).encode()).decode(),
    "socks": "socks5://user:pass@socks.example:1080#socks",
    "xhttp": (f"vless://{UUID}@xhttp.example:443?security=tls&sni=xhttp.example&type=xhttp&path=%2Fx&mode=auto"
              "&extra=" + urllib.parse.quote(json.dumps({"xmux": {"maxConcurrency": "16-32"},
                                                       "xPaddingBytes": "100-1000"})) + "#xhttp"),
}

# Native awgm-box JSON for types without share links: a mieru outbound and an
# AmneziaWG endpoint (it must land in "endpoints", with the userspace stack).
WG_KEY = base64.b64encode(bytes(range(32, 64))).decode()
NATIVE_OUTBOUNDS = {
    "mieru": {"type": "mieru", "server": "mieru.example", "server_port": 2022, "transport": "TCP",
              "username": "user", "password": "pass"},
    "awg": {"type": "awg", "private_key": WG_KEY, "address": ["10.8.0.2/32"], "jc": 4, "jmin": 40, "jmax": 70,
            "peers": [{"address": "awg.example", "port": 51820, "public_key": WG_KEY,
                       "allowed_ips": ["0.0.0.0/0", "::/0"]}]},
}


def load_webapp():
    spec = importlib.util.spec_from_file_location("mita_webapp_render", WEBAPP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render(out_dir):
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    app = load_webapp()
    app.DATA_DIR = out_dir  # rule-set files land in out_dir/geo-cache/rules

    outbounds = []
    for index, (name, link) in enumerate({**SHARE_LINKS, **NATIVE_OUTBOUNDS}.items()):
        outbound, _ = app.parse_outbound_config(link)
        outbounds.append({"name": name, "outbound": outbound, "port": app.SING_BOX_FIRST_PORT + index,
                          "password": f"local-{name}"})

    # Stored the way the admin panel stores it: under the normalized list URL.
    category = app.normalize_geo_urls(["geosite:ci-example"])[0]
    rule_set = {"version": 2, "rules": [
        {"domain": ["exact.example"], "domain_suffix": ["suffix.example"],
         "domain_keyword": ["keyword"], "domain_regex": ["^cdn[0-9]+\\.example$"]},
        {"ip_cidr": ["198.51.100.0/24", "2001:db8::/32"]},
    ]}
    path, digest = app.write_geo_rule_set(rule_set)

    settings = {
        "router_password": "router-secret",
        "outbounds": outbounds,
        "egress": {"proxies": [], "rules": [
            {"name": "Russia", "domainNames": ["ru", "su", "xn--p1ai"], "ipRanges": ["203.0.113.0/24"],
             "geoUrls": [category], "action": "DIRECT"},
            {"name": "Ads", "domainNames": ["ads.example"], "action": "REJECT"},
            {"name": "Streaming", "domainNames": ["video.example"], "action": "PROXY", "proxyNames": ["hy2"]},
            {"name": "Work", "ipRanges": ["192.0.2.0/24"], "action": "PROXY", "proxyNames": ["reality"]},
        ]},
        "geo_snapshots": {category: {"source": "json", "sha256": digest, "file": path.name,
                                     **app.rule_set_counts(rule_set)}},
        "routing_final": "trojan",
        "postgres_dsn": "postgresql://mita:secret@db.example:5432/mita?sslmode=require",
        "postgres_outbound": "vless-ws",
    }
    config = app.sing_box_config(settings)
    (out_dir / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: render_router_config.py OUT_DIR")
    rendered = render(sys.argv[1])
    print(f"rendered {len(rendered['inbounds'])} inbounds, {len(rendered['outbounds'])} outbounds, "
          f"{len(rendered['route']['rules'])} route rules")

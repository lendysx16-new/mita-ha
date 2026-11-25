"""Render a representative Hysteria ACL config for its real v2.14 parser."""
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("hysteria_ci", ROOT / "mita" / "webapp.py")
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)

settings = {
    "hysteria2_port": 8443,
    "hysteria2_auth_secret": "ci-auth-secret",
    "hysteria2_stats_secret": "ci-stats-secret",
    "routing_final": "block",
    "router_password": "ci-router-password",
}
users = [
    {"username": "alice", "password": "ci-password", "legacy_enabled": True,
     "allow_private_ip": False, "devices": []},
    {"username": "bob", "password": "ci-password", "legacy_enabled": True,
     "allow_private_ip": True, "devices": []},
]

destination = Path(sys.argv[1])
destination.mkdir(parents=True, exist_ok=True)
(destination / "server.yaml").write_text(
    json.dumps(app.hysteria2_config(settings, users), indent=2) + "\n", encoding="utf-8"
)

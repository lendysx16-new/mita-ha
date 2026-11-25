"""The Mita service records applied routing only after the daemon is running."""
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "mita_daemon_test", Path(__file__).resolve().parents[1] / "mita" / "daemon.py"
)
daemon = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daemon)


class DaemonTests(unittest.TestCase):
    def test_applied_routing_marker_uses_started_egress(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            egress = {"proxies": [{"name": "router"}, {"name": "nl"}], "rules": []}
            (path / "mita-server.json").write_text(json.dumps({"egress": egress}))
            with mock.patch.object(daemon, "DATA", path), \
                 mock.patch.object(daemon, "APPLIED", path / "mita-applied-outbounds.json"):
                daemon.record_applied_config()
            marker = json.loads((path / "mita-applied-outbounds.json").read_text())
            self.assertEqual(marker["outbounds"], ["nl"])
            self.assertTrue(marker["router"])
            self.assertEqual(marker["egress"], hashlib.sha256(
                json.dumps(egress, sort_keys=True).encode()).hexdigest())


if __name__ == "__main__":
    unittest.main()

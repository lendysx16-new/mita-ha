"""Regression tests for the Mita Home Assistant API and subscription state."""
import contextlib
import http.client
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "mita_webapp", Path(__file__).resolve().parents[1] / "mita" / "webapp.py"
)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class AdminAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original_paths = (app.DATA_DIR, app.USERS_PATH, app.SETTINGS_PATH, app.MITA_CONFIG_PATH)
        app.DATA_DIR = Path(self.temp.name)
        app.USERS_PATH = app.DATA_DIR / "users.json"
        app.SETTINGS_PATH = app.DATA_DIR / "settings.json"
        app.MITA_CONFIG_PATH = app.DATA_DIR / "mita-server.json"
        self.addCleanup(self.restore_paths)

        app.write_json(app.USERS_PATH, [{
            "username": "first", "password": "secret",
            "allow_private_ip": False, "allow_loopback_ip": False,
            "token": "A" * 32, "legacy_enabled": True,
            "device_limit": 0, "devices": [],
        }])
        app.write_json(app.SETTINGS_PATH, {
            "public_mieru_host": "vpn.example.com",
            "public_mieru_port": 8443,
            "subscription_base_url": "https://vpn.example.com",
            "log_level": "INFO",
            "prefer_ipv4": True,
            "postgres_dsn": "",
        })

        self.reload_patch = mock.patch.object(app, "reload_mita", return_value=(True, ""))
        self.reload_mock = self.reload_patch.start()
        self.addCleanup(self.reload_patch.stop)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.AdminHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)

    def restore_paths(self):
        app.DATA_DIR, app.USERS_PATH, app.SETTINGS_PATH, app.MITA_CONFIG_PATH = self.original_paths

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method, path, body=None, raw=False, headers=None):
        if body is not None and not raw:
            body = json.dumps(body)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {"Content-Type": "application/json"})
            response = connection.getresponse()
            payload = response.read()
            return response.status, json.loads(payload) if payload else {}
        finally:
            connection.close()

    def test_add_user_and_generate_password(self):
        status, data = self.call("POST", "/api/users", {"username": "second"})
        self.assertEqual(status, 201)
        self.assertTrue(data["generated_password"])
        self.assertEqual(len(app.get_users()), 2)
        self.assertTrue(data["mita_reloaded"])

    def test_failed_add_restores_user_database(self):
        self.reload_mock.side_effect = [(False, "reload rejected"), (True, "restored")]
        status, data = self.call("POST", "/api/users", {"username": "second"})
        self.assertEqual(status, 503)
        self.assertIn("reload rejected", data["error"])
        self.assertEqual([u["username"] for u in app.get_users()], ["first"])
        self.assertEqual(self.reload_mock.call_count, 2)

    def test_failed_delete_restores_user_database(self):
        self.call("POST", "/api/users", {"username": "second"})
        self.reload_mock.reset_mock(side_effect=True)
        self.reload_mock.side_effect = [(False, "reload rejected"), (True, "restored")]
        status, _ = self.call("DELETE", "/api/users/second")
        self.assertEqual(status, 503)
        self.assertEqual(len(app.get_users()), 2)

    def test_cannot_remove_final_user(self):
        status, _ = self.call("DELETE", "/api/users/first")
        self.assertEqual(status, 400)

    def test_failed_settings_reload_restores_previous_settings(self):
        self.reload_mock.side_effect = [(False, "reload rejected"), (True, "restored")]
        status, data = self.call("PUT", "/api/settings", {"log_level": "DEBUG"})
        self.assertEqual(status, 503)
        self.assertIn("reload rejected", data["error"])
        self.assertEqual(app.get_settings()["log_level"], "INFO")

    def test_settings_without_log_change_does_not_reload(self):
        status, _ = self.call("PUT", "/api/settings", {"public_mieru_port": 443})
        self.assertEqual(status, 200)
        self.assertEqual(app.get_settings()["public_mieru_port"], 443)
        self.reload_mock.assert_not_called()

    def test_invalid_payloads_return_400(self):
        for body in ('[]', '"string"', '{"broken"', 'null'):
            with self.subTest(body=body):
                status, _ = self.call("POST", "/api/users", body, raw=True)
                self.assertEqual(status, 400)
        status, _ = self.call("POST", "/api/users", '{}', raw=True, headers={"Content-Length": "-1"})
        self.assertEqual(status, 400)

    def test_concurrent_user_creations_are_not_lost(self):
        def create(i):
            return self.call("POST", "/api/users", {"username": f"user{i}"})[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(create, range(12)))
        self.assertEqual(statuses, [201] * 12)
        self.assertEqual(len(app.get_users()), 13)

    def test_rotate_token(self):
        status, data = self.call("POST", "/api/users/first/rotate-token", {})
        self.assertEqual(status, 200)
        self.assertNotIn("/" + "A" * 32, data["user"]["subscription_url"])

    def test_device_settings_reload_rollback(self):
        self.reload_mock.side_effect = [(False, "reload rejected"), (True, "restored")]
        status, _ = self.call("PUT", "/api/users/first/device-settings", {
            "legacy_enabled": False, "device_limit": 2
        })
        self.assertEqual(status, 503)
        self.assertTrue(app.get_users()[0]["legacy_enabled"])
        self.assertEqual(app.get_users()[0]["device_limit"], 0)


class SubscriptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.original_paths = (app.DATA_DIR, app.USERS_PATH, app.SETTINGS_PATH, app.MITA_CONFIG_PATH)
        app.DATA_DIR = Path(self.temp.name)
        app.USERS_PATH = app.DATA_DIR / "users.json"
        app.SETTINGS_PATH = app.DATA_DIR / "settings.json"
        app.MITA_CONFIG_PATH = app.DATA_DIR / "mita-server.json"
        self.addCleanup(self.restore_paths)
        app.write_json(app.USERS_PATH, [{
            "username": "first", "password": "secret",
            "allow_private_ip": False, "allow_loopback_ip": False,
            "token": "T" * 32, "legacy_enabled": True,
            "device_limit": 1, "devices": [],
        }])
        app.write_json(app.SETTINGS_PATH, {
            "public_mieru_host": "vpn.example.com",
            "public_mieru_port": 8443,
            "subscription_base_url": "https://vpn.example.com",
            "log_level": "INFO",
        })

    def restore_paths(self):
        app.DATA_DIR, app.USERS_PATH, app.SETTINGS_PATH, app.MITA_CONFIG_PATH = self.original_paths

    def test_device_creation_and_limit(self):
        with mock.patch.object(app, "reload_mita", return_value=(True, "")) as reload_mock:
            user, credential, _, status, _ = app.resolve_subscription(
                "T" * 32, {"x-hwid": "device-test-123", "x-device-os": "iOS"}
            )
            self.assertIsNone(status)
            self.assertTrue(credential["username"].startswith("dev_"))
            self.assertEqual(len(app.get_users()[0]["devices"]), 1)
            self.assertEqual(reload_mock.call_count, 1)
            _, _, _, status, _ = app.resolve_subscription(
                "T" * 32, {"x-hwid": "device-test-456"}
            )
            self.assertEqual(int(status), 404)

    def test_failed_registration_does_not_consume_device_slot(self):
        with mock.patch.object(app, "reload_mita", side_effect=[(False, "failed"), (True, "restored")]):
            _, _, _, status, _ = app.resolve_subscription(
                "T" * 32, {"x-hwid": "device-test-123"}
            )
        self.assertEqual(int(status), 503)
        self.assertEqual(app.get_users()[0]["devices"], [])

    def test_secret_not_logged(self):
        handler = app.CommonHandler.__new__(app.CommonHandler)
        handler.address_string = lambda: "127.0.0.1"
        token = "A" * 32
        with mock.patch("builtins.print") as log:
            handler.log_message('"%s" 200 -', f"GET /sub/{token}?raw=1 HTTP/1.1")
        line = log.call_args.args[0]
        self.assertNotIn(token, line)
        self.assertIn("[redacted]", line)

    def test_traffic_units_and_parser(self):
        self.assertEqual(app.parse_iec_bytes("1.5KiB"), 1536)
        self.assertEqual(app.format_iec_bytes(1536), "1.5KiB")
        output = "User  LastActive  1DayDown  1DayUp  7DaysDown  7DaysUp  30DaysDown  30DaysUp\nuser  -  1KiB  2KiB  3KiB  4KiB  5KiB  6KiB"
        rows = app.parse_mita_users_output(output)
        self.assertEqual(rows[0]["day_down"], "1KiB")
        self.assertEqual(rows[0]["username"], "user")


if __name__ == "__main__":
    unittest.main()

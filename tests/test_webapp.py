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

    def test_backup_status_is_admin_only_and_contains_no_dsn(self):
        with mock.patch.object(app, "get_backup_status", return_value={
            "enabled": True, "connected": True, "last_backup": "2026-10-11T02:00:00Z",
            "last_reason": "startup", "backup_count": 1, "last_error": ""
        }):
            status, data = self.call("GET", "/api/backup-status")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"]["backup_count"], 1)
        self.assertNotIn("postgresql://", json.dumps(data))

    def test_contact_email_is_optional_and_persisted_without_reloading_mita(self):
        status, data = self.call("PUT", "/api/settings", {"contact_email": "  support@example.com "})
        self.assertEqual(status, 200)
        self.assertEqual(data["settings"]["contact_email"], "support@example.com")
        self.assertEqual(app.get_settings()["contact_email"], "support@example.com")
        self.reload_mock.assert_not_called()

        status, data = self.call("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(data["settings"]["contact_email"], "support@example.com")

        status, data = self.call("PUT", "/api/settings", {"contact_email": ""})
        self.assertEqual(status, 200)
        self.assertEqual(data["settings"]["contact_email"], "")
        self.reload_mock.assert_not_called()

    def test_invalid_contact_email_does_not_change_settings(self):
        for address in ("bad-address", "user@localhost", "bad\\n@mail.com",
                        "bad@@example.com", "a" * 65 + "@example.com", 42,
                        "<img src=x onerror=alert(1)>@example.com"):
            with self.subTest(address=address):
                status, data = self.call("PUT", "/api/settings", {"contact_email": address})
                self.assertEqual(status, 400)
                self.assertIn("contact_email", data["error"])
                self.assertEqual(app.get_settings().get("contact_email", ""), "")
        self.reload_mock.assert_not_called()

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

    def test_contact_email_appears_only_when_configured(self):
        template_path = Path(__file__).resolve().parents[1] / "mita" / "subscription.html"
        with mock.patch.object(app, "SUBSCRIPTION_PAGE_PATH", template_path):
            empty_page = app.render_subscription_page(app.get_users()[0])
            self.assertNotIn('class="contact-info"', empty_page)
            self.assertNotIn("@@CONTACT_LINK_HTML@@", empty_page)
            settings = app.get_settings()
            settings["contact_email"] = "support+vpn@example.com"
            app.write_json(app.SETTINGS_PATH, settings)
            page = app.render_subscription_page(app.get_users()[0])
            self.assertIn('href="mailto:support+vpn@example.com"', page)
            self.assertIn("support+vpn@example.com</a>", page)
            self.assertNotIn("@@CONTACT_LINK_HTML@@", page)

    def test_device_creation_and_limit(self):
        with mock.patch.object(app, "reload_mita", return_value=(True, "")) as reload_mock:
            user, credential, _, status, _ = app.resolve_subscription(
                "T" * 32, {"x-hwid": "devicetest123", "x-device-os": "iOS"}
            )
            self.assertIsNone(status)
            self.assertTrue(credential["username"].startswith("dev_"))
            self.assertEqual(len(app.get_users()[0]["devices"]), 1)
            self.assertEqual(reload_mock.call_count, 1)
            _, _, _, status, _ = app.resolve_subscription(
                "T" * 32, {"x-hwid": "devicetest456"}
            )
            self.assertEqual(int(status), 404)

    def test_failed_registration_does_not_consume_device_slot(self):
        with mock.patch.object(app, "reload_mita", side_effect=[(False, "failed"), (True, "restored")]):
            _, _, _, status, _ = app.resolve_subscription(
                "T" * 32, {"x-hwid": "devicetest123"}
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


class ReleaseDownloadsTests(unittest.TestCase):
    def test_new_client_release_patterns_match_real_assets(self):
        import re
        cases = {
            "flclashx": {
                "android-arm64": "FlClashX-android-arm64-v8a.apk",
                "windows-amd64": "FlClashX-windows-amd64-setup.exe",
                "macos-arm64": "FlClashX-macos-arm64.dmg",
                "linux-amd64-appimage": "FlClashX-linux-amd64.AppImage",
            },
            "koala": {
                "windows-amd64": "Koala.Clash_x64-setup.exe",
                "macos-arm64": "Koala.Clash_arm64.pkg",
                "linux-amd64-deb": "Koala.Clash_amd64.deb",
                "linux-amd64-rpm": "Koala.Clash_x86_64.rpm",
            },
        }
        for client, targets in cases.items():
            self.assertIn("repo", app.GITHUB_DOWNLOADS[client])
            for target, filename in targets.items():
                with self.subTest(client=client, target=target):
                    pattern = app.GITHUB_DOWNLOADS[client]["targets"][target]
                    self.assertIsNotNone(re.fullmatch(pattern, filename))
                    self.assertIsNone(re.fullmatch(pattern, filename + ".sha256"))


class PlatformDetectionTests(unittest.TestCase):
    def test_ios_firefox_user_agents(self):
        cases = [
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 FxiOS/143.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) FxiOS/143.0 Mobile/15E148",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Mobile/15E148 Safari/604.1",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) CriOS/124.0",
        ]
        for user_agent in cases:
            with self.subTest(user_agent=user_agent):
                self.assertEqual(app.detect_platform_from_user_agent(user_agent), "ios")

    def test_real_macos_still_detected(self):
        ua = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) Gecko/20100101 Firefox/144.0"
        self.assertEqual(app.detect_platform_from_user_agent(ua), "macos")


class PureHelpersTests(unittest.TestCase):
    def test_config_builder_only_includes_active_credentials(self):
        users = [{
            "username": "first", "password": "old",
            "allow_private_ip": True, "allow_loopback_ip": False,
            "legacy_enabled": False,
            "devices": [{"mita_username": "dev_one", "password": "new"}],
        }]
        settings = {"prefer_ipv4": False, "log_level": "WARN"}
        config = app.build_mita_config(users, settings)
        self.assertEqual([u["name"] for u in config["users"]], ["dev_one"])
        self.assertEqual(config["users"][0]["password"], "new")
        self.assertTrue(config["users"][0]["allowPrivateIP"])
        self.assertEqual(config["dns"]["dualStack"], "USE_FIRST_IP")
        self.assertEqual(config["loggingLevel"], "WARN")

    def test_subscription_base_url_validation(self):
        self.assertEqual(app.normalize_base_url("https://example.com/vpn/"), "https://example.com/vpn")
        for value in (
            "https://user:secret@example.com",
            "https://example.com?mode=test",
            "https://example.com/#fragment",
            "ftp://example.com",
            "https://example.com:badport",
            "https://example.com/with space",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    app.normalize_base_url(value)


if __name__ == "__main__":
    unittest.main()

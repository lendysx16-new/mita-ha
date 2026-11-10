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

        self.server = app.Server(("127.0.0.1", 0), app.AdminHandler)
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
        status, _ = self.call("POST", "/api/users", '{}', raw=True,
                             headers={"Content-Type": "application/json", "Content-Length": "-1"})
        self.assertEqual(status, 400)

    def test_mutations_require_json_content_type(self):
        status, data = self.call("POST", "/api/users", '{"username":"csrf"}', raw=True,
                                 headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)
        self.assertEqual(len(app.get_users()), 1)
        status, _ = self.call("POST", "/api/users/first/rotate-token", headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_admin_rejects_clients_other_than_ingress(self):
        self.assertTrue(app.is_allowed_admin_client("172.30.32.2"))
        self.assertTrue(app.is_allowed_admin_client("::ffff:172.30.32.2"))
        self.assertFalse(app.is_allowed_admin_client("172.30.33.5"))
        with mock.patch.object(app, "admin_allowed_clients", return_value={"172.30.32.2"}):
            status, data = self.call("GET", "/api/state")
        self.assertEqual(status, 403)
        self.assertNotIn("users", data)

    def test_wrong_field_types_are_rejected_not_coerced(self):
        cases = [
            ("PUT", "/api/users/first/device-settings", {"device_limit": None}),
            ("PUT", "/api/users/first/device-settings", {"legacy_enabled": "false"}),
            ("PATCH", "/api/users/first", {"allow_private_ip": "false"}),
            ("PUT", "/api/settings", {"public_mieru_port": []}),
            ("PUT", "/api/settings", {"public_mieru_host": 5}),
            ("POST", "/api/users", {"username": "typed", "allow_private_ip": "false"}),
            ("POST", "/api/users", {"username": "typed", "password": 123}),
            ("POST", "/api/users", {"username": ["x"]}),
        ]
        for method, path, body in cases:
            with self.subTest(method=method, path=path, body=body):
                status, data = self.call(method, path, body)
                self.assertEqual(status, 400)
                self.assertIn("error", data)
        self.assertEqual(len(app.get_users()), 1)
        self.assertFalse(app.get_users()[0]["allow_private_ip"])
        self.reload_mock.assert_not_called()

    def test_null_password_generates_one(self):
        status, data = self.call("POST", "/api/users", {"username": "nullpw", "password": None})
        self.assertEqual(status, 201)
        self.assertTrue(data["generated_password"])
        self.assertNotEqual(app.get_users()[1]["password"], "None")

    def test_patch_user_updates_access_and_reloads_only_when_needed(self):
        status, data = self.call("PATCH", "/api/users/first", {"device_limit": 3})
        self.assertEqual(status, 200)
        self.assertEqual(data["user"]["device_limit"], 3)
        self.reload_mock.assert_not_called()
        status, data = self.call("PATCH", "/api/users/first", {"allow_private_ip": True, "allow_loopback_ip": True})
        self.assertEqual(status, 200)
        self.assertTrue(data["user"]["allow_private_ip"])
        self.assertTrue(app.get_users()[0]["allow_loopback_ip"])
        self.assertEqual(self.reload_mock.call_count, 1)
        status, _ = self.call("PATCH", "/api/users/missing", {"allow_private_ip": True})
        self.assertEqual(status, 404)

    def test_failed_access_change_is_rolled_back(self):
        self.reload_mock.side_effect = [(False, "reload rejected"), (True, "restored")]
        status, _ = self.call("PATCH", "/api/users/first", {"allow_private_ip": True})
        self.assertEqual(status, 503)
        self.assertFalse(app.get_users()[0]["allow_private_ip"])

    def test_reset_password_generated_or_supplied(self):
        status, data = self.call("POST", "/api/users/first/password", {})
        self.assertEqual(status, 200)
        generated = data["generated_password"]
        self.assertTrue(generated)
        self.assertEqual(app.get_users()[0]["password"], generated)
        self.assertNotIn("password", data["user"])
        self.assertEqual(self.reload_mock.call_count, 1)
        status, data = self.call("POST", "/api/users/first/password", {"password": "chosen-secret"})
        self.assertEqual(status, 200)
        self.assertIsNone(data["generated_password"])
        self.assertEqual(app.get_users()[0]["password"], "chosen-secret")
        status, _ = self.call("POST", "/api/users/first/password", {"password": "bad\nline"})
        self.assertEqual(status, 400)

    def test_corrupt_user_database_is_never_overwritten(self):
        app.USERS_PATH.write_text('[{"username": "first"', encoding="utf-8")
        status, data = self.call("GET", "/api/state")
        self.assertEqual(status, 503)
        self.assertIn("users.json", data["error"])
        status, _ = self.call("POST", "/api/users", {"username": "second"})
        self.assertEqual(status, 503)
        self.assertEqual(app.USERS_PATH.read_text(encoding="utf-8"), '[{"username": "first"')

    def test_unknown_method_and_unexpected_errors_return_json(self):
        status, data = self.call("PATCH", "/api/settings", {})
        self.assertEqual(status, 405)
        with mock.patch.object(app, "get_log_db_status", side_effect=KeyError("boom")):
            status, data = self.call("GET", "/api/log-db-status")
        self.assertEqual(status, 500)
        self.assertEqual(data["error"], "internal error")

    def test_failed_config_rebuild_rolls_back(self):
        self.reload_patch.stop()
        with mock.patch.object(app, "rebuild_mita_config", side_effect=[OSError("disk full"), None]), \
                mock.patch.object(app.subprocess, "run") as run:
            status, data = self.call("POST", "/api/users", {"username": "second"})
        self.reload_patch.start()
        self.assertEqual(status, 503)
        self.assertIn("disk full", data["error"])
        self.assertEqual(len(app.get_users()), 1)

    def test_admin_page_sends_security_headers(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("frame-ancestors 'self'", response.getheader("Content-Security-Policy"))
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("Referrer-Policy"), "no-referrer")

    def test_placeholder_password_is_flagged_and_cannot_be_set(self):
        users = app.get_users()
        users[0]["password"] = app.PLACEHOLDER_PASSWORD
        app.write_json(app.USERS_PATH, users)
        status, data = self.call("GET", "/api/state")
        self.assertTrue(data["users"][0]["default_password"])
        self.assertNotIn(app.PLACEHOLDER_PASSWORD, json.dumps(data))
        for method, path, body in (
            ("POST", "/api/users", {"username": "second", "password": app.PLACEHOLDER_PASSWORD}),
            ("POST", "/api/users/first/password", {"password": app.PLACEHOLDER_PASSWORD}),
        ):
            with self.subTest(path=path):
                status, _ = self.call(method, path, body)
                self.assertEqual(status, 400)
        status, data = self.call("POST", "/api/users/first/password", {})
        self.assertEqual(status, 200)
        self.assertFalse(data["user"]["default_password"])

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

    def test_page_bootstrap_carries_catalog_and_hwid_requirement(self):
        import base64, hashlib, re
        template_path = Path(__file__).resolve().parents[1] / "mita" / "subscription.html"
        users = app.get_users()
        users[0]["legacy_enabled"] = False
        app.write_json(app.USERS_PATH, users)
        with mock.patch.object(app, "SUBSCRIPTION_PAGE_PATH", template_path):
            page = app.render_subscription_page(app.get_users()[0])
        scripts = re.findall(r"<script>(.*?)</script>", page, re.S)
        bootstrap = json.loads(scripts[0].strip()[len("window.__SUBSCRIPTION__="):].rstrip(";"))
        self.assertTrue(bootstrap["hwid_required"])
        self.assertEqual([c["id"] for c in bootstrap["clients"]], [c["id"] for c in app.CLIENTS])
        serialized = json.dumps(bootstrap["clients"])
        self.assertNotIn('"asset"', serialized)
        self.assertNotIn('"repo"', serialized)

        csp = app.subscription_page_headers(page)["Content-Security-Policy"]
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn("'unsafe-inline' ;", csp)
        for script in scripts:
            digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
            self.assertIn(f"'sha256-{digest}'", csp)
        self.assertNotIn("unsafe-inline", csp.split("script-src", 1)[1].split(";", 1)[0])

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


class ClientCatalogTests(unittest.TestCase):
    def test_catalog_is_consistent(self):
        ids = [c["id"] for c in app.CLIENTS]
        self.assertEqual(len(ids), len(set(ids)))
        for client in app.CLIENTS:
            with self.subTest(client=client["id"]):
                targets = [d["target"] for d in client["downloads"] if "target" in d]
                self.assertEqual(len(targets), len(set(targets)), "duplicate target")
                self.assertTrue(client["downloads"], "client without downloads")
                for d in client["downloads"]:
                    self.assertIn(d["os"], {"ios", "android", "windows", "macos", "linux"})
                    if "url" in d:
                        self.assertTrue(d["url"].startswith("https://"))
                    if d["os"] == "linux" and "target" in d:
                        # install-linux.sh derives linux-<arch>-<format> targets.
                        self.assertRegex(d["target"], r"^linux-(amd64|arm64)-(deb|rpm|appimage)$")
                if client.get("scheme"):
                    self.assertRegex(client["scheme"], r"^[a-z][a-z0-9-]*$")

    def test_download_allowlist_is_built_from_catalog(self):
        self.assertEqual(
            app.GITHUB_DOWNLOADS["slothclash"]["targets"]["windows-amd64"],
            r"^SlothClash-windows-amd64-installer\.exe$",
        )
        self.assertNotIn("clashproxy", app.GITHUB_DOWNLOADS, "App Store-only client has no GitHub assets")
        with self.assertRaises(ValueError):
            app.resolve_github_download("clashproxy", "macos")
        with self.assertRaises(ValueError):
            app.resolve_github_download("karing", "linux-amd64-appimage")

    def test_invalid_catalog_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clients.json"
            path.write_text(json.dumps({"clients": [{"id": "x", "name": "X", "repo": "a/b", "downloads": [
                {"os": "linux", "label": "L", "note": "N", "target": "linux-amd64-deb", "asset": "^a$", "url": "https://x"}
            ]}]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                app.load_client_catalog(path)


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


class InitializeDefaultsTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        names = ("DATA_DIR", "OPTIONS_PATH", "USERS_PATH", "SETTINGS_PATH", "MITA_CONFIG_PATH")
        original = {name: getattr(app, name) for name in names}
        self.addCleanup(lambda: [setattr(app, k, v) for k, v in original.items()])
        app.DATA_DIR = Path(temp.name)
        app.OPTIONS_PATH = app.DATA_DIR / "options.json"
        app.USERS_PATH = app.DATA_DIR / "users.json"
        app.SETTINGS_PATH = app.DATA_DIR / "settings.json"
        app.MITA_CONFIG_PATH = app.DATA_DIR / "mita-server.json"

    def init(self, options):
        app.write_json(app.OPTIONS_PATH, options)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            app.initialize()
        return out.getvalue()

    def mita_passwords(self):
        return [u["password"] for u in json.loads(app.MITA_CONFIG_PATH.read_text())["users"]]

    def test_placeholder_from_options_never_becomes_a_live_credential(self):
        log = self.init({"users": [
            {"username": "change-me", "password": app.PLACEHOLDER_PASSWORD},
            {"username": "real", "password": "chosen-secret"},
        ]})
        passwords = self.mita_passwords()
        self.assertNotIn(app.PLACEHOLDER_PASSWORD, passwords)
        self.assertIn("chosen-secret", passwords)
        self.assertGreaterEqual(len(app.get_users()[0]["password"]), 24)
        self.assertIn("replaced the placeholder password of 'change-me'", log)
        self.assertNotIn(app.get_users()[0]["password"], log)

    def test_fallback_user_gets_a_random_password(self):
        self.init({"users": []})
        users = app.get_users()
        self.assertEqual([u["username"] for u in users], ["change-me"])
        self.assertNotEqual(users[0]["password"], app.PLACEHOLDER_PASSWORD)
        self.assertNotIn(app.PLACEHOLDER_PASSWORD, self.mita_passwords())

    def test_existing_placeholder_user_is_kept_but_warned_about(self):
        app.write_json(app.USERS_PATH, [{
            "username": "change-me", "password": app.PLACEHOLDER_PASSWORD, "token": "T" * 32,
        }])
        log = self.init({})
        self.assertEqual(app.get_users()[0]["password"], app.PLACEHOLDER_PASSWORD)
        self.assertIn("WARNING: user 'change-me' still uses the public placeholder password", log)

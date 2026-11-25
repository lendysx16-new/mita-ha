"""Hysteria 2 auth, subscription, ACL, and accounting regressions."""
import base64
import http.client
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "mita_hysteria_test", Path(__file__).resolve().parents[1] / "mita" / "webapp.py"
)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class HysteriaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        for name, value in {
            "DATA_DIR": directory,
            "USERS_PATH": directory / "users.json",
            "SETTINGS_PATH": directory / "settings.json",
            "MITA_CONFIG_PATH": directory / "mita-server.json",
            "HY2_CONFIG_PATH": directory / "hysteria2.yaml",
            "HY2_TRAFFIC_PATH": directory / "hysteria2-traffic.json",
            "HY2_GENERATION_PATH": directory / "hysteria-generation",
        }.items():
            patch = mock.patch.object(app, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        self.original_last = app.HY2_LAST_ACCOUNTS
        self.original_pending = set(app.HY2_PENDING_KICKS)
        app.HY2_LAST_ACCOUNTS = None
        app.HY2_PENDING_KICKS.clear()
        self.addCleanup(self.restore_hysteria_state)

        app.write_json(app.USERS_PATH, [{
            "username": "alice", "password": "secret:with:colon", "token": "x" * 32,
            "legacy_enabled": True, "allow_private_ip": False, "proxy_outbound": "",
            "devices": [{"id": "a" * 16, "mita_username": "dev_abc", "password": "secret-dev",
                         "hwid": "myphone123456", "model": "Phone"}],
        }])
        app.write_json(app.SETTINGS_PATH, {
            "public_mieru_host": "vpn.example.com", "public_mieru_port": 2022,
            "subscription_base_url": "https://vpn.example.com",
            "hysteria2_enabled": True, "hysteria2_host": "hy.example.com",
            "hysteria2_sni": "hy.example.com", "hysteria2_port": 443,
            "hysteria2_auth_secret": "a-very-long-secret-for-http-auth",
            "hysteria2_stats_secret": "another-secret-for-stats",
        })
        app.HY2_GENERATION_PATH.write_text("server-1")

    def restore_hysteria_state(self):
        app.HY2_LAST_ACCOUNTS = self.original_last
        app.HY2_PENDING_KICKS.clear()
        app.HY2_PENDING_KICKS.update(self.original_pending)

    def test_subscription_has_both_protocols_and_same_device_identity(self):
        user = app.get_users()[0]
        profile = app.subscription_yaml(user, {"username": "dev_abc", "password": "secret-dev", "label": "Phone"})
        self.assertIn("type: mieru", profile)
        self.assertIn("type: hysteria2", profile)
        self.assertIn("skip-cert-verify: false", profile)
        self.assertIn('"Hysteria 2 alice · Phone"', profile)
        self.assertIn('"hy.example.com"', profile)
        self.assertEqual(app.hy2_authenticate(app.hy2_credential("dev_abc", "secret-dev")), "dev_abc")
        self.assertIsNone(app.hy2_authenticate(app.hy2_credential("dev_abc", "wrong")))
        self.assertEqual(app.hy2_authenticate(app.hy2_credential("alice", "secret:with:colon")), "alice")
        self.assertIsNone(app.hy2_authenticate("not-a-credential"))
        self.assertIsNone(app.hy2_authenticate(app.hy2_credential("пользователь", "secret")))
        invalid_unicode = base64.urlsafe_b64encode(b'["\\ud800","secret"]').decode().rstrip("=")
        self.assertIsNone(app.hy2_authenticate(invalid_unicode))

        settings = app.get_settings()
        settings["hysteria2_enabled"] = False
        app.write_json(app.SETTINGS_PATH, settings)
        self.assertEqual(app.subscription_yaml(user).count("type: mieru"), 1)
        self.assertNotIn("type: hysteria2", app.subscription_yaml(user))

    def test_auth_api_refuses_deleted_device_and_never_registers_one(self):
        server = app.Server(("127.0.0.1", 0), app.HysteriaAuthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def auth(path, password):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                conn.request("POST", path, json.dumps({"auth": password}),
                             {"Content-Type": "application/json"})
                response = conn.getresponse()
                return response.status, json.loads(response.read())
            finally:
                conn.close()

        path = "/auth/a-very-long-secret-for-http-auth"
        credential = app.hy2_credential("dev_abc", "secret-dev")
        self.assertEqual(auth(path, credential), (200, {"ok": True, "id": "dev_abc"}))
        self.assertEqual(auth(path, app.hy2_credential("пользователь", "secret")),
                         (200, {"ok": False, "id": ""}))
        self.assertEqual(auth("/auth/wrong", credential)[0], 404)
        users = app.get_users()
        users[0]["devices"] = []
        users[0]["legacy_enabled"] = False
        app.write_json(app.USERS_PATH, users)
        self.assertEqual(auth(path, credential), (200, {"ok": False, "id": ""}))
        self.assertEqual(len(app.get_users()[0]["devices"]), 0)

    def test_acl_uses_same_router_and_private_ip_permissions(self):
        users = app.get_users()
        settings = app.get_settings()
        users[0]["proxy_outbound"] = "nl"
        users[0]["allow_private_ip"] = True
        settings["outbounds"] = [{"name": "nl", "port": 1081, "password": "socks-secret",
                                  "outbound": {"type": "socks", "server": "provider.example", "server_port": 1080}}]
        settings["router_password"] = "router-secret"
        config = app.hysteria2_config(settings, users)
        self.assertEqual(config["listen"], ":443")
        self.assertEqual(config["outbounds"][0]["socks5"]["addr"], "127.0.0.1:1180")
        self.assertEqual(config["outbounds"][1]["socks5"]["addr"], "127.0.0.1:2081")
        self.assertEqual(config["acl"]["rules"][-2]["outbound"], "via-0")
        self.assertIn('"dev_abc"', config["acl"]["rules"][-2]["match"])
        self.assertIn('"alice"', config["acl"]["rules"][-2]["match"])
        self.assertIn('auth.id in [', config["acl"]["rules"][1]["match"])
        self.assertIn('cidr("127.0.0.0/8")', config["acl"]["rules"][0]["match"])
        self.assertIn('cidr("10.0.0.0/8")', config["acl"]["rules"][1]["match"])
        self.assertEqual(config["acl"]["rules"][-1], {"outbound": "router"})

    def test_existing_host_certificate_paths_are_used(self):
        with mock.patch.dict(os.environ, {
            "MITA_HY2_TLS_CERT": "/certs/live/vpn.example.com/fullchain.pem",
            "MITA_HY2_TLS_KEY": "/certs/live/vpn.example.com/privkey.pem",
        }):
            config = app.hysteria2_config(app.get_settings(), app.get_users())
        self.assertEqual(config["tls"], {
            "cert": "/certs/live/vpn.example.com/fullchain.pem",
            "key": "/certs/live/vpn.example.com/privkey.pem",
        })

    def test_invalid_hysteria_env_port_is_reported_without_crashing(self):
        for value in ("oops", "0", "65536", "-1"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"MITA_HY2_PORT": value}):
                with self.assertRaisesRegex(app.StateError, "MITA_HY2_PORT must be an integer"):
                    app.env_hysteria_port()
        with mock.patch.dict(os.environ, {"MITA_HY2_PORT": "8443"}):
            self.assertEqual(app.env_hysteria_port(), 8443)

    def test_sync_does_not_restart_unnecessarily_and_kicks_rotated_credentials(self):
        with mock.patch.object(app, "hy2_api", return_value={}) as api:
            app.sync_hysteria2_config()
            first = app.HY2_CONFIG_PATH.read_bytes()
            mtime = app.HY2_CONFIG_PATH.stat().st_mtime_ns
            app.sync_hysteria2_config()
            self.assertEqual(app.HY2_CONFIG_PATH.stat().st_mtime_ns, mtime)
            self.assertEqual(first, app.HY2_CONFIG_PATH.read_bytes())
            users = app.get_users()
            users[0]["devices"][0]["password"] = "rotated"
            app.write_json(app.USERS_PATH, users)
            app.sync_hysteria2_config()
            api.assert_called_once_with("/kick", method="POST", payload=["dev_abc"])
            self.assertIsNone(app.hy2_authenticate(app.hy2_credential("dev_abc", "secret-dev")))

    def test_hysteria_counters_are_added_to_mita_counters(self):
        with mock.patch.object(app, "hy2_api", return_value={"alice": {"tx": 4096, "rx": 1024}}):
            app.poll_hysteria2_traffic()
        rows = app.hy2_traffic_rows()
        self.assertEqual(rows["alice"]["day_down"], "4.0KiB")
        self.assertEqual(rows["alice"]["day_up"], "1.0KiB")
        with mock.patch.object(app, "get_mita_user_traffic", return_value=[{
            "username": "alice", "day_down": "1KiB", "day_up": "2KiB",
            "week_down": "1KiB", "week_up": "2KiB", "month_down": "1KiB", "month_up": "2KiB",
        }]):
            view = app.get_traffic_view()
        self.assertEqual(view[0]["day_down"], "5.0KiB")
        self.assertEqual(view[0]["day_up"], "3.0KiB")

    def test_cumulative_counters_do_not_double_count_and_survive_restart(self):
        samples = [
            {"alice": {"tx": 4096, "rx": 1024}},
            {"alice": {"tx": 4096, "rx": 1024}},
            {"alice": {"tx": 5120, "rx": 1536}},
        ]
        with mock.patch.object(app, "hy2_api", side_effect=samples) as api:
            for _ in samples:
                app.poll_hysteria2_traffic()
        self.assertEqual(api.call_args_list, [mock.call("/traffic")] * 3)
        self.assertEqual(app.hy2_traffic_rows()["alice"]["day_down"], "5.0KiB")
        self.assertEqual(app.hy2_traffic_rows()["alice"]["day_up"], "1.5KiB")

        # A fresh process can have higher counters than the old one already.
        # Its generation marker still ensures those bytes are all accounted for.
        app.HY2_GENERATION_PATH.write_text("server-2")
        with mock.patch.object(app, "hy2_api", return_value={"alice": {"tx": 8192, "rx": 2048}}):
            app.poll_hysteria2_traffic()
        self.assertEqual(app.hy2_traffic_rows()["alice"]["day_down"], "13.0KiB")
        self.assertEqual(app.hy2_traffic_rows()["alice"]["day_up"], "3.5KiB")

    def test_failed_checkpoint_does_not_clear_hysteria_counters(self):
        with mock.patch.object(app, "hy2_api", return_value={"alice": {"tx": 4096, "rx": 1024}}), \
             mock.patch.object(app, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                app.poll_hysteria2_traffic()
        self.assertFalse(app.HY2_TRAFFIC_PATH.exists())

        with mock.patch.object(app, "hy2_api", return_value={"alice": {"tx": 6144, "rx": 2048}}):
            app.poll_hysteria2_traffic()
        self.assertEqual(app.hy2_traffic_rows()["alice"]["day_down"], "6.0KiB")
        self.assertEqual(app.hy2_traffic_rows()["alice"]["day_up"], "2.0KiB")

    def test_sample_is_discarded_if_hysteria_restarts_mid_request(self):
        with mock.patch.object(app, "hy2_generation", side_effect=["server-1", "server-2"]), \
             mock.patch.object(app, "hy2_api", return_value={"alice": {"tx": 4096, "rx": 1024}}):
            with self.assertRaisesRegex(ValueError, "restarted during"):
                app.poll_hysteria2_traffic()
        self.assertFalse(app.HY2_TRAFFIC_PATH.exists())

    def test_old_hourly_history_is_migrated_without_losing_totals(self):
        app.write_json(app.HY2_TRAFFIC_PATH, {
            "alice": {str(int(time.time() // 3600)): [1024, 512]},
        })
        with mock.patch.object(app, "hy2_api", return_value={"alice": {"tx": 2048, "rx": 1024}}):
            app.poll_hysteria2_traffic()
        self.assertEqual(app.hy2_traffic_rows()["alice"]["day_down"], "3.0KiB")
        self.assertEqual(app.hy2_traffic_rows()["alice"]["day_up"], "1.5KiB")
        self.assertEqual(app.load_json(app.HY2_TRAFFIC_PATH, {})["version"], 2)

    def test_config_change_flushes_traffic_before_hysteria_restart(self):
        with mock.patch.object(app, "hy2_api", return_value={}), \
             mock.patch.object(app, "poll_hysteria2_traffic") as flush:
            app.sync_hysteria2_config()
            self.assertEqual(flush.call_count, 0)
            settings = app.get_settings()
            settings["hysteria2_port"] = 8443
            app.write_json(app.SETTINGS_PATH, settings)
            app.sync_hysteria2_config()
            flush.assert_called_once_with()

    def test_hysteria_secrets_are_not_exposed_to_admin(self):
        settings = app.admin_settings(app.get_settings())
        self.assertNotIn("hysteria2_auth_secret", settings)
        self.assertNotIn("hysteria2_stats_secret", settings)

    def test_remote_mita_restart_uses_socket_not_local_process(self):
        socket_path = str(app.DATA_DIR / "control.sock")
        conn = mock.MagicMock()
        conn.recv.return_value = b'{"ok": true}\n'
        factory = mock.MagicMock()
        factory.return_value.__enter__.return_value = conn
        with mock.patch.dict(os.environ, {"MITA_CONTROL_SOCKET": socket_path}), \
             mock.patch.object(app.socket, "socket", factory), \
             mock.patch.object(app.subprocess, "run") as run, \
             mock.patch.object(app.SING_BOX, "stop"):
            self.assertIn("own container", app.restart_mita())
            run.assert_not_called()
        conn.connect.assert_called_once_with(socket_path)
        conn.sendall.assert_called_once_with(b"restart\n")


if __name__ == "__main__":
    unittest.main()

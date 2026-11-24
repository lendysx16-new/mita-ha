"""Regression tests for the Mita admin API, routing and subscription state."""
import base64
import contextlib
import http.client
import importlib.util
import io
import json
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "mita_webapp", Path(__file__).resolve().parents[1] / "mita" / "webapp.py"
)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class AdminAPITests(unittest.TestCase):
    def setUp(self):
        password_patch = mock.patch.dict(app.os.environ, {"MITA_ADMIN_PASSWORD": "test-admin-password-long-enough"})
        password_patch.start()
        self.addCleanup(password_patch.stop)
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

        app.OUTBOUND_HEALTH.clear()
        self.addCleanup(app.OUTBOUND_HEALTH.clear)
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

    def call(self, method, path, body=None, raw=False, headers=None, auth=True):
        if body is not None and not raw:
            body = json.dumps(body)
        headers = {"Content-Type": "application/json"} if headers is None else dict(headers)
        if auth:
            credentials = base64.b64encode(b"admin:test-admin-password-long-enough").decode()
            headers.setdefault("Authorization", f"Basic {credentials}")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read()
            return response.status, json.loads(payload) if payload else {}
        finally:
            connection.close()

    def test_admin_requires_password_and_never_allows_loopback(self):
        status, result = self.call("GET", "/api/state", auth=False)
        self.assertEqual(status, 401)
        self.assertNotIn("settings", result)
        status, _ = self.call("PUT", "/api/settings", {"log_level": "DEBUG"}, auth=False)
        self.assertEqual(status, 401)
        self.assertEqual(app.get_settings()["log_level"], "INFO")
        status, _ = self.call("GET", "/api/state", headers={"Authorization": "Basic !!!"})
        self.assertEqual(status, 401)
        self.assertEqual(self.call("GET", "/api/state")[0], 200)
        with mock.patch.dict(app.os.environ, {"MITA_ADMIN_PASSWORD": ""}):
            self.assertEqual(self.call("GET", "/api/state")[0], 503)

        users = app.get_users()
        users[0]["allow_loopback_ip"] = True
        self.assertFalse(app.build_mita_config(users, app.get_settings())["users"][0]["allowLoopbackIP"])

    @unittest.skipUnless(app.yaml, "PyYAML is required for Clash YAML import tests")
    def test_subscription_preview_requires_auth_and_imports_grouped_clash_servers(self):
        document = """
proxies:
  - {name: EU-A, type: trojan, server: a.example, port: 443, password: secret}
  - {name: EU-B, type: ss, server: b.example, port: 8443, cipher: aes-128-gcm, password: secret}
  - {name: Unavailable, type: wireguard, server: c.example, port: 443}
proxy-groups:
  - {name: Europe, type: select, proxies: [EU-A, EU-B]}
  - {name: Main, type: select, proxies: [Europe, DIRECT]}
"""
        url = "https://provider.example/sub?token=private"
        original = app.get_settings()
        with mock.patch.object(app, "sing_box_available", return_value=True), mock.patch.object(
            app, "fetch_outbound_subscription", return_value=document,
        ) as fetch:
            self.assertEqual(self.call("POST", "/api/outbound-subscription", {"url": url}, auth=False)[0], 401)
            fetch.assert_not_called()
            status, preview = self.call("POST", "/api/outbound-subscription", {"url": url})
        self.assertEqual(status, 200)
        fetch.assert_called_once_with(url, {})
        self.assertEqual(preview["skipped"], 1)
        self.assertEqual([node["name"] for node in preview["servers"]], ["EU-A", "EU-B"])
        self.assertEqual([preview["groups"][i] for i in preview["servers"][1]["groups"]], ["Europe", "Main"])
        self.assertEqual(preview["servers"][1]["config"]["type"], "shadowsocks")
        self.assertNotIn(url, json.dumps(preview))
        self.assertEqual(app.get_settings(), original)

    def test_subscription_parser_reads_base64_share_links_and_sing_box_selectors(self):
        uri = "trojan://secret@host.example:443#server-a"
        encoded = base64.b64encode(uri.encode()).decode()
        node = app.parse_subscription_servers(encoded)["servers"][0]
        self.assertEqual(node["name"], "server-a")
        profile = {"outbounds": [
            {"tag": "select", "type": "selector", "outbounds": ["server-a"]},
            dict(node["config"], tag="server-a"),
        ]}
        result = app.parse_subscription_servers(json.dumps(profile))
        selected = result["servers"][0]
        self.assertEqual([result["groups"][i] for i in selected["groups"]], ["select"])
        self.assertNotIn("tag", selected["config"])

    def test_subscription_download_keeps_targets_public(self):
        for target in ("http://provider.example/sub", "https://u:p@provider.example"):
            with self.assertRaises(ValueError):
                app.fetch_outbound_subscription(target)
        for ip in ("127.0.0.1", "192.168.1.1", "::1", "::ffff:127.0.0.1", "64:ff9b::7f00:1"):
            with mock.patch.object(app.socket, "getaddrinfo", return_value=[(0, 0, 0, "", (ip, 443))]):
                with self.assertRaisesRegex(ValueError, "public addresses"):
                    app.public_subscription_address("provider.example", 443)

    def test_reality_mlkem_setting_survives_link_and_clash_import(self):
        uri = (
            "vless://11111111-1111-4111-8111-111111111111@server.example:443"
            "?security=reality&pbk=example"
        )
        default, _ = app.parse_outbound_config(uri)
        self.assertNotIn("support_x25519mlkem768", default["tls"]["reality"])
        for suffix, expected in (("true", True), ("false", False)):
            link = uri + "&support-x25519mlkem768=" + suffix
            outbound, _ = app.parse_outbound_config(link)
            self.assertIs(outbound["tls"]["reality"]["support_x25519mlkem768"], expected)
            clash = {
                "name": "server", "type": "vless", "server": "server.example", "port": 443,
                "uuid": "11111111-1111-4111-8111-111111111111",
                "reality-opts": {"public-key": "example", "support-x25519mlkem768": expected},
            }
            servers = app.parse_subscription_servers(json.dumps({"proxies": [clash]}))["servers"]
            self.assertIs(servers[0]["config"]["tls"]["reality"]["support_x25519mlkem768"], expected)
        with self.assertRaisesRegex(ValueError, "must be true or false"):
            app.parse_outbound_config(uri + "&support-x25519mlkem768=invalid")

    def test_subscription_headers_are_validated_and_dropped_on_cross_origin_redirect(self):
        headers = app.parse_subscription_headers(
            "User-Agent: My client\r\nX-Device-OS: iOS\r\nAuthorization: Bearer secret"
        )
        self.assertEqual(headers["user-agent"], "My client")
        self.assertEqual(headers["x-device-os"], "iOS")
        for text in ("Host: other.example", "Transfer-Encoding: chunked", "Content-Length: 99",
                     "X-Bad: control\x00", "User-Agent: A\nuser-agent: B", "missing separator"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                app.parse_subscription_headers(text)

        first, second, third = (mock.Mock() for _ in range(3))
        first.getresponse.return_value.status = 302
        first.getresponse.return_value.getheader.return_value = "/next"
        second.getresponse.return_value.status = 307
        second.getresponse.return_value.getheader.return_value = "https://cdn.example/sub"
        third.getresponse.return_value.status = 200
        third.getresponse.return_value.read1.side_effect = [b"subscription", b""]
        with mock.patch.object(app, "SubscriptionHTTPSConnection", side_effect=[first, second, third]):
            self.assertEqual(app.fetch_outbound_subscription("https://provider.example/start", headers), "subscription")
        self.assertEqual(first.request.call_args.kwargs["headers"]["user-agent"], "My client")
        self.assertEqual(second.request.call_args.kwargs["headers"]["authorization"], "Bearer secret")
        self.assertEqual(third.request.call_args.kwargs["headers"], {
            "user-agent": "Mita-Outbound", "accept-encoding": "identity",
        })

    def test_outbound_subscription_refresh_keeps_selected_server_and_local_assignment(self):
        restart = self.routing()
        original_link = "trojan://secret@old.example:443#selected"
        original = app.parse_subscription_servers(original_link)["servers"][0]
        binding = {
            "url": "https://provider.example/sub?token=private",
            "headers": "X-Test: private-header",
            "selector": original["selector"],
        }
        status, result = self.call("POST", "/api/outbounds", {
            "name": "tracked", "config": original["config"], "subscription": binding,
        })
        self.assertEqual(status, 201)
        self.assertIn("subscription", result["outbound"])
        self.assertNotIn("private", json.dumps(result))

        def make_due():
            settings = app.get_settings()
            settings["outbounds"][0]["subscription"]["checked_at"] = "2000-01-01T00:00:00Z"
            app.write_json(app.SETTINGS_PATH, settings)

        make_due()
        previous = app.get_settings()["outbounds"][0]
        restart.reset_mock()
        latest = "trojan://updated@new.example:443#selected\ntrojan://other@other.example:443#different"
        with mock.patch.object(app, "fetch_outbound_subscription", return_value=latest) as fetch:
            app.refresh_outbound_subscriptions()
            app.refresh_outbound_subscriptions()
            fetch.assert_called_once_with(binding["url"], {"x-test": "private-header"})
        restart.assert_called_once()
        saved = app.get_settings()["outbounds"][0]
        self.assertEqual(saved["outbound"]["server"], "new.example")
        self.assertEqual(saved["port"], previous["port"])
        self.assertEqual(saved["password"], previous["password"])

        make_due()
        restart.reset_mock()
        with mock.patch.object(app, "fetch_outbound_subscription", return_value="trojan://other@other.example:443#different"):
            app.refresh_outbound_subscriptions()
        restart.assert_not_called()
        current = app.get_settings()["outbounds"][0]
        self.assertEqual(current["outbound"], saved["outbound"])
        self.assertIn("not found", current["subscription"]["error"])

        make_due()
        with mock.patch.object(app, "fetch_outbound_subscription", return_value=original_link), mock.patch.object(
            app, "apply_routing", side_effect=ValueError("rejected"),
        ):
            app.refresh_outbound_subscriptions()
        current = app.get_settings()["outbounds"][0]
        self.assertEqual(current["outbound"], saved["outbound"])
        self.assertIn("Could not apply", current["subscription"]["error"])

        status, _ = self.call("PUT", "/api/outbounds/tracked", {"config": current["outbound"], "subscription": None})
        self.assertEqual(status, 200)
        self.assertNotIn("subscription", app.get_settings()["outbounds"][0])

    def test_xhttp_share_link_and_clash_subscription_preserve_transport(self):
        uri = (
            "vless://11111111-1111-4111-8111-111111111111@example.com:443"
            "?security=reality&pbk=example&type=xhttp&host=front.example&path=%2Fsub&mode=packet-up"
            "&extra=%7B%22xPaddingBytes%22%3A%22100-500%22%7D"
        )
        outbound, _ = app.parse_outbound_config(uri)
        self.assertEqual(outbound["transport"], {
            "type": "xhttp", "host": "front.example", "path": "/sub",
            "mode": "packet-up", "x_padding_bytes": "100-500",
        })
        result = app.parse_subscription_servers(json.dumps({"proxies": [{
            "name": "xhttp", "type": "vless", "server": "example.com", "port": 443,
            "uuid": "11111111-1111-4111-8111-111111111111", "network": "xhttp",
            "xhttp-opts": {"path": "/sub", "mode": "stream-up", "no-grpc-header": True},
        }]}))
        self.assertEqual(result["servers"][0]["config"]["transport"], {
            "type": "xhttp", "path": "/sub", "mode": "stream-up", "no_grpc_header": True,
        })

    def test_awgm_box_outbound_types_generate_correct_config_sections(self):
        options = [
            {"type": "mieru", "server": "example.com", "server_ports": ["2022-2024"],
             "username": "user", "password": "test", "transport": "TCP"},
            {"type": "trusttunnel", "server": "example.com", "server_port": 443},
            {"type": "amneziawg", "address": ["10.0.0.2/32"], "private_key": "key",
             "peers": [{"address": "192.0.2.1", "port": 51820, "public_key": "key"}]},
            {"type": "masque-client", "server": "example.com", "server_port": 443},
        ]
        prepared = [app.parse_outbound_config(entry)[0] for entry in options]
        config = app.sing_box_config({
            "router_password": "bridge-secret",
            "outbounds": [{"name": f"route-{i}", "password": "test", "port": 2081 + i, "outbound": entry}
                          for i, entry in enumerate(prepared)],
        })
        self.assertEqual([entry["type"] for entry in config["outbounds"]], ["mieru", "trusttunnel", "direct"])
        self.assertEqual([entry["type"] for entry in config["endpoints"]], ["awg", "masque-client"])
        self.assertFalse(config["endpoints"][0]["useIntegratedTun"])
        self.assertEqual(config["endpoints"][0]["tag"], "out-2")

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

    HY2 = "hysteria2://pass@hy.example:8443/?sni=hy.example&obfs=salamander&obfs-password=ob#NL%20Hy2"
    VLESS = ("vless://11111111-1111-4111-8111-111111111111@v.example:443?security=reality"
             "&sni=www.example.com&fp=chrome&pbk=PUBKEY&sid=ab&flow=xtls-rprx-vision#DE")

    def persist_outbounds(self):
        """Run SING_BOX.replace without a real sing-box: just save."""
        def replace(updated, previous, persist):
            persist()
        return mock.patch.object(app.SING_BOX, "replace", side_effect=replace)

    def routing(self):
        """Standalone routing with sing-box present but not actually run."""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(app, "sing_box_available", return_value=True))
        replace = stack.enter_context(self.persist_outbounds())
        self.addCleanup(stack.close)
        return replace

    def rule_set(self, item, settings=None):
        snapshot = (settings or app.get_settings())["geo_snapshots"][item]
        return json.loads(app.geo_snapshot_path(snapshot).read_text())

    def add_outbound(self, name, link=None):
        status, data = self.call("POST", "/api/outbounds", {"name": name, "config": link or self.HY2})
        self.assertEqual(status, 201, data)
        return data

    def test_routing_policies_are_validated(self):
        self.routing()
        def put(rules, **extra):
            return self.call("PUT", "/api/settings", {"egress": {"rules": rules}, **extra})
        status, data = put([{"name": "Work", "domainNames": ["corp.example"], "action": "PROXY", "proxyNames": ["nl"]}])
        self.assertEqual(status, 400)
        self.assertIn("'Work' uses outbound 'nl'", data["error"])
        self.assertEqual(put([{"domainNames": ["Example.COM"], "action": "DIRECT"},
                              {"ipRanges": ["198.51.100.7/24"], "action": "REJECT"}])[0], 200)
        self.assertEqual(app.get_settings()["egress"]["rules"][0]["domainNames"], ["example.com"])
        self.assertEqual(put([{"domainNames": ["*.ru", ".su", "ru", "xn--p1ai"], "action": "DIRECT"}])[0], 200)
        self.assertEqual(app.get_settings()["egress"]["rules"][0]["domainNames"], ["ru", "su", "xn--p1ai"])
        self.assertEqual(put([{"domainNames": ["*.*.ru"], "action": "DIRECT"}])[0], 400)
        status, data = put([{"name": "All", "domainNames": ["*"], "action": "DIRECT"}])
        self.assertEqual(status, 400)
        self.assertIn("Everything else", data["error"])
        status, data = self.call("PUT", "/api/settings", {"egress": {
            "proxies": [{"name": "p", "host": "127.0.0.1", "port": 1080}], "rules": []}})
        self.assertEqual(status, 400)
        self.assertIn("Outbounds tab", data["error"])
        self.add_outbound("nl")
        self.add_outbound("de", self.VLESS)
        status, data = put([{"domainNames": ["x.example"], "action": "PROXY", "proxyNames": ["nl", "de"]}])
        self.assertEqual(status, 400)
        self.assertIn("exactly one outbound", data["error"])
        for bad in ("x" * 65, "line\nbreak", 7):
            self.assertEqual(put([{"name": bad, "domainNames": ["a.example"], "action": "DIRECT"}])[0], 400, bad)
        many = [f"d{i}.example" for i in range(app.EGRESS_RULE_DESTINATION_LIMIT)]
        self.assertEqual(put([{"name": "Big", "domainNames": many, "action": "DIRECT"}])[0], 200)
        self.assertEqual(put([{"name": "Big", "domainNames": many + ["one-more.example"], "action": "DIRECT"}])[0], 400)
        self.assertEqual(self.call("PUT", "/api/settings", {"routing_final": "missing"})[0], 400)
        self.assertEqual(self.call("PUT", "/api/settings", {"routing_final": "nl"})[0], 200)
        self.assertEqual(app.get_settings()["routing_final"], "nl")

    def test_sing_box_router_config_and_mita_egress(self):
        replace = self.routing()
        self.add_outbound("nl")
        self.add_outbound("de", self.VLESS)
        policies = [{"name": "Streaming", "domainNames": ["video.example"], "ipRanges": ["198.51.100.0/24"],
                     "action": "PROXY", "proxyNames": ["nl"]},
                    {"name": "Ads", "domainNames": ["ads.example"], "action": "REJECT"},
                    {"name": "Local", "domainNames": ["local.example"], "action": "DIRECT"}]
        status, data = self.call("PUT", "/api/settings", {"egress": {"rules": policies}, "routing_final": "de"})
        self.assertEqual(status, 200, data)
        self.assertFalse(data["restart_required"], "policies are applied by sing-box alone")
        updated = replace.call_args[0][0]
        settings = app.get_settings()
        self.assertEqual(updated, app.sing_box_config(settings))

        config = app.sing_box_config(settings)
        self.assertEqual([(i["tag"], i["listen_port"]) for i in config["inbounds"]],
                         [("router", 1180), ("probe-0", 1081), ("via-0", 2081), ("probe-1", 1082), ("via-1", 2082)])
        self.assertTrue(all(i["listen"] == "127.0.0.1" and i["users"][0]["password"] for i in config["inbounds"]))
        self.assertEqual([o["tag"] for o in config["outbounds"]], ["out-0", "out-1", "direct"])
        rules = config["route"]["rules"]
        self.assertEqual(rules[:3], [{"inbound": ["probe-0"], "outbound": "out-0"},
                                     {"inbound": ["probe-1"], "outbound": "out-1"},
                                     {"inbound": ["router", "via-0", "via-1"], "action": "sniff"}])
        router = [r for r in rules[3:] if r["inbound"] == ["router"]]
        self.assertEqual(router, [
            {"inbound": ["router"], "domain_suffix": ["video.example"], "ip_cidr": ["198.51.100.0/24"], "outbound": "out-0"},
            {"inbound": ["router"], "domain_suffix": ["ads.example"], "action": "reject"},
            {"inbound": ["router"], "domain_suffix": ["local.example"], "outbound": "direct"},
            {"inbound": ["router"], "outbound": "out-1"},  # Everything else -> de
        ])
        via_nl = [r for r in rules if r["inbound"] == ["via-0"]]
        self.assertEqual([r.get("outbound", r.get("action")) for r in via_nl], ["out-0", "reject", "direct", "out-0"],
                         "an account's outbound replaces every outbound target, not direct or block")

        mita = app.build_mita_config(app.get_users(), settings)["egress"]
        self.assertEqual([(p["name"], p["port"]) for p in mita["proxies"]], [("router", 1180), ("nl", 2081), ("de", 2082)])
        self.assertEqual(mita["rules"], [{"domainNames": ["*"], "ipRanges": ["*"], "action": "PROXY", "proxyNames": ["router"]}])
        self.assertEqual(mita["proxies"][0]["socks5Authentication"]["password"], settings["router_password"])
        _, state = self.call("GET", "/api/state")
        self.assertNotIn("router_password", state["settings"])
        self.assertEqual(state["sing_box_route"], config["route"])
        self.assertEqual(state["proxy_outbounds"], ["de", "nl"])

        # Without the sing-box binary Mita routes nothing.
        with mock.patch.object(app, "sing_box_available", return_value=False):
            self.assertNotIn("egress", app.build_mita_config(app.get_users(), settings))

    def test_account_outbound_needs_restart_before_assignment(self):
        self.routing()
        self.addCleanup(setattr, app, "APPLIED_PROXY_OUTBOUNDS", app.APPLIED_PROXY_OUTBOUNDS)
        self.addCleanup(setattr, app, "APPLIED_EGRESS", app.APPLIED_EGRESS)
        users = app.get_users()
        device = {"mita_username": "dev_first", "password": "device-secret"}
        users[0]["devices"] = [device]
        app.write_json(app.USERS_PATH, users)
        subscriptions = [app.subscription_yaml(users[0]), app.subscription_yaml(users[0], {**device, "username": "dev_first"})]
        self.add_outbound("nl")
        app.record_applied_proxy_outbounds(persist=True)  # `--init`, right before Mita starts
        app.load_applied_proxy_outbounds()
        self.add_outbound("de", self.VLESS)
        app.load_applied_proxy_outbounds()  # only the web process restarted; Mita kept running
        _, state = self.call("GET", "/api/state")
        self.assertEqual((state["proxy_outbounds"], state["pending_proxy_outbounds"]), (["nl"], ["de"]))
        self.assertTrue(state["restart_pending"])
        path = "/api/users/first"
        status, data = self.call("PATCH", path, {"proxy_outbound": "de"})
        self.assertEqual(status, 400)
        self.assertIn("restart Mita", data["error"])
        self.assertEqual(self.call("PATCH", path, {"proxy_outbound": "unknown"})[0], 400)
        self.reload_mock.assert_not_called()
        self.assertEqual(self.call("PATCH", path, {"proxy_outbound": "nl"})[0], 200)
        self.reload_mock.assert_called_once()
        self.assertEqual(self.call("PATCH", path, {"proxy_outbound": "nl", "device_limit": 3})[0], 200)

        config = app.build_mita_config(app.get_users(), app.get_settings())
        self.assertEqual({u["name"]: u["proxyOutbound"] for u in config["users"]}, {"first": "nl", "dev_first": "nl"})
        users = app.get_users()
        self.assertEqual([app.subscription_yaml(users[0]), app.subscription_yaml(users[0], {**device, "username": "dev_first"})],
                         subscriptions)
        self.reload_mock.side_effect = [(False, "reload failed"), (True, "")]
        self.assertEqual(self.call("PATCH", path, {"proxy_outbound": ""})[0], 503)
        self.assertEqual(app.get_users()[0]["proxy_outbound"], "nl")
        self.reload_mock.side_effect = None
        self.assertEqual(self.call("PATCH", path, {"proxy_outbound": ""})[0], 200)
        self.assertNotIn("proxy_outbound", app.get_users()[0])

    def test_outbounds_from_share_links_run_through_sing_box(self):
        replace = self.routing()
        data = self.add_outbound("nl")
        self.assertEqual(data["outbound"]["outbound"]["type"], "hysteria2")
        self.assertEqual(data["outbound"]["outbound"]["obfs"], {"type": "salamander", "password": "ob"})
        status, data = self.call("POST", "/api/outbounds", {"config": self.VLESS})
        self.assertEqual((status, data["outbound"]["name"]), (201, "DE"))
        self.assertEqual(self.call("POST", "/api/outbounds", {"config": self.HY2})[0], 400)  # "NL Hy2" has a space
        self.assertEqual(self.call("POST", "/api/outbounds", {"name": "nl", "config": self.VLESS})[0], 400)
        self.assertEqual(self.call("POST", "/api/outbounds", {"name": "router", "config": self.VLESS})[0], 400)
        self.assertEqual(self.call("POST", "/api/outbounds", {"name": "x", "config": "ftp://h:1"})[0], 400)
        entries = app.get_settings()["outbounds"]
        self.assertEqual([(e["name"], e["port"]) for e in entries], [("nl", 1081), ("DE", 1082)])
        _, state = self.call("GET", "/api/state")
        self.assertTrue(all("password" not in o for o in state["settings"]["outbounds"]))

        password = entries[0]["password"]
        status, data = self.call("PUT", "/api/outbounds/nl", {"config": self.VLESS})
        self.assertEqual((status, data["restart_required"]), (200, False))
        entry = app.get_settings()["outbounds"][0]
        self.assertEqual((entry["port"], entry["password"], entry["outbound"]["type"]), (1081, password, "vless"))
        self.assertEqual(self.call("PUT", "/api/outbounds/missing", {"config": self.VLESS})[0], 404)
        before = app.get_settings()
        replace.side_effect = ValueError("sing-box rejected the configuration")
        self.assertEqual(self.call("PUT", "/api/outbounds/nl", {"config": self.HY2})[0], 400)
        self.assertEqual(app.get_settings(), before)

    @mock.patch.object(app, "sing_box_available", return_value=True)
    def test_outbound_health_is_checked_on_save_on_demand_and_in_background(self, _available):
        self.addCleanup(app.OUTBOUND_HEALTH.clear)
        good = {"ok": True, "latency_ms": 42, "error": "", "checked_at": "2026-11-24T20:00:00Z"}
        bad = {"ok": False, "latency_ms": None, "error": "the outbound timed out", "checked_at": "2026-11-24T20:05:00Z"}
        with self.persist_outbounds(), mock.patch.object(app, "probe_outbound", return_value=bad) as probe:
            status, data = self.call("POST", "/api/outbounds", {"name": "nl", "config": self.HY2})
            self.assertEqual((status, data["health"]), (201, bad))
            self.assertEqual(probe.call_args[0][0]["name"], "nl")
            probe.return_value = good
            self.assertEqual(self.call("POST", "/api/outbounds/nl/test", {}), (200, {"health": good}))
            self.assertEqual(self.call("POST", "/api/outbounds/missing/test", {})[0], 404)
            _, state = self.call("GET", "/api/state")
            self.assertEqual(state["outbound_health"], {"nl": good})
            self.assertEqual(self.call("PUT", "/api/outbounds/nl", {"config": self.VLESS})[1]["health"], good)
            app.OUTBOUND_HEALTH["gone"] = bad
            probe.reset_mock()
            app.check_all_outbounds()
            self.assertEqual(probe.call_count, 1)
            self.assertNotIn("gone", app.OUTBOUND_HEALTH)
            self.assertEqual(self.call("DELETE", "/api/outbounds/nl")[0], 200)
            self.assertNotIn("nl", app.OUTBOUND_HEALTH)

    def test_outbound_in_use_cannot_be_deleted(self):
        self.routing()
        self.add_outbound("nl")
        policy = {"name": "Streaming", "domainNames": ["video.example"], "action": "PROXY", "proxyNames": ["nl"]}
        self.assertEqual(self.call("PUT", "/api/settings", {"egress": {"rules": [policy]}})[0], 200)
        status, data = self.call("DELETE", "/api/outbounds/nl")
        self.assertEqual(status, 400)
        self.assertIn("'Streaming' uses outbound 'nl'", data["error"])
        self.assertEqual(self.call("PUT", "/api/settings", {"egress": {"rules": []}, "routing_final": "nl"})[0], 200)
        status, data = self.call("DELETE", "/api/outbounds/nl")
        self.assertEqual(status, 400)
        self.assertIn("Everything else", data["error"])
        self.assertEqual(self.call("PUT", "/api/settings", {"routing_final": "direct"})[0], 200)
        users = app.get_users()
        users[0]["proxy_outbound"] = "nl"
        app.write_json(app.USERS_PATH, users)
        status, data = self.call("DELETE", "/api/outbounds/nl")
        self.assertEqual(status, 400)
        self.assertIn("still assigned to first", data["error"])
        users[0].pop("proxy_outbound")
        app.write_json(app.USERS_PATH, users)
        self.assertEqual(self.call("DELETE", "/api/outbounds/nl")[0], 200)
        self.assertEqual(app.get_settings()["outbounds"], [])

    def test_policy_categories_become_full_sing_box_rule_sets(self):
        replace = self.routing()
        url = "https://raw.githubusercontent.com/owner/repo/main/geosite.json"
        rule_set = {"version": 2, "rules": [{"domain": ["exact.example"], "domain_suffix": ["video.example"],
                                             "domain_keyword": ["vid"], "domain_regex": ["^cdn[0-9]+\\\\.example$"]}]}
        policy = {"name": "Geo", "geoUrls": [url.replace("raw.githubusercontent.com/owner/repo/main",
                                                         "github.com/owner/repo/blob/main"), "geoip:telegram"], "action": "REJECT"}
        ip_set = {"version": 2, "rules": [{"ip_cidr": ["149.154.160.0/20"]}]}
        with mock.patch.object(app, "fetch_geo_json", side_effect=lambda item: ip_set if "geoip" in item else rule_set) as fetch:
            self.assertEqual(self.call("PUT", "/api/settings", {"egress": {"rules": [policy]}})[0], 200)
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(self.call("PUT", "/api/settings", {"egress": {"rules": [policy]}})[0], 200)
            self.assertEqual(fetch.call_count, 2, "stored lists are not downloaded again")
        settings = app.get_settings()
        self.assertEqual(self.rule_set(url), rule_set, "nothing is dropped")
        config = app.sing_box_config(settings)
        tags = [rs["tag"] for rs in config["route"]["rule_set"]]
        self.assertEqual(len(tags), 2)
        self.assertIn({"inbound": ["router"], "rule_set": tags, "action": "reject"}, config["route"]["rules"])
        self.assertTrue(all(rs["type"] == "local" and rs["format"] == "source" for rs in config["route"]["rule_set"]))
        _, state = self.call("GET", "/api/state")
        self.assertEqual(state["settings"]["geo_snapshots"][url]["domains"], 4)

        replace.reset_mock()
        changed = {"version": 2, "rules": [{"domain_suffix": ["new.example"]}]}
        with mock.patch.object(app, "fetch_geo_json", side_effect=lambda item: ip_set if "geoip" in item else changed):
            app.refresh_geo_sources()
        self.assertEqual(self.rule_set(url), changed)
        replace.assert_called_once()  # new content is a new file: sing-box restarts on it
        replace.reset_mock()
        with mock.patch.object(app, "fetch_geo_json", side_effect=ValueError("offline")):
            app.refresh_geo_sources()
        replace.assert_not_called()
        other = dict(policy, geoUrls=["geosite:nope"])
        with mock.patch.object(app, "fetch_geo_json", side_effect=ValueError("could not download geosite:nope: HTTP 404")):
            status, data = self.call("PUT", "/api/settings", {"egress": {"rules": [other]}})
        self.assertEqual(status, 400)
        self.assertIn("geosite:nope", data["error"])
        self.assertEqual(self.call("PUT", "/api/settings", {"egress": {"rules": []}})[0], 200)
        self.assertEqual(app.get_settings()["geo_snapshots"], {})

    def test_geo_categories_come_from_dat_sources_when_set(self):
        self.routing()
        dat = Path(self.temp.name) / "geosite-test.dat"
        dat.write_bytes(geosite_dat([("RU-BLOCKED", [(2, "blocked.example", []), (1, "^re$", [])])]))
        source = "https://github.com/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geosite.dat"
        policy = {"name": "RU", "geoUrls": ["geosite:ru-blocked"], "action": "REJECT"}
        with mock.patch.object(app, "fetch_geo_dat", return_value=dat) as fetch, \
                mock.patch.object(app, "fetch_geo_json") as json_download:
            status, data = self.call("PUT", "/api/settings", {"geo_sources": {"geosite": source}, "egress": {"rules": [policy]}})
            self.assertEqual(status, 200, data)
            fetch.assert_called_once_with("geosite", source)
            json_download.assert_not_called()
        key = app.policy_geo_urls(app.get_settings())[0]
        self.assertEqual(self.rule_set(key),
                         {"version": 2, "rules": [{"domain_suffix": ["blocked.example"], "domain_regex": ["^re$"]}]})
        self.assertEqual(app.get_settings()["geo_snapshots"][key]["source"], source)
        missing = dict(policy, geoUrls=["geosite:nope"])
        with mock.patch.object(app, "fetch_geo_dat", return_value=dat):
            status, data = self.call("PUT", "/api/settings", {"egress": {"rules": [missing]}})
        self.assertEqual(status, 400)
        self.assertIn("geosite:nope: no category 'nope' in geosite.dat", data["error"])
        with mock.patch.object(app, "fetch_geo_json", return_value={"version": 2, "rules": [{"domain_suffix": ["m.example"]}]}) as json_download:
            self.assertEqual(self.call("PUT", "/api/settings", {"geo_sources": {}})[0], 200)
            json_download.assert_called_once()
        self.assertEqual(app.get_settings()["geo_snapshots"][key]["source"], "json")

    def test_restart_button_applies_saved_outbounds_without_container_restart(self):
        self.routing()
        self.addCleanup(setattr, app, "APPLIED_EGRESS", app.APPLIED_EGRESS)
        self.addCleanup(setattr, app, "APPLIED_PROXY_OUTBOUNDS", app.APPLIED_PROXY_OUTBOUNDS)
        self.add_outbound("nl")
        app.record_applied_proxy_outbounds(persist=True)
        self.assertFalse(self.call("GET", "/api/state")[1]["restart_pending"])
        rules = {"rules": [{"domainNames": ["ads.example"], "action": "REJECT"}]}
        self.assertEqual(self.call("PUT", "/api/settings", {"egress": rules})[0], 200)
        self.assertFalse(self.call("GET", "/api/state")[1]["restart_pending"], "policy changes need only sing-box")
        self.add_outbound("de", self.VLESS)
        self.assertTrue(self.call("GET", "/api/state")[1]["restart_pending"], "a new account proxy needs Mita")

        done = app.subprocess.CompletedProcess([], 0, stdout="ok")
        with mock.patch.object(app.subprocess, "run", return_value=done) as run:
            status, data = self.call("POST", "/api/mita/restart", {})
        self.assertEqual((status, data["restarted"]), (200, True))
        self.assertEqual([c.args[0] for c in run.call_args_list], [["/usr/bin/mita", "stop"], ["/usr/bin/mita", "start"]])
        self.assertFalse(self.call("GET", "/api/state")[1]["restart_pending"])
        failed = app.subprocess.CompletedProcess([], 1, stdout="config invalid")
        with mock.patch.object(app.subprocess, "run", side_effect=[done, failed, failed]), mock.patch.object(app.time, "sleep"):
            status, data = self.call("POST", "/api/mita/restart", {})
        self.assertEqual(status, 503)
        self.assertIn("mita start failed: config invalid", data["error"])
        app.write_json(app.applied_outbounds_path(), ["nl"])
        app.load_applied_proxy_outbounds()
        self.assertEqual((app.APPLIED_PROXY_OUTBOUNDS, app.APPLIED_EGRESS), ({"nl"}, None))

    def test_postgres_can_connect_through_an_outbound(self):
        replace = self.routing()
        self.add_outbound("nl")
        dsn = "postgresql://mita:secret@db.example:6432/mita?sslmode=verify-full"
        status, data = self.call("PUT", "/api/settings", {"postgres_dsn": dsn, "postgres_outbound": "nl"})
        self.assertEqual(status, 200, data)
        config = replace.call_args[0][0]
        self.assertIn({"type": "direct", "tag": "postgres", "listen": "127.0.0.1", "listen_port": app.PG_FORWARD_PORT,
                       "network": "tcp", "override_address": "db.example", "override_port": 6432}, config["inbounds"])
        self.assertIn({"inbound": ["postgres"], "outbound": "out-0"}, config["route"]["rules"])
        self.assertEqual(self.call("GET", "/api/state")[1]["settings"]["postgres_outbound"], "nl")

        driver = SimpleNamespace(connect=mock.Mock(return_value="conn"))
        with mock.patch.object(app, "psycopg2", driver):
            with mock.patch.object(app.SING_BOX, "running", return_value=False):
                with self.assertRaises(app.PgRouteError) as caught:
                    app.pg_connect(dsn)
                self.assertIn("awgm-box is not running", app.db_error_text(caught.exception, dsn))
            with mock.patch.object(app.SING_BOX, "running", return_value=True):
                app.pg_connect(dsn)
            driver.connect.assert_called_once_with(dsn, hostaddr="127.0.0.1", port=app.PG_FORWARD_PORT,
                                                   **app.PG_CONNECT_OPTIONS)
            driver.connect.reset_mock()
            app.pg_connect(dsn, direct=True)  # restore runs before sing-box
            driver.connect.assert_called_once_with(dsn, **app.PG_CONNECT_OPTIONS)

        status, data = self.call("DELETE", "/api/outbounds/nl")
        self.assertEqual(status, 400)
        self.assertIn("PostgreSQL connection uses outbound 'nl'", data["error"])
        self.assertEqual(self.call("PUT", "/api/settings", {"postgres_outbound": "missing"})[0], 400)
        status, data = self.call("PUT", "/api/settings", {"postgres_dsn": "postgresql://u:p@/m?host=/run/postgresql"})
        self.assertEqual(status, 400)
        self.assertIn("one TCP host", data["error"])
        # Without a connection string the choice has no effect and does not block saving.
        self.assertEqual(self.call("PUT", "/api/settings", {"postgres_dsn": ""})[0], 200)
        self.assertNotIn("postgres", [i["tag"] for i in replace.call_args[0][0]["inbounds"]])
        self.assertEqual(self.call("PUT", "/api/settings", {"postgres_outbound": ""})[0], 200)
        self.assertEqual(self.call("DELETE", "/api/outbounds/nl")[0], 200)

    def test_dsn_host_and_port_parsing(self):
        cases = {
            "postgresql://u:p%40ss@db.example:5433/mita?sslmode=require": ("db.example", 5433),
            "postgresql://u:p@db.example/mita": ("db.example", 5432),
            "postgres://u:p@[2001:db8::1]:6432/x": ("2001:db8::1", 6432),
            "host=db.example port=6543 user=u password='a b' dbname=m": ("db.example", 6543),
            "postgresql://u:p@db/x?hostaddr=10.0.0.5": ("10.0.0.5", 5432),
        }
        for dsn, expected in cases.items():
            self.assertEqual(app.pg_forward_target(dsn), expected, dsn)
        for bad in ("host=a,b dbname=x", "postgresql://u:p@/m?host=/var/run/postgresql", "", "host=db port=99999"):
            with self.assertRaises(ValueError, msg=bad):
                app.pg_forward_target(bad)

    def test_router_keeps_running_until_mita_stops_using_it(self):
        replace = self.routing()
        self.addCleanup(setattr, app, "APPLIED_ROUTER", app.APPLIED_ROUTER)
        self.addCleanup(setattr, app, "APPLIED_EGRESS", app.APPLIED_EGRESS)
        self.addCleanup(setattr, app, "APPLIED_PROXY_OUTBOUNDS", app.APPLIED_PROXY_OUTBOUNDS)
        self.add_outbound("nl")
        app.record_applied_proxy_outbounds(persist=True)  # Mita now sends everything to the router
        self.assertTrue(app.APPLIED_ROUTER)
        self.assertEqual(self.call("DELETE", "/api/outbounds/nl")[0], 200)
        self.assertFalse(app.routing_active(app.get_settings()), "Mita's next config routes directly")
        self.assertIsNotNone(replace.call_args[0][0], "but sing-box keeps running for the live Mita")
        self.assertTrue(self.call("GET", "/api/state")[1]["restart_pending"])

        done = app.subprocess.CompletedProcess([], 0, stdout="ok")
        with mock.patch.object(app.subprocess, "run", return_value=done), \
                mock.patch.object(app.SING_BOX, "stop") as stop:
            self.assertEqual(self.call("POST", "/api/mita/restart", {})[0], 200)
        stop.assert_called()
        self.assertFalse(app.APPLIED_ROUTER)
        self.assertIsNone(app.sing_box_config_or_none(app.get_settings()))

    def test_failed_reload_also_restores_sing_box_routing(self):
        replace = self.routing()
        before = app.get_settings()
        self.reload_mock.side_effect = [(False, "reload rejected"), (True, "restored")]
        status, _ = self.call("PUT", "/api/settings", {
            "log_level": "DEBUG", "egress": {"rules": [{"domainNames": ["ads.example"], "action": "REJECT"}]}})
        self.assertEqual(status, 503)
        self.assertEqual(app.get_settings().get("egress"), before.get("egress"))
        self.assertEqual(replace.call_count, 2, "sing-box got the new routing, then the previous one back")
        restored = replace.call_args_list[1][0][0]
        self.assertFalse(any(r.get("action") == "reject" for r in (restored or {"route": {"rules": []}})["route"]["rules"]))

    def test_rule_set_files_are_content_addressed_and_pruned(self):
        first, digest = app.write_geo_rule_set({"version": 2, "rules": [{"domain_suffix": ["a.example"]}]})
        again, _ = app.write_geo_rule_set({"rules": [{"domain_suffix": ["a.example"]}], "version": 2})
        other, _ = app.write_geo_rule_set({"version": 2, "rules": [{"domain_suffix": ["b.example"]}]})
        self.assertEqual(first, again)
        self.assertEqual(first.name, digest[:32] + ".json")
        self.assertIsNone(app.geo_snapshot_path({"file": "../settings.json"}))
        old = time.time() - 2 * app.GEO_RULE_PRUNE_AGE
        app.os.utime(first, (old, old))
        app.os.utime(other, (old, old))
        fresh, _ = app.write_geo_rule_set({"version": 2, "rules": [{"domain_suffix": ["c.example"]}]})
        app.prune_geo_rule_sets({"geo_snapshots": {"x": {"file": first.name}}})
        self.assertTrue(first.exists(), "referenced files stay")
        self.assertFalse(other.exists(), "old unreferenced files go")
        self.assertTrue(fresh.exists(), "recent files may belong to a save in progress")

    def test_earlier_routing_data_moves_to_the_router_model(self):
        url = "https://raw.githubusercontent.com/owner/repo/main/geosite.json"
        settings = app.get_settings()
        settings["sing_box"] = {
            "outbound": {"type": "trojan", "server": "t.example", "server_port": 443, "password": "p"},
            "password": "bridge-secret", "domains": ["manual.example"], "geo_urls": [url],
            "geo_snapshot": {"domains": ["geo.example"], "ip_ranges": []},
        }
        settings["egress"] = {
            "proxies": [{"name": "plain", "host": "198.51.100.9", "port": 1080,
                         "socks5Authentication": {"user": "u", "password": "pw"}}],
            "rules": [{"domainNames": ["x.example"], "action": "PROXY", "proxyNames": ["plain", "other"]},
                      {"domainNames": ["*"], "ipRanges": ["*"], "action": "PROXY", "proxyNames": ["plain"]}],
        }
        self.assertTrue(app.migrate_legacy_sing_box(settings))
        self.assertTrue(app.migrate_to_router(settings))
        self.assertEqual([e["name"] for e in settings["outbounds"]], ["managed-sing-box", "plain"])
        self.assertEqual(settings["outbounds"][1]["outbound"], {"type": "socks", "version": "5", "server": "198.51.100.9",
                                                                 "server_port": 1080, "username": "u", "password": "pw"})
        self.assertEqual(settings["egress"]["proxies"], [])
        self.assertEqual([r.get("name") for r in settings["egress"]["rules"]], ["sing-box", None])
        self.assertEqual(settings["egress"]["rules"][1]["proxyNames"], ["plain"])
        self.assertEqual(settings["routing_final"], "plain", "a catch-all policy becomes Everything else")
        self.assertEqual(self.rule_set(url, settings), {"version": 2, "rules": [{"domain_suffix": ["geo.example"]}]})
        self.assertIn("router_password", settings)
        with mock.patch.object(app, "sing_box_available", return_value=True):
            app.sing_box_config(settings)  # valid after migration
        self.assertFalse(app.migrate_to_router(settings))

    def test_sing_box_supervisor_backs_off_and_resumes_quick_checks(self):
        app.write_json(app.SETTINGS_PATH, {"router_password": "r", "outbounds": [
            {"name": "nl", "outbound": {"type": "socks", "server": "s.example", "server_port": 1080}, "port": 1081, "password": "p"}]})
        stop = mock.Mock()
        stop.wait.side_effect = [False, False, False, True]
        stop.is_set.return_value = False
        with mock.patch.object(app, "sing_box_available", return_value=True), \
                mock.patch.object(app.SING_BOX, "running", return_value=False), mock.patch.object(
            app.SING_BOX, "start", side_effect=[ValueError("port busy"), ValueError("port busy"), None],
        ) as start:
            app.supervise_sing_box(stop)
        self.assertEqual(start.call_count, 3)
        self.assertEqual(start.call_args[0][0]["inbounds"][0]["tag"], "router")
        self.assertEqual(stop.wait.call_args_list, [mock.call(1), mock.call(2), mock.call(4), mock.call(1)])

    def test_geo_categories_by_name(self):
        base = "https://raw.githubusercontent.com/MetaCubeX/meta-rules-dat/sing/geo/"
        self.assertEqual(app.normalize_geo_urls(["geosite:YouTube", " geoip:telegram ", "geosite:geolocation-!cn",
                                                 base + "geosite/youtube.json"]),
                         [base + "geosite/youtube.json", base + "geoip/telegram.json", base + "geosite/geolocation-!cn.json"])
        self.assertEqual(app.geo_label(base + "geoip/telegram.json"), "geoip:telegram")
        for bad in ("geosite:", "geosite:../x", "geosite:a/b", "geofoo:x", "youtube"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "geosite:<name>"):
                app.normalize_geo_urls([bad])
        with self.assertRaisesRegex(ValueError, "up to 8"):
            app.normalize_geo_urls([f"geosite:c{i}" for i in range(9)])
        missing = app.urllib.error.HTTPError(base + "geosite/nope.json", 404, "Not Found", {}, None)
        with mock.patch.object(app.urllib.request.OpenerDirector, "open", side_effect=missing):
            with self.assertRaisesRegex(ValueError, r"could not download geosite:nope: HTTP 404 \(no such category\?\)"):
                app.fetch_geo_json("geosite:nope")

    def test_github_geo_urls_and_rule_set_validation(self):
        self.assertEqual(app.normalize_geo_urls([
            "https://github.com/owner/repo/blob/main/geosite.json",
            "https://raw.githubusercontent.com/owner/repo/main/geosite.json",
        ]), ["https://raw.githubusercontent.com/owner/repo/main/geosite.json"])
        for url in ("http://raw.githubusercontent.com/owner/repo/main/list.json",
                    "https://raw.githubusercontent.com@other.example/owner/repo/main/list.json",
                    "https://github.com/owner/repo/blob/main/%2e%2e/list.json"):
            with self.assertRaises(ValueError):
                app.normalize_geo_urls([url])
        for bad in ({"rules": []}, {"version": 9, "rules": [{}]}, {"version": 2, "rules": "x"}, []):
            with self.assertRaises(ValueError):
                app.validate_rule_set(bad, "x")
        self.assertEqual(app.rule_set_counts({"version": 2, "rules": [
            {"domain": ["a"], "domain_suffix": "b", "ip_cidr": ["1.0.0.0/8", "2.0.0.0/8"]},
            {"type": "logical", "rules": [{"domain_keyword": ["k"]}]}]}), {"domains": 3, "ip_ranges": 2})

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

    def test_admin_answers_only_local_clients(self):
        for address in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertTrue(app.is_allowed_admin_client(address), address)
        for address in ("172.30.32.2", "10.0.0.5", "not-an-ip"):
            self.assertFalse(app.is_allowed_admin_client(address), address)
        with mock.patch.dict(app.os.environ, {"MITA_ADMIN_ALLOWED_IPS": "10.0.0.5"}):
            self.assertTrue(app.is_allowed_admin_client("10.0.0.5"))
        with mock.patch.object(app, "admin_allowed_clients", return_value={"10.9.9.9"}):
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
            credentials = base64.b64encode(b"admin:test-admin-password-long-enough").decode()
            connection.request("GET", "/", headers={"Authorization": f"Basic {credentials}"})
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
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
        names = ("DATA_DIR", "USERS_PATH", "SETTINGS_PATH", "MITA_CONFIG_PATH")
        original = {name: getattr(app, name) for name in names}
        self.addCleanup(lambda: [setattr(app, k, v) for k, v in original.items()])
        app.DATA_DIR = Path(temp.name)
        app.USERS_PATH = app.DATA_DIR / "users.json"
        app.SETTINGS_PATH = app.DATA_DIR / "settings.json"
        app.MITA_CONFIG_PATH = app.DATA_DIR / "mita-server.json"

    def init(self, env=None):
        with mock.patch.dict(app.os.environ, env or {}), contextlib.redirect_stdout(io.StringIO()) as out:
            app.initialize()
        return out.getvalue()

    def test_fresh_install_uses_environment_and_a_random_first_password(self):
        self.init({"MITA_PUBLIC_HOST": "vpn.example.com", "MITA_PUBLIC_PORT": "8443",
                   "MITA_SUBSCRIPTION_BASE_URL": "https://vpn.example.com/", "MITA_POSTGRES_DSN": "postgresql://db/mita"})
        settings = app.get_settings()
        self.assertEqual((settings["public_mieru_host"], settings["public_mieru_port"], settings["subscription_base_url"],
                          settings["postgres_dsn"]), ("vpn.example.com", 8443, "https://vpn.example.com", "postgresql://db/mita"))
        users = app.get_users()
        self.assertEqual([u["username"] for u in users], ["change-me"])
        self.assertGreaterEqual(len(users[0]["password"]), 24)
        self.assertNotEqual(users[0]["password"], app.PLACEHOLDER_PASSWORD)
        config = json.loads(app.MITA_CONFIG_PATH.read_text())
        self.assertNotIn(app.PLACEHOLDER_PASSWORD, [u["password"] for u in config["users"]])
        self.assertIn("router_password", settings)

    def test_invalid_public_port_stops_startup(self):
        with self.assertRaisesRegex(app.StateError, "MITA_PUBLIC_PORT"):
            self.init({"MITA_PUBLIC_PORT": "abc"})

    def test_existing_placeholder_user_is_kept_but_warned_about(self):
        app.write_json(app.USERS_PATH, [{
            "username": "change-me", "password": app.PLACEHOLDER_PASSWORD, "token": "T" * 32,
        }])
        log = self.init()
        self.assertEqual(app.get_users()[0]["password"], app.PLACEHOLDER_PASSWORD)
        self.assertIn("WARNING: user 'change-me' still uses the public placeholder password", log)


class OutboundProbeTests(unittest.TestCase):
    """probe_outbound speaks SOCKS5 to sing-box's loopback inbound, then HTTP(S) to the probe URL."""

    def fake_sing_box(self, behavior="ok"):
        server = socket.create_server(("127.0.0.1", 0))
        seen = {}

        def recv(conn, size):
            data = b""
            while len(data) < size:
                data += conn.recv(size - len(data))
            return data

        def handle():
            conn, _ = server.accept()
            with conn:
                conn.settimeout(5)
                recv(conn, 3)
                conn.sendall(b"\x05\x02")
                _, ulen = recv(conn, 2)
                seen["user"] = recv(conn, ulen)
                seen["password"] = recv(conn, recv(conn, 1)[0])
                if behavior == "badauth":
                    conn.sendall(b"\x01\x01")
                    return
                conn.sendall(b"\x01\x00")
                _, _, _, kind = recv(conn, 4)
                seen["host"] = recv(conn, recv(conn, 1)[0])
                seen["port"] = int.from_bytes(recv(conn, 2), "big")
                if behavior == "refused":
                    conn.sendall(b"\x05\x05\x00\x01" + b"\x00" * 6)
                    return
                conn.sendall(b"\x05\x00\x00\x01" + b"\x00" * 6)
                if behavior == "silent":
                    time.sleep(1.5)
                    return
                request = b""
                while not request.endswith(b"\r\n\r\n"):
                    request += conn.recv(1024)
                seen["request"] = request
                conn.sendall(b"HTTP/1.1 500 Oops\r\n\r\n" if behavior == "http500" else b"HTTP/1.1 204 No Content\r\n\r\n")

        threading.Thread(target=handle, daemon=True).start()
        self.addCleanup(server.close)
        return {"name": "nl", "port": server.getsockname()[1], "password": "local-secret"}, seen

    @mock.patch.dict(app.os.environ, {"MITA_OUTBOUND_PROBE_URL": "http://probe.test/generate_204"})
    def test_probe_results(self):
        entry, seen = self.fake_sing_box("ok")
        result = app.probe_outbound(entry)
        self.assertTrue(result["ok"], result)
        self.assertIsInstance(result["latency_ms"], int)
        self.assertEqual((seen["user"], seen["password"], seen["host"], seen["port"]),
                         (b"mita", b"local-secret", b"probe.test", 80))
        self.assertTrue(seen["request"].startswith(b"GET /generate_204 HTTP/1.1\r\nHost: probe.test\r\n"))

        for behavior, message in (("badauth", "credentials"), ("refused", "refused"), ("http500", "HTTP 500")):
            with self.subTest(behavior=behavior):
                entry, _ = self.fake_sing_box(behavior)
                result = app.probe_outbound(entry)
                self.assertFalse(result["ok"])
                self.assertIn(message, result["error"])

        entry, _ = self.fake_sing_box("silent")
        result = app.probe_outbound(entry, timeout=0.5)
        self.assertEqual((result["ok"], result["error"]), (False, "no answer within 0.5 s"))

        closed = socket.create_server(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        result = app.probe_outbound({"name": "nl", "port": port, "password": "x"})
        self.assertEqual(result["error"], "awgm-box is not listening for this outbound")

    def test_probe_url_must_be_http(self):
        with mock.patch.dict(app.os.environ, {"MITA_OUTBOUND_PROBE_URL": "ftp://probe.test/"}):
            with self.assertRaises(ValueError):
                app.probe_target()
        self.assertEqual(app.probe_target(), ("https", "www.gstatic.com", 443, "/generate_204"))


def _pb(field, value):
    """Tiny protobuf encoder for building test geosite.dat / geoip.dat files."""
    def varint(n):
        out = b""
        while True:
            byte, n = n & 0x7F, n >> 7
            out += bytes([byte | (0x80 if n else 0)])
            if not n:
                return out
    if isinstance(value, int):
        return varint(field << 3) + varint(value)
    if isinstance(value, str):
        value = value.encode()
    return varint(field << 3 | 2) + varint(len(value)) + value


def geosite_dat(categories):
    return b"".join(_pb(1, _pb(1, code) + b"".join(
        _pb(2, _pb(1, kind) + _pb(2, value) + b"".join(_pb(3, _pb(1, attr) + _pb(2, 1)) for attr in attrs))
        for kind, value, attrs in domains)) for code, domains in categories)


def geoip_dat(categories):
    return b"".join(_pb(1, _pb(1, code) + b"".join(
        _pb(2, _pb(1, app.ipaddress.ip_address(ip).packed) + _pb(2, prefix)) for ip, prefix in cidrs)
        + (_pb(3, 1) if reverse else b"")) for code, cidrs, reverse in categories)


class GeoDatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)

    def write(self, name, data):
        path = self.dir / name
        path.write_bytes(data)
        return path

    def test_read_geosite_and_geoip_categories(self):
        site = self.write("geosite.dat", geosite_dat([
            ("CN", [(2, "baidu.com", [])]),
            ("RU-BLOCKED", [(2, "Blocked.example", []), (3, "Exact.example", []), (0, "keyword", []),
                            (1, "^re$", []), (2, "cn-only.example", ["cn"])]),
        ]))
        self.assertEqual(app.read_dat_category(site, "geosite", "ru-blocked"), {"version": 2, "rules": [{
            "domain_keyword": ["keyword"], "domain_regex": ["^re$"],
            "domain_suffix": ["blocked.example", "cn-only.example"], "domain": ["exact.example"]}]})
        self.assertEqual(app.read_dat_category(site, "geosite", "ru-blocked@cn"),
                         {"version": 2, "rules": [{"domain_suffix": ["cn-only.example"]}]})
        with self.assertRaisesRegex(ValueError, "no category 'missing'"):
            app.read_dat_category(site, "geosite", "missing")
        ip = self.write("geoip.dat", geoip_dat([
            ("RU-BLOCKED", [("198.51.100.7", 24), ("2001:db8::", 32), ("0.0.0.0", 0)], False),
            ("REV", [("10.0.0.0", 8)], True),
        ]))
        self.assertEqual(app.read_dat_category(ip, "geoip", "ru-blocked"),
                         {"version": 2, "rules": [{"ip_cidr": ["198.51.100.0/24", "2001:db8::/32"]}]})
        with self.assertRaisesRegex(ValueError, "reverse-match"):
            app.read_dat_category(ip, "geoip", "rev")
        with self.assertRaisesRegex(ValueError, "corrupt|empty"):
            app.read_dat_category(self.write("bad.dat", b"\x0a\xff\xff\xff\xff\x0f"), "geosite", "x")
        with self.assertRaisesRegex(ValueError, "empty"):
            app.read_dat_category(self.write("empty.dat", b""), "geosite", "x")

    def test_geo_sources_validation_and_redirects(self):
        url = "https://github.com/runetfreedom/russia-v2ray-rules-dat/releases/latest/download/geosite.dat"
        self.assertEqual(app.normalize_geo_sources({"geosite": f" {url} ", "geoip": ""}), {"geosite": url})
        for bad in ("http://github.com/a/b/geosite.dat", "https://evil.example/geosite.dat",
                    "https://github.com/a/b/geosite.json", "https://user@github.com/a/geosite.dat",
                    "https://github.com:8443/a/geosite.dat"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                app.normalize_geo_sources({"geosite": bad})
        with self.assertRaises(ValueError):
            app.normalize_geo_sources({"other": url})
        handler = app.GeoDatRedirects()
        request = app.urllib.request.Request(url)
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/x.dat"))
        self.assertIsNotNone(handler.redirect_request(
            request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/x"))

    def test_fetch_geo_dat_caches_daily_and_redownloads_on_change(self):
        payloads = []

        class Response(io.BytesIO):
            def __enter__(self):
                return self

        class Opener:
            def open(self, request, timeout):
                payloads.append(request.full_url)
                return Response(b"DATA-" + str(len(payloads)).encode())

        with mock.patch.object(app, "DATA_DIR", self.dir), \
                mock.patch.object(app.urllib.request, "build_opener", return_value=Opener()):
            url = "https://github.com/o/r/releases/latest/download/geosite.dat"
            path = app.fetch_geo_dat("geosite", url)
            self.assertEqual(path.read_bytes(), b"DATA-1")
            app.fetch_geo_dat("geosite", url)
            self.assertEqual(len(payloads), 1, "cached copy is reused within a day")
            self.assertEqual(app.fetch_geo_dat("geosite", url, force=True).read_bytes(), b"DATA-2")
            app.fetch_geo_dat("geosite", url.replace("o/r", "o/other"))
            self.assertEqual(len(payloads), 3, "a different URL is downloaded")
            self.assertEqual(app.geo_dat_status()["geosite"]["bytes"], 6)
            with mock.patch.object(app, "GEO_DAT_MAX_BYTES", 3), self.assertRaisesRegex(ValueError, "exceeds"):
                app.fetch_geo_dat("geoip", "https://github.com/o/r/releases/latest/download/geoip.dat")
            self.assertFalse((self.dir / "geo-cache" / "geoip.dat").exists())


class RouterConfigRenderTests(unittest.TestCase):
    """tests/render_router_config.py feeds `awgm-box check` in CI; keep it complete."""

    def test_rendered_config_covers_everything_mita_generates(self):
        spec = importlib.util.spec_from_file_location(
            "render_router_config", Path(__file__).resolve().parent / "render_router_config.py")
        render = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(render)
        with tempfile.TemporaryDirectory() as out:
            config = render.render(out)
            self.assertEqual(json.loads((Path(out) / "config.json").read_text()), config)
            types = {outbound["type"] for outbound in config["outbounds"]}
            self.assertEqual(types, {"hysteria2", "vless", "trojan", "shadowsocks", "tuic", "vmess", "socks", "direct"})
            tags = {inbound["tag"] for inbound in config["inbounds"]}
            self.assertTrue({"router", "probe-0", "via-0", "postgres"} <= tags)
            rules = config["route"]["rules"]
            self.assertTrue(any(rule.get("action") == "sniff" for rule in rules))
            self.assertTrue(any(rule.get("action") == "reject" for rule in rules))
            self.assertTrue(any(rule.get("outbound") == "direct" for rule in rules))
            self.assertIn({"inbound": ["postgres"], "outbound": "out-2"}, rules)
            rule_sets = config["route"]["rule_set"]
            self.assertEqual(len(rule_sets), 1)
            self.assertTrue(Path(rule_sets[0]["path"]).is_file())
            self.assertTrue(rule_sets[0]["path"].startswith(str(Path(out).resolve())))
            self.assertTrue(any(rule.get("rule_set") == [rule_sets[0]["tag"]] for rule in rules))
            reality = next(o for o in config["outbounds"] if o.get("tls", {}).get("reality"))
            self.assertEqual(len(base64.urlsafe_b64decode(reality["tls"]["reality"]["public_key"] + "=")), 32)

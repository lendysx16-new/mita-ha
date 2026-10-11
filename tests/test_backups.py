"""PostgreSQL backup snapshot, retention, and worker regression tests."""
import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "mita_backup_webapp", Path(__file__).resolve().parents[1] / "mita" / "webapp.py"
)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.patcher = mock.patch.object(app, "DATA_DIR", self.root)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        (self.root / "users.json").write_text('[{"username":"first","token":"secret"}]', encoding="utf-8")
        (self.root / "settings.json").write_text(
            '{"postgres_dsn":"postgresql://user:password@db/db","contact_email":"support@example.com"}',
            encoding="utf-8",
        )
        (self.root / "mita-server.json").write_text('{"users":[]}', encoding="utf-8")
        (self.root / "mita-state").mkdir()
        (self.root / "mita-state" / "metrics.pb").write_bytes(b"metrics")
        (self.root / "mita.log").write_text("private log record\n", encoding="utf-8")
        (self.root / "log-forwarder-state.json").write_text('{"offset":900}', encoding="utf-8")

    def test_snapshot_includes_userdata_settings_config_and_metrics_only(self):
        payload, names, checksum = app.build_backup_archive()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), checksum)
        self.assertEqual(names, [
            "users.json", "settings.json", "mita-server.json", "mita-state/metrics.pb"
        ])
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            self.assertEqual(sorted(archive.getnames()), sorted(names))
            self.assertEqual(archive.extractfile("mita-state/metrics.pb").read(), b"metrics")
            self.assertIn(b'"token":"secret"', archive.extractfile("users.json").read())
            self.assertIn(b'"contact_email"', archive.extractfile("settings.json").read())
            for item in archive:
                self.assertEqual(item.mode, 0o600)
        self.assertNotIn(b"private log record", payload)
        self.assertNotIn("mita.log", names)
        self.assertNotIn("log-forwarder-state.json", names)

    def test_missing_user_database_and_symlinks_fail_closed(self):
        (self.root / "users.json").unlink()
        with self.assertRaisesRegex(RuntimeError, "required file missing"):
            app.build_backup_archive()
        (self.root / "users.json").write_text("[]", encoding="utf-8")
        (self.root / "mita-state" / "link").symlink_to(self.root / "users.json")
        with self.assertRaisesRegex(RuntimeError, "symlinks"):
            app.build_backup_archive()

    def test_snapshot_limits_size_before_upload(self):
        with mock.patch.object(app, "BACKUP_MAX_UNCOMPRESSED_BYTES", 8):
            with self.assertRaisesRegex(RuntimeError, "maximum uncompressed size"):
                app.build_backup_archive()

    def test_database_inserts_archive_and_prunes_to_thirty(self):
        connection = mock.MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (3,)
        driver = SimpleNamespace(
            connect=mock.Mock(return_value=connection),
            Binary=mock.Mock(side_effect=lambda b: b),
        )
        with mock.patch.object(app, "psycopg2", driver):
            result = app.save_postgres_backup("postgresql://db", "startup")
        self.assertEqual(result["reason"], "startup")
        self.assertEqual(result["count"], 3)
        self.assertIn("users.json", result["file_names"])
        statements = [
            (call.args[0], call.args[1:] if len(call.args) > 1 else ())
            for call in cursor.execute.call_args_list
        ]
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS mita_backups" in q for q, _ in statements))
        self.assertTrue(any("INSERT INTO mita_backups" in q for q, _ in statements))
        self.assertTrue(any("DELETE FROM mita_backups" in q for q, _ in statements))
        self.assertEqual(
            [args[0][0] for q, args in statements if "INSERT INTO mita_backups" in q],
            ["startup"],
        )
        self.assertEqual(
            [args[0] for q, args in statements if "DELETE FROM mita_backups" in q],
            [(30,)],
        )
        connection.close.assert_called_once()

    def test_worker_runs_on_start_and_after_24_hours(self):
        saved = []

        def save(_dsn, reason):
            saved.append(reason)
            return {"reason": reason, "count": len(saved)}

        # Scheduler calls monotonic before/after the first snapshot,
        # then before/after the next snapshot at the 24-hour mark.
        with (
            mock.patch.object(app, "psycopg2", object()),
            mock.patch.object(app, "get_settings", return_value={"postgres_dsn": "postgresql://db"}),
            mock.patch.object(app, "save_postgres_backup", side_effect=save),
            mock.patch.object(app.time, "monotonic", side_effect=[0, 0, 86401, 86401]),
            mock.patch.object(app.time, "sleep", side_effect=[None, StopIteration]),
        ):
            with self.assertRaises(StopIteration):
                app.backup_db_worker()
        self.assertEqual(saved, ["startup", "scheduled"])
        self.assertEqual(app.get_backup_status()["backup_count"], 2)

    def test_worker_retries_failed_backup_without_crashing_service(self):
        with (
            mock.patch.object(app, "psycopg2", object()),
            mock.patch.object(app, "get_settings", return_value={"postgres_dsn": "postgresql://db"}),
            mock.patch.object(app, "save_postgres_backup", side_effect=RuntimeError("secret DSN")),
            mock.patch.object(app.time, "monotonic", side_effect=[0, 0]),
            mock.patch.object(app.time, "sleep", side_effect=StopIteration),
        ):
            with self.assertRaises(StopIteration):
                app.backup_db_worker()
        self.assertEqual(app.get_backup_status()["last_error"], "RuntimeError")
        self.assertNotIn("secret", app.get_backup_status()["last_error"])


if __name__ == "__main__":
    unittest.main()

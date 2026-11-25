#!/usr/bin/env python3
"""Mita process supervisor. The admin backend controls it over a local Unix socket."""
import json
import hashlib
import os
from pathlib import Path
import socketserver
import signal
import stat
import subprocess
import threading
import time


DATA = Path("/data")
CONTROL = DATA / "mita-control.sock"
LOG = DATA / "mita.log"
APPLIED = DATA / "mita-applied-outbounds.json"
ENV = dict(os.environ, MITA_CONFIG_JSON_FILE=str(DATA / "mita-server.json"),
           MITA_LOG_NO_TIMESTAMP="true", MITA_UDS_PATH=str(DATA / "mita.sock"),
           MITA_INSECURE_UDS="1")


class MitaProcess:
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.log = None

    def start(self):
        self.log = LOG.open("ab", buffering=0)
        self.process = subprocess.Popen(["/usr/bin/mita", "run"], env=ENV,
                                        stdin=subprocess.DEVNULL, stdout=self.log,
                                        stderr=subprocess.STDOUT)
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError(f"mita exited with status {self.process.returncode}")
            try:
                probe = subprocess.run(["/usr/bin/mita", "get", "users"], env=ENV,
                                       capture_output=True, timeout=2)
                if probe.returncode == 0:
                    record_applied_config()
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
            time.sleep(0.1)
        raise RuntimeError("mita did not become ready")

    def stop(self):
        if self.process is None:
            return
        try:
            subprocess.run(["/usr/bin/mita", "stop"], env=ENV,
                           capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process = None
        if self.log:
            self.log.close()
            self.log = None

    def restart(self):
        with self.lock:
            self.stop()
            self.start()


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        if self.rfile.readline(128).strip() != b"restart":
            result = {"ok": False, "error": "unsupported command"}
        else:
            try:
                self.server.manager.restart()
                result = {"ok": True}
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
        self.wfile.write(json.dumps(result).encode() + b"\n")


class ControlServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def record_applied_config():
    """Publish the egress actually started by this Mita process."""
    config = json.loads((DATA / "mita-server.json").read_text(encoding="utf-8"))
    egress = config.get("egress") or {}
    result = {
        "outbounds": sorted(proxy["name"] for proxy in egress.get("proxies", [])
                            if proxy["name"] != "router"),
        "egress": hashlib.sha256(json.dumps(egress, sort_keys=True).encode()).hexdigest(),
        "router": bool(egress),
    }
    stage = APPLIED.with_suffix(".json.tmp")
    with stage.open("w", encoding="utf-8") as file:
        json.dump(result, file)
        file.write("\n")
    stage.chmod(0o600)
    stage.replace(APPLIED)


def main():
    def shutdown(_signum, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    DATA.mkdir(parents=True, exist_ok=True)
    metrics = DATA / "mita-state"
    metrics.mkdir(exist_ok=True)
    state = Path("/var/lib/mita")
    if state.is_symlink():
        if state.resolve() != metrics:
            raise RuntimeError("unexpected Mita state symlink")
    elif state.exists():
        # This is a fresh container filesystem, never the persistent /data volume.
        if any(state.iterdir()):
            raise RuntimeError("unexpected nonempty Mita state directory")
        state.rmdir()
        state.symlink_to(metrics)
    else:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.symlink_to(metrics)

    if CONTROL.exists() or CONTROL.is_symlink():
        if not stat.S_ISSOCK(CONTROL.lstat().st_mode):
            raise RuntimeError("control socket path is not a socket")
        CONTROL.unlink()
    manager = MitaProcess()
    manager.start()
    try:
        with ControlServer(str(CONTROL), Handler) as server:
            CONTROL.chmod(0o600)
            server.manager = manager
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            while True:
                with manager.lock:
                    alive = manager.process is not None and manager.process.poll() is None
                if not alive:
                    break
                time.sleep(1)
            server.shutdown()
            thread.join(timeout=5)
            raise RuntimeError("Mita exited unexpectedly")
    finally:
        manager.stop()
        CONTROL.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

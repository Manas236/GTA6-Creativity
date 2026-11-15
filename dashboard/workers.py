"""Detached pipeline workers and local runner process controls."""

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from pipeline import config, db, runner
from .store import transaction

START_KEY = "dashboard:runner_start"


def spawn(path, media_root, *args):
    logs = media_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    detach = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
              if os.name == "nt" else {"start_new_session": True})
    with (logs / f"dashboard-{args[0]}.log").open("ab") as stream:
        process = subprocess.Popen(
            [sys.executable, "-m", "pipeline.runner", "--db", str(path), *args],
            cwd=config.project_root(), env={**os.environ, "GTA6_MEDIA_DIR": str(media_root)},
            stdin=subprocess.DEVNULL, stdout=stream, stderr=stream, **detach,
        )
    threading.Thread(target=process.wait, daemon=True).start()
    return process.pid


def control(operation, path, cfg, media_root, expected_stage=None):
    from .commands import ControlError

    with transaction(path):
        lock = runner.active_lock(path, runner._lock_settings(cfg)[0])
        if operation == "start":
            if lock:
                raise ControlError("A runner already holds the lock.")
            pending = db.kv_get(START_KEY, db_path=path)
            if pending and time.time() - json.loads(pending)["at"] < 15:
                if runner.is_pid_alive(json.loads(pending)["pid"]):
                    raise ControlError("The runner is already starting.")
            pid = spawn(path, media_root, "loop")
            db.kv_set(START_KEY, json.dumps({"at": time.time(), "pid": pid}), path)
            return "Runner starting."
        if not lock or not lock.get("token"):
            raise ControlError("No controllable runner is active.")
        stage = db.kv_get("runner:stage", db_path=path) or "starting"
        raw = db.kv_get(runner.STOP_KEY, db_path=path)
        stop = json.loads(raw or "{}")
        if operation == "stop":
            if stop.get("token") != lock["token"]:
                db.kv_set(runner.STOP_KEY, json.dumps({"token": lock["token"], "at": time.time()}), path)
            return f"Stopping, finishing {stage}."
        if operation != "force" or stop.get("token") != lock["token"] or time.time() - stop["at"] < 120:
            raise ControlError("Force stop requires a stop request at least two minutes old.")
        if stage != expected_stage:
            raise ControlError("The runner stage changed. Review the new stage.")
        if (stage in ("post", "settle") or lock["host"] != socket.gethostname()
                or lock.get("kind") not in ("loop", "make")):
            raise ControlError("Force stop is unavailable for publishing or a remote runner.")
        if lock["pid"] == os.getpid():
            raise ControlError("The dashboard holds the lock for a local action.")
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(lock["pid"]), "/F"], check=True,
                           capture_output=True, timeout=10)
        else:
            os.kill(lock["pid"], signal.SIGKILL)
        return f"Interrupted {stage}."


def connection_request(payload):
    response = subprocess.run([sys.executable, "-m", "dashboard.connection_worker"],
                              cwd=config.project_root(), input=json.dumps(payload), text=True,
                              capture_output=True, timeout=15)
    if response.returncode:
        raise RuntimeError("Connection test worker failed.")
    return json.loads(response.stdout)


class ConnectionResponse:
    def __init__(self, payload):
        self.status_code = payload["status"]
        self.text = payload["text"]

    def iter_content(self, chunk_size):
        yield self.text.encode("utf-8")

    def close(self):
        pass


def connection_transport(method, url, **kwargs):
    return ConnectionResponse(connection_request(
        {"operation": "request", "method": method, "url": url, "kwargs": kwargs}))

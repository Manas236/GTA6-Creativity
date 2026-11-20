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


#: Windows: why a worker must outlive the dashboard's console (launch day, 15:30 and 16:22).
#: sys.executable is the venv's python.exe *launcher*. With DETACHED_PROCESS the launcher had no
#: console, so Windows gave the real interpreter it starts a brand-new console - a visible window
#: (a Windows Terminal tab next to the dashboard on this PC). Closing it sent CTRL_CLOSE_EVENT and
#: the Intel Fortran runtime that numpy/scipy load aborted the runner ("forrtl: error (200):
#: program aborting due to window-CLOSE event"); the launcher's kill-on-close job then took the
#: rest. CREATE_NO_WINDOW gives the launcher a console with no window, which the interpreter
#: inherits: there is no window to close, and the dashboard's console is never shared.
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def worker_env(media_root):
    # FOR_DISABLE_CONSOLE_CTRL_HANDLER: the Fortran runtime must not turn a console event into an abort.
    return {**os.environ, "GTA6_MEDIA_DIR": str(media_root), "FOR_DISABLE_CONSOLE_CTRL_HANDLER": "1"}


def spawn(path, media_root, *args):
    logs = media_root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "pipeline.runner", "--db", str(path), *args]
    with (logs / f"dashboard-{args[0]}.log").open("ab") as stream:
        common = dict(cwd=config.project_root(), env=worker_env(media_root),
                      stdin=subprocess.DEVNULL, stdout=stream, stderr=stream)
        if os.name == "nt":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                # Leave any job the dashboard runs in (a terminal or IDE may kill its job on close).
                process = subprocess.Popen(command, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **common)
            except OSError:
                # That job forbids breakaway: start inside it, and say so on the dashboard console.
                process = subprocess.Popen(command, creationflags=flags, **common)
                print("WARNING: the dashboard runs inside a Windows job that does not allow breakaway, so the "
                      f"{args[0]} worker (pid {process.pid}) may stop when the program that started the "
                      "dashboard closes. Start the dashboard with start-dashboard.bat.", flush=True)
        else:
            process = subprocess.Popen(command, start_new_session=True, **common)
    threading.Thread(target=process.wait, daemon=True).start()
    return process.pid


def post_worker_running(path, item_id):
    """A one-off posting worker for this item is already waiting (so a second one is not started)."""
    raw = db.kv_get(f"dashboard:post_worker:{item_id}", db_path=path)
    try:
        record = json.loads(raw or "{}")
        return bool(record.get("pid")) and runner.is_pid_alive(int(record["pid"]))
    except (TypeError, ValueError):
        return False


def spawn_post_worker(path, media_root, command_id, item_id):
    pid = spawn(path, media_root, "post_now", "--job", command_id)
    db.kv_set(f"dashboard:post_worker:{item_id}", json.dumps({"pid": pid, "job": command_id, "at": time.time()}), path)
    return pid


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
        publishing = json.loads(db.kv_get(runner.PUBLISHING_KEY, db_path=path) or "{}")
        if publishing.get("pid") == lock["pid"]:
            raise ControlError("The runner is publishing a post right now. Wait for it to finish.")
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

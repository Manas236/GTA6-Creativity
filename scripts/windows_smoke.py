"""Exercise the dashboard process and detached workers on a private database copy."""

import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import sysconfig
import tempfile
import time

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from pipeline import binding, config, creation, db, engagement, runner
from dashboard import commands, store

CHECKS = ("Runner start", "Graceful stop", "Immediate control", "Make N detach",
          "YouTube replay graph", "Twitch replay chat")
FORBIDDEN_STAGES = {"post", "posting", "publish", "settle", "measure", "analytics", "cleanup"}


class Skip(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def guard_copy(original, target, sandbox, cfg):
    """Fail before starting a dashboard if its target or configuration is unsafe."""
    require(cfg.get("app", {}).get("dry_run") is True, "Refusing to run without app.dry_run=true.")
    require(target.resolve() != original.resolve() and not target.is_symlink()
            and target.resolve().parent == sandbox.resolve()
            and target.name == "smoke-copy.db", "Refusing to use the original DB or a path outside the sandbox.")
    if target.exists() and original.exists():
        require(not target.samefile(original), "Refusing to use a hard link to the original DB.")


def copy_database(original, target, sandbox, cfg):
    guard_copy(original, target, sandbox, cfg)
    require(original.is_file(), "The requested source database does not exist.")
    with target.open("xb"):
        pass
    # SQLite's backup API includes committed WAL data. Open the source read-only.
    with closing(sqlite3.connect(original.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination, pages=256)


def database_fingerprint(path):
    """sha256 of the DB file and its -wal when present. -shm is SQLite's shared-memory index, not data."""
    path = Path(path)
    hashes = {}
    for candidate in (path, path.with_name(path.name + "-wal")):
        if candidate.is_file():
            with candidate.open("rb") as stream:
                hashes[candidate.name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return hashes


def database_check(path, before, after):
    """The summary row proving the source DB was not modified by the smoke run."""
    name = Path(path).name
    if name not in before:
        return "FAIL", f"{name} was not found before the run."
    if before != after:
        changed = sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
        return "FAIL", (f"changed: {', '.join(changed)} "
                        f"(sha256 {before[name][:12]} -> {after.get(name, 'missing')[:12]})")
    return "PASS", f"unchanged (sha256 {before[name][:12]})"


def download_row_ready(row):
    """The finished download row, None while yt-dlp is still running, or an error for any other status."""
    if row is None or row["status"] == "start":
        return None
    require(row["status"] == "success", row["message"])
    return row


SEED_ID = "smoke-approval-check"


def seed_approved_item(sandbox, path):
    """Add one placeholder approved clip to the sandbox copy so approve/unapprove run for real."""
    require(path.name == "smoke-copy.db" and not path.is_symlink()
            and path.resolve().parent == Path(sandbox).resolve(),
            "Refusing to seed anything outside the sandbox copy.")
    folder = Path(sandbox) / "media" / "smoke-seed"
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key in ("download_path", "cut_path", "voiceover_audio_path", "render_a_path", "render_b_path"):
        placeholder = folder / f"{key}.mp4"
        placeholder.write_bytes(b"smoke placeholder")
        paths[key] = str(placeholder)
    db.insert_discovered_item({
        "id": SEED_ID, "source": "youtube", "source_id": SEED_ID,
        "source_url": "https://example.invalid/smoke-approval-check",
        "author_name": "Smoke test", "original_title": "Smoke approval check", "raw_velocity": 0,
    }, path)
    script = "Smoke test approval check"
    fields = {**paths, "cut_start": 0.0, "cut_end": 10.0, "cut_duration": 10.0, "hook_text": "SMOKE TEST?",
              "voiceover_script": script, "hook_style": "question", "hook_grounded": 1}
    item = {"id": SEED_ID, **fields}
    fields["hook_binding"] = binding.hook_record(item, fields["hook_text"], script, script, "segment_check")
    fields["voiceover_binding"] = binding.voiceover_record(item, script, paths["voiceover_audio_path"])
    db.transition_stage(SEED_ID, "approved", {**fields, "approval_type": "approved"}, path)
    return SEED_ID


def wait_for(check, seconds, reason, progress=None):
    deadline = time.monotonic() + seconds
    next_report = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        if progress and time.monotonic() >= next_report:
            print(progress(), flush=True)
            next_report += 10
        time.sleep(0.25)
    raise RuntimeError(reason)


class WindowsJob:
    """Own the dashboard and all descendants, including detached processes."""

    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.handle = None

    def __enter__(self):
        class BasicLimits(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("process_limit", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
                        ("memory", ctypes.c_size_t * 4)]

        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.__exit__()
            raise ctypes.WinError(error)
        return self

    def attach(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def __exit__(self, *_):
        if self.handle:
            self.kernel.CloseHandle(self.handle)


def stage_sandbox(sandbox, original):
    import yaml

    for package in ("dashboard", "pipeline"):
        shutil.copytree(PROJECT / package, sandbox / package, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(PROJECT / "connection_checks.py", sandbox / "connection_checks.py")
    (sandbox / "scripts").mkdir()
    shutil.copy2(__file__, sandbox / "scripts" / Path(__file__).name)
    for folder in ("fonts", "sfx", "emoji"):
        source = PROJECT / "assets" / folder
        if source.is_dir():
            shutil.copytree(source, sandbox / "assets" / folder)

    cfg = config.merge_config(config.get_config(reload=True), {
        "app": {"dry_run": True},
        "posting": {"platforms": {"instagram": False, "facebook": False}},
        "orchestrator": {"heartbeat_interval_seconds": 2, "heartbeat_stale_seconds": 30},
    })
    (sandbox / "config.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    copy = sandbox / "smoke-copy.db"
    copy_database(original, copy, sandbox, cfg)
    store.initialize(copy)
    with db.db(copy) as connection:
        connection.execute("DELETE FROM dashboard_commands")
        connection.execute("DELETE FROM kv WHERE key IN (?, ?, ?, ?)",
                           (runner.LOCK_KEY, runner.STOP_KEY, "runner:stage", "dashboard:runner_start"))
    # The loop exercises real locking/heartbeats without running production stages.
    db.kv_set("dashboard:demo", "1", copy)
    path = sandbox / "media"
    path.mkdir()
    return copy


def worker_environment(sandbox):
    # Only discovery credentials are forwarded. The sandbox has no production .env.
    keys = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
            "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA",
            "HOME", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY"}
    env = {key: value for key, value in os.environ.items() if key.upper() in keys}
    for key in ("TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET"):
        value = config.secret(key)
        if value:
            env[key] = value
    if os.name == "nt":
        # Use the real interpreter, avoiding a venv redirector PID outside the job.
        paths = sysconfig.get_paths()
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys((paths["purelib"], paths["platlib"])))
    env.update(GTA6_MEDIA_DIR=str(sandbox / "media"), PYTHONUNBUFFERED="1")
    return env


def dashboard_child():
    from dashboard import workers
    from dashboard.app import create_app
    from waitress import create_server

    # The parent releases this gate only after assigning the process to its Windows job.
    if sys.stdin.readline().strip() != "start":
        return
    sandbox = PROJECT
    target = sandbox / "smoke-copy.db"
    cfg = config.get_config(reload=True)
    require(target.is_file() and not target.is_symlink() and target.resolve().parent == sandbox,
            "The smoke DB must be a regular file inside the isolated project.")
    require(cfg.get("app", {}).get("dry_run") is True, "Smoke dashboard refused live mode.")
    spawn = workers.spawn

    def track_spawn(path, root, *args):
        require(Path(path) == target and Path(root) == sandbox / "media"
                and config.get_config(reload=True)["app"]["dry_run"] is True,
                "Worker target or dry-run setting changed.")
        pid = spawn(path, root, *args)
        with (sandbox / "workers.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": pid, "args": args}) + "\n")
        return pid

    workers.spawn = track_spawn
    server = create_server(create_app(target, cfg=cfg, media_root=sandbox / "media"),
                           host="127.0.0.1", port=0, threads=4)
    (sandbox / "port").write_text(str(server.effective_port), encoding="ascii")
    server.run()


class Smoke:
    def __init__(self, sandbox, path):
        import requests

        self.sandbox = sandbox
        self.path = path
        self.http = requests.Session()
        self.http.trust_env = False
        self.runner_pid = None
        self.results = {}

    def workers(self):
        ledger = self.sandbox / "workers.jsonl"
        if not ledger.exists():
            return []
        with ledger.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.endswith("\n")]

    def api(self, path, payload=None):
        url = self.url + "/api/" + path
        response = self.http.get(url, timeout=10) if payload is None else self.http.post(
            url, json=payload, headers={"X-CSRF-Token": self.csrf}, timeout=15)
        require(response.ok, f"{path} HTTP {response.status_code}: {response.text[:240]}")
        return response.json()

    def submit(self, action, item_id=None, **payload):
        prepared = self.api("prepare", {"action": action, "item_id": item_id, "payload": payload})
        response = self.api("confirm", {"ticket": prepared["ticket"]})
        require(not response.get("warning"), response.get("warning"))
        return response["command"]

    def check(self, name, function):
        try:
            status, reason = "PASS", function()
        except Skip as exc:
            status, reason = "SKIP", str(exc)
        except Exception as exc:
            status, reason = "FAIL", f"{type(exc).__name__}: {exc}"
        reason = " ".join(str(reason).splitlines())
        self.results[name] = (status, reason)
        print(f"{status} {name}: {reason}", flush=True)

    def start(self):
        requested = time.time()
        self.api("runner", {"operation": "start"})

        def fresh():
            raw = db.kv_get(runner.LOCK_KEY, db_path=self.path)
            lock = json.loads(raw or "{}")
            if lock.get("kind") == "loop" and lock["started_at"] >= requested:
                self.runner_pid = lock["pid"]
                if lock["heartbeat"] > lock["started_at"] and runner.is_pid_alive(self.runner_pid):
                    return lock

        wait_for(fresh, 30, "Runner did not acquire and refresh its lock within 30 seconds.")
        return f"PID {self.runner_pid} acquired the lock and advanced its heartbeat."

    def stop(self):
        require(self.runner_pid, "No runner PID was observed.")
        self.api("runner", {"operation": "stop"})
        pids = [row["pid"] for row in self.workers()]
        require(self.runner_pid in pids, "The runner PID was missing from the process ledger.")
        wait_for(lambda: not db.kv_get(runner.LOCK_KEY, db_path=self.path)
                 and not any(runner.is_pid_alive(pid) for pid in pids),
                 120, f"Lock not released or a started worker still exists (PIDs {pids}).")
        return f"Lock released and all started runner PIDs are gone: {pids}."

    def apply_quick(self, action, item_id):
        result = self.submit(action, item_id)
        expected = "awaiting_approval" if action == "unapprove" else "approved"
        require(result["status"] == "applied" and db.get_item(item_id, self.path)["stage"] == expected,
                f"{action} did not apply synchronously: {result['status']}.")
        require(not db.kv_get(runner.LOCK_KEY, db_path=self.path), "Quick action left its lock behind.")

    def quick_control(self):
        state = self.api("state")
        for action in ("unapprove", "approve"):
            for item in state["items"]:
                if action not in item["actions"]:
                    continue
                try:
                    commands.prepare(action, item["id"], {}, self.path, self.cfg)
                except (commands.ControlError, binding.CaptionMismatchError):
                    continue
                self.apply_quick(action, item["id"])
                return f"{action} applied to {item['id']} during the API request and released its lock."
        # No real clip qualifies: exercise both controls on a placeholder in the temp copy only.
        item_id = seed_approved_item(self.sandbox, self.path)
        for action in ("unapprove", "approve"):
            self.apply_quick(action, item_id)
        return (f"Seeded {item_id} in the temp copy; unapprove then approve applied during "
                "the API request and released the lock.")

    def make_logs(self):
        log = self.sandbox / "media" / "logs" / "dashboard-make.log"
        return log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""

    def job_status(self, job_id):
        with db.db(self.path) as connection:
            return connection.execute("SELECT status, result FROM dashboard_commands WHERE id=?",
                                      (job_id,)).fetchone()

    def make(self):
        before = len(self.make_logs())
        job_id = self.submit("make", count=1)["id"]
        processes = [row for row in self.workers() if row["args"] == ["make", "--job", job_id]]
        require(len(processes) == 1, "The dashboard did not spawn a unique Make worker.")
        pid = processes[0]["pid"]
        require(runner.is_pid_alive(pid), f"PID {pid} was already gone when the HTTP request returned.")

        def finished():
            status, result = self.job_status(job_id)
            return (status, result) if status not in ("pending", "running") and not runner.is_pid_alive(pid) else None

        print(f"Make N worker PID {pid} survived the HTTP response; waiting for stage logs.", flush=True)
        status, result = wait_for(finished, 900, f"Make worker PID {pid} exceeded 15 minutes.",
                                  lambda: f"Make PID {pid}: {db.kv_get('runner:stage', db_path=self.path)}")
        log = self.make_logs()[before:]
        stages = set(re.findall(r"Make item \S+ stage (\w+)|Stage (\w+)", log))
        stages = {name for pair in stages for name in pair if name}
        require(not (stages & FORBIDDEN_STAGES), f"Forbidden stage logged: {stages & FORBIDDEN_STAGES}.")
        require(status == "applied" and "download" in stages,
                f"Make never reached download ({status}: {result}).")
        require(not db.kv_get(runner.LOCK_KEY, db_path=self.path), "Make worker left its lock behind.")
        return f"PID {pid} survived the response, reached download and exited without forbidden stages."

    def youtube(self, url):
        if not url:
            raise Skip("No YouTube URL was supplied.")
        platform, _, _ = creation.source_from_url(url)
        require(platform == "youtube", "Provide a YouTube video URL.")
        job_id = self.submit("clip_url", url=url)["id"]

        def downloaded():
            # Read the job first so a job that ends between the two queries still shows its final row.
            status, result = self.job_status(job_id)
            with db.db(self.path) as connection:
                row = connection.execute(
                    "SELECT items.heatmap, stage_log.status, stage_log.message FROM items "
                    "JOIN stage_log ON stage_log.item_id=items.id WHERE items.creation_job=? "
                    "AND stage_log.stage='download' ORDER BY stage_log.id DESC LIMIT 1", (job_id,)
                ).fetchone()
            if download_row_ready(row):
                return row
            with db.db(self.path) as connection:
                item = connection.execute(
                    "SELECT stage, error_stage, error_message FROM items WHERE creation_job=?", (job_id,)
                ).fetchone()
            if item and item["stage"] == "failed" and item["error_stage"] == "download":
                raise RuntimeError(item["error_message"])
            require(status in ("pending", "running"), f"Clip from URL ended before download: {status}: {result}")

        row = wait_for(downloaded, 900, "Clip from URL did not finish downloading within 15 minutes.")
        if not row["heatmap"]:
            raise Skip("video has no replay graph")
        return f"Clip from URL stored {len(json.loads(row['heatmap']))} yt-dlp heatmap intervals."

    def twitch(self, url):
        if not url:
            raise Skip("No Twitch VOD URL was supplied.")
        platform, source_id, canonical = creation.source_from_url(url)
        require(platform == "twitch" and source_id.startswith("vod:"), "Provide a Twitch VOD URL.")
        count, spikes = engagement.chat_replay(canonical, None, self.cfg)
        require(count > 0, "chat-downloader returned 0 timed messages and 0 spikes.")
        return f"chat-downloader fetched {count} timed messages; found {len(spikes)} spikes."

    def run(self, process, youtube, twitch):
        port = self.sandbox / "port"

        def ready():
            require(process.poll() is None, "Smoke dashboard exited before opening its port.")
            return port.exists() and port.read_text(encoding="ascii").strip()

        self.url = f"http://127.0.0.1:{wait_for(ready, 30, 'Smoke dashboard did not open a port.')}"
        state = self.api("state")
        require(state["dry_run"] is True, "Dashboard did not report dry-run mode.")
        self.csrf = state["csrf"]
        self.cfg = config.get_config()
        self.check(CHECKS[0], self.start)
        self.check(CHECKS[1], self.stop)
        if self.results[CHECKS[1]][0] != "PASS":
            raise RuntimeError("Runner shutdown was not verified; refusing to submit more work.")
        self.check(CHECKS[2], self.quick_control)
        db.kv_set("dashboard:demo", "", self.path)
        self.check(CHECKS[3], self.make)
        if db.kv_get(runner.LOCK_KEY, db_path=self.path):
            raise RuntimeError("Make did not release its lock; refusing to submit more work.")
        self.check(CHECKS[4], lambda: self.youtube(youtube))
        self.check(CHECKS[5], lambda: self.twitch(twitch))


def execute(smoke, youtube, twitch):
    from contextlib import nullcontext

    sandbox = smoke.sandbox
    with (sandbox / "dashboard.log").open("wb") as output:
        with (WindowsJob() if os.name == "nt" else nullcontext()) as job:
            process = subprocess.Popen(
                [sys._base_executable if os.name == "nt" else sys.executable, str(sandbox / "scripts" / Path(__file__).name), "--dashboard-child"],
                cwd=sandbox, env=worker_environment(sandbox), stdin=subprocess.PIPE,
                stdout=output, stderr=output, text=True, start_new_session=os.name != "nt",
            )
            try:
                if job:
                    job.attach(process)
                process.stdin.write("start\n")
                process.stdin.close()
                print(f"Dashboard PID {process.pid}", flush=True)
                smoke.run(process, youtube, twitch)
            finally:
                if not job:
                    # The Linux sandbox can also exercise the HTTP flow.
                    for row in smoke.workers():
                        try:
                            os.killpg(row["pid"], signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=15)
        pids = [process.pid, *(row["pid"] for row in smoke.workers())]
        wait_for(lambda: not any(runner.is_pid_alive(pid) for pid in pids), 15,
                 f"Cleanup could not confirm all PIDs are gone: {pids}.")
        print(f"Cleanup confirmed Python PIDs gone: {pids}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", type=Path, default=PROJECT / "pipeline.db")
    parser.add_argument("--youtube")
    parser.add_argument("--twitch")
    args = parser.parse_args()
    result = {}
    problem = None
    before = database_fingerprint(args.database)
    with tempfile.TemporaryDirectory(prefix="gta6-smoke-") as folder:
        sandbox = Path(folder).resolve()
        try:
            copy = stage_sandbox(sandbox, args.database)
            smoke = Smoke(sandbox, copy)
            result = smoke.results
            execute(smoke, args.youtube, args.twitch)
        except (Exception, KeyboardInterrupt) as exc:
            problem = " ".join(f"{type(exc).__name__}: {exc}".splitlines())
            for name in ("dashboard.log", "media/logs/dashboard-make.log"):
                log = sandbox / name
                if log.is_file():
                    print(f"{name}:\n" + "\n".join(log.read_text(errors="replace").splitlines()[-8:]))
    # After cleanup: the sandbox is deleted and the kill-on-close job has ended every process it owned.
    db_status, db_reason = database_check(args.database, before, database_fingerprint(args.database))
    rows = ["Check                  Result  Reason", "-" * 72]
    for name in CHECKS:
        status, reason = result.get(name, ("SKIP", "Blocked by an earlier check."))
        rows.append(f"{name:<23}{status:<8}{reason}")
    rows.append(f"{'Real DB':<23}{db_status:<8}{db_reason}")
    if problem:
        rows.append(f"Harness                 FAIL    {problem}")
    report = PROJECT / "media" / "smoke" / f"windows_smoke_{datetime.now():%Y%m%d_%H%M%S_%f}.txt"
    report.parent.mkdir(parents=True, exist_ok=True)
    summary = "\n".join(rows) + "\n"
    report.write_text(summary, encoding="utf-8")
    print(f"\n{summary}Report: {report}", flush=True)
    return int(bool(problem) or db_status == "FAIL" or any(status == "FAIL" for status, _ in result.values()))


if __name__ == "__main__":
    if sys.argv[1:] == ["--dashboard-child"]:
        dashboard_child()
    else:
        sys.exit(main())

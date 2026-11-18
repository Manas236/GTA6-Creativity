"""Cloudflare quick tunnel (`cloudflared tunnel --url ...`) as a supervised child process.

No Cloudflare account: cloudflared prints a random https://<words>.trycloudflare.com URL.
Exactly that hostname is added to the dashboard's Host/Origin allowlist for this run.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

URL_PATTERN = re.compile(r"https://([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.trycloudflare\.com)\b")
URL_TIMEOUT_S = 60
MAX_RESTARTS = 5
STABLE_S = 600  # a tunnel that ran this long resets the restart budget


class RemoteState:
    """Shared between the Flask app (allowlist, status line) and the tunnel supervisor."""

    def __init__(self, status: str = "off"):
        self.lock = threading.Lock()
        self.status = status  # off | waiting_for_password | starting | on | failed
        self.url: str | None = None
        self.host: str | None = None
        self.message = ""

    def set(self, status: str, url: str | None = None, message: str = "") -> None:
        with self.lock:
            self.status = status
            self.url = url
            self.host = URL_PATTERN.match(url).group(1) if url else None
            self.message = message

    def allowed_host(self, host: str) -> bool:
        with self.lock:
            return bool(self.host) and host.lower() == self.host

    def describe(self) -> dict:
        with self.lock:
            return {"status": self.status, "url": self.url, "message": self.message}


def find_cloudflared(configured: str | None = None) -> str | None:
    if configured:
        path = Path(os.path.expandvars(os.path.expanduser(str(configured))))
        return str(path) if path.is_file() else None
    found = shutil.which("cloudflared")
    if found:
        return found
    # winget adds cloudflared to PATH, but a console opened before the install does not see it yet.
    candidates = []
    for base, *rest in (("LOCALAPPDATA", "Microsoft", "WinGet", "Links", "cloudflared.exe"),
                        ("ProgramFiles(x86)", "cloudflared", "cloudflared.exe"),
                        ("ProgramFiles", "cloudflared", "cloudflared.exe")):
        if os.environ.get(base):
            candidates.append(Path(os.environ[base], *rest))
    return next((str(path) for path in candidates if path.is_file()), None)


def _kill_with_parent(proc) -> object | None:
    """Windows: a job object that kills cloudflared when this process ends for any reason,
    including the console window being closed (atexit does not run then)."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IoCounters(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BasicLimits(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        kernel32.AssignProcessToJobObject(job, int(proc._handle))
        return job  # the handle closes when this process exits, which kills the job's processes
    except Exception:  # noqa: BLE001 - best effort; atexit and stop() still terminate it
        return None


def _linux_parent_death():
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except Exception:  # noqa: BLE001
        pass


class Tunnel:
    def __init__(self, executable: str, port: int, state: RemoteState, notify, *,
                 popen=subprocess.Popen, wait=None, clock=time.monotonic,
                 timeout_s: float = URL_TIMEOUT_S, echo=None):
        self.executable = executable
        self.port = port
        self.state = state
        self.notify = notify
        self.popen = popen
        self.stopping = threading.Event()
        self.wait = wait or self.stopping.wait  # returns True when the dashboard is stopping
        self.clock = clock
        self.timeout_s = timeout_s
        self.echo = echo or (lambda text: print(text))
        self.proc = None
        self.job = None
        self.thread = None

    def command(self) -> list[str]:
        return [self.executable, "tunnel", "--url", f"http://127.0.0.1:{self.port}", "--no-autoupdate"]

    def start_once(self) -> str | None:
        """Start cloudflared and wait for its URL. Returns the URL, or None after the timeout."""
        self.state.set("starting")
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL,
                  "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1}
        if sys.platform.startswith("linux"):
            kwargs["preexec_fn"] = _linux_parent_death
        self.proc = self.popen(self.command(), **kwargs)
        self.job = _kill_with_parent(self.proc)
        found = threading.Event()
        result: dict[str, str] = {}

        def read(proc=self.proc):
            for line in proc.stdout:
                match = URL_PATTERN.search(line)
                if match and not found.is_set():
                    result["url"] = match.group(0)
                    found.set()
            found.set()  # output ended (process exited)

        threading.Thread(target=read, daemon=True).start()
        found.wait(self.timeout_s)
        url = result.get("url")
        if not url:
            self.terminate()
            return None
        self.state.set("on", url)
        self.echo("")
        self.echo("=" * 64)
        self.echo(f"  REMOTE LINK (password protected): {url}")
        self.echo("=" * 64)
        self.notify(f"Dashboard link: {url}")
        return url

    def start(self) -> None:
        self.thread = threading.Thread(target=self.supervise, daemon=True, name="cloudflared")
        self.thread.start()
        atexit.register(self.stop)

    def supervise(self) -> None:
        failures = 0
        while not self.stopping.is_set():
            started = self.clock()
            url = self.start_once()
            if url is None:
                if self.stopping.is_set():
                    break
                self.echo(f"Remote link: cloudflared printed no trycloudflare.com URL within "
                          f"{self.timeout_s:.0f} s. The dashboard keeps running locally.")
            else:
                self.proc.wait()
                if self.stopping.is_set():
                    break
                self.echo(f"Remote link: cloudflared exited (code {self.proc.returncode}).")
            if url is not None and self.clock() - started >= STABLE_S:
                failures = 0
            failures += 1
            if failures > MAX_RESTARTS:
                self.state.set("failed", message="cloudflared stopped; restart the dashboard to retry.")
                self.echo("Remote link: giving up after 5 restarts. Local access still works.")
                return
            self.state.set("starting", message=f"Restarting cloudflared (attempt {failures} of {MAX_RESTARTS}).")
            if self.wait(min(5 * 2 ** (failures - 1), 120)):
                break

    def terminate(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def stop(self) -> None:
        self.stopping.set()
        self.terminate()
        if self.state.status != "off":
            self.state.set("off")

"""Helpers for the Windows launchers (setup.bat, start-dashboard.bat, update.bat).

    python scripts/launcher_tools.py selftest            # PASS/FAIL checks; exit 0 = pass
    python scripts/launcher_tools.py port                # print the dashboard port (.env DASHBOARD_PORT)
    python scripts/launcher_tools.py port-status         # exit 0 free, 10 dashboard running, 11 other program
    python scripts/launcher_tools.py wait-open           # wait until the dashboard answers, open the browser

Never prints secret values: the env check reports variable names only.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

DEFAULT_PORT = 8765
FREE, DASHBOARD_RUNNING, OTHER_PROGRAM = 0, 10, 11


def configured_port() -> int:
    try:
        from env_wizard import read_env
        value = read_env()[1].get("DASHBOARD_PORT", "").strip()
        port = int(value) if value else DEFAULT_PORT
        return port if 1024 <= port <= 65535 else DEFAULT_PORT
    except Exception:
        return DEFAULT_PORT


def _get(url: str, timeout: float = 2.0) -> tuple[int, bytes]:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read(200_000)


def port_status(port: int) -> int:
    in_use = False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        in_use = s.connect_ex(("127.0.0.1", port)) == 0
    if not in_use:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                in_use = True
    if not in_use:
        return FREE
    try:
        status, body = _get(f"http://127.0.0.1:{port}/api/state")
        if status == 200 and "csrf" in json.loads(body):
            return DASHBOARD_RUNNING
    except Exception:
        pass
    return OTHER_PROGRAM


def wait_open(port: int, timeout: float) -> int:
    url = f"http://127.0.0.1:{port}/"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _get(url, timeout=1.5)[0] == 200:
                webbrowser.open(url)
                return 0
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.5)
    return 1


# --------------------------------------------------------------------------- self-test

class Report:
    def __init__(self) -> None:
        self.fails: list[str] = []
        self.warns: list[str] = []

    def ok(self, name: str, detail: str = "") -> None:
        print(f"  [PASS] {name}" + (f" - {detail}" if detail else ""))

    def fail(self, name: str, detail: str) -> None:
        self.fails.append(name)
        print(f"  [FAIL] {name} - {detail}")

    def warn(self, name: str, detail: str) -> None:
        self.warns.append(name)
        print(f"  [WARN] {name} - {detail}")


PACKAGES = ["flask", "waitress", "yaml", "requests", "numpy", "PIL", "cv2", "soundfile",
            "onnxruntime", "kokoro_onnx", "yt_dlp", "fontTools", "tzdata"]


def check_python(r: Report) -> None:
    v = sys.version_info
    if (3, 11) <= v[:2] <= (3, 13):
        r.ok("Python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        r.warn("Python", f"{v.major}.{v.minor} is untested; the project uses 3.11")


def check_imports(r: Report) -> None:
    bad = []
    for name in PACKAGES:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - report any import problem
            bad.append(f"{name} ({type(e).__name__}: {e})")
    if bad:
        r.fail("Python packages", "; ".join(bad) + " - re-run setup.bat")
    else:
        r.ok("Python packages", f"{len(PACKAGES)} imported")

    try:
        importlib.import_module("faster_whisper")
        r.ok("Transcription (faster-whisper)")
    except OSError as e:
        if getattr(e, "winerror", None) == 4551 or "Application Control" in str(e):
            r.warn("Transcription (faster-whisper)",
                   "Windows Smart App Control blocks ctranslate2.dll, so the cut/hooks stages can't "
                   "transcribe on this PC. The dashboard is not affected.")
        else:
            r.fail("Transcription (faster-whisper)", f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        r.fail("Transcription (faster-whisper)", f"{type(e).__name__}: {e}")

    for mod in ("pipeline.runner", "dashboard.app"):
        try:
            importlib.import_module(mod)
        except Exception as e:  # noqa: BLE001
            r.fail(f"import {mod}", f"{type(e).__name__}: {e}")
            return
    logging.getLogger().setLevel(logging.WARNING)  # pipeline.runner configures INFO logging
    r.ok("Project code", "pipeline + dashboard import")


def check_config(r: Report) -> None:
    try:
        from pipeline import config
        cfg = config.get_config(reload=True)
        mode = "dry run" if cfg.get("app", {}).get("dry_run") else "LIVE posting"
        r.ok("config.yaml", f"loads (app.dry_run -> {mode})")
    except Exception as e:  # noqa: BLE001
        r.fail("config.yaml", f"{type(e).__name__}: {e}")


def check_ffmpeg(r: Report) -> None:
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        missing = [n for n, p in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not p]
        r.fail("FFmpeg", f"{', '.join(missing)} not found. Install it: winget install Gyan.FFmpeg, "
                         "then run setup.bat again")
        return
    try:
        version = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True,
                                 timeout=30).stdout.splitlines()[0].split(" Copyright")[0]
        filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
                                 timeout=30).stdout
    except Exception as e:  # noqa: BLE001
        r.fail("FFmpeg", f"found at {ffmpeg} but would not run: {e}")
        return
    if not any(line.split()[1:2] == ["ass"] for line in filters.splitlines() if line.strip()):
        r.fail("FFmpeg", f"{version} lacks libass (the 'ass' filter). Install a full build: winget install Gyan.FFmpeg")
        return
    r.ok("FFmpeg", f"{version}, libass OK ({Path(ffmpeg).parent})")


def check_dashboard_app(r: Report) -> None:
    """The real Flask app against a throwaway database: the actual pipeline.db is not touched."""
    try:
        from dashboard.app import create_app
        with tempfile.TemporaryDirectory(prefix="gta6-selftest-", ignore_cleanup_errors=True) as tmp:
            app = create_app(Path(tmp) / "selftest.db", media_root=Path(tmp) / "media")
            client = app.test_client()
            home = client.get("/", base_url="http://127.0.0.1")
            state = client.get("/api/state", base_url="http://127.0.0.1")
            if home.status_code == 200 and state.status_code == 200 and "csrf" in state.get_json():
                r.ok("Dashboard app", "serves pages and /api/state (temporary database)")
            else:
                r.fail("Dashboard app", f"HTTP {home.status_code} / {state.status_code}")
    except Exception as e:  # noqa: BLE001
        r.fail("Dashboard app", f"{type(e).__name__}: {e}")


def check_env(r: Report) -> None:
    from env_wizard import ENV, missing_required, parse_example, read_env
    from pipeline import config
    if not ENV.exists():
        r.fail(".env", "not created yet - run setup.bat and answer the prompts")
        return
    values = config.load_env(ENV)  # same reader as the pipeline (.env, then Windows env vars)
    missing = missing_required(values, parse_example())
    if missing:
        r.fail("Required .env values", "empty: " + ", ".join(missing)
               + " - run setup.bat to add them (the dashboard works without them)")
    else:
        r.ok("Required .env values", "all set")


def check_assets(r: Report) -> None:
    fonts = list((ROOT / "assets" / "fonts").glob("Montserrat-*.ttf"))
    if not fonts:
        r.fail("Fonts", "assets/fonts/Montserrat-*.ttf missing from the checkout")
    else:
        r.ok("Fonts", f"{len(fonts)} Montserrat faces")
    if not (ROOT / "assets" / "music" / "bed.mp3").exists():
        r.warn("Music bed", "assets/music/bed.mp3 not downloaded (only preset P3 uses it); "
                            "setup.bat retries it next run")


def check_port(r: Report, port: int) -> None:
    status = port_status(port)
    if status == FREE:
        r.ok("Dashboard port", f"{port} is free")
    elif status == DASHBOARD_RUNNING:
        r.ok("Dashboard port", f"{port}: the dashboard is already running there")
    else:
        r.fail("Dashboard port", f"{port} is used by another program. Close it, or set "
                                 f"DASHBOARD_PORT to another number in .env")


def selftest(port: int) -> int:
    print("Self-test:")
    r = Report()
    check_python(r)
    check_imports(r)
    check_config(r)
    check_ffmpeg(r)
    check_assets(r)
    check_dashboard_app(r)
    check_env(r)
    check_port(r, port)
    print()
    if r.fails:
        print(f"SELF-TEST: FAIL ({', '.join(r.fails)})")
        return 1
    print("SELF-TEST: PASS" + (f" with {len(r.warns)} warning(s): {', '.join(r.warns)}" if r.warns else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Helpers for the Windows launchers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("selftest", "port-status", "wait-open"):
        p = sub.add_parser(name)
        p.add_argument("--port", type=int, default=None)
        if name == "wait-open":
            p.add_argument("--timeout", type=float, default=120.0)
    sub.add_parser("port")
    args = ap.parse_args(argv)
    port = getattr(args, "port", None) or configured_port()
    if args.cmd == "port":
        print(port)
        return 0
    if args.cmd == "port-status":
        return port_status(port)
    if args.cmd == "wait-open":
        return wait_open(port, args.timeout)
    return selftest(port)


if __name__ == "__main__":
    sys.exit(main())

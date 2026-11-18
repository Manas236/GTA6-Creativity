"""python -m dashboard [--db PATH] [--media-root PATH] [--port 8765]
python -m dashboard set-password [--db PATH]
python -m dashboard clear-password [--db PATH]"""

import argparse
import getpass
import sys
from pathlib import Path

from . import auth


def env_file() -> Path:
    from pipeline import config
    return config.project_root() / ".env"


def password_command(argv, *, prompt=getpass.getpass, env_path=None, out=print):
    """set-password / clear-password. Neither prints the password or its hash."""
    from pipeline import db
    from .store import initialize

    parser = argparse.ArgumentParser(prog=f"python -m dashboard {argv[0]}")
    parser.add_argument("--db", type=Path, help="SQLite database (default: pipeline.db)")
    args = parser.parse_args(argv[1:])
    target = Path(env_path or env_file())
    db_path = Path(args.db or db.DEFAULT_DB_PATH).resolve()
    if argv[0] == "set-password":
        first = prompt("New dashboard password (at least 12 characters): ")
        if len(first) < auth.MIN_LENGTH:
            out(f"Refused: the password must be at least {auth.MIN_LENGTH} characters. Nothing changed.")
            return 1
        if prompt("Repeat the password: ") != first:
            out("Refused: the two passwords differ. Nothing changed.")
            return 1
        backup = auth.write_hash(target, auth.make_hash(first))
        message = "Password set. Every existing dashboard session is signed out."
    else:
        if not auth.read_hash(target):
            out("No dashboard password is set. Nothing changed.")
            return 0
        backup = auth.write_hash(target, None)
        message = ("Password removed. The dashboard is unauthenticated again (loopback only) "
                   "and the remote link will not start.")
    initialize(db_path)
    auth.rotate_sessions(db_path)
    if backup:
        out(f"Backup of the previous .env: {backup.name}")
    out(message)
    out("Restart the dashboard if it is running, so the remote link picks up the change.")
    return 0


def start_remote(cfg, port, app, out=print):
    """Start the quick tunnel when dashboard.remote.enabled is true. Returns the Tunnel or None."""
    from .tunnel import Tunnel, find_cloudflared

    remote_cfg = ((cfg.get("dashboard") or {}).get("remote") or {})
    state = app.config["DASHBOARD_REMOTE"]
    if not remote_cfg.get("enabled"):
        state.set("off")
        return None
    if not auth.read_hash(app.config["DASHBOARD_ENV"]):
        state.set("waiting_for_password", message="Run python -m dashboard set-password, then restart.")
        out("Remote link: waiting for a password. dashboard.remote.enabled is true, but no "
            "DASHBOARD_PASSWORD_HASH is set, so the tunnel was NOT started. Run "
            "`python -m dashboard set-password`, then restart the dashboard. Local access works.")
        return None
    executable = find_cloudflared(remote_cfg.get("cloudflared_path"))
    if not executable:
        state.set("failed", message="cloudflared not found.")
        out("Remote link: cloudflared was not found (install it with `winget install --id "
            "Cloudflare.cloudflared`, or set dashboard.remote.cloudflared_path). Local access works.")
        return None
    notify = app.config["DASHBOARD_NOTIFY"]
    tunnel = Tunnel(executable, port, state, notify, echo=lambda text: out(text))
    out("Remote link: starting cloudflared (this can take up to a minute)...")
    tunnel.start()
    return tunnel


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("set-password", "clear-password"):
        raise SystemExit(password_command(argv))
    parser = argparse.ArgumentParser(description="Local GTA 6 pipeline control panel")
    parser.add_argument(
        "--db", type=Path, help="SQLite database (default: pipeline.db)"
    )
    parser.add_argument(
        "--media-root", type=Path, help="Allowed media directory, including demo media"
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    from waitress import serve

    from pipeline import config, telegram_bot
    from .app import create_app
    from .tunnel import RemoteState

    def out(text):
        print(text, flush=True)

    remote = RemoteState()
    app = create_app(args.db, media_root=args.media_root, remote=remote)
    cfg = config.get_config(reload=True)
    db_path = app.config["DASHBOARD_DB"]
    app.config["DASHBOARD_NOTIFY"] = lambda text: telegram_bot.send_alert(
        text, cfg=config.get_config(reload=True), db_path=db_path)
    out(f"GTA 6 control panel at http://127.0.0.1:{args.port}")
    out(f"Database: {app.config['DASHBOARD_DB']}")
    out("Quick controls apply immediately when the runner is stopped. Ctrl-C closes this dashboard.")
    tunnel = start_remote(cfg, args.port, app, out)
    try:
        serve(
            app,
            host="127.0.0.1",
            port=args.port,
            threads=4,
            clear_untrusted_proxy_headers=True,
        )
    finally:
        if tunnel is not None:
            tunnel.stop()


if __name__ == "__main__":
    main()

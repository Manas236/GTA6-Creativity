"""python -m dashboard [--db PATH] [--media-root PATH] [--port 8765]."""

import argparse
from pathlib import Path

from .app import create_app


def main():
    parser = argparse.ArgumentParser(description="Local GTA 6 pipeline control panel")
    parser.add_argument(
        "--db", type=Path, help="SQLite database (default: pipeline.db)"
    )
    parser.add_argument(
        "--media-root", type=Path, help="Allowed media directory, including demo media"
    )
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    from waitress import serve

    app = create_app(args.db, media_root=args.media_root)
    print(f"GTA 6 control panel at http://127.0.0.1:{args.port}", flush=True)
    print(f"Database: {app.config['DASHBOARD_DB']}", flush=True)
    print(
        "Controls wait for the runner's next stage boundary. Ctrl-C to stop.",
        flush=True,
    )
    serve(
        app,
        host="127.0.0.1",
        port=args.port,
        threads=4,
        clear_untrusted_proxy_headers=True,
    )


if __name__ == "__main__":
    main()

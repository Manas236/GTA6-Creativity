"""Control-plane dispatch. Pipeline work runs under the shared runner lease."""
import os
import subprocess
import sys
from datetime import datetime

from pipeline import config, db, runner

from . import commands


def spawn(path, *args):
    logs = config.media_root() / "jobs"
    logs.mkdir(parents=True, exist_ok=True)
    log = logs / f"{datetime.now():%Y%m%d-%H%M%S-%f}-{args[0]}.log"
    env = dict(os.environ, GTA6_MEDIA_DIR=str(config.media_root().resolve()))
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS}
               if os.name == "nt" else {"start_new_session": True})
    with log.open("ab") as output:
        child = subprocess.Popen(
            [sys.executable, "-m", "pipeline.runner", "--db", str(path), *args],
            cwd=config.ROOT_DIR, stdin=subprocess.DEVNULL, stdout=output,
            stderr=subprocess.STDOUT, env=env, **options,
        )
    return child.pid


def submit(plan, command_id, path, cfg):
    row = commands.enqueue(plan, command_id, path)
    if row["status"] != "pending":
        return row
    stale, interval = runner._lock_settings(cfg)
    if runner.lock_state(path, stale):
        return row
    if not runner.acquire_lock(path, stale, interval, mode="dashboard"):
        return row
    try:
        commands.process(command_id, cfg, path)
        with db.db(path) as conn:
            return dict(conn.execute("SELECT * FROM dashboard_commands WHERE id=?", (command_id,)).fetchone())
    finally:
        runner.release_lock(path)

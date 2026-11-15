"""Validate the dashboard settings form and update only the local overlay."""

from datetime import datetime, timedelta
import math
import re
import tempfile
from threading import Lock

import yaml

from pipeline import config, scheduler, twitch
from .commands import ControlError

_write_lock = Lock()


def values(cfg):
    sources = cfg.get("sources", {})
    posting = cfg.get("posting", {})
    platforms = posting.get("platforms", {})
    return {
        "slots": {name: profile["slots"] for name, profile in
                  (posting.get("profiles") or scheduler.DEFAULT_PROFILES).items()},
        "instagram": bool(platforms.get("instagram")),
        "facebook": bool(platforms.get("facebook")),
        "telegram": not cfg.get("approval", {}).get("dashboard_only", False)
                    and cfg.get("telegram", {}).get("enabled", True),
        "buffer": cfg.get("buffer", {}).get("target_ready_items", 6),
        "youtube": sources.get("youtube", {}).get("enabled", True),
        "twitch": twitch.is_enabled(cfg)[0],
        "official_weight": sources.get("official_channels", {}).get("weight", 0.2),
        "heatmap_weight": cfg.get("cut", {}).get("heatmap_weight", 0.25),
    }


def save(body, cfg, root):
    if not isinstance(body, dict) or set(body) != set(values(cfg)):
        raise ControlError("Submit the complete settings form.")
    for key in ("instagram", "facebook", "telegram", "youtube", "twitch"):
        if type(body[key]) is not bool:
            raise ControlError(f"{key} must be on or off.")
    if type(body["buffer"]) is not int or not 1 <= body["buffer"] <= 50:
        raise ControlError("Choose a buffer between 1 and 50.")
    for key, maximum in (("official_weight", 5), ("heatmap_weight", 2)):
        val = body[key]
        if type(val) not in (int, float) or not 0 <= val <= maximum or not math.isfinite(val):
            raise ControlError(f"{key.replace('_', ' ').capitalize()} must be between 0 and {maximum}.")
    slots = body["slots"]
    if not isinstance(slots, dict) or set(slots) != set(values(cfg)["slots"]):
        raise ControlError("Set slots for every posting profile.")
    for profile, hours in slots.items():
        if (not isinstance(hours, list) or not 1 <= len(hours) <= 24 or
                any(not isinstance(h, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", h)
                    for h in hours) or len(set(hours)) != len(hours)):
            raise ControlError(f"Enter unique HH:MM slots for {profile}.")
    profiles = cfg.get("posting", {}).get("profiles") or scheduler.DEFAULT_PROFILES
    override = {
        "posting": {"platforms": {p: body[p] for p in ("instagram", "facebook")},
                    "profiles": {name: {"timezone": profiles[name]["timezone"], "slots": sorted(hours)}
                                 for name, hours in slots.items()}},
        "approval": {"dashboard_only": not body["telegram"]},
        "telegram": {"enabled": body["telegram"]},
        "buffer": {"target_ready_items": body["buffer"]},
        "sources": {"youtube": {"enabled": body["youtube"]},
                    "twitch": {"enabled": body["twitch"], "enable_from": None},
                    "official_channels": {"weight": body["official_weight"]}},
        "cut": {"heatmap_weight": body["heatmap_weight"]},
    }
    local = root / "config.local.yaml"
    with _write_lock:
        previous = local.read_bytes() if local.exists() else b"{}\n"
        current = yaml.safe_load(previous) or {}
        merged = config.merge_config(current, override)
        stamp = datetime.now()
        while True:
            backup = local.with_name(f"{local.name}.{stamp:%Y-%m-%d_%H%M%S}.bak")
            try:
                with backup.open("xb") as stream:
                    stream.write(previous)
                break
            except FileExistsError:
                stamp += timedelta(seconds=1)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root,
                                         prefix="config.local.", delete=False) as stream:
            temp = root / stream.name
            yaml.safe_dump(merged, stream, sort_keys=False)
        try:
            temp.replace(local)
        finally:
            temp.unlink(missing_ok=True)
    return "Settings saved. The runner reloads them on its next cycle."

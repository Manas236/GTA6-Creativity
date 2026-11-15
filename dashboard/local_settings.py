"""Validate the dashboard's settings subset and atomically save its local override."""
import math
import os
import re
import tempfile
import threading
from datetime import datetime

import yaml

from pipeline import config, sentence_windows, twitch

from .commands import ControlError

_WRITE_LOCK = threading.Lock()


def editable(cfg):
    sources = cfg.get("sources", {})
    return {
        "slots": {name: profile["slots"] for name, profile in cfg["posting"]["profiles"].items()},
        "instagram": cfg["posting"]["platforms"]["instagram"],
        "facebook": cfg["posting"]["platforms"]["facebook"],
        "telegram": cfg["approval"]["mode"] == "telegram",
        "buffer": cfg["buffer"]["target_ready_items"],
        "youtube": sources.get("youtube", {}).get("enabled", True),
        "twitch": twitch.is_enabled(cfg)[0],
        "official_weight": sources.get("official_channels", {}).get("weight", .2),
        "heatmap_weight": cfg.get("cut", {}).get("heatmap_weight", sentence_windows.HEATMAP_WEIGHT),
    }


def validate(values, cfg):
    current = editable(cfg)
    if not isinstance(values, dict) or set(values) != set(current):
        raise ControlError("Submit all editable settings.")
    for key in ("instagram", "facebook", "telegram", "youtube", "twitch"):
        if type(values[key]) is not bool:
            raise ControlError(f"{key} must be on or off.")
    if type(values["buffer"]) is not int or not 1 <= values["buffer"] <= 100:
        raise ControlError("Buffer target must be an integer from 1 to 100.")
    for key in ("official_weight", "heatmap_weight"):
        value = values[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ControlError(f"{key} must be between 0 and 1.")
    slots = values["slots"]
    if not isinstance(slots, dict) or slots.keys() != current["slots"].keys():
        raise ControlError("Provide slots for each posting profile.")
    for name, times in slots.items():
        if (not isinstance(times, list) or not 1 <= len(times) <= 24
                or any(not isinstance(t, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in times)
                or len(set(times)) != len(times)):
            raise ControlError(f"{name} slots must be distinct HH:MM times.")
    return {
        "posting": {"profiles": {key: {"slots": sorted(value)} for key, value in slots.items()},
                    "platforms": {key: values[key] for key in ("instagram", "facebook")}},
        "approval": {"mode": "telegram" if values["telegram"] else "auto"},
        "buffer": {"target_ready_items": values["buffer"]},
        "sources": {"youtube": {"enabled": values["youtube"]},
                    "twitch": {"enabled": values["twitch"], "enable_from": None},
                    "official_channels": {"weight": values["official_weight"]}},
        "cut": {"heatmap_weight": values["heatmap_weight"]},
    }


def save(values, cfg):
    delta = validate(values, cfg)
    path = config.ROOT_DIR / "config.local.yaml"
    with _WRITE_LOCK:
        previous = path.read_bytes() if path.exists() else b"{}\n"
        merged = config.merge_config(yaml.safe_load(previous) or {}, delta)
        # Exclusive creation preserves earlier backups even when saving twice in one second.
        from datetime import timedelta
        stamp = datetime.now()
        while True:
            backup = path.with_name(f"{path.name}.{stamp:%Y-%m-%d_%H%M%S}.bak")
            try:
                with backup.open("xb") as output:
                    output.write(previous)
                break
            except FileExistsError:
                stamp += timedelta(seconds=1)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             delete=False) as output:
                temp = output.name
                yaml.safe_dump(merged, output, sort_keys=False)
            os.replace(temp, path)
        finally:
            if temp and os.path.exists(temp):
                os.unlink(temp)
    return "Settings saved. The runner picks them up next cycle."

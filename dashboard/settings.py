"""Validate the dashboard settings form and update only the local overlay."""

from datetime import date, datetime, timedelta
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
                    "twitch": {"enabled": body["twitch"]},
                    "official_channels": {"weight": body["official_weight"]}},
        "cut": {"heatmap_weight": body["heatmap_weight"]},
    }
    return save_overlay(override, root)


def save_overlay(override, root):
    local = root / "config.local.yaml"
    with _write_lock:
        previous = local.read_bytes() if local.exists() else b"{}\n"
        current = yaml.safe_load(previous) or {}
        if not isinstance(current, dict):
            raise ControlError("config.local.yaml must contain a mapping.")
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
        temp = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=root,
                                             prefix="config.local.", delete=False) as stream:
                temp = root / stream.name
                yaml.safe_dump(merged, stream, sort_keys=False)
            temp.replace(local)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
    return "Settings saved. The runner reloads them on its next cycle."


# UI field -> (config path, fallback). Specialized encodings are handled below.
FIELDS = {
    "posting": {
        "profile": ("posting.profile", "local"),
        "slots": ("posting.profiles", {}),
        "min_gap_hours": ("posting.min_gap_hours", 3.0),
        "daily_post_target": ("posting.daily_post_target", 2),
        "platforms": ("posting.platforms", {"instagram": False, "facebook": False}),
        "hashtags": ("posting.hashtags", []),
        "caption_templates": ("posting.caption_templates", {}),
    },
    "look": {
        "versions": ("style.versions", ["P1", "P2", "P3"]),
        "sfx": {
            "enabled_categories": ("style.sfx.enabled_categories", ["impact", "transition", "comedy", "ui"]),
            "max_per_clip": ("style.sfx.max_per_clip", 2),
            "min_gap_s": ("style.sfx.min_gap_s", 3.0),
        },
        "music": {"enabled": ("style.music.enabled", False)},
        "hook": {
            "font_size": ("render.hook.font_size", 150),
            "min_font_size": ("render.hook.min_font_size", 96),
            "warn_below": ("render.hook.warn_below_font_size", 130),
        },
        "caption": {"font_size": ("render.caption.font_size", 71)},
    },
    "sources": {
        "youtube": {"enabled": ("sources.youtube.enabled", True), "queries": ("sources.youtube.queries", [])},
        "twitch": {
            "enabled": ("sources.twitch.enabled", False),
            "enable_from": ("sources.twitch.enable_from", None),
            "lookback_hours": ("sources.twitch.lookback_hours", 48),
            "min_views": ("sources.twitch.min_views", 0),
        },
        "official_weight": ("sources.official_channels.weight", 0.2),
        "official_channels": ("sources.official_channels.channel_ids", []),
        "heatmap_weight": ("cut.heatmap_weight", 0.25),
        "max_source_minutes": ("sources.youtube.max_duration_seconds", 600),
    },
    "approval": {
        "telegram": ("telegram.enabled", True),
        "switch_to_auto_after_days": ("approval.switch_to_auto_after_days", 7),
        "auto_quality_gate": ("approval.auto_quality_gate.enabled", True),
    },
    "voiceover": {"voice": ("voiceover.voice", "am_adam"), "speed": ("voiceover.speed", 1.1)},
    "retention": {
        "posted_days": ("retention.purge_renders_after_days", 7),
        "failed_days": ("retention.purge_rejected_failed_after_days", 1),
    },
}


def _get(cfg, dotted, default):
    for key in dotted.split("."):
        if not isinstance(cfg, dict) or key not in cfg:
            return default
        cfg = cfg[key]
    return cfg


def _put(overlay, dotted, value):
    *parents, leaf = dotted.split(".")
    for key in parents:
        overlay = overlay.setdefault(key, {})
    overlay[leaf] = value


def grouped(cfg):
    def read(schema):
        result = {}
        for field, spec in schema.items():
            if isinstance(spec, dict):
                result[field] = read(spec)
                continue
            key, default = spec
            value = _get(cfg, key, default)
            if field == "slots":
                value = {name: profile["slots"] for name, profile in (value or scheduler.DEFAULT_PROFILES).items()}
            elif field == "platforms":
                value = {platform: bool(value.get(platform)) for platform in ("instagram", "facebook")}
            elif field == "hashtags" and isinstance(value, str):
                value = value.split()
            elif field == "max_source_minutes":
                value /= 60
            elif field == "telegram":
                value = bool(value and not _get(cfg, "approval.dashboard_only", False))
            result[field] = value
        return result

    return {**{group: read(schema) for group, schema in FIELDS.items()},
            "golive": {"dry_run": bool(_get(cfg, "app.dry_run", True)),
                       "dry_run_mode": _get(cfg, "posting.dry_run_mode", "plan_only")}}


RANGES = {
    "min_gap_hours": (3, 24), "daily_post_target": (1, 8),
    "max_per_clip": (0, 20), "min_gap_s": (0, 60),
    "font_size": (12, 400), "min_font_size": (12, 400), "warn_below": (12, 400),
    "lookback_hours": (1, 168), "min_views": (0, 10**9),
    "official_weight": (0, 5), "heatmap_weight": (0, 2), "max_source_minutes": (0.1, 1440),
    "switch_to_auto_after_days": (0, 365), "speed": (0.5, 2),
    "posted_days": (0, 3650), "failed_days": (0, 3650),
}


def _validate_field(field, value, default, cfg):
    if field == "profile":
        if value not in ("local", "server"):
            raise ControlError("profile must be local or server.")
    elif field == "slots":
        profiles = _get(cfg, "posting.profiles", {}) or scheduler.DEFAULT_PROFILES
        if not isinstance(value, dict) or not value or not set(value) <= set(profiles):
            raise ControlError("slots must name a configured posting profile.")
        for name, times in value.items():
            if (not isinstance(times, list) or not times or len(times) > 24
                    or any(not isinstance(t, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t)
                           for t in times) or len(times) != len(set(times))):
                raise ControlError(f"Enter unique HH:MM slots for {name}.")
        return {name: {"timezone": profiles[name]["timezone"], "slots": sorted(times)}
                for name, times in value.items()}
    elif field == "platforms":
        if (not isinstance(value, dict) or not value or not set(value) <= {"instagram", "facebook"}
                or any(type(flag) is not bool for flag in value.values())):
            raise ControlError("platforms must contain Instagram or Facebook toggles.")
    elif field == "caption_templates":
        from string import Formatter
        if not isinstance(value, dict) or any(not isinstance(v, str) or len(v) > 2200 for v in value.values()):
            raise ControlError("caption_templates must map sources to text of at most 2,200 characters.")
        try:
            for template in value.values():
                for _, name, spec, conversion in Formatter().parse(template):
                    if name is not None and (name not in {"hook_text", "author_name", "channel", "hashtags"}
                                             or spec or conversion):
                        raise ValueError()
        except ValueError:
            raise ControlError("Caption templates may use {hook_text}, {author_name}, {channel} and {hashtags}.") from None
    elif isinstance(default, list):
        if not isinstance(value, list) or len(value) > 100 or any(
            not isinstance(entry, str) or not entry.strip() or len(entry) > 220 for entry in value
        ):
            raise ControlError(f"{field} must be a list of nonempty strings.")
        if field == "versions" and (not value or len(set(value)) != len(value)
                                    or not set(value) <= {"P1", "P2", "P3"}):
            raise ControlError("versions must contain unique presets P1, P2 or P3.")
        if field == "hashtags":
            if len(value) > 30 or any(not re.fullmatch(r"#[\w]+", tag) for tag in value):
                raise ControlError("Use up to 30 hashtags, each beginning with #.")
            return " ".join(value)
    elif field == "enable_from":
        if value is not None:
            try:
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError()
            except ValueError:
                raise ControlError("enable_from must be YYYY-MM-DD or null.") from None
    elif field == "voice":
        if not isinstance(value, str) or not re.fullmatch(r"[a-z]{2}_[a-z0-9]+", value):
            raise ControlError("voice must be a Kokoro voice ID.")
    elif type(default) is bool:
        if type(value) is not bool:
            raise ControlError(f"{field} must be on or off.")
    elif field in RANGES:
        if field == "switch_to_auto_after_days" and value is None:
            return value
        low, high = RANGES[field]
        numeric = (int,) if type(default) is int and field != "max_source_minutes" else (int, float)
        if type(value) not in numeric or not low <= value <= high:
            raise ControlError(f"{field} must be between {low} and {high}.")
        if field == "max_source_minutes":
            return int(value * 60)
    return value


def group_overlay(group, body, cfg):
    if group not in FIELDS:
        raise ControlError("Choose a configurable settings group.")
    overlay = {}

    def write(schema, fields):
        if not isinstance(fields, dict) or not fields or not set(fields) <= set(schema):
            raise ControlError("Submit only known fields for this settings group.")
        for field, value in fields.items():
            spec = schema[field]
            if isinstance(spec, dict):
                write(spec, value)
            else:
                key, default = spec
                _put(overlay, key, _validate_field(field, value, default, cfg))
                if field == "telegram":
                    _put(overlay, "approval.dashboard_only", not value)

    write(FIELDS[group], body)
    if group == "look" and "hook" in body:
        hook = grouped(config.merge_config(cfg, overlay))["look"]["hook"]
        if hook["min_font_size"] > hook["font_size"]:
            raise ControlError("min_font_size cannot exceed font_size.")
    return overlay

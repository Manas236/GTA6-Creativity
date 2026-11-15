"""Display copy shared by the dashboard API and its confirmations."""

from datetime import timedelta

from pipeline import clock, posting, scheduler


def slot_time(slot, cfg, *, now=None):
    timezone = scheduler.active_profile(cfg)[1]
    local = slot.astimezone(timezone)
    today = (now or clock.utcnow()).astimezone(timezone).date()
    time = local.strftime("%I:%M %p").lstrip("0")
    if local.date() == today:
        return f"Today {time}"
    if local.date() == today + timedelta(days=1):
        return f"Tomorrow {time}"
    return f"{local:%a} {local.day} {local:%b}, {time}"


def posting_destination(cfg):
    platforms = posting.enabled_platforms(cfg)
    names = {"instagram": "Instagram", "facebook": "Facebook"}
    return {
        "text": ("Will post to: " + " + ".join(names[name] for name in platforms)
                 if platforms else "No posting platforms are enabled."),
        "warning": not platforms,
    }

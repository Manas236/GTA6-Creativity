"""CP3 dry run for the laptop. One command, no prompts, with the real .env:

    python scripts/cp3_dry_run.py

What it does:
  1. Telegram: sends ONE real approval card to TELEGRAM_CHAT_ID, using the highest-scoring existing
     render. If the database holds none, it builds a synthetic 9:16 pair with ffmpeg first, under a
     `synthetic-cp3-*` item id, so the card can be seen exactly as it will look in production.
  2. Meta: validates the tokens and permissions with READ-ONLY GET calls - /debug_token, the IG
     user, the IG publishing limit, the Facebook Page and the Page's linked IG account - and then
     runs the real posting flow in dry-run mode, which writes a plan of every request it would send.
     It FAILS (exit code 1, every missing permission listed) when a token lacks any of the nine
     permissions in REQUIRED_SCOPES, is not a PAGE token (for FB_PAGE_ID, when Meta names the Page),
     is not valid, or when IG_USER_ID is not the Instagram account linked to FB_PAGE_ID.

It can NEVER publish. Three independent guards:
  * `app.dry_run` is forced to true and `posting.dry_run_mode` to "plan_only" for this process;
  * a GuardedTransport refuses any request to a write or publish endpoint and raises;
  * the Meta side builds its own MetaClient around that guarded transport.

Exit code 0 when every check passed, 1 when something needs attention.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config, db, posting, telegram_bot  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("cp3_dry_run")

# Any request whose URL or body matches one of these is a write and must never be sent from here.
FORBIDDEN_URL_PARTS = ("media_publish", "rupload.facebook.com", "/video_reels")
FORBIDDEN_BODY_KEYS = ("upload_phase", "video_state", "creation_id", "upload_type")

SYNTHETIC_PREFIX = "synthetic-cp3-"

# Every Meta permission the pipeline uses, and what for (SETUP.md section 2). One Page token from
# me/accounts normally carries all nine. With a separate FB_PAGE_ACCESS_TOKEN they are split:
# the Instagram token needs IG_SCOPES and the Facebook token FB_SCOPES.
REQUIRED_SCOPES: dict[str, str] = {
    "instagram_basic": "IG account lookup and recent media (crash recovery)",
    "instagram_content_publish": "IG Reels: container, upload, media_publish",
    "instagram_manage_insights": "IG 24 h / 72 h metric snapshots",
    "business_management": "added by the Instagram use case; needed if the Page is in a business portfolio",
    "pages_show_list": "me/accounts; required by Page Reels publishing",
    "pages_read_engagement": "required by IG publishing and by Page Reels publishing",
    "pages_manage_posts": "Facebook Page Reels (video_reels start / finish)",
    "read_insights": "Facebook Reel metric snapshots (video_insights)",
    "pages_manage_engagement": "Facebook Reel metric snapshots (video_insights)",
}
IG_SCOPES = ("instagram_basic", "instagram_content_publish", "instagram_manage_insights",
             "business_management", "pages_show_list", "pages_read_engagement")
FB_SCOPES = ("pages_show_list", "pages_read_engagement", "pages_manage_posts",
             "read_insights", "pages_manage_engagement")


class PublishAttempted(RuntimeError):
    """The guard tripped: something tried to send a write request from the dry run."""


class GuardedTransport:
    """Read-only Meta transport. A GET to the Graph API is allowed; anything else is refused."""

    def __init__(self) -> None:
        self.inner = posting.RequestsTransport()
        self.gets: list[str] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        body = {**(kwargs.get("data") or {}), **(kwargs.get("params") or {})}
        bad_url = [p for p in FORBIDDEN_URL_PARTS if p in url]
        bad_body = [k for k in FORBIDDEN_BODY_KEYS if k in body] if isinstance(body, dict) else []
        if method != "GET" or bad_url or bad_body:
            raise PublishAttempted(
                f"the dry run refused a {method} request to {posting.redact(url)}"
                f"{f' (url: {bad_url})' if bad_url else ''}{f' (body: {bad_body})' if bad_body else ''}"
            )
        self.gets.append(posting.redact(url))
        return self.inner(method, url, **kwargs)


# ---------------------------------------------------------------------------
# A synthetic render pair, for when the database holds none
# ---------------------------------------------------------------------------

def make_synthetic_renders(out_dir: Path, seconds: float = 6.0) -> tuple[Path, Path]:
    """Two small 1080x1920 clips with a tone, so the card and the upload sizes are realistic."""
    out_dir.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for label, text in (("A", "VARIANT A - CP3 DRY RUN"), ("B", "VARIANT B - CP3 DRY RUN")):
        path = out_dir / f"synthetic_{label}.mp4"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error",
            "-f", "lavfi", "-i", f"color=c=0x101418:s=1080x1920:r=30:d={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}:sample_rate=48000",
            "-vf", f"drawtext=text='{text}':fontcolor=yellow:fontsize=54:x=(w-text_w)/2:y=420",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
            "-shortest", str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0 or not path.exists():
            raise RuntimeError(f"ffmpeg could not build the synthetic clip: {proc.stderr[-500:]}")
        made.append(path)
    return made[0], made[1]


def synthetic_item(db_path: Any = None) -> dict[str, Any]:
    item_id = f"{SYNTHETIC_PREFIX}{int(time.time())}"
    a_path, b_path = make_synthetic_renders(config.media_dir("dry_run"))
    db.insert_discovered_item(
        {
            "id": item_id,
            "source": "youtube",
            "source_id": item_id,
            "source_url": "https://www.youtube.com/watch?v=CP3DRYRUN",
            "author_name": "CP3 Dry Run",
            "original_title": "Synthetic CP3 dry-run item (not a real clip)",
            "discovery_score": 0.123,
            "raw_velocity": 1.0,
            "percentile_rank": 0.5,
        },
        db_path=db_path,
    )
    db.transition_stage(
        item_id,
        "rendered",
        extra_fields={
            "hook_text": "CP3 DRY RUN - IS THIS CARD RIGHT?",
            "hook_style": "question",
            "hook_grounded": 1,
            "authenticity": "unverified",
            "render_a_path": str(a_path),
            "render_b_path": str(b_path),
            "caption_text": posting.build_caption(
                "CP3 DRY RUN - IS THIS CARD RIGHT?", "CP3 Dry Run", "youtube"
            ),
        },
        db_path=db_path,
    )
    log.info("Built a synthetic item %s with renders in %s", item_id, config.media_dir("dry_run"))
    return db.get_item(item_id, db_path=db_path)


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def check_telegram(db_path: Any = None, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or config.get_config()
    out: dict[str, Any] = {"step": "telegram"}
    token, chat = config.secret("TELEGRAM_BOT_TOKEN"), config.secret("TELEGRAM_CHAT_ID")
    if not (token and chat):
        out["ok"] = False
        out["detail"] = "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing from .env"
        return out

    bot = telegram_bot.ApprovalBot(cfg=cfg, db_path=db_path)
    try:
        me = bot.call("getMe")
        out["bot"] = f"@{me.get('username')}"
        log.info("Telegram bot is @%s, approvals come from chat %s", me.get("username"), chat)
    except Exception as e:  # noqa: BLE001
        out["ok"] = False
        out["detail"] = f"getMe failed: {telegram_bot.redact(e)}"
        return out

    item = telegram_bot.next_item_for_review(db_path)
    if not item or not item.get("render_b_path"):
        log.info("No rendered item is waiting; building a synthetic one for the card.")
        item = synthetic_item(db_path)

    # One real card, sent fresh: an older open card would otherwise block it (one at a time).
    telegram_bot.set_card_state(None, db_path)
    sent = bot.send_card(item)
    state = telegram_bot.card_state(db_path) or {}
    out["ok"] = True
    out["item_id"] = sent
    out["message_id"] = state.get("mid")
    out["detail"] = (
        f"one card sent for {sent} as message {state.get('mid')} "
        f"(Variant A as message {state.get('extra_mids')})"
    )
    log.info("Telegram: %s", out["detail"])
    log.info("The buttons are live: a tap will really approve or reject %s in the local database.", sent)
    return out


# ---------------------------------------------------------------------------
# Meta
# ---------------------------------------------------------------------------

def _get_row(client: posting.MetaClient, step: str, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """One read-only GET as a result row: ok + the JSON body, or not ok + the redacted error."""
    row: dict[str, Any] = {"step": step}
    try:
        row["data"] = client.check(client.get(url, params=params, timeout=30), step)
        row["ok"] = True
    except Exception as e:  # noqa: BLE001
        row["ok"] = False
        row["detail"] = posting.redact(e)[:400]
    return row


def _date(epoch: Any) -> str:
    """A debug_token timestamp as a date; 0 means never."""
    try:
        value = int(epoch)
    except (TypeError, ValueError):
        return "?"
    return "never" if value == 0 else datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")


def check_token(
    client: posting.MetaClient, step: str, token: str, required: tuple[str, ...], page: str | None
) -> dict[str, Any]:
    """/debug_token for one token. It must be valid, a PAGE token (for `page`, when Meta names the
    Page in profile_id) and carry every permission in `required`; anything less fails the row.
    An answer with no scopes at all counts every required permission as missing."""
    row = _get_row(client, step, f"{client.base()}/debug_token", {"input_token": token, "access_token": token})
    if not row.get("ok"):
        return row
    info = (row.get("data") or {}).get("data") or {}
    scopes = set(info.get("scopes") or [])
    kind = info.get("type")
    row["type"] = kind
    row["missing"] = [s for s in required if s not in scopes]
    row["detail"] = (
        f"type={kind}, valid={info.get('is_valid')}, expires={_date(info.get('expires_at'))}"
        + (f", data access until {_date(info['data_access_expires_at'])}"
           if info.get("data_access_expires_at") is not None else "")
        + f", {len(required) - len(row['missing'])}/{len(required)} required permissions"
    )
    problems: list[str] = []
    if info.get("is_valid") is not True:
        problems.append("the token is not valid - make a new one (SETUP.md 2d)")
    if kind != "PAGE":
        problems.append(f"it is a {kind or 'UNKNOWN'} token, not a PAGE token - use the Page's "
                        "access_token from me/accounts (SETUP.md 2d)")
    profile = info.get("profile_id")
    if page and profile and str(profile) != str(page):
        problems.append(f"it is a token for Page {profile}, not for FB_PAGE_ID {page}")
    if row["missing"]:
        problems.append(f"MISSING {len(row['missing'])} permission(s): {', '.join(row['missing'])}")
    if problems:
        row["ok"] = False
        row["detail"] += "; " + "; ".join(problems)
    return row


def check_ig_link(client: posting.MetaClient, page: str, ig_user: str, token: str) -> dict[str, Any]:
    """IG_USER_ID must be the Instagram account linked to FB_PAGE_ID: IG publishing goes through it."""
    row = _get_row(client, "ig_linked_to_page", f"{client.base()}/{page}",
                   {"fields": "instagram_business_account", "access_token": token})
    if not row.get("ok"):
        return row
    linked = str(((row.get("data") or {}).get("instagram_business_account") or {}).get("id") or "")
    if not linked:
        row["ok"] = False
        row["detail"] = (f"Page {page} has no Instagram account linked: Page settings -> Linked accounts -> "
                         "Instagram (SETUP.md 2b), or the IG account was not ticked in the login dialog (2d)")
    elif linked != str(ig_user):
        row["ok"] = False
        row["detail"] = f"Page {page} is linked to Instagram account {linked}, but IG_USER_ID is {ig_user}"
    else:
        row["detail"] = f"Page {page} -> Instagram {linked}"
    return row


def check_meta_tokens(client: posting.MetaClient) -> list[dict[str, Any]]:
    """Read-only GETs only: every token in use, the IG user, the publishing limit, the Page and the
    Page's linked IG account. Each problem fails its row, and any failed row fails the run."""
    results: list[dict[str, Any]] = []
    ig_user, ig_tok = config.secret("IG_USER_ID"), posting.ig_token()
    page, fb_tok = config.secret("FB_PAGE_ID"), posting.fb_token()
    base = client.base()

    if not ig_tok:
        results.append({"step": "token", "ok": False, "detail": "IG_ACCESS_TOKEN missing from .env"})
        return results

    if fb_tok and fb_tok != ig_tok:
        # FB_PAGE_ACCESS_TOKEN is set: the nine permissions are split between the two tokens.
        results.append(check_token(client, "debug_token_ig", ig_tok, IG_SCOPES, page))
        results.append(check_token(client, "debug_token_fb", fb_tok, FB_SCOPES, page))
    else:
        results.append(check_token(client, "debug_token", ig_tok, tuple(REQUIRED_SCOPES), page))

    if ig_user:
        row = _get_row(client, "ig_user", f"{base}/{ig_user}", {"fields": "id,username", "access_token": ig_tok})
        if row.get("ok"):
            row["detail"] = f"@{(row['data'] or {}).get('username')} ({(row['data'] or {}).get('id')})"
        results.append(row)
        limit = _get_row(client, "ig_publishing_limit", f"{base}/{ig_user}/content_publishing_limit",
                         {"access_token": ig_tok})
        if limit.get("ok"):
            limit["detail"] = json.dumps((limit["data"] or {}).get("data"))
        results.append(limit)
    else:
        results.append({"step": "ig_user", "ok": False, "detail": "IG_USER_ID missing from .env"})

    if page:
        row = _get_row(client, "fb_page", f"{base}/{page}", {"fields": "id,name", "access_token": fb_tok})
        if row.get("ok"):
            data = row["data"] or {}
            row["detail"] = f"Page {data.get('name')} ({data.get('id')})"
        results.append(row)
        if ig_user:
            results.append(check_ig_link(client, page, ig_user, ig_tok))
    else:
        results.append({"step": "fb_page", "ok": False, "detail": "FB_PAGE_ID missing from .env"})

    for row in results:
        log.info("Meta %-22s %s  %s", row["step"], "OK  " if row.get("ok") else "FAIL", row.get("detail", ""))
    return results


def run_posting_dry_run(
    item_id: str, cfg: dict[str, Any], client: posting.MetaClient, db_path: Any = None
) -> dict[str, Any]:
    """The real posting flow in dry-run mode. No write request is built, let alone sent."""
    publisher = posting.Publisher(
        client=client,
        cfg=cfg,
        db_path=db_path,
        alert=lambda text, key=None: log.warning("ALERT (suppressed in the dry run): %s", text),
    )
    item = db.get_item(item_id, db_path=db_path)
    results = posting.run_posting_for_item(
        item["id"], cfg=cfg, db_path=db_path, publisher=publisher, slot_datetime=db.now()
    )
    plans: dict[str, Any] = {}
    for row in posting.get_posts_for_item(item["id"], db_path=db_path):
        detail = row.get("error_message") or ""
        if "plan: " in detail:
            path = Path(detail.split("plan: ")[-1])
            if path.exists():
                plans[row["platform"]] = {
                    "file": str(path),
                    "steps": [f"{s['method']} {s['url']}" for s in
                              json.loads(path.read_text(encoding="utf-8"))["steps"]],
                }
    for platform, plan in plans.items():
        log.info("Meta %s dry run would send:", platform)
        for step in plan["steps"]:
            log.info("    %s", step)
        log.info("  full plan: %s", plan["file"])
    return {"step": "posting_dry_run", "ok": bool(results) and all(
        s == posting.STATUS_DRY_RUN for s in results.values()
    ), "results": results, "plans": plans, "detail": json.dumps(results)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="CP3 dry run: one Telegram card + read-only Meta checks.")
    parser.add_argument("--db", default=None, help="Database path (default pipeline.db)")
    parser.add_argument("--skip-telegram", action="store_true", help="Only run the Meta checks")
    parser.add_argument("--skip-meta", action="store_true", help="Only send the Telegram card")
    args = parser.parse_args()

    db_path = args.db
    db.init_db(db_path)

    cfg = config.get_config(reload=True)
    # Guard 1: this process cannot leave dry run, whatever config.yaml says.
    cfg.setdefault("app", {})["dry_run"] = True
    cfg.setdefault("posting", {})["dry_run_mode"] = "plan_only"

    print("=" * 78)
    print(" CP3 DRY RUN - one Telegram card, read-only Meta checks, no publish is possible")
    print("=" * 78)

    rows: list[dict[str, Any]] = []

    if not args.skip_telegram:
        rows.append(check_telegram(db_path=db_path, cfg=cfg))

    if not args.skip_meta:
        # Guard 2 and 3: a read-only transport, wrapped in the client the publisher is given.
        guard = GuardedTransport()
        client = posting.MetaClient(transport=guard, cfg=cfg, db_path=db_path)
        rows.extend(check_meta_tokens(client))
        # Always a synthetic item here: the posting flow writes `posts` rows, and a real pipeline
        # item must not collect dry-run rows that its real post would later have to work around.
        target = synthetic_item(db_path)["id"]
        try:
            rows.append(run_posting_dry_run(target, cfg, client, db_path=db_path))
        except PublishAttempted as e:
            rows.append({"step": "posting_dry_run", "ok": False, "detail": f"GUARD TRIPPED: {e}"})
        log.info("Meta GETs actually sent: %d", len(guard.gets))

    return print_summary(rows)


def print_summary(rows: list[dict[str, Any]]) -> int:
    """Print every row, each missing permission on its own line; 1 if any row failed, else 0."""
    print("\n" + "=" * 78)
    print(" SUMMARY")
    print("=" * 78)
    for row in rows:
        print(f"  {'OK  ' if row.get('ok') else 'FAIL'}  {row['step']:<22} {str(row.get('detail', ''))[:300]}")
        for scope in row.get("missing") or []:
            print(f"          missing {scope:<26} {REQUIRED_SCOPES.get(scope, '')}")
    failed = [r["step"] for r in rows if not r.get("ok")]
    if failed:
        print(f"\n  Needs attention: {', '.join(failed)}")
        if any(r.get("missing") for r in rows):
            print("  Add the missing permissions to the app (SETUP.md 2c), then make a new token (2d).")
        print("  Nothing was published, and nothing can be: the dry run sends no write request.")
        return 1
    print("\n  Everything passed. Nothing was published.")
    print("  The live post happens only on your explicit go (set app.dry_run: false in config.yaml).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

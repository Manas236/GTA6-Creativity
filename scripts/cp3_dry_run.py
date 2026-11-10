"""CP3 dry run for the laptop. One command, no prompts, with the real .env:

    python scripts/cp3_dry_run.py

What it does:
  1. Telegram: sends ONE real approval card to TELEGRAM_CHAT_ID, using the highest-scoring existing
     render. If the database holds none, it builds a synthetic 9:16 pair with ffmpeg first, under a
     `synthetic-cp3-*` item id, so the card can be seen exactly as it will look in production.
  2. Meta: validates the tokens and permissions with READ-ONLY GET calls - /debug_token, the IG
     user, the IG publishing limit and the Facebook Page - and then runs the real posting flow in
     dry-run mode, which writes a plan of every request it would send.

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

def check_meta_tokens(client: posting.MetaClient) -> list[dict[str, Any]]:
    """Read-only GETs only: the token itself, the IG user, the publishing limit and the Page."""
    results: list[dict[str, Any]] = []
    ig_user, ig_tok = config.secret("IG_USER_ID"), posting.ig_token()
    page, fb_tok = config.secret("FB_PAGE_ID"), posting.fb_token()
    base = client.base()

    def gets(step: str, url: str, params: dict[str, Any]) -> dict[str, Any]:
        row: dict[str, Any] = {"step": step}
        try:
            data = client.check(client.get(url, params=params, timeout=30), step)
            row["ok"] = True
            row["data"] = data
        except Exception as e:  # noqa: BLE001
            row["ok"] = False
            row["detail"] = posting.redact(e)[:400]
        return row

    if not ig_tok:
        results.append({"step": "token", "ok": False, "detail": "IG_ACCESS_TOKEN missing from .env"})
        return results

    token_row = gets("debug_token", f"{base}/debug_token", {"input_token": ig_tok, "access_token": ig_tok})
    info = (token_row.get("data") or {}).get("data") or {}
    if token_row.get("ok"):
        scopes = info.get("scopes") or []
        expires = info.get("expires_at")
        token_row["detail"] = (
            f"type={info.get('type')}, valid={info.get('is_valid')}, "
            f"expires={'never' if expires == 0 else expires}, "
            f"{len(scopes)} permissions"
        )
        missing_ig = [s for s in ("instagram_basic", "instagram_content_publish") if scopes and s not in scopes]
        missing_fb = [s for s in ("pages_manage_posts", "pages_read_engagement") if scopes and s not in scopes]
        token_row["missing_instagram"] = missing_ig
        token_row["missing_facebook"] = missing_fb
        if missing_ig or missing_fb:
            token_row["ok"] = False
            token_row["detail"] += f"; MISSING: {missing_ig + missing_fb}"
        if info.get("is_valid") is False:
            token_row["ok"] = False
            token_row["detail"] += "; the token is no longer valid"
    results.append(token_row)

    if ig_user:
        row = gets("ig_user", f"{base}/{ig_user}", {"fields": "id,username", "access_token": ig_tok})
        if row.get("ok"):
            row["detail"] = f"@{(row['data'] or {}).get('username')} ({(row['data'] or {}).get('id')})"
        results.append(row)
        limit = gets("ig_publishing_limit", f"{base}/{ig_user}/content_publishing_limit", {"access_token": ig_tok})
        if limit.get("ok"):
            limit["detail"] = json.dumps((limit["data"] or {}).get("data"))
        results.append(limit)
    else:
        results.append({"step": "ig_user", "ok": False, "detail": "IG_USER_ID missing from .env"})

    if page:
        row = gets("fb_page", f"{base}/{page}", {"fields": "id,name", "access_token": fb_tok})
        if row.get("ok"):
            data = row["data"] or {}
            row["detail"] = f"Page {data.get('name')} ({data.get('id')})"
            if info.get("type") and info["type"] != "PAGE":
                row["ok"] = False
                row["detail"] += f"; the token is a {info['type']} token, Page Reels need a PAGE token"
        results.append(row)
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

    print("\n" + "=" * 78)
    print(" SUMMARY")
    print("=" * 78)
    for row in rows:
        print(f"  {'OK  ' if row.get('ok') else 'FAIL'}  {row['step']:<22} {str(row.get('detail', ''))[:300]}")
    failed = [r["step"] for r in rows if not r.get("ok")]
    if failed:
        print(f"\n  Needs attention: {', '.join(failed)}")
        print("  Nothing was published, and nothing can be: the dry run sends no write request.")
        return 1
    print("\n  Everything passed. Nothing was published.")
    print("  The live post happens only on your explicit go (set app.dry_run: false in config.yaml).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

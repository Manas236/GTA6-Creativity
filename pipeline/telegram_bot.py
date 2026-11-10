"""Stage 7: Telegram approval card (CP3).

ONE self-editing status card per item, strictly one item at a time: the next item is only sent
after the current one is decided. The card carries Variant B as the video message (plus Variant A
as a second message), the hook, the authenticity tag, the source + creator and the discovery score.

Buttons: Approve / Reject / Approve A only.
  * Approve         -> stage 'approved', approval_type 'approved'. chosen_variant stays NULL:
                       the CP4 scheduler assigns A/B later, not the reviewer.
  * Reject          -> stage 'rejected', rejection_reason 'user_rejected'.
  * Approve A only  -> stage 'approved', approval_type 'approved_a_only', chosen_variant 'A',
                       ab_excluded 1. Logged and excluded from the A/B comparison.

Stale-button protection: a click only counts when it comes from the message id and item id the open
card is recorded under in `kv`. Every other click answers STALE and changes nothing.

Edits are throttled to one per `telegram.edit_throttle_gap_s` per message (a button tap is answered
at once); a 429 holds every edit back for its retry_after.

The long poll (`getUpdates`) runs inside the runner's loop: `run_approval_stage()` is called once
per cycle and does one `ensure_card` + one `poll_once` + one `editor.flush`.

`approval.mode: auto` skips Telegram altogether and applies the PLAN.md quality gate instead.

Network sits behind the `Transport` interface, so the tests are fully mocked and make no calls.

Patterns reused from the author's earlier LoudGraph pipeline (`pipeline/approve_telegram.py`,
`pipeline/review.py`): the throttled `Editor`, the one-card/one-item review state in `kv`, the
message-id stale check and the persisted `getUpdates` offset. Re-implemented against this project's
schema; the throttling and 429 logic of `Editor` closely follow that source.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Protocol

import requests

from . import clock, config, db

log = logging.getLogger(__name__)

K_CARD = "telegram:card"          # JSON {"mid":, "item_id":, "extra_mids": [...]}
K_OFFSET = "telegram:offset"      # getUpdates offset, persisted so a restart never re-delivers taps
K_ALERT = "telegram:last_alert"   # JSON {"key":, "at": epoch} so one failure is not re-sent forever
K_AUTO_SINCE = "approval:auto_since"          # ISO time the approval mode switched to auto
K_AUTO_NOTICE = "approval:auto_notice_sent"   # set once the single switch notice went out

STALE = "This card is out of date - it was already decided. The current card is further down."
ALERT_REPEAT_GAP_S = 3600.0       # the same failure is not re-alerted more often than this
MAX_VIDEO_BYTES = 50 * 1024 * 1024  # Bot API upload limit for a bot-sent video

ACTIONS = {"ok": "approved", "no": "rejected", "aonly": "approved_a_only"}


class TelegramError(RuntimeError):
    """A Bot API error. `retry_after` is set on 429 Too Many Requests."""

    def __init__(self, method: str, code: Any, description: str, retry_after: Any = None):
        super().__init__(f"Telegram {method}: {code} {description}")
        self.method = method
        self.code = code
        self.description = description or ""
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Network interface (the only place a Bot API request is sent from)
# ---------------------------------------------------------------------------

class Transport(Protocol):
    """Sends one Bot API call and returns the decoded `result`. Tests supply a fake."""

    def __call__(
        self,
        method: str,
        params: dict[str, Any],
        files: dict[str, Any] | None = None,
        http_timeout: float = 30.0,
    ) -> Any:
        ...


class RequestsTransport:
    """The real Bot API transport over HTTPS. Long polling, so no public URL or webhook is needed."""

    def __init__(self, token: str | None = None, api_base: str | None = None):
        self._token = token
        self._api_base = api_base

    def token(self) -> str:
        return self._token if self._token is not None else config.secret("TELEGRAM_BOT_TOKEN")

    def api_base(self) -> str:
        if self._api_base:
            return self._api_base
        tg = config.get_config().get("telegram") or {}
        return str(tg.get("api_base", "https://api.telegram.org"))

    def url(self, method: str) -> str:
        return f"{self.api_base()}/bot{self.token()}/{method}"

    def __call__(
        self,
        method: str,
        params: dict[str, Any],
        files: dict[str, Any] | None = None,
        http_timeout: float = 30.0,
    ) -> Any:
        if files:
            data = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in params.items()}
            resp = requests.post(self.url(method), data=data, files=files, timeout=http_timeout)
        else:
            resp = requests.post(self.url(method), json=params, timeout=http_timeout)
        try:
            payload = resp.json()
        except ValueError:
            raise TelegramError(method, resp.status_code, resp.text[:200]) from None
        if not payload.get("ok"):
            raise TelegramError(
                method,
                payload.get("error_code"),
                payload.get("description", ""),
                (payload.get("parameters") or {}).get("retry_after"),
            )
        return payload["result"]


def redact(text: str) -> str:
    """A Bot API URL carries the bot token; never let it reach a log, the DB or the chat."""
    token = config.secret("TELEGRAM_BOT_TOKEN")
    return str(text).replace(token, "<TELEGRAM_BOT_TOKEN>") if token else str(text)


# ---------------------------------------------------------------------------
# Throttled edits
# ---------------------------------------------------------------------------

class Editor:
    """Message edits, at most one per `gap` seconds per message; the latest text wins.

    A 429 holds every edit back for its retry_after and keeps the pending edit. "message is not
    modified" counts as done. `now=True` (a button tap) skips the per-message gap but never a 429
    wait. Adapted from LoudGraph `pipeline/approve_telegram.py:Editor`.
    """

    def __init__(self, transport: Transport, chat_id: str, clock: Callable[[], float] = time.monotonic, gap: float = 2.5):
        self.transport = transport
        self.chat_id = chat_id
        self.clock = clock
        self.gap = gap
        self.last: dict[int, float] = {}
        self.pending: dict[int, tuple[str, str, dict[str, Any] | None]] = {}
        self.blocked_until = 0.0

    def touch(self, message_id: int) -> None:
        """The message was just sent: its first edit waits a full gap too."""
        self.last[message_id] = self.clock()

    def edit(self, message_id: int, kind: str, text: str, markup: dict[str, Any] | None, now: bool = False) -> bool:
        self.pending[message_id] = (kind, text, markup)
        return self._send(message_id, now)

    def flush(self) -> None:
        for mid in list(self.pending):
            self._send(mid)

    def forget(self, message_id: int) -> None:
        self.pending.pop(message_id, None)

    def _send(self, mid: int, now: bool = False) -> bool:
        t = self.clock()
        if t < self.blocked_until:
            return False
        if not now and (t - self.last.get(mid, float("-inf"))) < self.gap:
            return False
        kind, text, markup = self.pending.pop(mid)
        self.last[mid] = t
        params: dict[str, Any] = {"chat_id": self.chat_id, "message_id": mid}
        if markup is not None:
            params["reply_markup"] = markup
        if kind == "video":
            params["caption"] = text[:1024]
            method = "editMessageCaption"
        else:
            params["text"] = text[:4000]
            params["link_preview_options"] = {"is_disabled": True}
            method = "editMessageText"
        try:
            self.transport(method, params)
        except TelegramError as e:
            if e.retry_after:
                self.blocked_until = t + float(e.retry_after)
                self.pending.setdefault(mid, (kind, text, markup))
                log.info("telegram: rate limited, edits wait %ss", e.retry_after)
                return False
            if "not modified" not in e.description.lower():
                log.info("telegram: could not edit message %s: %s", mid, redact(e))
        except requests.RequestException as e:
            self.pending.setdefault(mid, (kind, text, markup))
            log.info("telegram: edit of message %s failed (%s); will try again", mid, redact(e))
            return False
        return True


# ---------------------------------------------------------------------------
# Card wording
# ---------------------------------------------------------------------------

AUTHENTICITY_TAG = {
    "official": "✅ OFFICIAL",
    "leak": "🕵️ LEAK / UNCONFIRMED",
    "rumor": "🕵️ RUMOR / UNCONFIRMED",
    "fan_made": "🎨 FAN MADE",
    "unverified": "❓ UNVERIFIED",
}


def authenticity_tag(value: str | None) -> str:
    return AUTHENTICITY_TAG.get((value or "unverified").lower(), f"❓ {(value or 'unverified').upper()}")


def card_renders(item: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """(variant A path, variant B path, preset) the card shows.

    Always the renders the pipeline made with the item's own `style_preset` - the ones recorded on
    the item row and the ones that will be posted - never a preview render of another preset and
    never whatever `style.preset` happens to say in config today.
    """
    preset = str(item.get("style_preset") or "").strip() or "P0"
    return item.get("render_a_path"), item.get("render_b_path"), preset


def card_text(item: dict[str, Any], decided: str = "") -> str:
    """The single status card's body. `decided` replaces the prompt line once a button was used."""
    lines = [
        "🎬 REVIEW · Variant B is the video above, Variant A is the message below.",
        "",
        f"🪝 Hook: {item.get('hook_text') or '(none)'}",
        f"🏷 {authenticity_tag(item.get('authenticity'))}"
        + ("" if item.get("hook_grounded", 1) else "  ·  hook fell back (not grounded)"),
        f"📺 Source: {item.get('source', '?')} · {item.get('source_url') or ''}",
        f"👤 Creator: {item.get('author_name') or 'Creator'}",
        f"📈 Discovery score: {float(item.get('discovery_score') or 0.0):.3f}",
        f"🎨 Style preset: {card_renders(item)[2]}",
    ]
    if item.get("original_title"):
        lines.append(f"🎞 Original: {str(item['original_title'])[:120]}")
    lines.append("")
    lines.append(decided or "Approve puts it in the posting queue; the scheduler assigns A or B later.")
    return "\n".join(lines)


def card_markup(item_id: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "✅ Approve", "callback_data": f"a:ok:{item_id}"}],
            [
                {"text": "❌ Reject", "callback_data": f"a:no:{item_id}"},
                {"text": "🅰 Approve A only", "callback_data": f"a:aonly:{item_id}"},
            ],
        ]
    }


DECIDED_TEXT = {
    "approved": "✅ Approved. It is in the posting queue; the scheduler assigns A or B.",
    "rejected": "❌ Rejected. It will not be posted.",
    "approved_a_only": "🅰 Approved, Variant A only. Excluded from the A/B comparison.",
}


# ---------------------------------------------------------------------------
# Card state in kv (one card, one item, at a time)
# ---------------------------------------------------------------------------

def card_state(db_path: Any = None) -> dict[str, Any] | None:
    try:
        state = json.loads(db.kv_get(K_CARD, db_path=db_path) or "null")
    except ValueError:
        return None
    return state if isinstance(state, dict) and state.get("mid") else None


def set_card_state(state: dict[str, Any] | None, db_path: Any = None) -> None:
    db.kv_set(K_CARD, json.dumps(state) if state else "", db_path=db_path)


def next_item_for_review(db_path: Any = None) -> dict[str, Any] | None:
    """The highest-scoring rendered item waiting for a decision.

    `awaiting_approval` rows come first: an item whose card was sent but whose decision never
    landed (the bot died between the send and the tap) is shown again before a fresh one.
    preview_only items are filtered out here explicitly; the card never depends on the SQLite
    trigger firing to keep them away.
    """
    pending = db.get_items_by_stage("awaiting_approval", limit=1, db_path=db_path, releasable_only=True)
    if pending:
        return pending[0]
    ready = db.get_items_by_stage("rendered", limit=1, db_path=db_path, releasable_only=True)
    return ready[0] if ready else None


def refuse_item(item_id: str, stage: str, exc: BaseException, db_path: Any = None) -> str:
    """A preview_only trigger refused a write: fail that ONE item and carry on.

    `failed` is not a release stage, so the trigger lets this through; the item then leaves every
    approval / posting queue and the runner cannot loop on it.
    """
    msg = f"refused by the database: {exc}"
    log.error("%s: item %s %s", stage, item_id, msg)
    try:
        db.mark_failed(item_id, stage, msg, db_path=db_path)
        db.log_stage(item_id, stage, "failure", message=msg, db_path=db_path)
    except sqlite3.Error as e:  # noqa: PERF203 - never let the clean-up itself crash the runner
        log.error("%s: could not mark %s failed: %s", stage, item_id, e)
    return msg


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

def decide(item_id: str, action: str, db_path: Any = None) -> tuple[str, str]:
    """Apply a button. Returns (outcome, short answer to show on the button).

    `outcome` is '' when nothing was applied (the item was already decided elsewhere).
    """
    item = db.get_item(item_id, db_path=db_path)
    if not item:
        return "", "That item is gone."
    if item.get("stage") not in ("awaiting_approval", "rendered"):
        return "", f"Already decided ({item.get('stage')})."
    if action in ("ok", "aonly") and int(item.get("preview_only") or 0):
        refuse_item(item_id, "approval", RuntimeError("preview_only item cannot be approved"), db_path)
        return "", "This item is preview-only and cannot be approved."
    try:
        return _apply_decision(item_id, action, db_path)
    except sqlite3.IntegrityError as e:
        refuse_item(item_id, "approval", e, db_path)
        return "", "The database refused that item; it was failed and skipped."


def _apply_decision(item_id: str, action: str, db_path: Any = None) -> tuple[str, str]:
    if action == "no":
        db.mark_rejected(item_id, "user_rejected", db_path=db_path)
        db.log_stage(item_id, "approval", "success", message="rejected via telegram", db_path=db_path)
        return "rejected", DECIDED_TEXT["rejected"]

    if action == "ok":
        db.transition_stage(
            item_id,
            "approved",
            extra_fields={"approval_type": "approved", "chosen_variant": None, "ab_excluded": 0},
            db_path=db_path,
        )
        db.log_stage(
            item_id, "approval", "success",
            message="approved via telegram; scheduler assigns A/B", db_path=db_path,
        )
        return "approved", DECIDED_TEXT["approved"]

    if action == "aonly":
        db.transition_stage(
            item_id,
            "approved",
            extra_fields={"approval_type": "approved_a_only", "chosen_variant": "A", "ab_excluded": 1},
            db_path=db_path,
        )
        db.log_stage(
            item_id, "approval", "success",
            message="approved A only via telegram; excluded from the A/B comparison", db_path=db_path,
        )
        return "approved_a_only", DECIDED_TEXT["approved_a_only"]

    return "", STALE


# ---------------------------------------------------------------------------
# The bot
# ---------------------------------------------------------------------------

class ApprovalBot:
    """One self-editing card at a time, driven from the runner's loop."""

    def __init__(
        self,
        transport: Transport | None = None,
        chat_id: str | None = None,
        cfg: dict[str, Any] | None = None,
        db_path: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cfg = cfg or config.get_config()
        tg = self.cfg.get("telegram") or {}
        self.transport: Transport = transport or RequestsTransport()
        self.chat_id = chat_id if chat_id is not None else config.secret("TELEGRAM_CHAT_ID")
        self.db_path = db_path
        self.clock = clock
        self.poll_timeout = int(tg.get("poll_timeout_s", 30))
        self.editor = Editor(
            self.transport, self.chat_id, clock=clock, gap=float(tg.get("edit_throttle_gap_s", 2.5))
        )

    # ----------------------------------------------------------- primitives
    def call(self, method: str, files: dict[str, Any] | None = None, http_timeout: float = 30.0, **params: Any) -> Any:
        return self.transport(method, params, files, http_timeout)

    def send_message(self, text: str, **extra: Any) -> dict[str, Any]:
        return self.call(
            "sendMessage",
            chat_id=self.chat_id,
            text=text[:4000],
            link_preview_options={"is_disabled": True},
            **extra,
        )

    def send_video(self, path: Path | str, caption: str, markup: dict[str, Any] | None = None) -> dict[str, Any]:
        """A video message. Over the Bot API limit, the caption is sent on its own instead."""
        video = Path(path)
        params: dict[str, Any] = {
            "chat_id": self.chat_id,
            "caption": caption[:1024],
            "supports_streaming": True,
        }
        if markup is not None:
            params["reply_markup"] = markup
        if not video.exists() or video.stat().st_size > MAX_VIDEO_BYTES:
            why = "missing" if not video.exists() else f"{video.stat().st_size / 1e6:.0f} MB, over the 50 MB bot limit"
            log.warning("telegram: %s not sent as a video (%s); sending the card as text", video.name, why)
            return self.send_message(f"{caption}\n\n(video not attached: {why})", reply_markup=markup)
        with open(video, "rb") as fh:
            return self.call("sendVideo", files={"video": (video.name, fh)}, http_timeout=300.0, **params)

    # ----------------------------------------------------------- the card
    def ensure_card(self) -> str | None:
        """Send the next card, but only when no card is open. Returns the item id, or None.

        This is the one-at-a-time rule: while `kv` holds an open card, nothing new is sent.
        """
        state = card_state(self.db_path)
        if state:
            return None
        item = next_item_for_review(self.db_path)
        if not item:
            return None
        return self.send_card(item)

    def send_card(self, item: dict[str, Any]) -> str | None:
        item_id = item["id"]
        if int(item.get("preview_only") or 0):
            log.warning("telegram: %s is preview_only; no card is sent for it", item_id)
            return None
        # The stage moves BEFORE anything is sent: if the database refuses the item, no card for it
        # ever reaches the chat, and a crash after this line re-offers the item (awaiting_approval
        # is picked up first by next_item_for_review).
        if item.get("stage") != "awaiting_approval":
            try:
                db.transition_stage(item_id, "awaiting_approval", db_path=self.db_path)
            except sqlite3.IntegrityError as e:
                refuse_item(item_id, "approval", e, self.db_path)
                return None
        a_path, b_path, preset = card_renders(item)
        text = card_text(item)

        if b_path:
            main = self.send_video(b_path, text, card_markup(item_id))
        else:
            main = self.send_message(text, reply_markup=card_markup(item_id))
        mid = int(main["message_id"])
        self.editor.touch(mid)

        extra_mids: list[int] = []
        if a_path:
            try:
                second = self.send_video(
                    a_path, f"🅰 Variant A (captions only, {preset}) · {item.get('hook_text') or ''}"
                )
                extra_mids.append(int(second["message_id"]))
            except (TelegramError, requests.RequestException, OSError) as e:
                log.warning("telegram: Variant A not sent for %s: %s", item_id, redact(e))

        set_card_state({"mid": mid, "item_id": item_id, "extra_mids": extra_mids}, self.db_path)
        db.log_stage(item_id, "approval", "start", message=f"card sent as message {mid}", db_path=self.db_path)
        log.info("telegram: card for %s sent as message %s", item_id, mid)
        return item_id

    def close_card(self, state: dict[str, Any], item: dict[str, Any], outcome: str) -> None:
        """Edit the open card into its decided form, drop the buttons and free the slot."""
        kind = "video" if item.get("render_b_path") else "text"
        self.editor.edit(
            int(state["mid"]),
            kind,
            card_text(item, decided=DECIDED_TEXT.get(outcome, outcome)),
            {"inline_keyboard": []},
            now=True,
        )
        set_card_state(None, self.db_path)

    def release_stuck_card(self, poll_timeout: int | None = None) -> bool:
        """Free the one-card slot when the open card can no longer be decided.

        That is a card whose item is gone, already decided elsewhere, or is a synthetic dry-run
        item (scripts/cp3_dry_run.py leaves its test card open). Without this, an untapped test
        card would block every real card forever under the one-at-a-time rule. A tap that is
        already waiting is collected first, so a real decision is never thrown away.
        """
        state = card_state(self.db_path)
        if not state:
            return False
        item = db.get_item(state.get("item_id") or "", db_path=self.db_path)
        stuck = (not item) or item.get("stage") not in ("awaiting_approval", "rendered") or db.is_synthetic(item)
        if not stuck:
            return False
        if item and db.is_synthetic(item):
            self.poll_once(0)                       # honour a tap on the test card if there is one
            state = card_state(self.db_path)
            if not state:
                return True
            item = db.get_item(state.get("item_id") or "", db_path=self.db_path) or item
            if item.get("stage") in ("awaiting_approval", "rendered"):
                db.mark_rejected(item["id"], "synthetic_dry_run_card_closed", db_path=self.db_path)
        self.close_card(state, item or {"id": state.get("item_id")},
                        "⌛ Closed: this card can no longer be decided.")
        log.info("telegram: released the open card for %s", state.get("item_id"))
        return True

    # ----------------------------------------------------------- updates
    def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" not in update:
            return
        cq = update["callback_query"]
        msg = cq.get("message") or {}
        if not self._allowed(msg.get("chat") or {}, cq.get("from") or {}):
            log.info("telegram: ignored a button from chat %s", (msg.get("chat") or {}).get("id"))
            return
        try:
            answer = self.on_button(cq.get("data") or "", msg.get("message_id"))
        except Exception as e:  # noqa: BLE001 - a button is always answered, in plain words
            log.exception("telegram: button failed")
            answer = f"That didn't work: {redact(e)[:120]}"
        try:
            self.call("answerCallbackQuery", callback_query_id=cq.get("id"), text=answer[:190])
        except (TelegramError, requests.RequestException) as e:
            log.info("telegram: could not answer a button: %s", redact(e))

    def _allowed(self, chat: dict[str, Any], sender: dict[str, Any]) -> bool:
        """Only TELEGRAM_CHAT_ID; in a private chat the sender must be that same account."""
        allowed = str(self.chat_id or "")
        if not allowed or str(chat.get("id")) != allowed:
            return False
        return chat.get("type") != "private" or str(sender.get("id")) == allowed

    def on_button(self, data: str, message_id: Any) -> str:
        """Stale protection: only the message id and item id the open card is recorded under count."""
        kind, _, rest = data.partition(":")
        if kind != "a":
            return STALE
        action, _, item_id = rest.partition(":")
        if action not in ACTIONS:
            return STALE
        state = card_state(self.db_path)
        if not state or message_id is None or int(state["mid"]) != int(message_id):
            log.info("telegram: stale click on message %s (action %s)", message_id, action)
            return STALE
        if state.get("item_id") != item_id:
            log.info("telegram: stale click for item %s (card holds %s)", item_id, state.get("item_id"))
            return STALE

        outcome, answer = decide(item_id, action, db_path=self.db_path)
        item = db.get_item(item_id, db_path=self.db_path) or {"id": item_id}
        if outcome:
            self.close_card(state, item, outcome)
        else:
            set_card_state(None, self.db_path)  # decided elsewhere: free the slot for the next item
        return answer

    def poll_once(self, timeout: int | None = None) -> int:
        """One getUpdates round using the offset in `kv`, so a restart never re-delivers a tap."""
        wait = self.poll_timeout if timeout is None else timeout
        params: dict[str, Any] = {"timeout": wait, "allowed_updates": ["callback_query"]}
        saved = db.kv_get(K_OFFSET, db_path=self.db_path)
        if saved:
            params["offset"] = int(saved)
        updates = self.transport("getUpdates", params, None, wait + 15.0) or []
        handled = 0
        for u in updates:
            # saved before handling: a crash skips an update, it never repeats one
            db.kv_set(K_OFFSET, str(int(u["update_id"]) + 1), db_path=self.db_path)
            self.handle_update(u)
            handled += 1
        return handled

    # ----------------------------------------------------------- alerts
    def alert(self, text: str, key: str | None = None, repeat_gap_s: float | None = ALERT_REPEAT_GAP_S) -> bool:
        """A failure-only alert to the same chat. The same `key` is not re-sent within the hour
        (`repeat_gap_s=None`: that key is sent once, ever). Times are kept per key, so two
        different failures alternating every cycle are each still limited."""
        now_t = clock.epoch()
        if key:
            try:
                seen = json.loads(db.kv_get(K_ALERT, db_path=self.db_path) or "null") or {}
            except ValueError:
                seen = {}
            if "keys" not in seen:   # the pre-CP4 shape held a single {"key", "at"}
                seen = {"keys": {seen["key"]: seen.get("at", 0.0)} if seen.get("key") else {}}
            keys: dict[str, float] = seen["keys"]
            last = keys.get(key)
            if last is not None and (repeat_gap_s is None or (now_t - float(last)) < repeat_gap_s):
                log.info("telegram: alert %r already sent; not repeating", key)
                return False
            keys[key] = now_t
            for old in [k for k, at in keys.items() if now_t - float(at) > 30 * 86400]:
                keys.pop(old, None)
            db.kv_set(K_ALERT, json.dumps({"keys": keys}), db_path=self.db_path)
        try:
            self.send_message(f"🚨 {redact(text)}")
            return True
        except (TelegramError, requests.RequestException) as e:
            log.warning("telegram: alert not delivered: %s", redact(e))
            return False


def no_clean_ending_line(cfg: dict[str, Any] | None = None, db_path: Any = None) -> str | None:
    """One line for failure alerts once the last-24 h no_clean_ending rate passes
    cut.no_clean_ending_alert.rate (default 40 %, and only after min_attempts cut attempts)."""
    try:
        conf = ((cfg or config.get_config()).get("cut") or {}).get("no_clean_ending_alert") or {}
        threshold = float(conf.get("rate", 0.40))
        min_attempts = int(conf.get("min_attempts", 5))
        st = db.cut_outcome_stats(db_path, hours=24)
        if st["attempts"] < min_attempts or st["rate"] < threshold:
            return None
        return (f"Cut: {st['no_clean_ending']} of {st['attempts']} cut attempts in the last 24 h failed with "
                f"no_clean_ending ({st['rate']:.0%}; alert threshold {threshold:.0%}).")
    except Exception:  # noqa: BLE001 - a statistic must never stop an alert
        return None


def send_alert(
    text: str,
    key: str | None = None,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    bot: ApprovalBot | None = None,
    repeat_gap_s: float | None = ALERT_REPEAT_GAP_S,
) -> bool:
    """Failure alert from any stage. Never raises: with Telegram off or no credentials it logs."""
    try:
        cfg = cfg or config.get_config()
        rate_line = no_clean_ending_line(cfg, db_path)
        if rate_line and rate_line not in text:
            text = f"{text}\n{rate_line}"
        if bot is None:
            if not (cfg.get("telegram") or {}).get("enabled", True) or not (
                config.secret("TELEGRAM_BOT_TOKEN") and config.secret("TELEGRAM_CHAT_ID")
            ):
                log.warning("ALERT (not delivered, telegram off or no credentials): %s", redact(text))
                return False
            bot = ApprovalBot(cfg=cfg, db_path=db_path)
        return bot.alert(text, key=key, repeat_gap_s=repeat_gap_s)
    except Exception as e:  # noqa: BLE001 - alerting must never break the stage that alerts
        log.warning("alert not delivered (%s): %s", e, text)
        return False


# ---------------------------------------------------------------------------
# Auto mode: the PLAN.md quality gate, no Telegram
# ---------------------------------------------------------------------------

def _ffprobe_duration(path: Path | str, runner: Callable[[list[str]], Any] = None) -> float | None:
    """Seconds of the container, or None when ffprobe cannot read the file."""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nokey=1:noprint_wrappers=1", str(path),
    ]
    try:
        proc = (runner or (lambda c: subprocess.run(c, capture_output=True, text=True, timeout=30)))(cmd)
        return float(str(proc.stdout).strip())
    except Exception as e:  # noqa: BLE001 - any failure is a gate failure, not a crash
        log.info("ffprobe failed on %s: %s", path, e)
        return None


def quality_gate(
    item: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    probe: Callable[[Path | str], float | None] | None = None,
) -> tuple[bool, str]:
    """The `approval.auto_quality_gate` checks from PLAN.md. Returns (passed, reason).

    `max_duration_seconds` is applied to Variant A (the cut-length render). Variant B is a
    freeze-frame plus the cut (PLAN.md section 7), so it is longer by design and is checked against
    `max_b_extra_seconds` of headroom over A instead.
    """
    cfg = cfg or config.get_config()
    gate = ((cfg.get("approval") or {}).get("auto_quality_gate") or {})
    probe = probe or _ffprobe_duration

    a_path, b_path = item.get("render_a_path"), item.get("render_b_path")
    if gate.get("require_both_renders", True):
        for label, p in (("A", a_path), ("B", b_path)):
            if not p or not Path(p).exists():
                return False, f"variant {label} render missing"

    if gate.get("require_grounded_hook", True):
        if not item.get("hook_text"):
            return False, "no hook text"
        if not int(item.get("hook_grounded") or 0):
            return False, "hook is not grounded (fell back)"

    if gate.get("require_ffprobe_pass", True):
        max_a = float(gate.get("max_duration_seconds", 22.0))
        max_extra = float(gate.get("max_b_extra_seconds", 8.0))
        dur_a = probe(a_path) if a_path else None
        dur_b = probe(b_path) if b_path else None
        if dur_a is None:
            return False, "ffprobe could not read variant A"
        if dur_a > max_a:
            return False, f"variant A is {dur_a:.1f}s (limit {max_a:.1f}s)"
        if b_path:
            if dur_b is None:
                return False, "ffprobe could not read variant B"
            if dur_b - dur_a > max_extra:
                return False, f"variant B is {dur_b - dur_a:.1f}s longer than A (limit {max_extra:.1f}s)"
    return True, "quality gate passed"


def run_auto_approval(
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    probe: Callable[[Path | str], float | None] | None = None,
) -> dict[str, list[str]]:
    """`approval.mode: auto`: gate every rendered item, no Telegram. Returns approved / rejected ids."""
    cfg = cfg or config.get_config()
    approved: list[str] = []
    rejected: list[str] = []
    for stage in ("rendered", "awaiting_approval"):
        for item in db.get_items_by_stage(stage, db_path=db_path, releasable_only=True):
            passed, reason = quality_gate(item, cfg=cfg, probe=probe)
            if passed:
                try:
                    db.transition_stage(
                        item["id"],
                        "approved",
                        extra_fields={"approval_type": "auto_approved", "chosen_variant": None, "ab_excluded": 0},
                        db_path=db_path,
                    )
                except sqlite3.IntegrityError as e:
                    refuse_item(item["id"], "approval", e, db_path)
                    rejected.append(item["id"])
                    continue
                db.log_stage(item["id"], "approval", "success", message=f"auto approved: {reason}", db_path=db_path)
                approved.append(item["id"])
            else:
                db.mark_rejected(item["id"], f"auto_quality_gate: {reason}", db_path=db_path)
                db.log_stage(item["id"], "approval", "failure", message=f"auto rejected: {reason}", db_path=db_path)
                rejected.append(item["id"])
    log.info("auto approval: %d approved, %d rejected", len(approved), len(rejected))
    return {"approved": approved, "rejected": rejected}


# ---------------------------------------------------------------------------
# The runner's entry point
# ---------------------------------------------------------------------------

def effective_mode(cfg: dict[str, Any] | None = None, db_path: Any = None) -> str:
    """'telegram' or 'auto' for this pass.

    `approval.mode: auto` is always auto. `approval.mode: telegram` is Telegram for the first
    `approval.switch_to_auto_after_days` days counted from the FIRST LIVE POST and auto after that;
    the moment of the switch is pinned in `kv`. Set switch_to_auto_after_days to null (or 0) to
    keep approving by hand - that also undoes a switch that already happened.
    """
    approval = ((cfg or {}).get("approval") or {})
    mode = str(approval.get("mode") or "telegram").lower()
    if mode == "auto":
        return "auto"
    after_days = approval.get("switch_to_auto_after_days")
    if not after_days:
        return "telegram"
    if db.kv_get(K_AUTO_SINCE, db_path=db_path):
        return "auto"
    days = db.days_since_first_live(db_path)
    if days is not None and days >= float(after_days):
        db.kv_set(K_AUTO_SINCE, db.now(), db_path=db_path)
        log.info("approval: %.1f days since the first live post; switching to auto approval", days)
        return "auto"
    return "telegram"


def announce_auto_switch(
    cfg: dict[str, Any], db_path: Any = None, bot: ApprovalBot | None = None
) -> bool:
    """Send the ONE Telegram notice that approvals are now automatic, and retire any open card.

    Returns True when the notice went out on this call. The flag is only set once the message was
    really delivered (or can never be: Telegram off / no credentials), so a network blip retries on
    the next pass and never produces a second notice.
    """
    if db.kv_get(K_AUTO_NOTICE, db_path=db_path) or not db.kv_get(K_AUTO_SINCE, db_path=db_path):
        return False
    tg = cfg.get("telegram") or {}
    have_creds = bool(config.secret("TELEGRAM_BOT_TOKEN") and config.secret("TELEGRAM_CHAT_ID"))
    if bot is None and (not tg.get("enabled", True) or not have_creds):
        log.warning("approval switched to auto; no Telegram notice (telegram off or credentials missing)")
        db.kv_set(K_AUTO_NOTICE, "skipped:" + db.now(), db_path=db_path)
        return False
    bot = bot or ApprovalBot(cfg=cfg, db_path=db_path)
    days = (cfg.get("approval") or {}).get("switch_to_auto_after_days")
    try:
        state = card_state(db_path)
        if state:   # the item on the open card is decided by the gate from now on
            item = db.get_item(state.get("item_id") or "", db_path=db_path) or {"id": state.get("item_id")}
            bot.close_card(state, item, "🤖 Auto approval took over; the quality gate decides this item.")
        bot.send_message(
            f"🤖 Approvals are now automatic. It has been {days} days since the first live post, so "
            "items are approved by the quality gate (both renders, grounded hook, duration) without "
            "a card. You will still get failure alerts here. To go back to cards, set "
            "approval.switch_to_auto_after_days to null in config.yaml."
        )
    except (TelegramError, requests.RequestException) as e:
        log.warning("approval: switch notice not delivered yet (%s); will retry", redact(e))
        return False
    db.kv_set(K_AUTO_NOTICE, db.now(), db_path=db_path)
    return True


def run_approval_stage(
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
    bot: ApprovalBot | None = None,
    poll_timeout: int | None = None,
    probe: Callable[[Path | str], float | None] | None = None,
) -> dict[str, Any]:
    """One approval pass inside the runner's loop.

    auto     -> the quality gate, no approval card (see `effective_mode` for when that is).
    telegram -> ensure one card is open, then one long-poll round.
    """
    cfg = cfg or config.get_config()
    mode = effective_mode(cfg, db_path)

    if mode == "auto":
        switched = db.kv_get(K_AUTO_SINCE, db_path=db_path)
        notice = announce_auto_switch(cfg, db_path, bot) if switched else False
        result = run_auto_approval(cfg=cfg, db_path=db_path, probe=probe)
        out: dict[str, Any] = {"mode": "auto", **result}
        if switched:
            out["auto_since"] = switched
            out["switch_notice_sent"] = notice
        return out

    tg = cfg.get("telegram") or {}
    if not tg.get("enabled", True):
        log.info("telegram is disabled in config; no approval pass")
        return {"mode": "telegram", "skipped": "telegram.enabled is false"}
    if not (config.secret("TELEGRAM_BOT_TOKEN") and config.secret("TELEGRAM_CHAT_ID")) and bot is None:
        log.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing; no approval pass")
        return {"mode": "telegram", "skipped": "credentials missing"}

    bot = bot or ApprovalBot(cfg=cfg, db_path=db_path)
    bot.release_stuck_card(poll_timeout)
    sent = bot.ensure_card()
    handled = bot.poll_once(poll_timeout)
    bot.editor.flush()
    return {"mode": "telegram", "card_sent": sent, "updates_handled": handled}

"""Stage 5: Gemini multimodal hook generation, authenticity classification, and grounding verification.

Failure policy (no silent placeholders):
  * Gemini answers that are truncated (finishReason MAX_TOKENS), not valid JSON, or missing
    hook_text / voiceover_script raise GeminiError: that attempt failed, nothing is defaulted.
  * Calls are spaced to the free-tier limit (gemini.requests_per_minute) and an HTTP 429 is
    retried after the server's retryDelay.
  * A grounding check that errors (429, network, odd reply) means NOT grounded.
  * Only when Gemini answered but the hook got `grounding_fail_limit` FAIL verdicts is the
    fixed question hook used (saved with hook_grounded = 0).
  * No valid Gemini hook after `max_attempts` -> the hooks stage is marked failed, and any
    previously stored hook fields are cleared.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from . import config, db, keywords

log = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

GEMINI_DEFAULTS: dict[str, Any] = {
    "model": "gemini-2.5-flash",
    "thinking_budget": 0,
    "max_output_tokens": 2048,
    "requests_per_minute": 5,
    "rate_limit_retries": 2,
    "max_rate_limit_wait_s": 120,
    "max_attempts": 3,
    "grounding_fail_limit": 2,
    "fallback_hook": "IS THIS REALLY GTA 6?",
}
FALLBACK_VOICEOVER = "Check out this crazy GTA 6 moment right now!"

# Free-tier pacing state (per process). _sleep/_now are module attributes so tests can fake them.
_LAST_CALL_T: float | None = None
_QUOTA_BLOCKED_UNTIL: float | None = None  # set by a daily-quota 429; later calls fail fast
_sleep = time.sleep
_now = time.monotonic


class GeminiError(RuntimeError):
    """A Gemini call did not produce a usable answer (the attempt failed)."""


class GeminiRateLimitError(GeminiError):
    """HTTP 429 persisted after the configured retries."""


class GeminiQuotaExhaustedError(GeminiRateLimitError):
    """HTTP 429 whose retryDelay is longer than max_rate_limit_wait_s (the daily quota)."""


class GeminiTruncatedError(GeminiError):
    """finishReason MAX_TOKENS: the answer was cut off."""


class GeminiBadResponseError(GeminiError):
    """Empty, blocked, or unparseable answer, or required fields missing."""


def gemini_settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or config.get_config()
    out = dict(GEMINI_DEFAULTS)
    out.update({k: v for k, v in (cfg.get("gemini") or {}).items()})
    return out


def _redact(text: str) -> str:
    """Never let the API key (it rides in the request URL) reach logs or the DB."""
    text = re.sub(r"key=[^&\s\"']+", "key=REDACTED", str(text))
    return re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "REDACTED", text)


def extract_sample_frames(video_path: Path | str, count: int = 4) -> list[bytes]:
    """Extract count frames (JPEG bytes) spaced evenly across the cut clip."""
    video_path_str = str(video_path)
    # Get duration
    cmd_dur = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "format=duration",
        "-of", "json",
        video_path_str,
    ]
    try:
        p = subprocess.run(cmd_dur, capture_output=True, text=True, timeout=10)
        dur = float(json.loads(p.stdout)["format"]["duration"])
    except Exception:
        dur = 20.0

    frames: list[bytes] = []
    # Sample at 15%, 40%, 65%, 90%
    ratios = [0.15, 0.40, 0.65, 0.90][:count]
    for r in ratios:
        ss = max(0.2, dur * r)
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-ss", f"{ss:.2f}",
            "-i", video_path_str,
            "-vframes", "1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "-q:v", "3",
            "-",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=10)
            if res.stdout:
                frames.append(res.stdout)
        except Exception as e:
            log.debug("Frame extraction failed at %.2fs: %s", ss, e)

    return frames


# --------------------------------------------------------------------------------------
# Gemini transport: pacing, 429 handling, truncation detection
# --------------------------------------------------------------------------------------

def _throttle(min_interval_s: float) -> None:
    """Keep consecutive Gemini calls at least min_interval_s apart."""
    global _LAST_CALL_T
    if _LAST_CALL_T is not None:
        wait = float(min_interval_s) - (_now() - _LAST_CALL_T)
        if wait > 0:
            log.info("Gemini pacing: waiting %.1fs (free-tier rate limit)", wait)
            _sleep(wait)
    _LAST_CALL_T = _now()


def _retry_delay_s(res: Any) -> float | None:
    """Server-requested wait for a 429: Retry-After header or RetryInfo.retryDelay ("54s")."""
    try:
        ra = (res.headers or {}).get("Retry-After")
        if ra:
            return float(ra)
    except Exception:
        pass
    try:
        for d in res.json().get("error", {}).get("details", []) or []:
            m = re.match(r"^\s*([\d.]+)s\s*$", str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    except Exception:
        pass
    return None


def _call_gemini(
    prompt: str,
    images: list[bytes] | None = None,
    model: str = "gemini-2.5-flash",
    api_key: str | None = None,
    json_mode: bool = False,
    cfg: dict[str, Any] | None = None,
) -> str:
    """Call Gemini generateContent with text and optional inline images. Raises GeminiError."""
    global _QUOTA_BLOCKED_UNTIL
    gs = gemini_settings(cfg)
    if _QUOTA_BLOCKED_UNTIL is not None and _now() < _QUOTA_BLOCKED_UNTIL:
        raise GeminiQuotaExhaustedError(
            f"Gemini daily quota exhausted (retry in {_QUOTA_BLOCKED_UNTIL - _now():.0f}s); not calling"
        )
    if not api_key:
        env = config.load_env()
        api_key = env.get("GEMINI_API_KEY", "")

    if not api_key:
        raise ValueError("GEMINI_API_KEY missing from environment or .env")

    url = f"{GEMINI_API_BASE}/{model}:generateContent?key={api_key}"

    parts: list[dict[str, Any]] = []
    if images:
        for img_bytes in images:
            parts.append({
                "inline_data": {
                    "mime_type": "image/jpeg",
                    "data": base64.b64encode(img_bytes).decode("utf-8"),
                }
            })
    parts.append({"text": prompt})

    gen_cfg: dict[str, Any] = {
        "temperature": 0.4,
        "maxOutputTokens": int(gs["max_output_tokens"]),
    }
    if gs.get("thinking_budget") is not None:
        gen_cfg["thinkingConfig"] = {"thinkingBudget": int(gs["thinking_budget"])}
    if json_mode:
        gen_cfg["responseMimeType"] = "application/json"
    payload = {"contents": [{"parts": parts}], "generationConfig": gen_cfg}

    min_interval = 60.0 / max(1e-6, float(gs["requests_per_minute"]))
    retries = max(0, int(gs["rate_limit_retries"]))
    res = None
    for i in range(retries + 1):
        _throttle(min_interval)
        try:
            res = requests.post(url, json=payload, timeout=60)
        except requests.RequestException as e:
            raise GeminiError(_redact(f"Gemini request failed: {e}")) from None
        if res.status_code != 429:
            break
        delay = _retry_delay_s(res)
        max_wait = float(gs["max_rate_limit_wait_s"])
        if delay is not None and delay > max_wait:
            # A per-minute 429 asks for < 60 s; hours means the daily quota is gone. Never
            # sleep that long (the runner would hold its lock for half a day): fail fast.
            _QUOTA_BLOCKED_UNTIL = _now() + delay
            raise GeminiQuotaExhaustedError(
                f"Gemini daily quota exhausted (HTTP 429, retryDelay {delay:.0f}s > {max_wait:.0f}s cap)"
            )
        if i >= retries:
            raise GeminiRateLimitError(f"Gemini HTTP 429 after {i + 1} tries (retryDelay {delay}s)")
        wait = (delay if delay is not None else min_interval) + 1.0
        log.warning("Gemini HTTP 429: waiting %.0fs as requested, then retrying (%d/%d)", wait, i + 1, retries)
        _sleep(wait)

    if res.status_code != 200:
        raise GeminiError(_redact(f"Gemini API HTTP {res.status_code}: {res.text[:300]}"))

    resp_json = res.json()
    candidates = resp_json.get("candidates") or []
    if not candidates:
        raise GeminiBadResponseError(_redact(f"No candidates returned from Gemini: {str(resp_json)[:300]}"))

    cand = candidates[0]
    finish = cand.get("finishReason")
    if finish == "MAX_TOKENS":
        raise GeminiTruncatedError(
            f"Gemini answer truncated (finishReason MAX_TOKENS, usage {resp_json.get('usageMetadata')})"
        )
    if finish not in (None, "STOP"):
        raise GeminiBadResponseError(f"Gemini finishReason {finish}")
    text = "".join(
        p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [] if not p.get("thought")
    ).strip()
    if not text:
        raise GeminiBadResponseError("Gemini returned an empty answer")
    return text


# --------------------------------------------------------------------------------------
# Hook generation + grounding
# --------------------------------------------------------------------------------------

def generate_hook_and_voiceover(
    video_path: Path | str,
    source_title: str,
    transcript_text: str = "",
    model: str = "gemini-2.5-flash",
    api_key: str | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Generate hook, authenticity tag, voiceover line and keywords with one multimodal call.

    Raises GeminiError when the answer is unusable; never substitutes defaults.
    """
    sample_frames = extract_sample_frames(video_path, count=4)

    prompt = f"""You are a content strategist for high-performing, authentic GTA 6 short-form vertical videos (Reels).
Analyze these 4 sampled video frames, the source title, and audio transcript below.

SOURCE TITLE: "{source_title}"
AUDIO TRANSCRIPT: "{transcript_text if transcript_text else '[No speech detected]'}"

TASK 1: CLASSIFY AUTHENTICITY
- "official": Footage is verified official Rockstar Games trailer, teaser, or platform upload (PlayStation/Xbox/Netflix).
- "unverified": Footage is a leak, rumor, fan concept, GTA 5 mod, VR mod, or unconfirmed gameplay.
(Rule: When in doubt or if creator-made, classify as "unverified".)

TASK 2: WRITE HOOK
- Hook style: Choose one of "curiosity", "question", or "hype_analysis".
- Hook text: 1-2 short lines, ALL-CAPS, maximum 6-8 words. High impact, readable in 2 seconds.
- CRITICAL HONESTY RULE: If authenticity is "unverified", the hook MUST NOT claim this is confirmed real GTA 6! Frame it honestly as a question or speculation (e.g., "IS THIS REALLY GTA 6?", "COULD THIS LEAK BE REAL?"). Never deceive viewers.

TASK 3: WRITE VOICEOVER LINE
- Single punchy voiceover line of 3 to 6 seconds (around 10-18 spoken words).
- Must play near the start of the video.
- Conversational, high energy. If unverified, keep it speculative.

TASK 4: PICK CAPTION KEYWORDS
- 3 to 6 single words that are the most important things said or shown in this clip
  (names, places, objects, numbers - e.g. "LUCIA", "VICE", "MAP", "POLICE", "2026").
- ALL-CAPS, one word each, no punctuation, no duplicates.
- They must be words that actually appear in the transcript or are clearly visible in
  the frames; these get highlighted inside the burned-in captions.

Return ONLY a valid JSON object with EXACTLY this structure:
{{
  "authenticity": "official" or "unverified",
  "hook_style": "curiosity" or "question" or "hype_analysis",
  "hook_text": "HOOK TEXT IN ALL CAPS",
  "voiceover_script": "Short 3-6 second spoken line.",
  "keywords": ["WORD", "WORD", "WORD"]
}}
"""
    raw_response = _call_gemini(prompt, images=sample_frames, model=model, api_key=api_key,
                                json_mode=True, cfg=cfg)

    # Tolerate a markdown fence around the JSON, nothing else.
    clean = re.sub(r"^```(?:json)?\s*", "", raw_response.strip())
    clean = re.sub(r"\s*```$", "", clean)
    try:
        data = json.loads(clean)
    except Exception:
        raise GeminiBadResponseError(f"Gemini answer is not valid JSON: {raw_response[:160]!r}") from None
    if not isinstance(data, dict):
        raise GeminiBadResponseError(f"Gemini answer is not a JSON object: {raw_response[:160]!r}")

    hook_raw = data.get("hook_text")
    vo_raw = data.get("voiceover_script")
    if not isinstance(hook_raw, str) or not hook_raw.strip():
        raise GeminiBadResponseError("Gemini answer has no hook_text")
    if not isinstance(vo_raw, str) or not vo_raw.strip():
        raise GeminiBadResponseError("Gemini answer has no voiceover_script")

    # Classification normalisation (conservative): anything unrecognised is 'unverified'.
    auth = data.get("authenticity", "unverified")
    if auth not in ("official", "unverified"):
        auth = "unverified"

    hook_style = data.get("hook_style", "question")
    if hook_style not in ("curiosity", "question", "hype_analysis"):
        hook_style = "question"

    hook_text = hook_raw.strip().upper()
    vo_script = vo_raw.strip()

    # Guarantee unverified clips have question framing
    if auth == "unverified" and not hook_text.endswith("?"):
        hook_text = f"{hook_text}?"

    # C1: 3-6 caption keywords. If Gemini gave fewer than 3, top up from the rule
    # extractor over the same transcript/title - and say so in keywords_source.
    kw = keywords.clean_keywords(data.get("keywords"), cfg)
    kw_source = "gemini"
    if len(kw) < 3:
        extra = keywords.rule_keywords(f"{transcript_text} {source_title} {hook_text}", cfg)
        kw += [k for k in extra if k not in kw]
        kw = kw[:6]
        kw_source = "gemini+rules"

    return {
        "authenticity": auth,
        "hook_style": hook_style,
        "hook_text": hook_text,
        "voiceover_script": vo_script,
        "keywords": kw,
        "keywords_source": kw_source,
    }


def verify_hook_grounding(
    hook_text: str,
    authenticity: str,
    source_title: str,
    transcript_text: str = "",
    model: str = "gemini-2.5-flash",
    api_key: str | None = None,
    cfg: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Verification check call to Gemini ensuring hook is grounded and honest.

    Checks:
    1. Does the clip/transcript actually show or discuss what the hook claims?
    2. Does the hook falsely claim this is real GTA 6 when it is unverified?
    Returns (passed, reason). reason starts with "FAIL" for a FAIL verdict and with
    "ERROR" when no verdict was obtained (429, network, unexpected reply) - both are
    NOT grounded.
    """
    check_prompt = f"""You are a strict quality control auditor for short-form video hooks.

HOOK CANDIDATE: "{hook_text}"
AUTHENTICITY TAG: "{authenticity}" (official or unverified)
SOURCE TITLE: "{source_title}"
TRANSCRIPT: "{transcript_text if transcript_text else '[No speech]'}"

VERIFICATION RULES:
1. Grounding: Does the clip/title/transcript actually show or discuss what the hook claims?
2. Honesty (STRICT): If AUTHENTICITY is "unverified", does the hook falsely claim or state as a confirmed fact that this is real GTA 6? (If it claims fake/modded/leak footage is confirmed GTA 6, that is a direct violation).

Respond with EXACTLY:
PASS
or
FAIL: <one-sentence specific reason>
"""
    try:
        reply = _call_gemini(check_prompt, images=None, model=model, api_key=api_key, cfg=cfg)
    except Exception as e:
        log.warning("Hook verification call error (treated as NOT grounded): %s", _redact(e))
        return False, _redact(f"ERROR: grounding check failed: {e}")
    first_line = (reply.strip().splitlines() or [""])[0].strip()
    if first_line.startswith("PASS"):
        return True, "Passed grounding and honesty verification"
    if first_line.startswith("FAIL"):
        return False, first_line
    return False, f"ERROR: unexpected grounding reply: {first_line[:120]!r}"


def _clear_hook_fields(item_id: str, db_path: Any = None) -> None:
    """Drop any previously stored hook so a failed item cannot render a stale/placeholder one."""
    with db.db(db_path) as conn:
        conn.execute(
            """
            UPDATE items SET hook_style = NULL, hook_text = NULL, hook_grounded = 0,
                authenticity = 'unverified', voiceover_script = NULL, keywords = NULL,
                updated_at = :now
            WHERE id = :id
            """,
            {"id": item_id, "now": db.now()},
        )


def _cut_transcript(item: dict[str, Any], cut_path: Path, cfg: dict[str, Any], db_path: Any) -> list[dict[str, Any]]:
    """faster-whisper words for the cut window, cached on the item for render to reuse."""
    from . import render  # lazy: render imports this module

    model_name = render.render_settings(cfg)["whisper_model"]
    words = render.cached_cut_words(item, model_name)
    if words is not None:
        return words
    log.info("Transcribing cut %s with faster-whisper (%s)...", cut_path.name, model_name)
    # raise_errors: a broken whisper (missing model cache, bad HF_HOME, ...) must fail the
    # stage, not silently hand Gemini an empty transcript.
    words = render.transcribe_words(cut_path, model_name=model_name, raise_errors=True)
    if words:  # nothing spoken: nothing worth caching (render will simply transcribe again)
        render.store_cut_words(item["id"], item, words, model_name, db_path=db_path)
    return words


def run_hooks_for_item(
    item_id: str,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
) -> tuple[bool, str]:
    """Execute hook generation and grounding verification for an item in 'cut' stage."""
    cfg = cfg or config.get_config()
    item = db.get_item(item_id, db_path=db_path)
    if not item:
        return False, f"Item {item_id} not found"

    cut_path_str = item.get("cut_path")
    if not cut_path_str or not Path(cut_path_str).exists():
        db.mark_failed(item_id, "hooks", "Cut file not found on disk", db_path=db_path)
        return False, "Cut file not found"

    cut_path = Path(cut_path_str)
    source_title = item.get("original_title") or "GTA 6"
    gs = gemini_settings(cfg)
    model = gs["model"]

    # Transcript of the cut window (PLAN §8): Gemini gets it with the 4 frames and title.
    try:
        words = _cut_transcript(item, cut_path, cfg, db_path)
    except Exception as e:
        reason = _redact(f"Whisper transcription failed: {type(e).__name__}: {e}")
        log.error("%s (item %s)", reason, item_id)
        _clear_hook_fields(item_id, db_path=db_path)
        db.mark_failed(item_id, "hooks", reason, db_path=db_path)
        db.log_stage(item_id, "hooks", "failed", message=reason, db_path=db_path)
        return False, reason
    transcript = " ".join(str(w.get("word", "")).strip() for w in words).strip()
    log.info("Cut transcript for %s: %d words", item_id, len(words))

    max_attempts = max(1, int(gs["max_attempts"]))
    fail_limit = max(1, int(gs["grounding_fail_limit"]))
    problems: list[str] = []
    grounding_fails = 0
    final_data: dict[str, Any] | None = None
    passed_grounding = False
    used_fallback = False

    for attempt in range(1, max_attempts + 1):
        log.info("Generating hook for item %s (attempt %d/%d)...", item_id, attempt, max_attempts)
        try:
            data = generate_hook_and_voiceover(
                cut_path, source_title, transcript_text=transcript, model=model, cfg=cfg,
            )
        except Exception as e:
            msg = _redact(f"attempt {attempt}: {type(e).__name__}: {e}")
            log.warning("Gemini generation failed: %s", msg)
            problems.append(msg)
            continue

        is_grounded, reason = verify_hook_grounding(
            data["hook_text"], data["authenticity"], source_title,
            transcript_text=transcript, model=model, cfg=cfg,
        )
        if is_grounded:
            log.info("Hook passed verification: %s (%s)", data["hook_text"], data["authenticity"])
            passed_grounding = True
            final_data = data
            break

        problems.append(f"attempt {attempt}: hook {data['hook_text']!r} not grounded: {reason}")
        if reason.startswith("FAIL"):
            grounding_fails += 1
            log.warning("Hook failed grounding (%d/%d): %s. Reason: %s",
                        grounding_fails, fail_limit, data["hook_text"], reason)
            if grounding_fails >= fail_limit:
                # Gemini answered, but its hooks failed grounding: the designed fallback.
                log.warning("Falling back to the plain question hook for item %s", item_id)
                final_data = {
                    "authenticity": "unverified",
                    "hook_style": "question",
                    "hook_text": str(gs["fallback_hook"]),
                    "voiceover_script": FALLBACK_VOICEOVER,
                    "keywords": keywords.rule_keywords(f"{transcript} {source_title}", cfg=cfg),
                    "keywords_source": "rules",
                }
                used_fallback = True
                break
        else:
            log.warning("Hook not grounded (no verdict) on attempt %d: %s", attempt, reason)

    if final_data is None:
        reason = f"No valid Gemini hook after {max_attempts} attempts: " + " | ".join(problems)
        _clear_hook_fields(item_id, db_path=db_path)
        db.mark_failed(item_id, "hooks", reason, db_path=db_path)
        db.log_stage(item_id, "hooks", "failed", message=reason, db_path=db_path)
        return False, reason

    # Save hook, authenticity and the C1 keyword cache to the database
    with db.db(db_path) as conn:
        conn.execute(
            """
            UPDATE items SET
                hook_style = :hook_style,
                hook_text = :hook_text,
                hook_grounded = :hook_grounded,
                authenticity = :authenticity,
                voiceover_script = :voiceover_script,
                keywords = :keywords,
                updated_at = :now
            WHERE id = :id
            """,
            {
                "id": item_id,
                "hook_style": final_data["hook_style"],
                "hook_text": final_data["hook_text"],
                "hook_grounded": 1 if passed_grounding else 0,
                "authenticity": final_data["authenticity"],
                "voiceover_script": final_data["voiceover_script"],
                "keywords": json.dumps(final_data.get("keywords") or []),
                "now": db.now(),
            },
        )

    source = "fallback (grounding failed)" if used_fallback else "gemini"
    db.log_stage(
        item_id,
        "hooks",
        "fallback" if used_fallback else "success",
        message=(
            f"Hook: '{final_data['hook_text']}' [{final_data['authenticity']}, grounded={passed_grounding}, "
            f"source={source}, keywords={final_data.get('keywords_source')}, transcript_words={len(words)}]"
        ),
        db_path=db_path,
    )
    return True, f"Hook generated [{final_data['authenticity']}, source={source}]"

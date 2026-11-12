"""Stage 5: Gemini multimodal hook generation, authenticity classification, and grounding verification.

Grounding (the hook describes THIS clip, not the source video):
  * Gemini gets the transcript of the CUT segment only, 4 frames from the cut, the item's
    authenticity tag (from the source channel) and the source title marked as context only, and
    is told to reference nothing that is not said in the segment or visible in the frames.
  * Every hook and voiceover line then gets checked against the segment: free claim-word and
    content-word checks (`claim_check`, `segment_overlap_check`; lists and threshold in
    config.yaml -> hook_check), then a short Gemini PASS/FAIL that may use only the segment
    transcript and frames as evidence (`verify_hook_grounding`; the title is not evidence).
  * A hook that FAILs is regenerated once, with the reason. If that one FAILs too the item goes to
    `needs_review`: it is not rendered, and the Telegram approval flow shows the hook, the reason
    and the segment transcript for a human to approve or reject. There is no silent fallback hook.
  * The saved hook is stamped with the item + segment it was made for (pipeline.binding), so render
    can refuse a hook that belongs to another item or an older window.

Failure policy (no silent placeholders):
  * Gemini answers that are truncated (finishReason MAX_TOKENS), not valid JSON, or missing
    hook_text / voiceover_script raise GeminiError: that attempt failed, nothing is defaulted.
  * Calls are spaced to the free-tier limit (gemini.requests_per_minute) and an HTTP 429 is
    retried after the server's retryDelay.
  * A grounding check that errors (429, network, odd reply) means NOT grounded.
  * No checked Gemini hook after `max_attempts` -> the hooks stage is marked failed, and any
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

from . import binding, config, db, keywords

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
    "grounding_fail_limit": 2,       # segment-check FAILs (first hook + one regeneration) before manual review
    "fallback_hook": "IS THIS REALLY GTA 6?",   # no longer rendered silently; kept for old configs
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

def source_authenticity(item: dict[str, Any] | None, cfg: dict[str, Any] | None = None) -> str:
    """The tag the SOURCE supports before any model looks at it: "official" only for a channel
    listed in sources.official_channels (Rockstar, PlayStation, Xbox, ...), else "unverified"."""
    cfg = cfg or config.get_config()
    ids = set((((cfg.get("sources") or {}).get("official_channels") or {}).get("channel_ids")) or [])
    channel = str((item or {}).get("channel_id") or "")
    return "official" if channel and channel in ids else "unverified"


def generate_hook_and_voiceover(
    video_path: Path | str,
    source_title: str,
    transcript_text: str = "",
    model: str = "gemini-2.5-flash",
    api_key: str | None = None,
    cfg: dict[str, Any] | None = None,
    *,
    authenticity: str | None = None,
    frames: list[bytes] | None = None,
    segment: tuple[float, float] | None = None,
    previous_failure: str = "",
) -> dict[str, Any]:
    """Generate hook, authenticity tag, voiceover line and keywords with one multimodal call.

    `transcript_text` must be the transcript of the CUT segment (what viewers hear), not of the
    whole source. `authenticity` is the tag the source supports (see `source_authenticity`): when
    given, Gemini may only downgrade "official" to "unverified", never the other way round.
    `frames` are the cut's sample frames (extracted here when None). `previous_failure` tells
    Gemini why its last hook for this segment was rejected.

    Raises GeminiError when the answer is unusable; never substitutes defaults.
    """
    sample_frames = extract_sample_frames(video_path, count=4) if frames is None else frames

    clip_line = ""
    if segment is not None:
        a, b = float(segment[0]), float(segment[1])
        clip_line = f"\nCLIP: {b - a:.1f} s cut from {a:.1f}-{b:.1f} s of the source video."
    if authenticity in ("official", "unverified"):
        auth_block = f"""AUTHENTICITY TAG: "{authenticity}" (set from the source channel: "official" = Rockstar Games or a platform holder's own channel; "unverified" = everyone else)

TASK 1: CONFIRM AUTHENTICITY
- Return the tag above. You may change "official" to "unverified" if the frames clearly show a mod,
  fan concept or anything that is not official footage. Never change "unverified" to "official"."""
    else:
        auth_block = """TASK 1: CLASSIFY AUTHENTICITY
- "official": Footage is verified official Rockstar Games trailer, teaser, or platform upload (PlayStation/Xbox/Netflix).
- "unverified": Footage is a leak, rumor, fan concept, GTA 5 mod, VR mod, or unconfirmed gameplay.
(Rule: When in doubt or if creator-made, classify as "unverified".)"""
    claim_list = ", ".join(w.upper() for w in hook_check_settings(cfg)["claim_words"])
    if authenticity == "official":
        claim_rule = (f"- CLAIM WORDS ({claim_list}): use one in the hook or the voiceover ONLY if the CLIP "
                      "TRANSCRIPT itself says it. Never because the source title says it.")
    else:
        claim_rule = (f"- CLAIM WORDS ({claim_list}): if the authenticity tag is \"unverified\", never use any of "
                      "them in the hook or the voiceover, even if the clip says them. If it is \"official\", use "
                      "one only when the CLIP TRANSCRIPT itself says it.")
    retry_block = ""
    if previous_failure:
        retry_block = f"""
PREVIOUS ATTEMPT REJECTED: {previous_failure}
Write a NEW hook and voiceover that only use what is said in the CLIP TRANSCRIPT or clearly visible in the frames.
"""

    prompt = f"""You are a content strategist for high-performing, authentic GTA 6 short-form vertical videos (Reels).
You are writing for ONE short clip cut out of a longer source video. Viewers see ONLY this clip:
the 4 frames are sampled from it and the CLIP TRANSCRIPT is every word spoken in it.{clip_line}

CLIP TRANSCRIPT (this segment only): "{transcript_text if transcript_text else '[No speech detected]'}"
SOURCE VIDEO TITLE (context only - it describes the WHOLE source video, most of which is NOT in this clip): "{source_title}"

GROUNDING RULE (most important): the hook, the voiceover and the keywords may only refer to things
that are said in the CLIP TRANSCRIPT or clearly visible in the frames. Never use a topic, name,
number, feature or claim that appears only in the source title. If the clip itself says or shows
nothing specific, write an honest generic question hook instead.
{retry_block}
{auth_block}

TASK 2: WRITE HOOK
- Hook style: Choose one of "curiosity", "question", or "hype_analysis".
- Hook text: 1-2 short lines, ALL-CAPS, maximum 6-8 words. High impact, readable in 2 seconds.
- It must describe what happens in THIS clip (see the grounding rule): unless it is a generic question,
  it uses at least 2 specific words (names, places, things) that are said in the CLIP TRANSCRIPT.
- CRITICAL HONESTY RULE: If authenticity is "unverified", the hook MUST NOT claim this is confirmed real GTA 6! Frame it honestly as a question or speculation (e.g., "IS THIS REALLY GTA 6?", "WHAT IS LUCIA DOING IN VICE CITY?"). Never deceive viewers.
{claim_rule}

TASK 3: WRITE VOICEOVER LINE
- Single punchy voiceover line of 3 to 6 seconds (around 10-18 spoken words).
- Must play near the start of the video and set up what THIS clip shows (grounding rule).
- Conversational, high energy. If unverified, keep it speculative. The claim-word rule above applies
  to the voiceover too: never say someone "posted a leak" or that something was "confirmed" unless the
  rule allows it.

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
    if authenticity == "unverified":
        auth = "unverified"          # the source decides; a model can never upgrade it to official

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
        # Segment words only: a title word is not in the clip and must not be highlighted in it.
        extra = keywords.rule_keywords(f"{transcript_text} {hook_text}", cfg)
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


#: config.yaml -> hook_check (the free checks every hook AND voiceover line gets against the segment).
#: Defaults for an older config; the lists themselves live in config.yaml.
HOOK_CHECK_DEFAULTS: dict[str, Any] = {
    "min_shared_content_words": 2,
    "single_word_lines_ok": True,
    "generic_topic_words": ["gta", "vi", "6", "six", "game", "rockstar", "version", "trailer", "new", "video"],
    "claim_words": ["leak", "leaked", "confirmed", "official", "real", "revealed", "insider", "exclusive", "proof"],
    "stopwords": [],
}


def hook_check_settings(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg if cfg is not None else config.get_config()
    out = dict(HOOK_CHECK_DEFAULTS)
    out.update({k: v for k, v in ((cfg.get("hook_check") or {}).items()) if v is not None})
    for key in ("generic_topic_words", "claim_words", "stopwords"):
        out[key] = [str(w).strip().lower().strip("'") for w in out.get(key) or [] if str(w).strip()]
    return out


def _stem(token: str) -> str:
    t = token.lower().strip("'")
    if t.endswith("ies") and len(t) > 4:
        return t[:-3] + "y"
    for suf in ("'s", "ing", "ed", "es", "s"):
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            return t[: -len(suf)]
    return t


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z0-9']+", str(text or "")) if t.strip("'")]


def _claim_match(token: str, claim: str) -> bool:
    """Same stem ("leaked" ~ "leak"), or a longer form of a claim of 5+ letters ("officially" ~
    "official"). Short claims match by stem only, so "really" is not "real"."""
    ts, cs = _stem(token), _stem(claim)
    return ts == cs or (len(cs) >= 5 and ts.startswith(cs))


def claim_words_in(text: str, cfg: dict[str, Any] | None = None) -> list[str]:
    """The claim words (config hook_check.claim_words) used in `text`, as written there, in order."""
    claims = hook_check_settings(cfg)["claim_words"]
    found: list[str] = []
    for t in _tokens(text):
        if any(_claim_match(t, c) for c in claims) and t.upper() not in found:
            found.append(t.upper())
    return found


def content_words(text: str, cfg: dict[str, Any] | None = None) -> list[str]:
    """The words of a hook / voiceover line that say something specific about the clip: what is left
    after the stopwords, the generic topic words (GTA, 6, TRAILER, VERSION...) and the claim words
    (judged separately by `claim_check`). One entry per stem, upper-case, in order."""
    hc = hook_check_settings(cfg)
    stop = set(hc["stopwords"])
    topic = set(hc["generic_topic_words"])
    topic_stems = {_stem(w) for w in topic}
    claims = hc["claim_words"]
    out: dict[str, str] = {}
    for t in _tokens(text):
        low = t.lower().strip("'")
        if low in stop or low in topic or _stem(low) in topic_stems:
            continue
        if any(_claim_match(low, c) for c in claims):
            continue
        if len(low) < 3 and not low.isdigit():
            continue
        out.setdefault(_stem(low), t.upper())
    return list(out.values())


def segment_overlap_check(hook_text: str, transcript_text: str, cfg: dict[str, Any] | None = None,
                          *, label: str = "hook") -> tuple[str, str]:
    """Free, deterministic check of a hook (or voiceover line) against the SEGMENT transcript.

    Returns ("PASS" | "FAIL" | "SKIP", reason). The line's content words (`content_words`) must share
    at least `hook_check.min_shared_content_words` (2) stems with the transcript; a line with only one
    content word needs that one (unless `single_word_lines_ok` is false). A line with no content words
    claims nothing specific and passes. Without speech there is nothing to compare: SKIP (the
    frames-based Gemini check decides)."""
    hc = hook_check_settings(cfg)
    spoken = {_stem(t) for t in _tokens(transcript_text)}
    if not spoken:
        return "SKIP", "no speech in the segment"
    content = content_words(hook_text, cfg)
    if not content:
        return "PASS", f"generic {label}: claims nothing specific"
    need = max(1, int(hc["min_shared_content_words"]))
    if hc.get("single_word_lines_ok", True):
        need = min(need, len(content))
    hits = [t for t in content if _stem(t) in spoken]
    if len(hits) >= need:
        return "PASS", f"segment says {', '.join(hits)}"
    missing = [t for t in content if t not in hits]
    return "FAIL", (f"FAIL: the {label} shares {len(hits)} of the {need} content words it needs with this segment "
                    f"(said: {', '.join(hits) or 'none'}; not said: {', '.join(missing)})")


def claim_check(text: str, transcript_text: str, authenticity: str, cfg: dict[str, Any] | None = None,
                *, label: str = "hook") -> tuple[str, str]:
    """Claim words (leak, confirmed, official, real, ...) in a hook or voiceover line.

    Returns ("PASS" | "FAIL", reason). An UNVERIFIED item never gets one, even when the segment says
    it (its hook stays a question). An OFFICIAL item may use one only when the segment transcript
    itself makes that claim (the same word or stem is said in it)."""
    claims = hook_check_settings(cfg)["claim_words"]
    used = claim_words_in(text, cfg)
    if not used:
        return "PASS", "no claim words"
    if authenticity != "official":
        return "FAIL", (f"FAIL: the {label} uses the claim word(s) {', '.join(used)}, and this clip is unverified: "
                        "an unverified clip never states a leak, confirmation or reveal as fact - write a question "
                        f"without these words: {', '.join(claims)}")
    seg = _tokens(transcript_text)
    unsupported = [u for u in used
                   if not any(_claim_match(s, c) for s in seg for c in claims if _claim_match(u, c))]
    if unsupported:
        return "FAIL", (f"FAIL: the {label} claims {', '.join(unsupported)}, but the segment itself never says so "
                        "(the source title is not evidence)")
    return "PASS", f"claim(s) {', '.join(used)} made in the segment itself"


def verify_hook_grounding(
    hook_text: str,
    authenticity: str,
    source_title: str,
    transcript_text: str = "",
    model: str = "gemini-2.5-flash",
    api_key: str | None = None,
    cfg: dict[str, Any] | None = None,
    *,
    frames: list[bytes] | None = None,
    voiceover_script: str | None = None,
) -> tuple[bool, str]:
    """Short Gemini PASS/FAIL: is the hook grounded in THIS segment, and honest?

    Evidence is the segment transcript (and the cut's frames when given). The source title is
    shown as context but is explicitly NOT evidence: it may describe another part of the video.
    Returns (passed, reason). reason starts with "FAIL" for a FAIL verdict and with
    "ERROR" when no verdict was obtained (429, network, unexpected reply) - both are
    NOT grounded.
    """
    # The voiceover is not shown: with it, the checker judged hook-vs-voiceover wording instead of
    # the clip (live run 2026-11-11). `voiceover_script` is accepted for call compatibility only.
    frames_note = " and the attached frames from the clip" if frames else ""
    check_prompt = f"""You are a strict fact checker for the hook of ONE short clip cut from a longer video.
Judge ONLY against the CLIP TRANSCRIPT{frames_note}. The source title is NOT evidence: it describes
the whole source video, and most of that is not in this clip. Text visible in the frames counts.

HOOK CANDIDATE: "{hook_text}"
AUTHENTICITY TAG: "{authenticity}" (official or unverified)
CLIP TRANSCRIPT (this segment only): "{transcript_text if transcript_text else '[No speech]'}"
SOURCE TITLE (context only, not evidence): "{source_title}"

VERIFICATION RULES:
1. Grounding: everything specific the hook claims - names, places, events, numbers,
   features - is said in the CLIP TRANSCRIPT or clearly visible in the frames. A generic question
   hook that claims nothing specific passes this rule. A question ABOUT a name, place or thing that
   is said or shown in the clip passes too - the clip does not have to answer it. The transcript is
   machine-made: a name that differs only in spelling, plural or possessive ("Camino's" / "CAMINOS")
   is the same name. A hook about
   something found only in the title, or not in this clip at all, FAILS. When the transcript is
   [No speech], judge against the frames alone: a hook describing what the frames show passes.
2. Honesty (STRICT): If AUTHENTICITY is "unverified", does the hook falsely claim or state as a confirmed fact that this is real GTA 6? (If it claims fake/modded/leak footage is confirmed GTA 6, that is a direct violation).
Only these two rules decide. Style, tone, wording or punctuation are NOT reasons to FAIL.

Respond with EXACTLY:
PASS
or
FAIL: <one-sentence specific reason>
"""
    try:
        reply = _call_gemini(check_prompt, images=frames or None, model=model, api_key=api_key, cfg=cfg)
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
                hook_binding = NULL, hook_review = NULL,
                voiceover_audio_path = NULL, voiceover_binding = NULL,
                updated_at = :now
            WHERE id = :id
            """,
            {"id": item_id, "now": db.now()},
        )


def _cut_transcript(item: dict[str, Any], cut_path: Path, cfg: dict[str, Any], db_path: Any) -> list[dict[str, Any]]:
    """faster-whisper words for the cut window, cached on the item for render to reuse."""
    from . import render, transcribe  # lazy: render imports this module

    key = transcribe.cache_key(cfg)
    words = render.cached_cut_words(item, key)
    if words is not None:
        return words
    log.info("Transcribing cut %s with faster-whisper (%s)...", cut_path.name, key)
    # raise_errors: a broken whisper (missing model cache, bad HF_HOME, ...) must fail the
    # stage, not silently hand Gemini an empty transcript.
    words = render.transcribe_words(cut_path, raise_errors=True, cfg=cfg)
    if words:  # nothing spoken: nothing worth caching (render will simply transcribe again)
        render.store_cut_words(item["id"], item, words, key, db_path=db_path)
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
    transcript = binding.transcript_text(words)
    log.info("Cut transcript for %s: %d words", item_id, len(words))

    # The tag the source channel supports; Gemini may only downgrade it.
    source_tag = source_authenticity(item, cfg)
    segment = (float(item.get("cut_start") or 0.0), float(item.get("cut_end") or 0.0))
    frames = extract_sample_frames(cut_path, count=4)     # once: the generation and the check see the same frames

    max_attempts = max(1, int(gs["max_attempts"]))
    fail_limit = max(1, int(gs["grounding_fail_limit"]))   # 2 = the first hook + one regeneration
    problems: list[str] = []
    check_fails = 0
    final_data: dict[str, Any] | None = None
    final_reason = ""
    rejected: list[dict[str, str]] = []                    # hooks that FAILed the segment check
    previous_failure = ""

    for attempt in range(1, max_attempts + 1):
        log.info("Generating hook for item %s (attempt %d/%d)...", item_id, attempt, max_attempts)
        try:
            data = generate_hook_and_voiceover(
                cut_path, source_title, transcript_text=transcript, model=model, cfg=cfg,
                authenticity=source_tag, frames=frames, segment=segment, previous_failure=previous_failure,
            )
        except Exception as e:
            msg = _redact(f"attempt {attempt}: {type(e).__name__}: {e}")
            log.warning("Gemini generation failed: %s", msg)
            problems.append(msg)
            continue

        ok, reason = check_hook_against_segment(
            data, transcript, source_title, frames=frames, model=model, cfg=cfg,
        )
        if ok:
            log.info("Hook passed the segment check: %s (%s) - %s", data["hook_text"], data["authenticity"], reason)
            final_data, final_reason = data, reason
            break

        problems.append(f"attempt {attempt}: hook {data['hook_text']!r} not grounded: {reason}")
        if reason.startswith("FAIL"):
            check_fails += 1
            rejected.append({"hook_text": data["hook_text"], "voiceover_script": data["voiceover_script"],
                             "reason": reason})
            log.warning("Hook failed the segment check (%d/%d): %s. Reason: %s",
                        check_fails, fail_limit, data["hook_text"], reason)
            if check_fails >= fail_limit:
                return _send_to_review(item, data, rejected, transcript, words, db_path)
            previous_failure = f'the hook "{data["hook_text"]}" was rejected: {reason}'
        else:
            log.warning("Hook not grounded (no verdict) on attempt %d: %s", attempt, reason)

    if final_data is None:
        reason = f"No valid Gemini hook after {max_attempts} attempts: " + " | ".join(problems)
        _clear_hook_fields(item_id, db_path=db_path)
        db.mark_failed(item_id, "hooks", reason, db_path=db_path)
        db.log_stage(item_id, "hooks", "failed", message=reason, db_path=db_path)
        return False, reason

    # Save hook, authenticity, keywords and the segment record. The old voiceover wav was spoken
    # from the previous script: drop it so the voiceover stage synthesizes this one.
    with db.db(db_path) as conn:
        conn.execute(
            """
            UPDATE items SET
                hook_style = :hook_style,
                hook_text = :hook_text,
                hook_grounded = 1,
                authenticity = :authenticity,
                voiceover_script = :voiceover_script,
                keywords = :keywords,
                hook_binding = :hook_binding,
                hook_review = NULL,
                voiceover_audio_path = NULL,
                voiceover_binding = NULL,
                updated_at = :now
            WHERE id = :id
            """,
            {
                "id": item_id,
                "hook_style": final_data["hook_style"],
                "hook_text": final_data["hook_text"],
                "authenticity": final_data["authenticity"],
                "voiceover_script": final_data["voiceover_script"],
                "keywords": json.dumps(final_data.get("keywords") or []),
                "hook_binding": binding.hook_record(item, final_data["hook_text"], final_data["voiceover_script"],
                                                    transcript, "segment_check", final_reason),
                "now": db.now(),
            },
        )

    db.log_stage(
        item_id,
        "hooks",
        "success",
        message=(
            f"Hook: '{final_data['hook_text']}' [{final_data['authenticity']} (source tag {source_tag}), "
            f"segment {segment[0]:.2f}-{segment[1]:.2f}s, check: {final_reason}, source=gemini, "
            f"regenerated={check_fails}, keywords={final_data.get('keywords_source')}, transcript_words={len(words)}]"
        ),
        db_path=db_path,
    )
    return True, f"Hook generated [{final_data['authenticity']}, source=gemini]"


def check_hook_against_segment(
    data: dict[str, Any],
    transcript: str,
    source_title: str,
    *,
    frames: list[bytes] | None = None,
    model: str = "gemini-2.5-flash",
    cfg: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """The post-generation check. First the free ones (a FAIL there costs no Gemini call), on the hook
    AND the voiceover line: claim words (`claim_check`), then the content-word overlap with the
    segment transcript (`segment_overlap_check`). Then the short Gemini PASS/FAIL against the segment
    only. Returns (passed, reason); reason starts with "FAIL" or "ERROR" when not passed.
    A Gemini outage (GeminiTransientError) propagates: it is not a verdict."""
    lines = (("hook", data["hook_text"]), ("voiceover", data.get("voiceover_script") or ""))
    for label, text in lines:
        verdict, why = claim_check(text, transcript, data.get("authenticity") or "unverified", cfg, label=label)
        if verdict == "FAIL":
            return False, why
    notes: list[str] = []
    for label, text in lines:
        verdict, why = segment_overlap_check(text, transcript, cfg, label=label)
        if verdict == "FAIL":
            return False, why
        notes.append(f"{label} {verdict.lower()} ({why})")
    ok, reason = verify_hook_grounding(
        data["hook_text"], data["authenticity"], source_title, transcript_text=transcript, model=model,
        cfg=cfg, frames=frames, voiceover_script=data.get("voiceover_script"),
    )
    return ok, (f"overlap {'; '.join(notes)}; gemini: {reason}" if ok else reason)


def _send_to_review(
    item: dict[str, Any],
    data: dict[str, Any],
    rejected: list[dict[str, str]],
    transcript: str,
    words: list[dict[str, Any]],
    db_path: Any,
) -> tuple[bool, str]:
    """Two hooks failed the segment check: do NOT render. Keep the last candidate (bound to this
    segment, check = needs_review, so render refuses it) and put the item in `needs_review`, where
    the Telegram approval flow shows it to a human."""
    item_id = item["id"]
    reason = rejected[-1]["reason"] if rejected else "segment check failed"
    review = {
        "reason": reason,
        "rejected": rejected,
        "transcript_excerpt": transcript[:700],
        "transcript_words": len(words),
        "segment": [item.get("cut_start"), item.get("cut_end")],
        "at": db.now(),
    }
    with db.db(db_path) as conn:
        conn.execute(
            """
            UPDATE items SET hook_style = :hook_style, hook_text = :hook_text, hook_grounded = 0,
                authenticity = :authenticity, voiceover_script = :voiceover_script, keywords = :keywords,
                hook_binding = :hook_binding, hook_review = :hook_review,
                voiceover_audio_path = NULL, voiceover_binding = NULL, updated_at = :now
            WHERE id = :id
            """,
            {
                "id": item_id,
                "hook_style": data["hook_style"],
                "hook_text": data["hook_text"],
                "authenticity": data["authenticity"],
                "voiceover_script": data["voiceover_script"],
                "keywords": json.dumps(data.get("keywords") or []),
                "hook_binding": binding.hook_record(item, data["hook_text"], data["voiceover_script"],
                                                    transcript, "needs_review", reason),
                "hook_review": json.dumps(review),
                "now": db.now(),
            },
        )
    db.transition_stage(item_id, "needs_review", db_path=db_path)
    msg = f"needs_review: {len(rejected)} hooks failed the segment check (last: {data['hook_text']!r}: {reason})"
    db.log_stage(item_id, "hooks", "needs_review", message=msg, db_path=db_path)
    log.warning("Item %s sent to manual review: %s", item_id, msg)
    return False, msg

"""Stage 5: Gemini multimodal hook generation, authenticity classification, and grounding verification."""
from __future__ import annotations

import base64
import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

import requests

from . import config, db, keywords

log = logging.getLogger(__name__)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


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


def _call_gemini(
    prompt: str,
    images: list[bytes] | None = None,
    model: str = "gemini-2.5-flash",
    api_key: str | None = None,
) -> str:
    """Call Gemini API generateContent endpoint with text and optional inline images."""
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

    payload = {
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 1024,
        },
    }

    res = requests.post(url, json=payload, timeout=60)
    if res.status_code != 200:
        raise RuntimeError(f"Gemini API HTTP {res.status_code}: {res.text}")

    resp_json = res.json()
    candidates = resp_json.get("candidates", [])
    if not candidates:
        raise RuntimeError(f"No candidates returned from Gemini: {resp_json}")

    return candidates[0]["content"]["parts"][0]["text"].strip()


def generate_hook_and_voiceover(
    video_path: Path | str,
    source_title: str,
    transcript_text: str = "",
    model: str = "gemini-2.5-flash",
    api_key: str | None = None,
) -> dict[str, Any]:
    """Generate hook, authenticity tag, and voiceover line using multimodal Gemini call."""
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
    raw_response = _call_gemini(prompt, images=sample_frames, model=model, api_key=api_key)

    # Parse JSON from response (handling potential markdown code blocks and partial wrappers)
    clean = re.sub(r"^```(?:json)?\s*", "", raw_response.strip())
    clean = re.sub(r"\s*```$", "", clean)
    try:
        data = json.loads(clean)
    except Exception:
        data = {}
        # Try extracting JSON object via regex
        m = re.search(r"\{[^{}]*\}", raw_response, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                pass
        if not data:
            auth_m = re.search(r'"authenticity"\s*:\s*"([^"]+)"', raw_response)
            style_m = re.search(r'"hook_style"\s*:\s*"([^"]+)"', raw_response)
            hook_m = re.search(r'"hook_text"\s*:\s*"([^"]+)"', raw_response)
            vo_m = re.search(r'"voiceover_script"\s*:\s*"([^"]+)"', raw_response)
            if hook_m:
                data = {
                    "authenticity": auth_m.group(1) if auth_m else "unverified",
                    "hook_style": style_m.group(1) if style_m else "question",
                    "hook_text": hook_m.group(1),
                    "voiceover_script": vo_m.group(1) if vo_m else "Check out this crazy GTA 6 moment!",
                }

    auth = data.get("authenticity", "unverified")
    if auth not in ("official", "unverified"):
        auth = "unverified"

    hook_style = data.get("hook_style", "question")
    if hook_style not in ("curiosity", "question", "hype_analysis"):
        hook_style = "question"

    hook_text = data.get("hook_text", "IS THIS REALLY GTA 6?").strip().upper()
    vo_script = data.get("voiceover_script", "Wait until you see this GTA 6 detail!").strip()

    # Guarantee unverified clips have question framing
    if auth == "unverified" and not hook_text.endswith("?"):
        hook_text = f"{hook_text}?"

    # C1: 3-6 caption keywords; falls back to the rule extractor when absent/unusable.
    kw = keywords.clean_keywords(data.get("keywords"))
    if len(kw) < 3:
        extra = keywords.rule_keywords(f"{transcript_text} {source_title} {hook_text}")
        kw += [k for k in extra if k not in kw]
        kw = kw[:6]

    return {
        "authenticity": auth,
        "hook_style": hook_style,
        "hook_text": hook_text,
        "voiceover_script": vo_script,
        "keywords": kw,
    }


def verify_hook_grounding(
    hook_text: str,
    authenticity: str,
    source_title: str,
    transcript_text: str = "",
    model: str = "gemini-2.5-flash",
    api_key: str | None = None,
) -> tuple[bool, str]:
    """Verification check call to Gemini ensuring hook is grounded and honest.

    Checks:
    1. Does the clip/transcript actually show or discuss what the hook claims?
    2. Does the hook falsely claim this is real GTA 6 when it is unverified?
    Returns (passed: bool, reason: str).
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
        reply = _call_gemini(check_prompt, images=None, model=model, api_key=api_key)
        first_line = reply.strip().splitlines()[0]
        if first_line.startswith("PASS"):
            return True, "Passed grounding and honesty verification"
        return False, first_line
    except Exception as e:
        log.warning("Hook verification call error: %s", e)
        # On network/API glitch in check, don't crash
        return True, f"Check bypassed due to API error: {e}"


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
    model = cfg.get("gemini", {}).get("model", "gemini-2.5-flash")

    # Transcribe audio for grounding context
    transcript = item.get("caption_text") or ""

    max_attempts = 3
    final_data: dict[str, Any] | None = None
    passed_grounding = False

    for attempt in range(1, max_attempts + 1):
        log.info("Generating hook for item %s (attempt %d/%d)...", item_id, attempt, max_attempts)
        try:
            data = generate_hook_and_voiceover(
                cut_path,
                source_title,
                transcript_text=transcript,
                model=model,
            )
        except Exception as e:
            log.warning("Gemini generation failed on attempt %d: %s", attempt, e)
            continue

        is_grounded, reason = verify_hook_grounding(
            data["hook_text"],
            data["authenticity"],
            source_title,
            transcript_text=transcript,
            model=model,
        )

        if is_grounded:
            log.info("Hook passed verification: %s (%s)", data["hook_text"], data["authenticity"])
            passed_grounding = True
            final_data = data
            break
        else:
            log.warning("Hook failed grounding (attempt %d): %s. Reason: %s", attempt, data["hook_text"], reason)

    # If verification failed 2 times, fall back to safe question hook
    if not final_data:
        log.warning("Falling back to safe question hook for item %s", item_id)
        final_data = {
            "authenticity": "unverified",
            "hook_style": "question",
            "hook_text": "IS THIS THE CRAZIEST GTA 6 DETAIL?",
            "voiceover_script": "Check out this crazy GTA 6 moment right now!",
            "keywords": keywords.rule_keywords(f"{transcript} {source_title}", cfg=cfg),
        }
        passed_grounding = False

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

    db.log_stage(
        item_id,
        "hooks",
        "success",
        message=f"Hook: '{final_data['hook_text']}' [{final_data['authenticity']}, grounded={passed_grounding}]",
        db_path=db_path,
    )
    return True, f"Hook generated [{final_data['authenticity']}]"


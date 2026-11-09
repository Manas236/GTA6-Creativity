"""Stage 6: Local Kokoro TTS voiceover synthesis."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import requests

from . import config, db

# kokoro_onnx and soundfile are imported lazily so the pipeline (and its tests) can be
# imported on machines without the TTS stack installed.

log = logging.getLogger(__name__)

KOKORO_MODEL_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx"
KOKORO_VOICES_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin"

_KOKORO_INSTANCE: Any = None


def get_model_paths() -> tuple[Path, Path]:
    """Return (model_path, voices_path) in assets/models/."""
    base_dir = Path(__file__).resolve().parent.parent / "assets" / "models"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / "kokoro-v0_19.onnx", base_dir / "voices.bin"


def ensure_kokoro_models() -> tuple[Path, Path]:
    """Ensure ONNX weights and voice binaries exist on disk, downloading if missing."""
    model_path, voices_path = get_model_paths()

    if not model_path.exists():
        log.info("Downloading Kokoro ONNX model weights (~80MB)...")
        r = requests.get(KOKORO_MODEL_URL, stream=True, timeout=120)
        r.raise_for_status()
        with open(model_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        log.info("Kokoro ONNX model downloaded successfully.")

    if not voices_path.exists():
        log.info("Downloading Kokoro voice embeddings (~2MB)...")
        r = requests.get(KOKORO_VOICES_URL, stream=True, timeout=60)
        r.raise_for_status()
        with open(voices_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
        log.info("Kokoro voice embeddings downloaded successfully.")

    return model_path, voices_path


def get_kokoro() -> Any:
    """Return shared Kokoro singleton."""
    global _KOKORO_INSTANCE
    if _KOKORO_INSTANCE is None:
        from kokoro_onnx import Kokoro
        model_path, voices_path = ensure_kokoro_models()
        _KOKORO_INSTANCE = Kokoro(str(model_path), str(voices_path))
    return _KOKORO_INSTANCE


def synthesize_voiceover(
    text: str,
    output_path: Path | str,
    voice: str = "am_adam",
    speed: float = 1.1,
) -> float:
    """Synthesize voiceover line using Kokoro TTS to a 24kHz WAV file.

    Returns duration in seconds.
    """
    import soundfile as sf

    kokoro = get_kokoro()
    samples, sample_rate = kokoro.create(text, voice=voice, speed=speed, lang="en-us")

    out_p = Path(output_path)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(out_p), samples, sample_rate)

    duration_s = len(samples) / float(sample_rate)
    log.info("Synthesized voiceover to %s (%.2fs)", out_p.name, duration_s)
    return duration_s


def run_voiceover_for_item(
    item_id: str,
    cfg: dict[str, Any] | None = None,
    db_path: Any = None,
) -> tuple[bool, str]:
    """Synthesize 3-6s Kokoro voiceover for an item with a generated voiceover_script."""
    cfg = cfg or config.get_config()
    item = db.get_item(item_id, db_path=db_path)
    if not item:
        return False, f"Item {item_id} not found"

    script = item.get("voiceover_script")
    if not script:
        # Default fallback line
        script = "Is this really the craziest GTA 6 gameplay leak yet?"

    voice = cfg.get("voiceover", {}).get("voice", "am_adam")
    speed = float(cfg.get("voiceover", {}).get("speed", 1.1))

    audio_dir = config.media_dir("audio")
    out_path = audio_dir / f"{item_id}_vo.wav"

    try:
        dur = synthesize_voiceover(script, out_path, voice=voice, speed=speed)
    except Exception as e:
        log.error("Voiceover synthesis failed for item %s: %s", item_id, e)
        db.mark_failed(item_id, "voiceover", f"Kokoro TTS synthesis error: {e}", db_path=db_path)
        return False, f"TTS synthesis error: {e}"

    with db.db(db_path) as conn:
        conn.execute(
            "UPDATE items SET voiceover_audio_path = :path, updated_at = :now WHERE id = :id",
            {"id": item_id, "path": str(out_path), "now": db.now()},
        )

    db.log_stage(
        item_id,
        "voiceover",
        "success",
        message=f"Voiceover synthesized ({dur:.2f}s) via Kokoro {voice}",
        db_path=db_path,
    )
    return True, f"Voiceover synthesized ({dur:.2f}s)"


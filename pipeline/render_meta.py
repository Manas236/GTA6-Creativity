"""Read and atomically replace metadata alongside a verified video render."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def _sidecar(path: Path | str) -> Path:
    path = Path(path)
    return path if path.name.endswith(".meta.json") else path.with_suffix(".meta.json")


def write(path: Path | str, meta: dict[str, Any]) -> None:
    target = _sidecar(path)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                     suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(meta, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.close()
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)


def read(path: Path | str) -> dict[str, Any]:
    try:
        return json.loads(_sidecar(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def summary(path: Path | str) -> str:
    meta = read(path)
    if not meta:
        return f"{Path(path).name}: sidecar missing"
    cues = ", ".join(f"{cue['t']:g}s {cue['category']}/{cue['file']} ({cue['trigger']})"
                     for cue in meta["sfx_cues"]) or "none"
    return (f"{Path(path).name}: hook {meta['hook_font_px']} px | cues {cues} | "
            f"music {meta['music_track'] or 'none'} | {len(meta['caption_words'])} caption words")

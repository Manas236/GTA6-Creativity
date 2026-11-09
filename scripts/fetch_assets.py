"""Fetch the style-layer assets that could not be fetched in the cloud sandbox.

What is already in the repository (nothing to download):
  * assets/emoji/*.png   Twemoji 72x72, CC-BY 4.0 (jdecked/twemoji) - C2
  * assets/sfx/*.wav     boom / whoosh, synthesized here, CC0 - A1 (see LICENSES.md)

What this script fetches:
  * assets/music/bed.mp3  a CC0 music bed from FreePD - A2
                          (freepd.com is blocked from the cloud sandbox: the proxy
                          answers 403 to CONNECT, so A2 was built and tested against a
                          generated tone and no synthetic "music" track was committed)
  * optionally, the Kenney CC0 impact pack to replace the synthesized SFX
    (--prefer-download)

Every download is verified before it is installed (HTTP 200, real audio, long enough)
and the source plus licence is written to a LICENSES.md next to the file. The script is
idempotent: a file that already passes verification is left alone unless --force.

Usage:
    python scripts/fetch_assets.py                  # fetch what is missing
    python scripts/fetch_assets.py --force          # re-fetch even if present
    python scripts/fetch_assets.py --prefer-download  # also try the Kenney CC0 SFX pack
    python scripts/fetch_assets.py --check          # verify only, download nothing
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import audio_mix, config, overlay, style  # noqa: E402

USER_AGENT = "GTA6-Clipping-Pipeline/1.0 (asset fetcher)"

# FreePD publishes everything under CC0 1.0 (public domain). These filenames were taken
# from published projects that credit FreePD; they are tried in order and verified.
MUSIC_CANDIDATES: list[str] = [
    "https://freepd.com/music/Arpent.mp3",
    "https://freepd.com/music/Evil%20Incoming.mp3",
    "https://freepd.com/music/Lucky%20Break.mp3",
    "https://freepd.com/music/Think%20About%20It.mp3",
    "https://freepd.com/music/Floating%20Cities.mp3",
]
MUSIC_LICENSE = "CC0 1.0 (public domain) - https://freepd.com (all FreePD music is CC0)"
MUSIC_BROWSE = "https://freepd.com/epic.php or https://freepd.com/electronic.php"
MUSIC_MIN_SECONDS = 25.0

KENNEY_SFX_URL = "https://kenney.nl/media/pages/assets/impact-sounds/6e1a3e6a52-1677669547/kenney_impact-sounds.zip"
KENNEY_LICENSE = "CC0 1.0 (public domain) - https://kenney.nl/assets/impact-sounds"


def log(msg: str) -> None:
    print(msg, flush=True)


def _get(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if r.status != 200:
            raise RuntimeError(f"HTTP {r.status}")
        return r.read()


def audio_seconds(path: Path) -> float:
    """Duration of an audio file, or 0.0 if ffprobe cannot read it."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        ).stdout
        streams = json.loads(out or "{}").get("streams") or []
        return float(streams[0].get("duration") or 0.0) if streams else 0.0
    except Exception:
        return 0.0


def write_licenses(path: Path, rows: list[tuple[str, str, str]]) -> None:
    """rows = [(file, source, license)]"""
    lines = [f"# Asset sources and licenses ({path.parent.name})", ""]
    lines += ["| File | Source | License |", "|---|---|---|"]
    lines += [f"| `{f}` | {s} | {l} |" for f, s, l in rows]
    lines += ["", "Written by `python scripts/fetch_assets.py`.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


# --------------------------------------------------------------------------------------
# A2: music bed
# --------------------------------------------------------------------------------------

def verify_music(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return False, "missing"
    if path.stat().st_size < 100_000:
        return False, f"too small ({path.stat().st_size} bytes)"
    dur = audio_seconds(path)
    if dur < MUSIC_MIN_SECONDS:
        return False, f"only {dur:.1f}s of audio (need >= {MUSIC_MIN_SECONDS:.0f}s)"
    return True, f"{dur:.1f}s"


def fetch_music(dest: Path, force: bool = False, check: bool = False) -> bool:
    ok, why = verify_music(dest)
    if ok and not force:
        log(f"  music bed OK, left alone: {dest} ({why})")
        return True
    if check:
        log(f"  music bed NOT usable: {dest} ({why})")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    for url in MUSIC_CANDIDATES:
        log(f"  trying {url}")
        try:
            data = _get(url)
        except (urllib.error.URLError, RuntimeError, TimeoutError, OSError) as e:
            log(f"    failed: {e}")
            continue
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(data)
        ok, why = verify_music(tmp)
        if not ok:
            log(f"    rejected: {why}")
            tmp.unlink(missing_ok=True)
            continue
        tmp.replace(dest)
        write_licenses(dest.parent / "LICENSES.md", [(dest.name, url, MUSIC_LICENSE)])
        log(f"    installed {dest} ({why}) - {MUSIC_LICENSE}")
        return True
    log(
        "  Could not fetch a CC0 bed automatically.\n"
        f"  MANUAL STEP: open {MUSIC_BROWSE}, download any track (all FreePD music is CC0),\n"
        f"  save it as {dest}, then re-run this script to verify it.\n"
        "  Until then the renders simply skip A2 (the music bed); nothing else breaks."
    )
    return False


# --------------------------------------------------------------------------------------
# A1: SFX
# --------------------------------------------------------------------------------------

def fetch_sfx(ss: dict, force: bool = False, check: bool = False, prefer_download: bool = False) -> bool:
    files = audio_mix.sfx_files(ss)
    missing = [n for n, p in files.items() if not p.exists() or p.stat().st_size < 1000]
    if prefer_download and not check:
        if install_kenney_sfx(files):
            return True
        log("  keeping the committed synthesized SFX (CC0, see assets/sfx/LICENSES.md)")
    if check:
        for n, p in files.items():
            log(f"  sfx {n}: {'OK' if p.exists() else 'MISSING'} ({p})")
        return not missing
    if missing or force:
        log(f"  synthesizing SFX: {missing or list(files)}")
        audio_mix.ensure_sfx_assets(ss, regenerate=force)
    for n, p in audio_mix.sfx_files(ss).items():
        log(f"  sfx {n}: {'OK' if p.exists() else 'FAILED'} ({p})")
    return all(p.exists() for p in audio_mix.sfx_files(ss).values())


def install_kenney_sfx(files: dict[str, Path]) -> bool:
    """Optional: replace the synthesized cues with Kenney's CC0 originals.

    Only installs if the archive downloads, contains a licence file, and holds a usable
    impact and whoosh/swoosh sample.
    """
    log(f"  trying the Kenney CC0 pack: {KENNEY_SFX_URL}")
    try:
        data = _get(KENNEY_SFX_URL, timeout=120)
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        log(f"    failed: {e}")
        return False
    names = zf.namelist()
    if not any("license" in n.lower() for n in names):
        log("    rejected: no license file in the archive")
        return False

    def pick(*words: str) -> str | None:
        for n in names:
            low = n.lower()
            if low.endswith((".wav", ".ogg")) and any(w in low for w in words):
                return n
        return None

    boom_src, whoosh_src = pick("impact", "thud", "boom"), pick("swoosh", "whoosh", "swish")
    if not (boom_src and whoosh_src):
        log("    rejected: no impact/whoosh samples found")
        return False
    out_dir = files["boom"].parent
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for kind, src in (("boom", boom_src), ("whoosh", whoosh_src)):
        raw = out_dir / f"kenney_{kind}{Path(src).suffix}"
        raw.write_bytes(zf.read(src))
        dest = files[kind]
        gain = audio_mix.PEAK_TARGET_DB - audio_mix.file_peak_db(raw, audio_mix.PEAK_TARGET_DB)
        res = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(raw),
             "-af", f"volume={gain:.3f}dB", "-ac", "1", "-ar", "48000",
             "-c:a", "pcm_s16le", str(dest)],
            capture_output=True,
        )
        raw.unlink(missing_ok=True)
        if res.returncode != 0:
            log(f"    rejected: could not convert {src}")
            return False
        rows.append((dest.name, f"{KENNEY_SFX_URL} ({src})", KENNEY_LICENSE))
        log(f"    installed {dest} from {src}")
    write_licenses(out_dir / "LICENSES.md", rows)
    return True


# --------------------------------------------------------------------------------------
# C2: emoji (already committed; verify only)
# --------------------------------------------------------------------------------------

def verify_emoji(ss: dict) -> bool:
    codes = sorted(set(overlay.emoji_map(ss).values()))
    missing = [c for c in codes if not overlay.emoji_path(c, ss).exists()]
    for c in codes:
        if c in missing:
            log(f"  emoji {c}.png MISSING")
    log(f"  emoji: {len(codes) - len(missing)}/{len(codes)} present (CC-BY 4.0, jdecked/twemoji)")
    if missing:
        log("  MANUAL STEP: re-run `git pull`, or download the missing codepoints from")
        log("  https://github.com/jdecked/twemoji/tree/master/assets/72x72 into assets/emoji/")
    return not missing


# --------------------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch/verify the style-layer assets. Idempotent.")
    ap.add_argument("--force", action="store_true", help="re-fetch even when a file already verifies")
    ap.add_argument("--check", action="store_true", help="verify only; download nothing")
    ap.add_argument("--prefer-download", action="store_true",
                    help="try the Kenney CC0 SFX pack instead of the committed synthesized cues")
    args = ap.parse_args()

    cfg = config.get_config()
    ss = style.style_settings(cfg)
    if shutil.which("ffprobe") is None:
        log("ffprobe is not on PATH; install ffmpeg first.")
        return 1

    log("A1 SFX (assets/sfx)")
    ok_sfx = fetch_sfx(ss, force=args.force, check=args.check, prefer_download=args.prefer_download)
    log("A2 music bed (assets/music)")
    ok_music = fetch_music(audio_mix.music_path(ss), force=args.force, check=args.check)
    log("C2 emoji (assets/emoji)")
    ok_emoji = verify_emoji(ss)

    log("")
    log(f"SFX   : {'OK' if ok_sfx else 'MISSING'}")
    log(f"music : {'OK' if ok_music else 'MISSING (A2 will be skipped)'}")
    log(f"emoji : {'OK' if ok_emoji else 'MISSING (C2 will be skipped)'}")
    # A missing music bed is not fatal: the renders just skip A2.
    return 0 if (ok_sfx and ok_emoji) else 1


if __name__ == "__main__":
    raise SystemExit(main())

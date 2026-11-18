"""Install and verify the manifest audio libraries and the existing fallback assets.

The manifests specify source, trim, licence and SHA-256. WAVs are installed locally
at 48 kHz with -3 dBFS peaks. See each library's NEEDED.md for optional MP3 downloads.

Usage:
    python scripts/fetch_assets.py                    # install missing library assets
    python scripts/fetch_assets.py --check            # verify only
    python scripts/fetch_assets.py --force            # re-fetch and verify
    python scripts/fetch_assets.py --prefer-download  # also try the legacy Kenney fallback pack
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import tempfile
import wave
import zipfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline import audio_mix, config, overlay, style  # noqa: E402

USER_AGENT = "GTA6-Clipping-Pipeline/1.0 (asset fetcher)"

# FreePD (the original source) closed permanently in 2025. OpenGameArt shows each work's
# licence on its page; a candidate is used only if that page says CC0 and links the file.
# (page_url, file_url), tried in order.
MUSIC_CANDIDATES: list[tuple[str, str]] = [
    ("https://opengameart.org/content/trace-agent",
     "https://opengameart.org/sites/default/files/trace_agent_0.mp3"),
    ("https://opengameart.org/content/main-menu-music-loop",
     "https://opengameart.org/sites/default/files/main_menu_music_mastered_0.mp3"),
]
MUSIC_LICENSE = "CC0 1.0 (public domain), as stated on the OpenGameArt page"
MUSIC_BROWSE = "https://opengameart.org (Browse > Music, filter License: CC0)"
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


def page_says_cc0(page_html: str, file_url: str) -> tuple[bool, str]:
    """True only if the page's licence field lists exactly CC0 and the page links the file."""
    m = re.search(r"field-name-field-art-licenses.*?License\(s\):(.*?)Collections:", page_html, re.S)
    if not m:
        return False, "no licence field on the page"
    text = re.sub(r"<[^>]+>", " ", m.group(1)).replace("&nbsp;", " ")
    labels = text.split()
    if labels != ["CC0"]:
        return False, f"page licence is {' '.join(labels) or 'empty'}, not CC0 only"
    if file_url not in page_html:
        return False, "the page does not link that file"
    return True, "CC0"


def fetch_music(dest: Path, force: bool = False, check: bool = False) -> bool:
    ok, why = verify_music(dest)
    if ok and not force:
        log(f"  music bed OK, left alone: {dest} ({why})")
        return True
    if check:
        log(f"  music bed NOT usable: {dest} ({why})")
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    for page_url, url in MUSIC_CANDIDATES:
        log(f"  trying {url}")
        try:
            page = _get(page_url).decode("utf-8", "replace")
        except (urllib.error.URLError, RuntimeError, TimeoutError, OSError) as e:
            log(f"    failed to read its page {page_url}: {e}")
            continue
        lic_ok, lic_why = page_says_cc0(page, url)
        if not lic_ok:
            log(f"    rejected: {lic_why}")
            continue
        try:
            data = _get(url, timeout=120)
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
        licences_name = "LEGACY-LICENSES.md" if (dest.parent / "manifest.yaml").is_file() else "LICENSES.md"
        write_licenses(dest.parent / licences_name,
                       [(dest.name, f"{url} (page: {page_url})", MUSIC_LICENSE)])
        log(f"    installed {dest} ({why}) - {MUSIC_LICENSE}")
        return True
    log(
        "  Could not fetch a CC0 bed automatically.\n"
        f"  MANUAL STEP: open {MUSIC_BROWSE}, download a track whose page says CC0,\n"
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
    write_licenses(out_dir / "LEGACY-LICENSES.md", rows)
    return True


# --------------------------------------------------------------------------------------
# C2: emoji (already committed; verify only)
# --------------------------------------------------------------------------------------

def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _verify_wav(path: Path, entry: dict, music: bool) -> tuple[float, float]:
    with wave.open(str(path), "rb") as handle:
        duration = handle.getnframes() / handle.getframerate()
        if (handle.getframerate(), handle.getsampwidth(), handle.getnchannels()) != (48000, 2, 2 if music else 1):
            raise ValueError("expected 48 kHz PCM16 WAV with the correct channel count")
    start, end = entry["trim"]
    cap = 2.0 if entry["category"] == "riser" else 1.2
    if music and duration < 60:
        raise ValueError(f"music is {duration:.3f}s, below 60s")
    if not music and not 0 < duration <= cap + 1 / 48000:
        raise ValueError(f"cue is {duration:.3f}s, outside its {cap:g}s cap")
    if duration > end - start + 1 / 48000:
        raise ValueError("WAV is longer than the trim window")
    peak = audio_mix.file_peak_db(path, default=float("-inf"))
    if not -3.15 <= peak <= -2.85:
        raise ValueError(f"peak is {peak:.1f} dBFS, expected -3 dBFS")
    return duration, peak


def _convert_wav(raw: Path, output: Path, entry: dict, music: bool) -> None:
    start, end = entry["trim"]
    length = end - start
    if not music:
        length = min(length, 2.0 if entry["category"] == "riser" else 1.2)
    converted = output.with_name("trimmed.wav")
    common = ["ffmpeg", "-v", "error", "-y"]
    subprocess.run([
        *common, "-ss", str(start), "-i", str(raw), "-t", str(length),
        "-map", "0:a:0", "-ar", "48000", "-ac", "2" if music else "1",
        "-c:a", "pcm_s16le", "-fflags", "+bitexact", str(converted),
    ], check=True, capture_output=True)
    peak = audio_mix.file_peak_db(converted, default=float("-inf"))
    if not math.isfinite(peak) or peak <= -90:
        raise ValueError("no audible signal in the trim window")
    subprocess.run([
        *common, "-i", str(converted), "-af", f"volume={-3.0 - peak:.3f}dB",
        "-c:a", "pcm_s16le", "-fflags", "+bitexact", str(output),
    ], check=True, capture_output=True)


def fetch_manifest(directory: Path, *, check: bool = False, force: bool = False) -> int:
    """Install or verify normalized library WAVs and return the count available."""
    manifest = directory / "manifest.yaml"
    entries = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    music = directory.name == "music"
    downloaded: dict[str, bytes | Exception] = {}
    installed = 0
    for entry in entries:
        category, filename = entry["category"], entry["file"]
        dest = directory / filename if music else directory / category / filename
        expected = entry["sha256"]
        try:
            if dest.is_file() and not force:
                try:
                    if expected and _digest(dest) != expected:
                        raise ValueError("SHA-256 mismatch")
                    duration, peak = _verify_wav(dest, entry, music)
                except (ValueError, EOFError, OSError, wave.Error):
                    if check:
                        raise
                else:
                    log(f"  OK {filename}: {duration:.3f}s, {peak:.1f} dBFS")
                    installed += 1
                    continue
            if check:
                raise ValueError("missing WAV")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=dest.parent) as workspace:
                work = Path(workspace)
                url = entry["source_url"]
                if entry.get("manual"):
                    raw = directory / ".incoming" / f"{Path(filename).stem}.mp3"
                    if not raw.is_file():
                        raise ValueError(f"download {url} to {raw}")
                else:
                    if url not in downloaded:
                        try:
                            downloaded[url] = _get(url, timeout=15)
                        except (OSError, RuntimeError) as exc:
                            downloaded[url] = exc
                    content = downloaded[url]
                    if isinstance(content, Exception):
                        raise content
                    member = entry.get("archive_path")
                    raw = work / "source"
                    if member:
                        with zipfile.ZipFile(io.BytesIO(content)) as archive:
                            content = archive.read(member)
                    raw.write_bytes(content)
                output = work / "normalized.wav"
                _convert_wav(raw, output, entry, music)
                duration, peak = _verify_wav(output, entry, music)
                digest = _digest(output)
                if expected and digest != expected:
                    raise ValueError(f"SHA-256 mismatch: expected {expected}, got {digest}")
                output.replace(dest)
            log(f"  Installed {filename}: {duration:.3f}s, {peak:.1f} dBFS, SHA-256 {digest}")
            installed += 1
        except (EOFError, ValueError, OSError, KeyError, RuntimeError, wave.Error,
                subprocess.CalledProcessError, zipfile.BadZipFile) as exc:
            log(f"  Unavailable {filename}: {exc}")
    return installed


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
    fetch_manifest(ROOT / "assets" / "sfx", check=args.check, force=args.force)
    ok_sfx = fetch_sfx(ss, force=args.force, check=args.check, prefer_download=args.prefer_download)
    log("A2 music bed (assets/music)")
    installed_music = fetch_manifest(ROOT / "assets" / "music", check=args.check, force=args.force)
    ok_music = bool(installed_music) or fetch_music(audio_mix.music_path(ss), force=args.force, check=args.check)
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

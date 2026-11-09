# Project Handoff: GTA 6 Clipping Pipeline

## 0. Laptop: what to run next

```bash
git pull
pip install -r requirements.txt
python scripts/fetch_assets.py        # CC0 music bed for A2 + asset/license verification
python scripts/render_previews.py     # P0-P3 x A/B for the 3 CP2 items, plus 2x2 grids
```

Then look at `media/previews/<item_id>/grid_A.mp4` and `grid_B.mp4` (2x2, tiles labelled
P0-P3), pick a preset, and set `style.preset` in `config.yaml`. **The pipeline default is
still `P0`** and nothing changes until that choice is made.
`media/previews/README.txt` lists every file, its hook, its keywords and its render time.

`scripts/render_previews.py` is one command with no prompts: it re-downloads and re-cuts
`item_a35aead652d3`, `item_763c2e191a1f` and `item_4622278cd0fc`, reuses their stored
hooks and voiceover scripts, generates the C1 keywords with Gemini when `GEMINI_API_KEY`
is set (and the rule fallback otherwise), and renders into
`media/previews/<id>/P{n}_{A,B}.mp4`.

---

## 1. Project State
- **Checkpoints 0 - 2**: Completed and committed.
  - CP0: Architecture plan, database schema, configuration, environment setup.
  - CP1: Discovery (YouTube/official channels), pull-based download, perceptual deduplication (pHash/Hamming), crash recovery.
  - CP2: Cut detection (loudness + scene changes), Gemini grounded hooks, Kokoro TTS voiceover, ASS subtitle generation, vertical 9:16 rendering (Variants A & B).
- **Style layer**: Completed and committed. Edit effects as independently toggleable
  flags grouped into presets **P0-P3**, all numbers under `style:` in `config.yaml`,
  verified on a synthetic clip (`samples/synthetic/presets_sheet.png`). See **PLAN.md
  §7B** and *Calls I made* #33-59. **The default preset stays P0.**
- **Checkpoint 3**: Telegram review bot + Meta (Instagram & Facebook Reels) posting has **not** been started (another session is building it on a branch).
- **Safety Defaults**: `dry_run: true` remains active by default in `config.yaml`.

---

## 2. What Is NOT in the Repository (and Why)
- **`.env`**: Contains sensitive API keys and secrets (Gemini API key, Telegram bot token, Meta access tokens). Excluded for security. (See `.env.example` for required keys).
- **`assets/models/`**: Large binary weights (`kokoro-v0_19.onnx` ~325 MB, `voices.bin` ~5.7 MB) and Whisper caches. Excluded because GitHub rejects files > 100 MB and to keep repository size lean.
- **`media/`**: Temporary downloads, cuts, audio files, and output video renders. Excluded to keep repository lightweight.
- **`pipeline.db`** (and SQLite `-wal` / `-shm`): Local runtime SQLite database. Runtime state and local execution tables are created fresh per deployment.

### Fresh Environment Setup
1. **Python Environment**:
   - Requires Python 3.11+.
   - Create and activate virtual environment:
     ```bash
     python -m venv .venv
     source .venv/bin/activate  # On Windows: .venv\Scripts\Activate.ps1
     ```
2. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **External Binaries**:
   - Ensure `ffmpeg` and `ffprobe` are installed and accessible on your `PATH`, and that ffmpeg is built with libass (`ffmpeg -filters` lists `ass`).
4. **Configuration & Secrets**:
   - Copy `.env.example` to `.env`:
     ```bash
     cp .env.example .env
     ```
   - Provide your `GEMINI_API_KEY` for hook generation.
5. **Models**:
   - Kokoro TTS models (`kokoro-v0_19.onnx` and `voices.bin`) download automatically to `assets/models/` when running the voiceover stage (`python -m pipeline.runner voiceover` or `pipeline.voiceover.ensure_kokoro_models()`), or can be manually downloaded from:
     - `https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx`
     - `https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin`
   - Faster-Whisper (`base.en`) weights are cached automatically on the first transcription run.

---

## 3. Sample Pack Note
The remote repository is **public** (`https://github.com/Manas236/GTA6-Creativity`), so the sample pack (`samples/cp2/` and `scripts/render_samples.py`) was omitted to avoid hosting raw media clips on a public repository. If private sample packs are later added, run `python scripts/render_samples.py` to re-render using local FFmpeg.

---

## 4. CP2 Render Issues: FIXED (render-fix pass, see PLAN.md §7 and Calls I Made #21-32)
> **Laptop: run `git pull` on `main` before continuing** (new font file, config keys, render code and tests).

1. **Variant B audio died after the VO** (3.1 s audio in a 20 s video; sidechaincompress ended with the VO): **FIXED.** Ducking removed. B is now freeze-frame: first frame held for VO + 0.3 s with VO only, then the cut with original audio at full level. B = freeze + cut (≈ 23 s).
2. **Variant A audio stopped dead at 20.0 s**: **FIXED.** Cut END snapped to a ≥ 0.25 s whisper word gap or ebur128 trough within 18-22 s (18 s → cut length at render time), plus a 0.25 s fade-out.
3. **Text too small and thin**: **FIXED.** Static Montserrat Black (OFL) in `assets/fonts/`; captions 62 px caps (Fontsize 138, outline 7), hook 68 px caps (Fontsize 150, outline 8, max 2 lines in 900 px). A test confirms libass renders the Black face.
4. **Text in Instagram's covered zones**: **FIXED.** Safe area x 90-990, y 250-1440; captions centred at y 1300, hook in the band y 280-560.
5. **59.94 fps / 96 kHz output**: **FIXED.** 30 fps and 48 kHz stereo AAC.
6. **Guard**: every render is ffprobe-verified and FAILS if audio/video stream durations differ by > 0.1 s or fps / sample rate / channels / resolution differ from config.
7. *Content selection was good; kept.*

**Verification status:** no sample pack is in the public repo, so the fixes were verified on a synthetic 20 s 16:9 clip (`samples/synthetic/contact_sheet.png`, `render_report.json`). **Real-clip verification must still run on the laptop:** `python scripts/render_check.py` (reads `samples/cp2/<id>/cut.mp4, vo.wav, item.json`, writes `A_v2.mp4`, `B_v2.mp4`, `contact_sheet.png`). If the laptop's `item.json` uses different key names than the ones listed at the top of that script, adjust `load_item`.

---

## 4B. Style Layer: state, assets and what is left for the laptop

### Presets (pick one, then set `style.preset`)
| Preset | Adds | Elements |
|---|---|---|
| **P0** Control | zoom-to-fill framing | F1 — **the current default** |
| **P1** Clean | + keyword colour/emoji, progress bar, credit tag, caption pop, dead-air removal, loop end | C1-C4, M1, A3, A4 |
| **P2** Punchy | + hook pop/shake, punch-ins, SFX | M2, M3, A1 |
| **P3** Full hype | + slow push-in, music bed | M4, A2 |

### Assets
| Asset | State |
|---|---|
| `assets/emoji/*.png` (14 Twemoji, CC-BY 4.0) | **committed**, credited in `README.md` |
| `assets/sfx/boom.wav`, `whoosh.wav` (CC0) | **committed**, synthesized with ffmpeg — see `assets/sfx/LICENSES.md` |
| `assets/music/bed.mp3` (CC0) | **NOT committed** — `python scripts/fetch_assets.py` downloads it |

### What could not be done in the cloud sandbox (and why)
The build sandbox's egress proxy answers **403 to `CONNECT`** for every asset host except
GitHub, so:
1. **A2's CC0 music bed could not be fetched** (`freepd.com` blocked). The music code was
   built and verified against a **generated test tone**; per the brief no synthetic
   "music" track was committed. `scripts/fetch_assets.py` fetches a real FreePD CC0 track
   on the laptop and writes `assets/music/LICENSES.md`. Until then **P3 renders fine and
   simply skips A2**, with a warning.
2. **A1's CC0 SFX could not be downloaded** (`kenney.nl`, `opengameart.org`,
   `freesound.org`, `cdn.pixabay.com` all blocked). Both cues were synthesized with
   ffmpeg and committed; `python scripts/fetch_assets.py --prefer-download` will try the
   Kenney CC0 pack and only install it if the archive carries its licence file.
3. **No real clips exist in this public repo**, so the style layer was verified on a
   synthetic 22 s source (static text card → moving footage with scene cuts, a 1 s silent
   gap over the still card, loud peaks). All **8** outputs (P0-P3 × A/B) pass the existing
   ffprobe guard. Real previews are what `scripts/render_previews.py` is for.

### Verification you can re-run here
```bash
python scripts/style_check.py     # rebuilds the synthetic clip, renders P0-P3 x A/B,
                                  # rewrites samples/synthetic/presets_sheet.png
python scripts/fetch_assets.py --check   # verify assets and licences, download nothing
```

### Two latent bugs this pass found and fixed (they predate the style layer)
1. `extract_audio_loudness_profile` passed `ebur128=meter=100`, but `meter` only accepts
   **9-18**, so the filter never initialised and the profile was **empty on every call**.
   The cut window's audio-energy term (weight 0.75) had silently been a constant, and every
   loudness-trough fallback was dead. **Re-run `python -m pipeline.runner cut` on any item
   you want re-scored with working audio energy.**
2. ebur128 reports `nan` over digital silence, which the parser skipped — punching a hole
   in the profile exactly where dead-air detection looks. `nan`/`-inf` now map to -70 LUFS.

---

## 5. Test Commands

### Zero-Network Tests (Local / Offline)
Runs all unit and mock tests:
```bash
pytest
```
*(Default `pytest.ini` runs `-m "not live"`, passing completely offline).*
**140 passed, 1 deselected** as of the style-layer pass — 65 pre-existing plus:
`tests/test_style.py` (presets, F1 segments and geometry, safe-area placement, C1-C4,
M1-M4, A1-A4 maths, graph assembly), `tests/test_style_render.py` (the same through
ffmpeg/libass: the F1 band really resizes, the bar really fills, the hook outlines really
clear, the bed really ducks 8 dB, dead air really shortens the render) and
`tests/test_scripts.py` (both laptop scripts, fully mocked).

### Live Network Integration Tests
Runs live crash-recovery and network tests:
```bash
pytest -m live
```

### CLI Runner Commands
```bash
# Check status of database and stage queues
python -m pipeline.runner status

# Run individual pipeline stages
python -m pipeline.runner discover
python -m pipeline.runner download
python -m pipeline.runner cut
python -m pipeline.runner hooks
python -m pipeline.runner voiceover
python -m pipeline.runner render

# Run a complete cycle
python -m pipeline.runner cycle
```

### Style layer
```bash
# Synthetic verification: P0-P3 x A/B + the committed contact sheet
python scripts/style_check.py
python scripts/style_check.py --presets P0 P3 --no-sheet

# Assets (idempotent; --check verifies without downloading)
python scripts/fetch_assets.py
python scripts/fetch_assets.py --check
python scripts/fetch_assets.py --prefer-download     # try the Kenney CC0 SFX pack

# Real previews for the 3 CP2 items (one command, no prompts)
python scripts/render_previews.py
python scripts/render_previews.py --presets P0 P3 --skip-download
```

# Project Handoff: GTA 6 Clipping Pipeline

## 1. Project State
- **Checkpoints 0 - 2**: Completed and committed.
  - CP0: Architecture plan, database schema, configuration, environment setup.
  - CP1: Discovery (YouTube/official channels), pull-based download, perceptual deduplication (pHash/Hamming), crash recovery.
  - CP2: Cut detection (loudness + scene changes), Gemini grounded hooks, Kokoro TTS voiceover, ASS subtitle generation, vertical 9:16 rendering (Variants A & B).
- **Checkpoint 3**: Telegram review bot + Meta (Instagram & Facebook Reels) posting has **not** been started.
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
   - Ensure `ffmpeg` and `ffprobe` are installed and accessible on your `PATH`.
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

## 4. Open Render Issues (from Review of CP2 Renders)
*Verbatim intent from review:*
1. **Caption and hook text is too thin and too small**:
   - Research standard typography for vertical short-form clips. Text in these formats is noticeably larger, bolder, and heavier. Update ASS subtitle styles / font rendering to match that standard.
2. **Audio cuts off at the end of the clip**:
   - Investigate the cut/muxing logic in `pipeline/render.py` (e.g. audio stream trimming, pad filters, or ffmpeg duration flags) and resolve the cutoff.
3. **Variant B narration freeze frame**:
   - In Variant B, the background video should **PAUSE** (freeze frame) while the narrator speaks the line, then resume when the narration finishes. Currently, the background video continues playing under the voiceover.
4. *Content selection was good; keep it.*

---

## 5. Test Commands

### Zero-Network Tests (Local / Offline)
Runs all unit and mock tests:
```bash
pytest
```
*(Default `pytest.ini` runs `-m "not live"`, passing completely offline).*

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

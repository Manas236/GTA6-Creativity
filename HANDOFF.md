# Project Handoff: GTA 6 Clipping Pipeline

## 1. Project State
- **Checkpoints 0 - 3**: Completed and committed.
  - CP0: Architecture plan, database schema, configuration, environment setup.
  - CP1: Discovery (YouTube/official channels), pull-based download, perceptual deduplication (pHash/Hamming), crash recovery.
  - CP2: Cut detection (loudness + scene changes), Gemini grounded hooks, Kokoro TTS voiceover, ASS subtitle generation, vertical 9:16 rendering (Variants A & B).
  - CP3: Telegram approval card (one self-editing card, strictly one item at a time) and Meta
    posting (Instagram Reels resumable upload + Facebook Page Reels `video_reels`), two-phase and
    crash-safe. Branch `cp3-telegram-meta`.
- **Checkpoint 3**: built and tested with mocks. **Not yet run against the real APIs** - that is the
  laptop dry run in section 5 below, and no live post happens until you say go.
- **Safety Defaults**: `dry_run: true` remains active by default in `config.yaml`, and
  `posting.dry_run_mode: plan_only` means a dry run sends no write request to Meta at all.

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

## 5. CP3 Laptop Steps: run the dry run
> **Run `git fetch && git checkout cp3-telegram-meta && git pull` first.** Nothing below posts
> anything publicly; the dry run cannot reach a publish endpoint.

### 5.1 Fill in `.env`
Copy the new keys from `.env.example` (each has a one-line comment):

| Key | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather -> `/newbot` (or `/token` for an existing bot) |
| `TELEGRAM_CHAT_ID` | message your bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and take `message.chat.id` |
| `IG_USER_ID` | Graph API Explorer -> `me/accounts?fields=instagram_business_account` |
| `IG_ACCESS_TOKEN` | a long-lived **Page** token with `instagram_basic`, `instagram_content_publish`, `instagram_manage_insights`, `pages_read_engagement` |
| `FB_PAGE_ID` | the Page's About tab, or `me/accounts` |
| `FB_PAGE_ACCESS_TOKEN` | optional; only if `IG_ACCESS_TOKEN` is not already a Page token for `FB_PAGE_ID`. Needs `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` |

### 5.2 Run it (one command, no prompts)
```bash
git pull
pip install -r requirements.txt     # if the venv is new
python scripts/cp3_dry_run.py
```

What it does, in order:
1. **Telegram** - `getMe`, then sends **one real approval card** to your chat: Variant B as the
   video with the three buttons, Variant A as a second message, and the hook, authenticity tag,
   source + creator and discovery score in the caption. It uses the highest-scoring render in
   `pipeline.db`; if there is none it builds a synthetic 1080x1920 pair with ffmpeg first, so you
   can still see the card exactly as it will look.
   *The buttons are live:* tapping Approve / Reject / Approve A only really updates that item in
   your local database. Tapping an older card answers "This card is out of date" and does nothing.
2. **Meta** - read-only GETs: `/debug_token` (type, validity, expiry, permissions, and which
   permissions are missing), the IG user, the IG `content_publishing_limit`, and the Page.
3. **Posting flow in dry run** - the full two-phase state machine, logging and writing every request
   it would send to `media/dry_run/post_<id>_<platform>.json`, with every token redacted.

Exit code 0 means every check passed; 1 lists what needs attention. Nothing is published, and
nothing can be: `app.dry_run` and `plan_only` are forced for that process, and a guard raises on any
non-GET request and on any URL or body naming a write endpoint.

### 5.3 Then stop
Report what the card looked like and what the summary said. The first live post needs your explicit
go; until then leave `app.dry_run: true`.

### 5.4 Running the pipeline with approvals on
```bash
python -m pipeline.runner approve     # one approval pass: send a card if none is open, poll once
python -m pipeline.runner post        # settle any crashed publish, then post approved items
python -m pipeline.runner cycle       # a full cycle, including the approval poll and posting
python -m pipeline.runner loop        # cycles continuously; the Telegram long poll runs inside it
```
To skip Telegram entirely, set `approval.mode: auto` in `config.yaml`: items are then approved or
rejected by `approval.auto_quality_gate` (both renders present, hook grounded, ffprobe within the
duration caps) with no network calls.

---

## 6. Test Commands

### Zero-Network Tests (Local / Offline)
Runs all unit and mock tests:
```bash
pytest
```
*(Default `pytest.ini` runs `-m "not live"`, passing completely offline).*

CP3 adds 60 of those tests and they make no network calls of any kind - both modules put every
request behind an injectable transport, and the tests pass a fake:
```bash
pytest tests/test_telegram_bot.py tests/test_posting.py -q
```
They cover the card lifecycle (send, edit, approve, reject, A-only, stale click, foreign chat),
one-at-a-time ordering, auto mode and the quality gate, edit throttling and 429 handling, the
two-phase posting state machine, a crash at **every** status and resume without a duplicate publish,
unknown remote state -> failed with no publish, FB-only retry, the 429 source pause, the dry run
never calling publish, and the caption template.**

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

python -m pipeline.runner approve
python -m pipeline.runner post

# Run a complete cycle, or loop forever
python -m pipeline.runner cycle
python -m pipeline.runner loop
```

# Project Handoff: GTA 6 Clipping Pipeline

## 0. Laptop: what to run next

Everything below is still yours to do, in this order. Nothing posts publicly until step 7.

1. **Update and check** (Windows PowerShell, in the project folder):
   ```powershell
   git pull
   .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python scripts/fetch_assets.py      # music bed now comes from OpenGameArt (FreePD closed)
   pytest                              # expect all green
   ```
2. **Pick a style preset.** Watch `media/previews/<item>/grid_A.mp4` and `grid_B.mp4` for the three
   items (2x2 tiles labelled P0-P3; the single files are `P{n}_A.mp4` / `P{n}_B.mp4`), then set
   `style.preset` in `config.yaml`. It is still **P0**. `media/previews/README.txt` ends with the
   endings/SFX check table (also in section 0b below). To re-render later:
   `python scripts/render_previews.py` then `python scripts/verify_previews.py`.
3. **Fill in `.env`** (copy names from `.env.example`). Missing on this laptop as of 2026-11-10:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IG_USER_ID`, `IG_ACCESS_TOKEN`, `FB_PAGE_ID`,
   `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`; optional and also missing: `FB_PAGE_ACCESS_TOKEN`
   (only if `IG_ACCESS_TOKEN` is not a Page token for the Page) and `HEALTHCHECK_URL`.
   `GEMINI_API_KEY` is set. Section 5.1 says where to get each value.
4. **CP3 real-token dry run** (sends one real Telegram card; Meta is read-only):
   `python scripts/cp3_dry_run.py` - see section 5.2, and tell me what the card looked like and
   what the summary said.
5. **Check the insight metric names** in Graph API Explorer before the first 24 h snapshot
   (`{ig-media-id}/insights?metric=views`, `{video-id}/video_insights`): PLAN.md calls #103-#104.
   They are config (`analytics.instagram`, `analytics.facebook`), not code.
6. **Twitch credentials before 19 Nov**: create an app at dev.twitch.tv, put `TWITCH_CLIENT_ID` /
   `TWITCH_CLIENT_SECRET` in `.env`. Twitch discovery switches itself on 2026-11-19 (UTC); without
   the keys it is skipped with one Telegram alert.
7. **Go live on 19 Nov - only after you say go**: set `app.dry_run: false` in `config.yaml` and run
   `python -m pipeline.runner loop` (or the server service, step 8). The first live post starts
   week 1: 50/50 A/B and Telegram cards for 7 days, then the bandit and auto approval.
8. *Optional*: move to an always-on server (README "Moving to a server": systemd unit in
   `deploy/`, set `posting.profile: "server"`), and set `HEALTHCHECK_URL` for the dead-man switch.
9. *Optional*: this machine has `HF_HOME=D:\huggingface_cache` set system-wide but no D: drive;
   whisper now falls back to `~/.cache/huggingface` (PLAN call #136), but you may want to remove or
   fix that variable.

## 0b. Fix pass 2026-11-10: results

### Task 3: real-clip check of every preview (`python scripts/verify_previews.py`): 24/24 pass
Items: item_00983f85d1fd (GamingBolt), item_1032dd2c31da (Tridzo GTA), item_5a67cc8642ed (Hazard),
the three in this laptop's `pipeline.db` (none was swapped out). All were re-downloaded as H.264
1080p and re-cut under the final rule; item_1032 needed its 8th-best window (the first 7 had no
sentence end in 18-22 s). The hooks/VO are the ones stored earlier; item_1032's window moved
(111.1 s -> 114.4 s), item_00983 and item_5a67 kept their start.
"Source check" = the end placed back in the source, re-transcribed with 2 s after it, and the audio
level at the cut point relative to the speaker. Whisper hears Hazard's "wanted level." as "wanna
love." / "-lup." in the source too; that is whisper, not the cut.

```
item               file         dur  last words (render tail)           source check                                               SFX cues (s)     gaps (s)  result
item_00983f85d1fd  P0_A.mp4   21.07  not to do so.                      pause: 'so.' | next The +0.89s | end -16 dB                -                -         OK
item_00983f85d1fd  P0_B.mp4   27.27  not to do so.                      pause: 'so.' | next The +0.89s | end -16 dB                -                -         OK
item_00983f85d1fd  P1_A.mp4   21.07  not to do so.                      pause: 'so.' | next The +0.89s | end -16 dB                -                -         OK
item_00983f85d1fd  P1_B.mp4   27.27  not to do so.                      pause: 'so.' | next The +0.89s | end -16 dB                -                -         OK
item_00983f85d1fd  P2_A.mp4   21.07  not to do so.                      pause: 'so.' | next The +0.89s | end -16 dB                0.00, 13.24      13.24     OK
item_00983f85d1fd  P2_B.mp4   27.27  not to do so.                      pause: 'so.' | next The +0.89s | end -16 dB                0.00, 19.44      19.44     OK
item_00983f85d1fd  P3_A.mp4   21.07  not to do so.                      pause: 'so.' | next The +0.89s | end -16 dB                0.00, 13.24      13.24     OK
item_00983f85d1fd  P3_B.mp4   27.27  not to do so.                      pause: 'so.' | next The +0.89s | end -16 dB                0.00, 19.44      19.44     OK
item_1032dd2c31da  P0_A.mp4   20.60  download one megabyte pack.        punct_audio: 'pack.' | next Now +0.18s | end -24 dB        -                -         OK
item_1032dd2c31da  P0_B.mp4   22.50  download one megabyte pack.        punct_audio: 'pack.' | next Now +0.18s | end -24 dB        -                -         OK
item_1032dd2c31da  P1_A.mp4   20.60  download one megabyte pack.        punct_audio: 'pack.' | next Now +0.18s | end -24 dB        -                -         OK
item_1032dd2c31da  P1_B.mp4   22.50  download one megabyte pack.        punct_audio: 'pack.' | next Now +0.18s | end -24 dB        -                -         OK
item_1032dd2c31da  P2_A.mp4   20.60  download one megabyte pack.        punct_audio: 'pack.' | next Now +0.18s | end -24 dB        0.00, 17.14      17.14     OK
item_1032dd2c31da  P2_B.mp4   22.50  download one megabyte pack.        punct_audio: 'pack.' | next Now +0.18s | end -24 dB        0.00, 19.04      19.04     OK
item_1032dd2c31da  P3_A.mp4   20.60  download one megabyte pack.        punct_audio: 'pack.' | next Now +0.18s | end -24 dB        0.00, 17.14      17.14     OK
item_1032dd2c31da  P3_B.mp4   22.50  download one megabyte pack.        punct_audio: 'pack.' | next Now +0.18s | end -24 dB        0.00, 19.04      19.04     OK
item_5a67cc8642ed  P0_A.mp4   18.47  of your wanna -lup.                punct_audio: 'love.' | next They're +0.08s | end -16 dB    -                -         OK
item_5a67cc8642ed  P0_B.mp4   21.17  of your wanna -lup.                punct_audio: 'love.' | next They're +0.08s | end -16 dB    -                -         OK
item_5a67cc8642ed  P1_A.mp4   18.47  of your wanna love.                punct_audio: 'love.' | next They're +0.08s | end -16 dB    -                -         OK
item_5a67cc8642ed  P1_B.mp4   21.17  of your wanna -lup.                punct_audio: 'love.' | next They're +0.08s | end -16 dB    -                -         OK
item_5a67cc8642ed  P2_A.mp4   18.47  of your wanna love.                punct_audio: 'love.' | next They're +0.08s | end -16 dB    0.00, 16.34      16.34     OK
item_5a67cc8642ed  P2_B.mp4   21.17  of your wanna love.                punct_audio: 'love.' | next They're +0.08s | end -16 dB    0.00, 19.04      19.04     OK
item_5a67cc8642ed  P3_A.mp4   18.47  of your wanna love.                punct_audio: 'love.' | next They're +0.08s | end -16 dB    0.00, 16.34      16.34     OK
item_5a67cc8642ed  P3_B.mp4   21.17  of your wanna love.                punct_audio: 'love.' | next They're +0.08s | end -16 dB    0.00, 19.04      19.04     OK
```


### CP4 dry run (`python scripts/cp4_dry_run.py`, seed 7)
10 simulated days on a fake clock, real stage code, every network call faked, temporary database
(the real `pipeline.db` and `media/` are checked to be untouched). Result: **PASS**.

```
window      : 2026-11-12T00:00:00Z -> 2026-11-22T00:00:00Z (10 days, 480 ticks of 30 min)
cycles      : 412 run, 1 with a failed stage (the simulated crash); laptop asleep 16 Nov 06:00Z -> 17 Nov 16:00Z
healthcheck : 411 pings (one per fully successful cycle; none for the failed one)
approval    : 20 Telegram cards, taps ok 15 / A-only 2 / reject 3
auto switch : first live 12 Nov 07:30Z; auto approval from 19 Nov 07:30Z (7 days); 1 switch notice
posts       : 16 items published, 32 platform publishes, max 2 per local day, >= 3 h apart
missed slots: 4 re-slotted after the sleep, one per slot (16 Nov 13:00 -> 18 Nov 13:00 IST, ...)
crash       : sim-02 - runner died right after Instagram published -> settled live, 1 publish
lost reply  : sim-04 - Instagram published, reply lost -> settled live, 1 publish call
variant     : drawn at each slot; week 1: A 7 / B 2 (50/50 random); after week 1 (19 Nov 07:30Z):
              4 draws still 50/50 (B had < 3 measured items), then 2 by the bandit
              (B leads 0.91 vs 0.41 -> B 80% / A 20%), one A and one B
metrics     : 24 x 24h and 16 x 72h snapshots
cleanup     : 6 items' renders deleted 7 days after posting, 6 items 'cleaned'
invariants  : no double post / no burst / nothing preview_only, rejected or unapproved posted /
              cleanup kept every file an open post needs / uploaded variant == row variant /
              real pipeline.db + media untouched  -> all PASS
```


---

## 1. Project State
- **CP0-CP2 + style layer**: done (PLAN.md §1-§8, §7B; calls #1-#59). Default preset **P0**.
- **Endings** (fix pass 2026-11-10): a clip only ends after a completed sentence, checked against
  the audio; an item with no clean ending fails at `cut` with `no_clean_ending` (PLAN.md §7,
  calls #124-#136, #146-#151).
- **SFX** (fix pass 2026-11-10): the hook boom, then at most one whoosh at least 5 s from any other
  cue; max 2 per clip (calls #137-#139).
- **CP3** (Telegram approval + Meta posting): built and tested with mocks on `main`. **Not yet run
  against the real APIs** (step 4 above).
- **CP4** (scheduler, slot-time A/B draw, approval switch, analytics, cleanup, Twitch, healthcheck,
  server unit): built and tested with mocks on `main`; `scripts/cp4_dry_run.py` passes every
  invariant over 10 simulated days (section 0b). **Not yet run against the real APIs.**
- **Safety defaults**: `app.dry_run: true` and `style.preset: "P0"` in `config.yaml`;
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

## 5. CP3 Laptop Steps: run the dry run
> **Run `git pull` on `main` first.** Nothing below posts
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

## 5B. Test Commands

### Zero-Network Tests (Local / Offline)
Runs all unit and mock tests:
```bash
pytest
```
*(Default `pytest.ini` runs `-m "not live"`, passing completely offline).*
**140 passed, 1 deselected** as of the style-layer pass (see the final message of the latest pass for the current count) — 65 pre-existing plus:
`tests/test_style.py` (presets, F1 segments and geometry, safe-area placement, C1-C4,
M1-M4, A1-A4 maths, graph assembly), `tests/test_style_render.py` (the same through
ffmpeg/libass: the F1 band really resizes, the bar really fills, the hook outlines really
clear, the bed really ducks 8 dB, dead air really shortens the render) and
`tests/test_scripts.py` (both laptop scripts, fully mocked).

**Windows note:** Pillow's Windows wheels ship without libraqm, so `text_width_px` measures
*without* GPOS kerning (Montserrat kerns only via GPOS; libass still kerns when it draws).
Near-tie hook wraps can therefore differ from Linux, e.g. `IS THIS REALLY / GTA 6?` vs
`IS THIS / REALLY GTA 6?` (792 vs 793 px). `test_build_ass_subtitles_no_speech` accepts
either split when they are within 2 px.

CP3 adds 60 of those tests and they make no network calls of any kind - both modules put every
request behind an injectable transport, and the tests pass a fake:
```bash
pytest tests/test_telegram_bot.py tests/test_posting.py -q
```
They cover the card lifecycle (send, edit, approve, reject, A-only, stale click, foreign chat),
one-at-a-time ordering, auto mode and the quality gate, edit throttling and 429 handling, the
two-phase posting state machine, a crash at **every** status and resume without a duplicate publish,
unknown remote state -> failed with no publish, FB-only retry, the 429 source pause, the dry run
never calling publish, and the caption template.

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

---

## 6. Hooks stage: failure policy (laptop pass, 2026-11-10)
At the old `maxOutputTokens: 1024`, gemini-2.5-flash spent ~920 tokens thinking, the JSON
came back truncated (`finishReason: MAX_TOKENS`) and the parser silently saved its
defaults (`IS THIS REALLY GTA 6?` / "Wait until you see this GTA 6 detail!"). Now
(`config.yaml -> gemini:`): thinking off, JSON response mode, 2048-token cap; truncated or
unparseable answers are failed attempts; calls are spaced 12 s apart (free tier, 5/min)
and 429s wait the server's `retryDelay`; a grounding check that errors is NOT grounded.
No valid hook after 3 attempts -> the item is marked `failed` at `hooks` and its hook
fields are cleared. The fixed `IS THIS REALLY GTA 6?` hook (`hook_grounded = 0`) is used
only when Gemini answered but the hook got 2 grounding FAIL verdicts. The hooks stage now
transcribes the cut (faster-whisper `base.en`), sends that transcript to Gemini, and caches
the words in `items.transcript_words` (keyed on the cut window + model) for render to reuse.

**`items.preview_only`** (for CP3): an item flagged with `db.set_preview_only(id, True)`
holds preview-only data (e.g. a hand-set hook for style previews). SQLite triggers refuse
to move it into `rendered`/`awaiting_approval`/`approved`/`queued`/`posted`/`measured`
and refuse any `posts` insert/update for it, whatever code tries. Clear the flag only
after a real hooks run.

## 7. Known issues: both FIXED (PLAN.md calls 81-83)
1. **Dedupe/scene-detection timeouts on AV1 1440p sources** - FIXED: downloads are capped at
   1080p H.264 and an AV1 / over-cap file is refused. Verified on the laptop 2026-11-10: the three
   preview sources were re-downloaded as H.264 1080p and a full window scan took ~36 s instead of
   ~4 min. (`media/downloads/dl_item_1.mp4` is a leftover of the old test behaviour; safe to delete.)
2. **The test suite wrote into the real project folder** - FIXED: tests use `tmp_path` for media,
   and `tests/test_zz_media_isolation.py` fails the run if anything under the real `media/` changes.

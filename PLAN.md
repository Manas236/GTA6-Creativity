# GTA 6 Clipping Channel: Fully Automated Short-Video Pipeline
## Architecture Plan & Engineering Specification (PLAN.md - Revision 2)

---

## 1. System Overview & Core Principles

This pipeline automates the end-to-end lifecycle of short vertical videos (Reels) covering Grand Theft Auto VI:
1. **Discover**: Find viral, trending content from YouTube pre-launch (and Twitch post-launch from 19 Nov 2026). Reddit is completely dropped per Decision A (Reddit ended unauthenticated `.json` access globally in 2026 and new developer API apps are closed).
2. **Download & Dedupe**: Fetch source media idempotently to a temp file with atomic rename; deduplicate by source ID and multi-frame 1 fps perceptual hashing with coverage-based temporal alignment ($Coverage \ge 0.65$).
3. **Pull-Based Buffer**: Maintain a capped buffer of ready items (default 6), pulling and processing the highest-scoring items first to conserve disk and API quotas.
4. **Cut**: Select optimal ~20s highlight windows via audio loudness peak analysis and scene-change density (rejecting static videos with $< 0.05$ scene changes/s). Re-check perceptual deduplication at the cut level ($N=3$ consecutive frames).
5. **Enrich & Render**:
   - Generate curiosity, question, or hype/analysis hooks via Gemini free tier, strictly grounded in the video (verified by a yes/no grounding check against transcript, 4 frames, and title; falling back to a plain question hook after 2 failures).
   - Hook burned via ASS subtitle file (not drawtext) to support line wrapping and special characters.
   - Transcribe word-level timestamps via local `faster-whisper`. If no speech is detected, Variant A displays the hook only.
   - Synthesize a punchy 3–6s voiceover line near the start via local `Kokoro TTS`.
   - Render two 1080x1920 (9:16), 30 fps, 48 kHz stereo AAC variants (**Variant A**: clip + hook + captions; **Variant B**: freeze-frame format: the cut's first frame held while only the Kokoro VO plays, then the cut plays with its original audio at full level, with VO captions then speech captions). No ducking.
6. **Approve & Schedule**:
   - Single config setting: `approval.mode: telegram | auto`.
   - **Telegram Approval**: Single self-editing status card with `Approve`, `Reject`, and `Approve A only` buttons. ("A only" logs the item as A-only and excludes it from the A/B comparison).
   - **Auto Mode**: Autonomous approval governed by a strict quality gate (renders exist and pass ffprobe, duration within bounds, hook passed grounding check, no error flags).
   - **Scheduler**: 2 daily posting slots managed via named timezone profiles (`local` = Asia/Kolkata 13:00 & 18:00; `server` = America/New_York 18:00 & 21:00). Stored as full ISO-8601 datetimes.
   - **A/B Allocation**: Random 50/50 in Week 1, transitioning to an epsilon-greedy multi-armed bandit with a 20% exploration floor.
7. **Publish & Track**:
   - Two-phase posting to prevent duplicate publications on crash: record container/video ID before publishing (`uploading` -> `container_ready` -> `publishing` -> `live`). On resume, inspect remote state before republishing.
   - Resumable chunked upload to Instagram Reels and Facebook Page Reels via Meta Graph API.
   - Pull 24h and 72h performance metrics; rebuild `ab_learning` cache from `metrics` (ranking within each platform first to prevent IG drowning FB).
8. **Maintenance & Safety**:
   - Dead-man switch ping to healthchecks.io on every successful runner cycle.
   - Failure-only Telegram alerts.
   - Disk cleanup daemon (source deleted post-render; renders deleted 7 days post-post; rejected/failed media deleted after 3 days).
   - Crash resilience via SQLite WAL mode, heartbeat-based lock with immediate dead PID detection, and atomic file swaps.

---

## 2. Folder & File Layout

All code and assets reside inside this project directory (`c:\Users\Admin\Desktop\Creativity`). Code is strictly cross-platform using `pathlib.Path`.

```
Creativity/
├── .env.example              # Secret key template with one-line descriptions (secrets only)
├── .env                      # Real secrets (gitignored, never committed or printed)
├── .gitignore                # Git ignore rules for media, db, cache, .venv
├── .venv/                    # Python 3.11 virtual environment (gitignored)
├── PLAN.md                   # Master architectural blueprint
├── README.md                 # Setup, operations, timezone profile switch, Linux migration (CP4)
├── config.yaml               # Pipeline settings, scoring formula, schedules, fonts, dry_run
├── requirements.txt          # Python dependencies
├── pipeline/
│   ├── __init__.py           # Package marker
│   ├── config.py             # Config loader, env validator, brand constants
│   ├── db.py                 # SQLite WAL state store, migrations, transactions, atomic kv
│   ├── errors.py             # Custom exceptions, error categorizations
│   ├── rate_limit.py         # Per-source rate limiting and cooldown state
│   ├── discover.py           # Stage 1: YouTube, Reddit JSON, Twitch Helix discovery
│   ├── download.py           # Stage 2: Resumable, crash-safe media fetch via yt-dlp to .part
│   ├── dedupe.py             # Stage 3: 1 fps black-bar cropped perceptual hash deduplication
│   ├── cut.py                # Stage 4: Audio loudness peaks & scene cut window selection + cut dedupe
│   ├── hooks.py              # Stage 5: Grounded Gemini hook generation & grounding check
│   ├── voiceover.py          # Stage 6: 3-6s Gemini VO script & local Kokoro TTS synthesis
│   ├── render.py             # Stage 7: ffmpeg 9:16 compositing, ASS captions & hook, B freeze-frame VO, ffprobe guard
│   ├── telegram_bot.py       # Stage 8: Single self-editing Telegram approval & alert card
│   ├── posting.py            # Stage 9: Two-phase Meta Graph API Reel uploader (IG + FB)
│   ├── scheduler.py          # Stage 10: 2 daily slots, A/B bandit allocator, healthchecks ping
│   ├── analytics.py          # Stage 11: 24h/72h metric scraping & normalized bandit cache rebuild
│   ├── cleanup.py            # Stage 12: Disk retention manager (source, render, rejected/failed purge)
│   └── runner.py             # Single long-running orchestrator with stale-lock detection
├── assets/
│   └── fonts/                # Bundled OFL fonts (Montserrat-Black.ttf, Montserrat-Bold.ttf, OFL.txt)
├── media/                    # Local storage (gitignored)
│   ├── downloads/            # Source media (purged immediately post-render)
│   ├── cuts/                 # 20s cropped clips
│   ├── audio/                # Kokoro TTS audio clips
│   └── renders/              # Final Variant A & B MP4s (purged 7 days post-publish)
└── tests/
    ├── conftest.py           # Mock fixtures: DB, yt-dlp, Reddit, Gemini, Meta, Telegram
    ├── test_config.py        # Config & env validation tests
    ├── test_db.py            # WAL mode, state transitions, idempotency, crash resume
    ├── test_discover.py      # Source parsing and percentile discovery score formula calculation
    ├── test_download.py      # Download retry, atomic rename, ffprobe validation, crash recovery
    ├── test_dedupe.py        # 1 fps black-bar crop hash collision & consecutive frame match
    ├── test_cut.py           # Audio peak + scene density scoring, boundary snap, cut dedupe
    ├── test_hooks.py         # Hook grounding check, fallback to question hook, style extraction
    ├── test_voiceover.py     # 3-6s TTS generation (Kokoro mocked)
    ├── test_render.py        # ffmpeg filtergraph syntax, ASS subtitle & hook formatting
    ├── test_telegram.py      # Self-editing card, Approve / Reject / Approve A only buttons
    ├── test_posting.py       # Two-phase upload flow, remote inspection on resume, partial FB retry
    ├── test_scheduler.py     # Named timezone profiles, ISO datetime slots, bandit with 20% floor
    ├── test_analytics.py     # Rebuilt bandit cache from metrics, intra-platform rank combination
    └── test_cleanup.py       # Source cleanup post-render, 7-day renders, 3-day failed media purge
```

---

## 3. Database Schema (SQLite in WAL Mode)

Database filename: `pipeline.db`.
Connection initialization runs:
```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 10000;
```

### State Machine Lifecycle
Items progress sequentially:
`discovered` -> `downloaded` -> `cut` -> `rendered` -> `awaiting_approval` -> `approved` -> `queued` -> `posted` -> `measured` -> `cleaned`.
Any stage may transition to `failed` (with `error_stage` and `error_message`) or `rejected` (e.g. duplicate or user rejected).

### DDL Specification

```sql
-- 1. Main Pipeline Items
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,                       -- UUID / ULID
    source TEXT NOT NULL,                      -- 'youtube', 'twitch' (Reddit dropped per Decision A)
    source_id TEXT NOT NULL,                   -- e.g. YouTube video ID
    source_url TEXT NOT NULL,                  -- Original URL
    author_name TEXT NOT NULL,                 -- Original creator/channel for credit
    channel_id TEXT,                           -- Channel identifier for official channel weighting
    original_title TEXT,                       -- Source title
    original_duration REAL,                    -- Duration in seconds
    view_count INTEGER DEFAULT 0,              -- Raw view count at discovery
    upvote_count INTEGER DEFAULT 0,            -- Upvotes / likes
    comment_count INTEGER DEFAULT 0,           -- Comments
    upload_time TEXT NOT NULL,                 -- ISO-8601 UTC timestamp of creation
    discovered_at TEXT NOT NULL,               -- ISO-8601 UTC when discovered
    raw_velocity REAL NOT NULL,                -- Source-specific engagement velocity
    percentile_rank REAL NOT NULL,             -- Percentile rank (0.0 to 1.0) within recent source items
    discovery_score REAL NOT NULL,             -- Percentile-based discovery score
    
    -- Download & Deduplication
    download_path TEXT,                        -- Local file path to raw download
    downloaded_at TEXT,                        -- Timestamp download finished
    is_duplicate INTEGER DEFAULT 0,            -- 1 if flagged duplicate
    rejection_reason TEXT,                     -- e.g. 'duplicate_download', 'duplicate_cut', 'user_rejected'
    
    -- Cut Stage
    cut_start REAL,                            -- Seconds into source video
    cut_end REAL,                              -- Seconds into source video
    cut_duration REAL,                         -- Target ~20s
    cut_path TEXT,                             -- Local file path to cut clip
    
    -- Content & Copy
    hook_style TEXT,                           -- 'curiosity', 'question', 'hype_analysis'
    hook_text TEXT,                            -- Hook title overlay
    hook_grounded INTEGER DEFAULT 1,           -- 1 if verified grounded, 0 if fell back
    voiceover_script TEXT,                     -- 3-6s TTS script for Variant B
    voiceover_audio_path TEXT,                 -- Audio file for Variant B
    caption_text TEXT,                         -- Post description with creator credit & tags
    
    -- Renders
    render_a_path TEXT,                        -- Local path to Variant A MP4
    render_b_path TEXT,                        -- Local path to Variant B MP4
    chosen_variant TEXT,                       -- 'A' or 'B' (assigned by scheduler/user)
    approval_type TEXT,                        -- 'approved', 'approved_a_only', 'auto_approved'
    
    -- State Tracking
    stage TEXT NOT NULL,                       -- Stage enum
    error_stage TEXT,                          -- Stage that produced failure
    error_message TEXT,                        -- Failure diagnostics
    retry_count INTEGER DEFAULT 0,             -- Retry attempts within current stage
    stage_updated_at TEXT NOT NULL,            -- Timestamp of last state change
    created_at TEXT NOT NULL,                  -- Timestamp row created
    updated_at TEXT NOT NULL,                  -- Timestamp row updated
    
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_items_stage ON items(stage);
CREATE INDEX IF NOT EXISTS idx_items_discovery_score ON items(discovery_score DESC);
CREATE INDEX IF NOT EXISTS idx_items_source_url ON items(source_url);

-- 2. Perceptual Fingerprints (1 fps black-bar cropped hashes for download and cut levels)
CREATE TABLE IF NOT EXISTS fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    scope TEXT NOT NULL,                       -- 'download' or 'cut'
    second_offset INTEGER NOT NULL,            -- 0, 1, 2, ... second index
    phash_uint64 INTEGER NOT NULL,             -- 64-bit perceptual hash integer
    created_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fingerprints_item_scope ON fingerprints(item_id, scope);
CREATE INDEX IF NOT EXISTS idx_fingerprints_hash ON fingerprints(phash_uint64);

-- 3. Social Media Posts (Two-Phase Publishing)
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    platform TEXT NOT NULL,                    -- 'instagram', 'facebook'
    variant TEXT NOT NULL,                     -- 'A' or 'B'
    hook_style TEXT NOT NULL,                  -- Captured for learning
    source TEXT NOT NULL,                      -- Captured for learning
    slot_datetime TEXT NOT NULL,               -- Full ISO-8601 UTC slot datetime (e.g. '2026-11-10T13:00:00Z')
    status TEXT NOT NULL,                      -- 'pending', 'uploading', 'container_ready', 'publishing', 'live', 'failed', 'dry_run'
    remote_container_id TEXT,                  -- IG creation ID or FB upload session ID (saved before publish)
    remote_id TEXT,                            -- Final IG Media ID or FB Video ID
    remote_url TEXT,                           -- Permalink to published Reel
    error_message TEXT,
    posted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE,
    UNIQUE(item_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_slot_datetime ON posts(slot_datetime);

-- 4. Analytics Snapshots (24h and 72h)
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id INTEGER NOT NULL,
    snapshot_hours INTEGER NOT NULL,           -- 24 or 72
    views INTEGER DEFAULT 0,
    likes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    raw_json TEXT,
    fetched_at TEXT NOT NULL,
    FOREIGN KEY(post_id) REFERENCES posts(id) ON DELETE CASCADE,
    UNIQUE(post_id, snapshot_hours)
);

-- 5. Multi-Armed Bandit / Bayesian A/B Cache (Rebuilt on every run from metrics, never incremented)
CREATE TABLE IF NOT EXISTS ab_learning (
    dimension TEXT NOT NULL,                   -- 'variant', 'hook_style', 'source'
    dimension_value TEXT NOT NULL,             -- e.g. 'A', 'B', 'curiosity', 'youtube'
    sample_count INTEGER DEFAULT 0,            -- Number of evaluated posts
    normalized_score REAL DEFAULT 0.0,         -- Combined intra-platform normalized score
    weight REAL DEFAULT 1.0,                   -- Sampling weight for bandit allocation
    rebuilt_at TEXT NOT NULL,
    PRIMARY KEY(dimension, dimension_value)
);

-- 6. Rate Limit & Error Cooldown Tracking
CREATE TABLE IF NOT EXISTS rate_limits (
    source TEXT PRIMARY KEY,                   -- 'youtube', 'reddit', 'twitch', 'meta_graph', 'gemini'
    paused_until TEXT,                         -- Timestamp until which calls are withheld
    consecutive_errors INTEGER DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);

-- 7. Key-Value Store (Mutex locks, heartbeats, Telegram status card message ID, runner state)
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 8. Audit Log of Stage Transitions
CREATE TABLE IF NOT EXISTS stage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL,                      -- 'start', 'success', 'failure'
    message TEXT,
    duration_s REAL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_stage_log_item ON stage_log(item_id);
```

---

## 4. Discovery Score Formula (Normalized Percentile & Source / Official Channel Weights)

Per user requirements and decisions:
- **Sources**: YouTube pre-launch; Twitch from 19 Nov 2026. (Reddit is dropped completely per Decision A: Reddit ended unauthenticated public `.json` globally in 2026 and new developer apps are closed).
- **Search Queries**: Combines standard relevance search with date-sorted search (`sp=CAI%253D`) to ensure fresh creator uploads surface alongside top trending videos.
- **Real Metadata Fetching**: Flat-playlist search omits upload timestamps and views; discovery executes rate-limited individual metadata extraction (0.5s pause) to pull true `upload_date` and `view_count`.
- **Official Channel Down-Weighting (Decision B)**: Official publisher channels (Rockstar Games, PlayStation, Xbox, Netflix) are identified by `channel_id` (not display name) and assigned a down-weight of `0.20` so high-velocity creator videos rank above platform trailers.
- **Percentile Normalization**: Raw velocity is converted to a percentile rank within that source's recent rolling pool (last 100 items).
- **Recency Decay**: True exponential half-life decay ($36\text{ h}$) applied against real item age.

### Velocity Formulas by Source
- **YouTube**:
  $$\text{RawVelocity} = \frac{\text{views}}{\max(\text{age\_hours}, \text{min\_age\_hours})}$$
- **Twitch (post-launch)**:
  $$\text{RawVelocity} = \frac{\text{views}}{\max(\text{age\_hours}, \text{min\_age\_hours})}$$

### Percentile Rank Calculation
Within the recent rolling pool for source $S$:
$$\text{PercentileRank} = \frac{\text{rank of item's RawVelocity in pool}}{N_{\text{pool}}} \in [0.01, 1.0]$$

### Overall Discovery Score Formula in `config.yaml`
```yaml
discovery:
  min_age_hours: 0.5
  half_life_hours: 36.0
  velocity_power: 1.2
  official_channel_weight: 0.20
  official_channels:
    - "UC6VcWc1rAoWdBCM0JxrRQ3A" # Rockstar Games
    - "UC-2Y8dQb0S6DtpxNgAKoJKA" # PlayStation
    - "UCjBp_7RuDBUYbd1LegWEJ8g" # Xbox
    - "UCWOA1ZGywLbqmigxE4Qlvvw" # Netflix
  # Formula evaluated dynamically via safe AST expression evaluator:
  score_formula: >
    (percentile_rank ** velocity_power) * (0.5 ** (age_hours / half_life_hours)) * source_weight
```

---

## 5. Deduplication: Coverage-Based Download Dedupe & Cut-Level Matching

Per user requirements and review fixes:
1. **Download-Level Duplicate Detection (Coverage-Based)**:
   - At the download level, duplicate must mean the *same video*, not merely sharing a brief segment.
   - For candidate sequence $C$ and target sequence $T$ with lengths $L_C, L_T$ and $L_{\min} = \min(L_C, L_T)$:
     $$M^* = \max_{\Delta} \sum_{i} \mathbb{I}(\text{Hamming}(C[i], T[i+\Delta]) \le 8)$$
     $$\text{Coverage} = \frac{M^*}{L_{\min}}$$
   - If $\text{Coverage} \ge 0.65$ (configurable via `dedupe.download_coverage_threshold`), the item is flagged as `rejected: duplicate_download`.
   - **Verification**: Caught real reposts (Trailer 2 Rockstar vs PlayStation at 99.4% coverage; Launch Trailer Rockstar vs Xbox at 95.6% coverage). Did NOT flag Trailer 1 vs Trailer 2 (11.1% coverage) or GamingBolt creator reaction video containing trailer footage (10.8% coverage).
2. **Cut-Level Duplicate Detection (CP2)**:
   - Partial overlap is handled at the cut level.
   - Uses consecutive matching subsequence: $N=3$ consecutive frames matching a previously rendered/queued/posted cut. If matched, the next-best 20s candidate window is selected.
3. **1 fps Fingerprinting**: Extract frames at 1 frame per second (`fps=1`) using ffmpeg raw gray pipe.
4. **64-bit dHash Generation**: Grayscale frame converted to $9\times8$ resolution; horizontal difference between adjacent pixels generates a 64-bit unsigned integer hash.
5. **Crop Handling**:
   - `crop_black_bars` in `config.yaml` defaults to `false` because 9x8 downscaled dHash gradients are naturally robust across minor border differences.
   - If enabled, crop detection validates stream dimensions and rejects boxes where both width and height are smaller than stream dimensions (preventing ESRB rating cards or intro logos from masquerading as letterboxing).

---

## 6. Cut Window Selection Algorithm

- Target duration: $W = 20.0$ seconds (range 18–22s).
- **Clips $\le 30.0$s**: Trim directly to $[0, 20.0]$ or center slice.
- **Clips $> 30.0$s**:
  1. Audio profiling: ffmpeg `ebur128=meter=100` extracting momentary LUFS every 100ms. Normalize into $[0, 1]$.
  2. Scene cuts: ffmpeg `select='gt(scene,0.3)',showinfo` extracting visual cuts.
  3. Sliding window scoring:
     $$\text{Score}(t) = 0.75 \cdot \text{MeanAudioEnergy}(t, t+W) + 0.25 \cdot \text{SceneDensity}(t, t+W) - \text{IntroOutroPenalty}(t)$$
     *where $\text{IntroOutroPenalty}$ applies a 5% penalty to the first 5% and last 5% of the video.*
  4. Boundary snapping: Snap to nearest preceding scene cut or audio pause within 1.0s.
  5. Cut-level dedupe check (CP2): Check consecutive frame hashes against rendered/posted cuts. If duplicate, evaluate next-highest scoring window.

---

## 7. Render Layout & Audio Engineering

*Revised after the CP2 render review (all numbers live in `config.yaml` → `render:`).*

### Output Format (verified on every render)
- 1080x1920, **30 fps** (`fps=30` on the composite), H.264 yuv420p.
- Audio: **48 kHz stereo AAC** 192k. `loudnorm` (I=-14, LRA=11, TP=-1.5) is followed by `aresample=48000` (loudnorm runs internally at 192 kHz, which is why CP2 shipped 96 kHz).
- Audio is padded/trimmed to the exact sample count of the video (`apad=whole_len`, `atrim=end_sample`); video is trimmed by frame count (`trim=end_frame`).
- **0.25 s audio fade-out** (`afade=t=out`) at the very end of A and B.
- **Fail-closed verification** (`render.verify_render`): ffprobe each output; the render FAILS if |audio stream duration - video stream duration| > 0.1 s, or fps / sample rate / channels / resolution differ from config, or the length differs from the expected length. Both `render_variant_a` and `render_variant_b` call it before reporting success.

### Canvas Layout (1080x1920, 9:16)
- **Background Layer**: Source video scaled to cover, cropped, blurred (`boxblur=8:8` at 360x640, upscaled), darkened by -22% brightness.
- **Foreground Layer**: Source video centred; a 16:9 source is 1080x608 at y = 656-1264.
- **Instagram safe area**: x 90-990, y 250-1440 (`render.safe_area`). Instagram's own UI covers the top band and everything below ~1440.
- **Font**: static **Montserrat Black** (`assets/fonts/Montserrat-Black.ttf`, OFL in `assets/fonts/OFL.txt`), referenced as `Montserrat Black` (its nameID 1). A test rasterises the same glyphs with the TTF directly and checks libass's ink matches the Black face (and is >1.2x the Bold face), so a silent fallback fails CI.
- **libass sizing**: libass scales the face so usWinAscent + usWinDescent (1109 + 453 units) = Fontsize, so cap height = 700/1562 = **0.448 x Fontsize** at PlayRes 1080x1920.
- **Hook**: Fontsize 150 → **68 px caps measured**; yellow `&H0000FFFF`, black outline 8, shadow 3. Wrapped by measured glyph widths into **max 2 lines** inside the 900 px safe width (minus outline+shadow); a hook that cannot fit 2 lines is shrunk just enough (logged when below `min_font_size` 96). Lines are separate `\an5\pos` events centred in the band y 280-560 (line pitch 1.45 x cap height). Shown for the whole video (A and B).
- **Captions**: 1-3 words per chunk (chunk also breaks on a >0.6 s gap or if it would exceed the safe width), Fontsize 138 → **62 px caps measured**; white with yellow active word, black outline 7, shadow 3; `\an5\pos(540,1300)` (≈68% down, ink y≈1258-1340, never below 1440).
- **No speech fallback**: if whisper finds no speech, Variant A shows the hook only.

### Cut End Snapping: only after a completed sentence (fix pass 2026-11-10, calls #124-#135)
- **The rule** (`pipeline/endings.py`, numbers in `cut.ending`, `RULE_VERSION` 6): a clip may end only in a *sentence break*: after a word ending in `.` `?` `!` where the **audio** pauses (≥ 0.2 s at ≥ 12 dB under the speaker; the end goes in the middle of the earliest such pause), or in a measured quiet stretch ≥ `pause_gap_s` 0.6 s at ≥ 8 dB under the speaker. Every end is in measured quiet. Whisper's word timestamps alone decide nothing when the audio is available (calls #146, #152, #154, #159, #160); without audio, the whisper-only rule applies (. ? ! + ≥ 0.15 s, or ≥ 0.6 s). A plain word gap is never an end.
- **Cut stage**: each scored window is transcribed (faster-whisper `base.en`, same settings as hooks) from its start to max end + `tail_s` 2.0 s, so the gap after the last word is really seen. End = the sentence break nearest the proposed end inside [start+18, start+22] (ties → the earlier). None → the next-best scored window (up to `max_windows` 8; duplicate cuts move on the same way). No window qualifies → the item fails at `cut` with `no_clean_ending`. A window with no speech keeps the ebur128 loudness-trough end. The words are cached in `items.transcript_words`, keyed on the new window and model with an `ending` verdict, so hooks and render reuse them.
- **Render stage** (`analyze_clip` / `render.snap_render_end`): the same rule inside the cut, aimed at the cut's end, so it never ends before the last completed sentence; the cut's own end counts only when the cut stage verified it. **A4** may only choose among these safe ends; **A3** only drops silence that sits in a sentence break and touches no word. The end fade never reaches back into the last word.

### Variant B: Freeze-Frame Voiceover (replaces ducking)
- **VO Script**: one punchy 3-6 s line (Gemini, grounded); **Kokoro TTS** (`am_adam`).
- **Video**: the cut's FIRST frame is held as a still (`tpad=start=N:start_mode=clone`) for `freeze = VO duration + 0.3 s` (rounded up to a whole frame), then the cut plays from its start.
- **Audio**: VO only during the freeze (padded with silence to exactly the freeze), then the cut's original audio at full level (`concat`), then the shared loudnorm/48 kHz/fade tail. No sidechaincompress/amix (sidechaincompress ended at its shortest input, which cut CP2's B audio to 3.1 s).
- **Length**: B = freeze + cut (≈ 23 s for a 20 s cut), intended.
- **Captions**: VO words (whisper on the VO wav, else evenly spread script words) during the freeze, then the cut's speech captions offset by the freeze.

### Verification Script
- `python scripts/render_check.py` renders A_v2/B_v2 for every `samples/cp2/<id>/` (cut.mp4, vo.wav, item.json) or, if no pack exists, a synthetic 20 s clip; writes `contact_sheet.png` (A @1 s, B mid-freeze, B freeze+2 s, A last 0.5 s, plus A/B waveforms) and `render_report.json`, prints the ffprobe table and measured cap heights, exits non-zero on any failure.

---

## 7B. Style Layer — Edit Effects as Config-Toggled Presets (P0–P3)

Every edit effect is an **independently toggleable flag**; presets are declared in
`config.yaml` as deltas over a base preset and resolved into a flat `{flag: bool}` map.
**All numbers live in `config.yaml` → `style:`** (code defaults in `pipeline/style.py`
keep an older config loading). The pipeline default is **P0** and does not change until a
preset is picked.

| Preset | Base | Adds | Meaning |
|---|---|---|---|
| **P0** Control | — | `zoom_fill` | the CP2 renders plus F1 framing |
| **P1** Clean | P0 | `keyword_color`, `keyword_emoji`, `progress_bar`, `credit_tag`, `caption_pop`, `dead_air`, `loop_end` | C1–C4, M1, A3, A4 |
| **P2** Punchy | P1 | `hook_pop`, `punch_ins`, `sfx` | M2, M3, A1 |
| **P3** Full hype | P2 | `slow_push`, `music_bed` | M4, A2 |

A test asserts P0 ⊂ P1 ⊂ P2 ⊂ P3, that P0 enables **only** `zoom_fill`, and that
`style.preset` is still `P0`.

### Module layout and why it is split this way

| Module | Owns |
|---|---|
| `pipeline/style.py` | flags, presets, every style number, F1 geometry, safe-area placement |
| `pipeline/edits.py` | timeline analysis: segments (F1), punch-ins (M3), dead air + `Timeline` (A3), loop end (A4), the zoom program |
| `pipeline/overlay.py` | ASS tags (C1, C4, M1, M2), the C3 progress-bar event, the C2 emoji plan and overlay chain |
| `pipeline/audio_mix.py` | A1 SFX synthesis/cues/gain staging, A2 music chain and ducking |
| `pipeline/keywords.py` | C1 keywords: Gemini cache + rule fallback |
| `pipeline/plan.py` | `analyze_clip` (probe once) → `StylePlan` (one per variant) |
| `pipeline/rendercore.py` | render settings, libass font metrics, text layout, ffprobe readers — shared by the planner and the renderer (re-exported from `render`) |
| `pipeline/render.py` | ASS assembly and the filtergraph; consumes a `StylePlan` and never re-derives a timestamp |

**Why:** planning and graph-building have different failure modes. Planning is pure
arithmetic over probed data, so it is unit-testable with synthetic numbers and no ffmpeg;
the filtergraph is where label collisions and ordering bugs live, so `render.py` owns
**all** stream labels and the helpers return fragments with the in/out labels passed in.
**Where this could have gone wrong, and what covers it:**
1. *Timeline drift* — A3 and the Variant B freeze both move time, and captions, punch-ins,
   SFX, emojis, music ducking and the progress bar all carry timestamps. One `Timeline`
   object is computed once and **everything** maps through `Timeline.remap` plus the same
   freeze offset; the existing ffprobe guard catches any residual A/V mismatch.
2. *Filtergraph fragility* — the helper modules emit `{IDXn}` placeholders that only
   `render.py` resolves to real ffmpeg input indices, and all 8 preset × variant
   combinations are really rendered in the synthetic verification.
3. *Config sprawl* (≈70 new numbers) — one `style:` block, one sub-block per flag, named
   exactly after the flag, deep-merged over code defaults.
4. *Preset drift* — presets are deltas, resolved by one function, with the subset test
   above.

### F1 — Zoom-to-fill framing (on in every preset)

- Foreground sizes, derived (not hand-placed): fit `1080×608`; fill
  `1080×852` = `even(608 × fill_scale 1.40)`. Bands: **fill y 534–1386**, **fit y 656–1264**.
- The source sits on a **supersampled transparent canvas** (`supersample 1.5` →
  `1620×1278`, content `1620×912`) in `yuva420p`; `zoompan` zooms between
  **z = 1.0 (full-width fit)** and **z = `fill_zoom` = 1278/912 = 1.40132** (fills the band,
  sides centre-cropped). At `fill_zoom` the visible width is 1/1.4013 = 71.4 % of the frame
  and nothing is upscaled past the source. A **constant** zoom (the common P0 case) skips
  `zoompan` entirely and uses `crop`+`scale`.
- **Per-segment decision**: split at scene cuts (ignoring cuts closer than
  `min_segment_s 1.2`), take the mean frame-difference per segment
  (`motion_step_s 0.2`), and label it `fit` below `motion_threshold 0.018`, else `fill`.
  Adjacent same-mode segments merge. A short **cross-scale** of `crossfade_s 0.25` sits on
  every mode change. The Variant B freeze is a static segment (`fit`, and M4 pushes across it).
- **Safe-area re-placement** (`edge_clearance_px 40`): the caption centre is the **nearest
  valid y to `caption.center_y` 1300** whose ink box is entirely clear of *every* video edge
  the clip uses and inside the safe area; the hook band is centred in the room above the
  highest edge; the credit tag goes to the bottom-left of the video band and is pushed
  below (then above) the captions if it would touch them. Resulting placements:

  | Modes in the clip | Video edges | Caption centre (ink) | Hook centre (2-line ink) | Credit baseline (ink) |
  |---|---|---|---|---|
  | fill only | 534, 1386 | 1300 (1254–1346) | 372 (269–475) | 1421 (1370–1421) |
  | fit only | 656, 1264 | 1350 (1304–1396) | 433 (330–536) | 1248 (1197–1248) |
  | fill + fit | 534, 656, 1264, 1386 | **1178 (1132–1224)** | 372 (269–475) | 1311 (1248–1311) |

  In a mixed clip there is no y that is both below the fit edge (1264) and 40 px clear of
  the fill edge (1386) inside the safe area, so the captions move **up** to 1178 — on the
  footage in both modes, which reads better than sitting half on the blurred background.
- **Hook line spacing**: `line_spacing 1.75` (≈118 px pitch, ≈29 px between the two
  outlines; 1.45 left only ≈9 px) **plus** a floor of `min_line_gap_px 18`, because
  outline/shadow are absolute pixels — on a hook that had to shrink to size 68 a pure
  cap-height multiple collapsed the gap to **3 px**.

### C1–C4 caption extras (P1–P3)

- **C1 keyword colour**: the Gemini hook call now also returns `keywords` (3–6), cached in
  `items.keywords` as JSON. Yellow `&H0000FFFF` always marks the word being spoken; other
  keywords in the chunk are green `&H0000FF00`; the rest stay white. Zero-network fallback:
  tokens containing a digit, the baked GTA name/place list, plus
  `style.keyword_color.extra_terms`.
- **C2 keyword emojis**: `keyword → Twemoji codepoint` map in config; **max one per
  `min_gap_s 4.0`**, `size_px 110`, held `hold_s 1.6`, rising `rise_px 24` over
  `rise_ms 120`, `gap_above_caption_px 28` above the caption ink. Composited as PNG
  overlays because **libass cannot draw colour emoji**; only the 14 codepoints the map uses
  are committed (CC-BY 4.0, credited in `README.md`). `overlay` must use
  `repeatlast=1` — `eof_action=pass` drops a single-frame PNG after one frame.
- **C3 progress bar**: a `height_px 10` yellow bar at y 0, drawn as an **ASS rectangle**
  scaled by `\t(0, duration_ms, \fscx100)`, because `drawbox`'s `t` is its *thickness*
  parameter, not a time variable — there is no way to animate a box width in ffmpeg 6.1.
- **C4 credit tag**: `via {channel}`, Fontsize 71 → **32 px caps measured**, white at
  `\alpha&H33&` (≈80 % opacity), `\an1` at the bottom-left of the video band, inset by
  outline+shadow so the ink stays inside x ≥ 90. Its box is measured **ascender-to-descender**
  (mixed case), which is ≈20 px taller than a cap-height box — using cap height broke the
  "never overlaps the captions" guarantee.

### M1–M4 motion

- **M1 caption pop-in**: `\fscx80\fscy80\t(0,60,\fscx115\fscy115)\t(60,160,\fscx100\fscy100)`
  on the **first event of each chunk** (not every word). The 115 % peak is fed into the
  safe-area placement, so the transient size respects the edges too.
- **M2 hook pop + shake**: `\fscx55 → 100` over `scale_ms 250`, plus a decaying
  `\frz` shake of `±shake_deg 1.5°` in `shake_steps 4` over `shake_ms 400`, rotating about
  `\org(540, hook_centre)` so both lines move together instead of scissoring.
- **M3 punch-ins**: `zoom 1.12`, `ease_in_s 0.12` / `hold_s 0.8` / `ease_out_s 0.12`
  (cosine ease, not a step), at least `min_gap_s 3.0` apart, `max_count 4`, never before
  `first_at_least_s 1.5`, ranked by ebur128 momentary loudness with scene cuts as
  secondary candidates.
- **M4 slow push-in**: `1.00 → zoom_to 1.06` across each static segment and the B freeze.
- All three fold into **one zoompan `z` expression**, `z(t) = base(t) × (1 + 0.12·env(t))`,
  built as half-open gated linear pieces; `edits.zoom_value` is the Python mirror the
  tests assert against.

### A1–A4 audio

- **A1 SFX**: boom on the hook's first frame, then a whoosh `whoosh_lead_s 0.06` before a
  punch-in **only if** it starts ≥ `min_gap_s 5.0` from every other cue, at most
  `max_per_clip 2` cues per clip (calls #137-#139); other punch-ins zoom silently. Each cue is gain-staged so its peak lands `headroom_db 6.0` **under the clip's
  measured speech peak** (`volumedetect`). The CC0 sources (Kenney.nl, OpenGameArt,
  Freesound, Pixabay) are all blocked from the build sandbox (proxy `403` on `CONNECT`), so
  both cues were **synthesized with ffmpeg** and committed at −1.0 dBFS; see
  `assets/sfx/LICENSES.md`.
- **A2 music bed**: CC0 track at `level_db −24.0`, ducked a further `duck_db −8.0` during
  speech with `duck_ramp_s 0.08` ramps, from whisper word timestamps merged into spans
  (`speech_merge_gap_s 0.3`). **No `sidechaincompress`** anywhere — it is `volume` automation
  (`eval=frame`). The bed is `apad=whole_len=N,atrim=end_sample=N` to the clip's **exact**
  sample count. freepd.com is blocked from the sandbox, so A2 was built and tested against a
  generated tone and **no synthetic "music" track is committed**;
  `scripts/fetch_assets.py` downloads the real CC0 bed on the laptop.
- **A3 dead-air removal**: drop stretches over `min_silence_s 0.4` that are **both** below
  `loudness_threshold_lufs −45.0` **and** below `motion_threshold 0.012`, shrunk by
  `keep_pad_s 0.12` at each end, longest first, stopping before the output would fall below
  `min_output_s 15.0`. Cuts are made with `trim`/`atrim` + `concat` (frame- and
  sample-exact) and every timestamp is remapped through `Timeline.remap`; words inside a cut
  span are dropped. `loudness_lag_s 0.2` re-centres ebur128's momentary window, which
  reports the 400 ms **ending** at `t`.
- **A4 loop-friendly ending**: inside `[cut.min_duration, min(cut.max_duration, cut length)]`,
  pick the frame whose **dHash is closest to frame 1** (`search_step_s 0.2`), subtracting
  `pause_bonus_bits 6` for a candidate in a speech pause. **No video fade**; the audio fade
  drops to `audio_fade_s 0.15` (from 0.25) when A4 is on.

### Synthetic verification

`python scripts/style_check.py` builds a 22 s source that exercises everything — 0–8 s a
static, text-heavy card; 8–12 / 12–16 / 16–22 s moving footage with scene cuts at 8/12/16 s;
speech-like bursts with a **1.0 s silent gap at 5.0–6.0 s** over the still card; loud peaks
at 9.4 / 13.4 / 17.4 s — then renders **P0–P3 × A/B** and checks every output against the
existing ffprobe guard.

| Preset | A (video/audio s) | B (video/audio s) | Segments | Punch-ins | Emoji | SFX | Dead air dropped | Audio fade |
|---|---|---|---|---|---|---|---|---|
| P0 | 22.000 / 22.000 | 25.500 / 25.500 | fit + fill | 0 | 0 | 0 | — | 0.25 s |
| P1 | 21.167 / 21.200 | 24.667 / 24.700 | fit + fill | 0 | 3 | 0 | 5.12–5.78 s | 0.15 s |
| P2 | 21.167 / 21.200 | 24.667 / 24.700 | fit + fill | 4 | 3 | 5 | 5.12–5.78 s | 0.15 s |
| P3 | 21.167 / 21.200 | 24.667 / 24.700 | fit + fill | 4 | 3 | 5 | 5.12–5.78 s | 0.15 s |

All 8 outputs: 30 fps, 48 kHz stereo, 1080×1920, |audio − video| ≤ 0.033 s (tolerance 0.1 s).
F1 split the source at the real boundary (static card → `fit`, motion 0.000; moving footage
→ `fill`, motion 0.041). `samples/synthetic/presets_sheet.png` is the committed evidence;
the mp4s are not committed.

---

## 8. Grounded Hooks Specification (CP2)

1. **Context Provided to Gemini**:
   - The transcript of the selected 20s cut window.
   - 4 representative frames extracted from the cut window.
   - The original source video title.
2. **Hook Styles**: `question`, `curiosity`, `hype_analysis`.
3. **Verification Grounding Check**:
   - Every generated hook must pass a strict verification prompt: *"Does the clip actually show or discuss this? Reply strictly with YES or NO."*
   - If `NO`: Regenerate with a temperature adjustment.
   - If 2 consecutive failures: Fall back automatically to a verified plain question hook (e.g. "WHAT HAPPENED HERE?").

---

## 9. Social Media Publishing & Crash Safety (CP3)

### Two-Phase Resumable Posting
1. **Phase 1: Container Creation**:
   - Call Meta Graph API `/media` (or FB `/video_reels`).
   - Save the returned `remote_container_id` in the `posts` table immediately.
   - Update post status to `container_ready`.
2. **Phase 2: Video Bytes Upload & Publish**:
   - Stream video bytes to `rupload.facebook.com`.
   - Update post status to `publishing`.
   - Issue `/media_publish`.
   - Update post status to `live` with `remote_id` and `remote_url`.
3. **Crash Recovery in `publishing`**:
   - On pipeline startup or resume, rows in `publishing` inspect remote container status and recent account media first.
   - If published: record remote ID and mark `live`.
   - If container failed: re-upload container.
   - If state is unknown: **never re-publish**; mark `failed` and alert Telegram.
4. **Partial Platform Retry**:
   - An item is marked `posted` only when both enabled platform rows (IG + FB) are `live`.
   - If FB fails while IG succeeds, the subsequent retry processes FB only.

### Creator Credit Formatting
Templates live in `config.yaml` → `posting.caption_templates` (code defaults in
`posting.CAPTION_TEMPLATES`); an unknown source uses the YouTube one. Reddit was dropped as a
source (Decision A), so its template was removed.
- **YouTube items**:
  `{hook_text}\n\nCredit: {author_name}\n.\n#gta6 #grandtheftauto #gta #gaming #rockstargames`
- **Twitch items**:
  `{hook_text}\n\nClipped from {author_name} on Twitch\ntwitch.tv/{channel}\n.\n#gta6 #grandtheftauto #gta #gaming #rockstargames #twitchclips`
  (`{channel}` is the broadcaster name lower-cased with non-login characters removed; the link line
  is dropped when nothing is left, e.g. a non-Latin display name).

Implemented as `posting.build_caption()`; `posting.caption_for_item()` prefers the caption the
render stage already stored and falls back to the template. The caption is frozen onto each
`posts` row (`posts.caption_text`) when the row is created, so a retry after a crash sends exactly
the caption the first attempt did - which is also what the recent-media crash check matches on.
Capped at Instagram's 2,200 characters.

---

## 9b. CP3 Implementation (Telegram approval + Meta posting)

### Telegram approval card (`pipeline/telegram_bot.py`)
- **One self-editing card per item, strictly one item at a time.** The open card is recorded in
  `kv` under `telegram:card` as `{"mid", "item_id", "extra_mids"}`. `ensure_card()` returns
  immediately while that key holds a card, so the next item is only sent after the current one is
  decided. Items are offered highest `discovery_score` first, and an `awaiting_approval` item whose
  card was lost with a crashed process is re-sent before any fresh item.
- **What the card carries**: Variant B as the video message with the buttons, Variant A as a second
  message without buttons, and in the caption the hook, the authenticity tag, the source + source
  URL, the creator and the discovery score. A render over the Bot API's 50 MB limit degrades to a
  text card rather than failing.
- **Buttons**: `Approve` -> `approved` / `approval_type='approved'` with `chosen_variant` left NULL,
  because the CP4 scheduler assigns A/B, not the reviewer. `Reject` -> `rejected` /
  `rejection_reason='user_rejected'`. `Approve A only` -> `approved` /
  `approval_type='approved_a_only'`, `chosen_variant='A'` and `items.ab_excluded=1`, logged to
  `stage_log` and excluded from the A/B comparison.
- **Stale-button protection**: a click only counts when its `message_id` equals the recorded `mid`
  AND its item id equals the recorded `item_id`. Anything else answers "This card is out of date"
  and changes nothing. A click from any chat other than `TELEGRAM_CHAT_ID` is ignored entirely.
- **Throttled edits**: `Editor` sends at most one edit per `telegram.edit_throttle_gap_s` per
  message, the latest text winning; a button tap skips that gap but never a 429's `retry_after`,
  which holds every edit back and keeps the pending text.
- **The long poll runs inside the runner's loop**: `run_approval_stage()` does one `ensure_card()`,
  one `getUpdates` round and one `editor.flush()` per cycle. The `getUpdates` offset is persisted in
  `kv` under `telegram:offset` before each update is handled, so a crash skips an update and never
  repeats one.
- **`approval.mode: auto`** skips Telegram entirely and applies `approval.auto_quality_gate`:
  both renders present, hook present and grounded, and ffprobe readable within the duration caps.
  A pass becomes `approved` / `auto_approved`; a failure becomes `rejected` with the reason.
- **Failure-only alerts** go to the same chat through `ApprovalBot.alert(text, key=...)`; the same
  `key` is not re-sent within an hour, so one broken item cannot flood the chat.

### Meta posting (`pipeline/posting.py`)
- Instagram Reels by **resumable upload from the local file**: `POST /{ig-user-id}/media` with
  `upload_type=resumable`, then the bytes to `rupload.facebook.com/ig-api-upload/{ver}/{container}`,
  then `GET /{container}?fields=status_code` until `FINISHED`, then `POST /{ig-user-id}/media_publish`.
- Facebook Page Reels by the **`video_reels` start / upload / finish** flow:
  `upload_phase=start` -> the bytes to `rupload.facebook.com/video-upload/{ver}/{video-id}` ->
  `upload_phase=finish` with `video_state=PUBLISHED`.
- **Status order is exactly** `uploading -> container_ready -> publishing -> live`. The container /
  video id is written to `posts.remote_container_id` *before* any byte is uploaded, and the row is
  moved to `publishing` with `publish_started_at` *before* the publish call is issued.
- **Crash recovery in `publishing`** (`remote_state()` / `settle()`), called from the runner before
  anything new is published:
  - `published` -> record the remote id / permalink and mark `live`. For Instagram that is a
    container reading `PUBLISHED`, or a caption-and-time match in the account's recent media; for
    Facebook, `publishing_phase.publish_status == published` or `video_status in (ready, published)`.
  - `not_published` -> only on positive proof that publishing is impossible: the container is
    `ERROR` / `EXPIRED`, the Facebook video is `error` / `upload_failed` / `expired`, or no container
    id was ever saved. Only then is a fresh container started.
  - `unknown` -> **mark `failed` and alert, never publish again.** A container that merely reads
    `FINISHED` with no matching recent media counts as unknown, because it cannot tell a publish
    that never happened from one whose media has not yet surfaced in `/media`.
- **Partial platform retry**: a row already `live` is skipped, so a failed Facebook row retries
  Facebook alone. The item becomes `posted` only when *every* enabled platform row is `live`.
- **Dry run** (`app.dry_run`, true by default): `posting.dry_run_mode: plan_only` (the default)
  sends no write request at all - the whole state machine runs, every request that would go out is
  logged and written to `media/dry_run/post_<id>_<platform>.json` with every token redacted, and the
  row ends `dry_run`. `up_to_publish` additionally creates the real container and uploads the real
  bytes and skips only the publish call; that touches the live account, so it is opt-in. A dry run
  never advances the item to `posted`, so the real post still happens once dry run is switched off.
- **Pacing, retries and limits**: every request waits `posting.meta_min_gap_seconds` after the
  previous one, the moment being kept in `kv` under `meta_api:next_free` so the gap holds across
  processes. 5xx and network errors retry with capped exponential backoff
  (`max_retries`, `retry_backoff_s`, `retry_backoff_cap_s`); a row is given up on after
  `max_post_attempts`. An HTTP 429 or a Graph block code (4, 17, 32, 613, 80004) pauses the whole
  `meta_graph` source for `posting.rate_limit_pause_hours` through `rate_limit` and alerts Telegram;
  the pause is checked before every posting pass.
- **Secret hygiene**: `posting.redact()` strips `access_token=...` and the token values themselves
  out of every message that reaches a log, the database, a dry-run plan or Telegram;
  `telegram_bot.redact()` does the same for the bot token.

### The laptop dry run (`scripts/cp3_dry_run.py`)
One command, no prompts: `python scripts/cp3_dry_run.py`. It sends one real Telegram card (using the
highest-scoring existing render, or a synthetic 1080x1920 pair built with ffmpeg when the database
holds none), validates the Meta tokens with read-only GETs (`/debug_token`, the IG user, the IG
publishing limit, the Page, the Page's linked IG account) and then runs the real posting flow in
dry-run mode. A token missing any of the nine permissions in `REQUIRED_SCOPES` (call #169), a token
that is not a valid PAGE token for `FB_PAGE_ID`, or an `IG_USER_ID` that is not the Page's linked
Instagram account fails the run (call #170). It cannot publish:
`app.dry_run` and `plan_only` are forced for the process, a `GuardedTransport` raises on any
non-GET request and on any URL or body naming a write endpoint, and the posting flow is given a
client built around that guarded transport. Exit code 0 when every check passed, 1 otherwise.

---

## 10. Scheduling & Multi-Armed Bandit Learning (CP4)

### Named Timezone Profiles
Two named profiles in `config.yaml`, toggled by `posting.profile`:
- `"local"`: `Asia/Kolkata` slots at `13:00` and `18:00`.
- `"server"`: `America/New_York` slots at `18:00` and `21:00`.
Each post's slot is stored in `posts.slot_datetime` as a full UTC datetime string (`2026-11-10T13:00:00Z`).

### A/B Multi-Armed Bandit Allocation
- **Week 1**: 50/50 random variant allocation.
- **Post-Week 1**: Multi-armed bandit with a **20% exploration floor** (each variant guaranteed at least 20% allocation) to prevent premature lock-in on noisy early data.
- **Normalized Multi-Platform Metrics**:
  - `ab_learning` cache is wiped and rebuilt from `metrics` on every run.
  - Views on Instagram and Facebook are normalized to percentile ranks within each respective platform before averaging, preventing Instagram's higher view counts from drowning Facebook engagement.

### 10b. CP4 implementation (what is built; details in *Calls I made* #84-#122 and #140-#145)
- **Scheduler** (`pipeline/scheduler.py`): `posting.profile` slots stored as UTC; missed slots re-slotted one per slot (never sent late, never burst); at most one new post per pass and never within `min_gap_hours` of a live post.
- **Variant**: drawn when the slot comes due (`posting.decide_variant`), from week 1 (50/50 until 7 days after the first live post) or the bandit (leader 80 % / other 20 % once both variants have 3 measured items); frozen once a row begins uploading. "Approve A only" stays A.
- **Approval switch**: Telegram cards until 7 days after the first live post, then the auto quality gate, with one Telegram notice (`approval.switch_to_auto_after_days`).
- **Analytics** (`pipeline/analytics.py`): 24 h and 72 h insight snapshots inside their windows; `ab_learning` rebuilt from `metrics` every cycle, ranked within each platform.
- **Cleanup** (`pipeline/cleanup.py`): sources after render, renders 7 days after the post, rejected/failed media after 3 days; never a file an open post needs.
- **Twitch** (`pipeline/twitch.py`): Helix top clips, enabled automatically from 2026-11-19 (UTC), skipped with one alert without credentials.
- **Runner**: every stage isolated; `loop` holds the lock; Healthchecks.io ping after each fully successful cycle (`pipeline/healthcheck.py`).
- **Server**: `deploy/gta6-pipeline.service` + README "Moving to a server".
- **Proof**: `python scripts/cp4_dry_run.py` simulates 10 days end to end with every network call faked (summary in HANDOFF.md).

---

## 11. Configuration File Shape (`config.yaml`)

```yaml
app:
  brand_name: "GTA 6 Clipping Pipeline"
  dry_run: true # Defaults to true: nothing posts publicly until CP3 confirmation

sources:
  youtube:
    enabled: true
    weight: 1.0
    queries:
      - "GTA 6 gameplay"
      - "GTA 6 leak"
      - "GTA VI trailer breakdown"
      - "Grand Theft Auto VI secret"
    max_results_per_query: 15
    max_duration_seconds: 600
  reddit:
    enabled: true
    weight: 1.25
    subreddits:
      - "GTA6"
      - "GTA"
    listing: "hot"
    limit: 25
  twitch:
    enabled: false # Active from launch (19 Nov 2026)
    game_name: "Grand Theft Auto VI" # Game ID resolved dynamically via Helix API
    weight: 1.10
    limit: 20
  kick:
    enabled: false
    # Official API at api.kick.com/public has no clips endpoint.
    # Website clips require scraping around undocumented endpoints.
    # Disabled until an official clips endpoint is released.

discovery:
  min_age_hours: 0.5
  half_life_hours: 36.0
  rolling_pool_size: 100
  score_formula: >
    percentile_rank * (0.5 ** (age_hours / half_life_hours)) * source_weight

dedupe:
  crop_black_bars: true
  fps: 1
  hamming_threshold: 8 # Max bit difference per frame
  consecutive_frames: 3 # N consecutive frame matches constitutes duplicate

buffer:
  target_ready_items: 6 # Pull-based cap: process only enough to maintain 6 ready items

cut:
  target_duration: 20.0
  min_duration: 18.0
  max_duration: 22.0
  short_clip_threshold: 30.0
  scene_threshold: 0.3
  audio_weight: 0.75
  scene_weight: 0.25
  intro_outro_penalty_pct: 0.05

render:
  width: 1080
  height: 1920
  fps: 30
  audio_sample_rate: 48000
  audio_channels: 2
  audio_bitrate: "192k"
  fonts_dir: "assets/fonts"
  font_path: "assets/fonts/Montserrat-Black.ttf"
  font_name: "Montserrat Black" # static Black (900) face; nameID 1 of Montserrat-Black.ttf
  whisper_model: "base.en"
  target_lufs: -14.0
  voiceover_target_duration_s: 4.5
  # Variant B freeze-frame format: first frame held for VO duration + pad, then the cut plays at full level.
  freeze_pad_s: 0.3
  end_fade_s: 0.25 # audio fade-out at the very end of A and B
  av_duration_tolerance_s: 0.1 # verification fails if |audio - video| stream duration exceeds this
  # Instagram-safe text area (pixels on the 1080x1920 canvas).
  safe_area:
    x_min: 90
    x_max: 990
    y_min: 250
    y_max: 1440
  # libass sizes the font so (winAscent + winDescent) = Fontsize; for Montserrat that gives
  # cap height = 0.448 x Fontsize. 138 -> ~62 px caps, 150 -> ~67 px caps (measured).
  caption:
    font_size: 138
    outline: 7
    shadow: 3
    max_words: 3
    max_gap_s: 0.6
    center_y: 1300
    color: "&H00FFFFFF" # white
    highlight_color: "&H0000FFFF" # yellow (ASS is &HAABBGGRR)
  hook:
    font_size: 150
    min_font_size: 96 # only used if a hook cannot fit 2 lines inside the safe width
    outline: 8
    shadow: 3
    max_lines: 2
    band_top_y: 280
    band_bottom_y: 560
    line_spacing: 1.45 # line pitch as a multiple of cap height
    color: "&H0000FFFF" # yellow

approval:
  mode: "telegram" # 'telegram' or 'auto'
  auto_quality_gate:
    require_both_renders: true
    require_ffprobe_pass: true
    require_grounded_hook: true
    max_duration_seconds: 22.0

posting:
  profile: "local" # 'local' or 'server'
  profiles:
    local:
      timezone: "Asia/Kolkata"
      slots: ["13:00", "18:00"]
    server:
      timezone: "America/New_York"
      slots: ["18:00", "21:00"]
  min_gap_hours: 3.0
  meta_min_gap_seconds: 3.0
  daily_post_target: 2
  platforms:
    instagram: true
    facebook: true

telegram:
  enabled: true
  api_base: "https://api.telegram.org"
  poll_timeout_s: 30
  edit_throttle_gap_s: 2.5

autopilot:
  alert_every_hours: 12
  bandit_min_exploration_floor: 0.20

retention:
  purge_downloads_immediately: true
  purge_renders_after_days: 7
  purge_rejected_failed_after_days: 3

orchestrator:
  loop_interval_seconds: 60
  stale_lock_seconds: 120.0
  heartbeat_interval_seconds: 30.0
```

---

## 12. Kick Streaming API & Cloud IP Blocking Notes

### Kick Streaming API Assessment
Kick does provide an official public API (`api.kick.com/public`) requiring application credentials, but it currently lacks a clips endpoint. Clips are only served via undocumented website endpoints, which constitutes scraping. Per project rules (*"do not scrape around it"*), Kick remains disabled until official clips support is released.

### Cloud Server IP Blocking Note
Upon deploying to a cloud Linux server (AWS/Hetzner/DigitalOcean), YouTube frequently blocks datacenter IP ranges (HTTP 429 / CAPTCHA). Per project rules (*"do not add cookies or proxies to get around it"*), the pipeline detects blocking, records rate-limit cooldown in the database, emits a Telegram alert, and gracefully pauses affected calls while remaining crash-safe and resumable.

---

## 13. Calls I Made (Comprehensive Engineering Audit)

1. **Virtual Environment Python Version**: Selected `Python 3.11` for `.venv` because `Kokoro TTS` requires `numpy 1.26.4` which lacks binary wheels for Python 3.14 on Windows and triggers C-compilation failures, whereas Python 3.11 provides fully compatible prebuilt binary wheels for Kokoro, Torch, and faster-whisper. (Server README explicitly notes Python 3.11 requirement).
2. **Reddit Dropped as Source (Decision A)**: Reddit ended unauthenticated public `.json` access globally in 2026, returning `403 Blocked by network security`, and developer API applications are closed. Dropped entirely from config, discovery, and `.env.example`.
3. **Official Channel Down-Weighting (Decision B)**: Assigned `official_channel_weight: 0.20` to official publisher/platform channels (Rockstar Games, PlayStation, Xbox, Netflix) matched strictly by channel ID, ensuring fresh creator videos rank higher.
4. **YouTube Real Metadata Fetching**: Rate-limited individual video extraction (0.5s pause) to pull true `upload_date` and `view_count` instead of flat-playlist estimation.
5. **YouTube Date-Sorted Search**: Added `sp=CAI%253D` search queries alongside relevance search to surface fresh creator uploads.
6. **Download Dedupe Coverage Threshold**: Set to `0.65` (65% temporal alignment overlap of shorter video) to catch full reposts while preventing reaction videos sharing brief trailer footage from being falsely flagged.
7. **Crop Detection Logic**: Set `crop_black_bars: false` by default for raw 9x8 gradients; smart cropdetect rejects boxes where both width and height are smaller than stream dimensions (preventing ESRB rating cards or intro logos from masquerading as letterboxing).
8. **Heartbeat Lock Timing**: Configured a 30s heartbeat refresh interval and 120s stale remote timeout, with immediate dead PID termination check.
9. **Audio/Scene Cut Weights**: Standardized cut scoring to 0.75 audio loudness weight and 0.25 visual scene-cut density.
10. **Intro/Outro Penalty Window**: Applied a 5% duration penalty to the beginning and end of long-form videos to avoid channel intros and outro slates.
11. **Static Video Density Floor**: Added 0.05 scene changes/second minimum floor (CP2) to reject static podcast/cover art uploads.
12. **Target Cut Duration Range**: Hard-bounded between 18.0s and 22.0s with a 20.0s target window.
13. **YouTube Query Duration Ceiling**: Capped search results at 600s (10 minutes) to avoid downloading multi-hour livestreams.
14. **Local Whisper Model**: Selected `base.en` as default for sub-second CPU transcription latency while retaining high word-timestamp accuracy.
15. **Voiceover Line**: 3–6s via local Kokoro TTS, played over a freeze of the cut's first frame (freeze = VO + 0.3 s); no ducking (superseded the CP2 -14 dB sidechain design).
16. **Font Choice**: Static `Montserrat-Black.ttf` (weight 900, OFL) from JulietaUla/Montserrat `fonts/ttf/`, referenced as `Montserrat Black`; `Montserrat-Bold.ttf` kept only for script labels.
17. **Audio Loudness Target**: Standardized at $-14.0\text{ LUFS}$ per Instagram and Facebook Reels ingest specs.
18. **Ready Buffer Size**: Fixed at 6 ready rendered items to prevent unbounded disk usage and conserve Gemini API calls.
19. **Bandit Exploration Floor**: Set minimum exploration floor at 20% for each variant to guarantee ongoing statistical validation.
20. **Failed Media Retention**: Set retention for rejected and failed item media at 3 days before disk purge.
21. **Output Format**: 30 fps (`fps=30` on the composite) and 48 kHz stereo AAC 192k (`aresample=48000` after loudnorm).
22. **A/V Verification Tolerance**: render FAILS if audio and video stream durations differ by > 0.1 s, or fps / sample rate / channels / resolution / expected length differ from config.
23. **End Snap**: word gap ≥ 0.25 s, else ebur128 trough, within 18-22 s (render stage: 18 s → cut length); end placed ≥ 0.1 s after the last word, ≤ 0.4 s into the gap.
24. **End Fade**: 0.25 s audio fade-out at the very end of A and B.
25. **Freeze Pad**: Variant B freeze = VO duration + 0.3 s, rounded up to a whole 1/30 s frame. B length = freeze + cut (≈ 23 s).
26. **Caption Size**: ASS Fontsize 138 → 62 px caps measured (target 60-65). The brief's "Fontsize 88-92" estimate would give only ~40 px with this font, because libass scales Montserrat by its win ascent+descent (cap = 0.448 x Fontsize); the measured pixel target was used.
27. **Hook Size**: ASS Fontsize 150 → 68 px caps measured (target 65-70); max 2 lines in the 900 px safe width; shrinks only when a hook cannot fit (e.g. a 43-character hook → Fontsize 91). Typical 4-6 word hooks fit at full size.
28. **Outlines / Shadow**: captions outline 7, hook outline 8, shadow 3 on both (semi-transparent black).
29. **Caption Chunking**: 1-3 words; new chunk on > 0.6 s gap or if the chunk would exceed the safe width.
30. **Safe Area**: x 90-990, y 250-1440. Captions centred at y 1300 (ink ≈ 1258-1340); hook centred in band y 280-560 (2-line ink ≈ 328-510).
31. **Lazy ML Imports**: `faster_whisper`, `kokoro_onnx` and `soundfile` are imported inside the functions that use them, so the pipeline and the zero-network tests import without the ML stack.
32. **Config Key Fix**: `cut.select_best_cut_window` now reads `min_duration` / `max_duration` / `target_duration` (the keys in config.yaml), falling back to the old `*_seconds` names.

### Style layer (see §7B)

33. **Bug found and fixed — ebur128 never produced a loudness profile.** `extract_audio_loudness_profile` passed `ebur128=meter=100`, but `meter` is the **visual meter's LU range and only accepts 9–18**, so the filter failed to initialise and the function returned an **empty list on every call** — silently. Consequences across CP1/CP2: the cut window's audio-energy term (weight **0.75**) fell back to a constant `0.5` for every window, and every loudness-trough end-snap fallback was dead. Now `ebur128=metadata=1,ametadata=print:key=lavfi.r128.M` with both the log line and the frame metadata parsed (221 samples over 22 s), and an empty result is logged instead of swallowed.
34. **Bug found and fixed — `nan` during digital silence.** ebur128 reports `lavfi.r128.M=nan` (and `-inf`) over exact silence, which the numeric regex skipped, punching a hole in the profile at precisely the moments A3 looks for. `nan`/`-inf` now map to the **−70 LUFS** floor.
35. **Style layer architecture**: planning (`style`/`edits`/`plan`/`keywords`) is separated from graph-building (`render`), with the shared primitives in `rendercore` so the planner can measure text and probe media without importing the renderer. `render.py` owns **every** stream label; helper modules return fragments with in/out labels passed in and `{IDXn}` placeholders only `render.py` resolves. Rationale and the four failure modes this covers are in §7B.
36. **Presets as deltas**: each preset names a `base` and an `enable` list; one resolver walks the chain into a flat flag map. A test asserts **P0 ⊂ P1 ⊂ P2 ⊂ P3** and that `style.preset` is still **P0**, so the default cannot drift.
37. **F1 fill scale 1.40, supersample 1.5**: foreground band 608 px → **852 px**, i.e. `fill_zoom` = 1278/912 = **1.40132**, showing **71.4 %** of the frame width. The 1.5× working canvas (1620×1278) means even the fill state is a *downscale* of a 1920-wide source, so zooming never softens the picture.
38. **`zoompan` over `crop` for animated zoom**: `scale`/`crop` output sizes are fixed at filter-configuration time, so only `zoompan` can animate a zoom. It is fed a `yuva420p` canvas (alpha verified to survive it) so the blurred background shows through in fit segments, and it is **skipped entirely for a constant zoom** (the common P0 case) in favour of `crop`+`scale`. Its clock is `it`; `t` is not a zoompan variable.
39. **F1 per-segment thresholds**: `motion_threshold 0.018` mean frame difference, sampled every `motion_step_s 0.2`, segments no shorter than `min_segment_s 1.2`, `crossfade_s 0.25` cross-scale on a mode change. Measured on the synthetic source: static text card **0.000**, moving footage **0.041**.
40. **Safe-area re-placement rule**: nearest valid caption centre to the configured 1300 with `edge_clearance_px 40` from every video edge. In a mixed-mode clip no y is both below the fit edge (1264) and 40 px clear of the fill edge (1386) inside the safe area, so captions move **up to 1178** (ink 1132–1224) — on the footage in both modes rather than half on the blurred background. Fill-only clips keep 1300; fit-only clips get 1350. Full table in §7B.
41. **Hook line spacing 1.75 plus a `min_line_gap_px 18` floor**: 1.45 left only ≈9 px between the two outlines. A cap-height multiple alone is not enough because outline (8) and shadow (3) are **absolute pixels** — on a long hook that shrank to Fontsize 68 the gap measured **3 px**; the floor restores ≈28 px at any size.
42. **Credit tag box measured ascender-to-descender**: `via GamingBolt` is mixed case, so a cap-height box under-read it by ≈20 px and the "never overlaps the captions" guarantee would have failed. Fontsize **71 → 32 px caps measured**, `\alpha&H33&` (≈80 % opacity), inset by outline+shadow so the ink stays inside x ≥ 90.
43. **M1 caption pop 80 → 115 → 100 % over 160 ms**, fired once per caption **chunk**, not per word. The **115 % peak is fed into the safe-area placement**, so the transient size also respects the edges.
44. **M2 hook pop 55 % → 100 % in 250 ms, shake ±1.5° in 4 steps over 400 ms**, rotating about `\org(540, hook_centre)` so a 2-line hook moves as one block instead of scissoring. Rotation was chosen over a pixel shake because ASS cannot animate `\pos` (only a single linear `\move`).
45. **M3 punch-ins 1.12×, 120 ms cosine ease in, 0.8 s hold, 120 ms out, ≥3.0 s apart, max 4, never before 1.5 s**, ranked by ebur128 momentary loudness with scene cuts as secondary candidates. 0.8 s sits in the middle of the 0.6–1.0 s window.
46. **M4 slow push 1.00 → 1.06** across static segments and the B freeze. The freeze is merged into a following static segment so the push runs once instead of resetting at the freeze boundary.
47. **One zoom expression for F1 + M3 + M4**: `z(t) = base(t) × (1 + 0.12·env(t))` as half-open gated linear pieces, with `edits.zoom_value` as the Python mirror the unit tests assert against and the real renders verified by measuring the band height on screen.
48. **C1 keyword colour**: the Gemini hook JSON gained a `keywords` field (3–6 words), cached in a new `items.keywords` column. **Yellow always wins for the spoken word**; other keywords are green `&H0000FF00`. Rule fallback (used here, no API key): digit-bearing tokens, a baked GTA name/place list, plus `style.keyword_color.extra_terms`.
49. **C2 emoji as PNG overlays**: libass cannot rasterise colour emoji, so Twemoji 72×72 PNGs (CC-BY 4.0, credited in `README.md`) are composited with `overlay`. **Max one per 4.0 s**, 110 px, held 1.6 s, rising 24 px over 120 ms, 28 px above the caption ink. Only the **14** codepoints the config map uses are committed. `repeatlast=1` is required — `eof_action=pass` drops a single-frame PNG after one frame.
50. **C3 progress bar drawn in ASS, not with `drawbox`**: `drawbox`'s `t` is its *thickness* parameter, and ffmpeg 6.1 exposes no time variable to it, so an animated bar width is impossible there. A 1080×10 ASS rectangle scaled by `\t(0, duration_ms, \fscx100)` fills exactly linearly and costs nothing extra (verified: 26 px → 1052 px over 4 s).
51. **A1 SFX synthesized, not downloaded**: Kenney.nl, OpenGameArt, Freesound and Pixabay are **all blocked from the cloud sandbox** (egress proxy answers `403` to `CONNECT`). Both cues were generated with ffmpeg `aevalsrc`, peak-normalized to **−1.0 dBFS**, and committed with their provenance in `assets/sfx/LICENSES.md`. `scripts/fetch_assets.py --prefer-download` can swap in the Kenney CC0 originals on the laptop.
52. **A1 gain staging `headroom_db 6.0`**: each cue's gain is computed from the clip's **measured** speech peak (`volumedetect`) so its peak lands at least 6 dB under the voice, rather than using a fixed dB value that would be wrong for every clip.
53. **A2 ducking by `volume` automation, never `sidechaincompress`**: speech spans (words merged across gaps ≤ `speech_merge_gap_s 0.3`) drive a per-frame `volume` expression — bed at **−24 dB**, a further **−8 dB** under speech, with **80 ms** ramps so the duck does not click. The bed is `apad=whole_len=N,atrim=end_sample=N` to the clip's **exact** sample count (pad then trim, so a short bed is padded and a long one trimmed).
54. **A2 track not committed**: freepd.com is blocked from the sandbox, so per the brief the code was built and tested against a **generated test tone** and no synthetic "music" track was committed; `scripts/fetch_assets.py` downloads a real CC0 FreePD track on the laptop and writes `assets/music/LICENSES.md`. A missing bed is logged and skipped, not fatal.
55. **A3 dead-air gate needs silence AND stillness**: over `min_silence_s 0.4`, below `−45.0 LUFS` **and** below motion `0.012`, shrunk by `keep_pad_s 0.12` at each end, longest candidate first, stopping before the output would drop under **`min_output_s 15.0`**. Cuts use `trim`/`atrim` + `concat` (frame- and sample-exact) rather than `select`, and every timestamp remaps through one `Timeline`.
56. **A3 `loudness_lag_s 0.2`**: ebur128's momentary value describes the 400 ms window **ending** at `t`, so an uncorrected profile cuts ≈0.2 s late and clips the start of the next sound.
57. **A4 loop end: dHash distance with a 6-bit pause bonus**, searched every `0.2 s` inside `[cut.min_duration, min(cut.max_duration, cut length)]`, reusing the existing `dedupe.dhash_from_raw_gray_9x8`. **No video fade**; the audio fade drops from 0.25 s to **0.15 s** when A4 is on (the brief's cap).
58. **New DB columns**: `items.keywords` (JSON array, the C1 cache) and `items.style_preset` (which preset produced the renders), both added by the existing idempotent migration in `init_db`.
59. **Default preset unchanged**: `style.preset: "P0"`, so the pipeline's behaviour changes only by gaining F1 framing until a preset is chosen.

### CP3: Telegram approval + Meta posting (see §9b; renumbered from 33-47 on the merge into main)

60. **A `FINISHED` Container Counts as Unknown (CP3)**: on resume in `publishing`, only `ERROR` / `EXPIRED` (publishing impossible) lets a fresh container be started. A container reading `FINISHED` with no matching recent media is treated as *unknown* and the row is failed and alerted, never re-published: Instagram's `/media` list is eventually consistent, so "FINISHED and not in `/media`" cannot distinguish a publish that never happened from one whose media has not yet appeared. A duplicate public post is worse than a post that needs one manual retry.
61. **Recent-Media Matching by Caption and Time**: Graph gives no way to ask "which media came from container X", so a published media is identified by its caption's first line matching `posts.caption_text` and its timestamp being at or after `posts.publish_started_at`. The caption is therefore frozen onto the row when the row is created, not rebuilt at publish time.
62. **Dry Run Defaults to `plan_only` (CP3)**: "everything except the publish call" was read conservatively. Creating a container and uploading bytes are real writes against the live account that leave unpublished containers behind, so the default dry run sends nothing and instead walks the whole state machine while writing a redacted plan of every request. `posting.dry_run_mode: up_to_publish` gives the literal reading (real container, real upload, no publish) as a one-line opt-in.
63. **A Dry Run Never Advances an Item to `posted`**: `run_posting_for_item` marks the item `posted` only when every platform row is `live`. All-`dry_run` leaves the stage alone, otherwise flipping `dry_run: false` would find the item already "posted" and it would never actually go out.
64. **`max_duration_seconds` Applies to Variant A Only (CP3)**: PLAN.md section 7 / call #25 make Variant B a freeze-frame plus the cut (approximately 23 s), which the 22.0 s gate would have rejected on every item. The gate now caps Variant A at `max_duration_seconds` and allows Variant B up to `approval.auto_quality_gate.max_b_extra_seconds` (8.0 s) longer than A.
65. **The Reviewer Does Not Pick the Variant**: `Approve` leaves `chosen_variant` NULL for the CP4 scheduler's bandit to assign. Only `Approve A only` pins a variant, and it sets `items.ab_excluded = 1` so that item never enters the A/B comparison.
66. **New Columns for CP3**: `items.ab_excluded`, and on `posts`: `ab_excluded`, `attempt_count`, `publish_started_at` and `caption_text`. All added with `ALTER TABLE` migrations in `db.init_db`, so an existing `pipeline.db` upgrades in place.
67. **Stale Protection Keys on the Message Id and the Item Id**: both the recorded `mid` and the recorded `item_id` must match the click. The message id alone would let a tap on a re-sent card decide the wrong item; the item id alone would let an old message decide a current item.
68. **Strict One-at-a-Time Enforced in `kv`, Not in Memory**: the open card lives in the database, so a restarted runner still refuses to send a second card and an item whose decision was lost is re-offered rather than skipped.
69. **The `getUpdates` Offset Is Saved Before the Update Is Handled**: a crash mid-handling therefore skips that tap instead of replaying it. Replaying a decision is worse than losing one, which the user can simply tap again.
70. **Telegram Alerts Are Deduplicated for an Hour per Key**: a failure that recurs every cycle would otherwise fill the approval chat and bury the cards.
71. **Meta Request Pacing Lives in the Database**: `kv:meta_api:next_free` keeps `posting.meta_min_gap_seconds` between any two Meta requests across processes, so the publisher, the bot and the dry-run script cannot collectively trip the limit.
72. **Rate-Limit Pause Is Source-Wide**: a 429 or block pauses the whole `meta_graph` source for `posting.rate_limit_pause_hours` (3 h), not just the failing row, and the row is left at its current status rather than failed, so it resumes cleanly when the pause lifts.
73. **Facebook Can Use Its Own Page Token**: `FB_PAGE_ACCESS_TOKEN` is read first and falls back to `IG_ACCESS_TOKEN`, since Page Reels need a PAGE token with `pages_manage_posts` while Instagram publishing needs `instagram_content_publish`; one token often covers both, but not always.
74. **Graph API v25.0 Pinned in Config**: `posting.api_version`, `posting.graph_base` and `posting.rupload_base` are config keys, so a version bump is an edit rather than a code change.

### Merge of CP3 into main, and CP3 made to respect `preview_only`

75. **CP3's calls renumbered 33-47 → 60-74**: both branches had continued from 32. Main's 33-59 (style layer) keep their numbers because HANDOFF.md and the style commits already cite them; CP3's moved (+27) and its one cross-reference (#64 → call #25) still points at main's #25.
76. **`preview_only` is filtered in the queries, not left to the trigger**: `db.get_items_by_stage(..., releasable_only=True)` is used by the Telegram card, the auto gate and the posting stage, and `send_card`, `decide`, `ensure_post_rows`, `run_posting_for_item` and `render.run_render_item` each check `db.is_releasable()` themselves. The tests drop the triggers and assert the filters still hold. The triggers stay as the last line of defence.
77. **A trigger refusal fails that one item**: every write that a trigger could refuse (`rendered`, `awaiting_approval`, `approved`, any `posts` write) catches `sqlite3.IntegrityError`, marks that item `failed` (not a release stage, so the trigger allows it) with `error_stage` `render` / `approval` / `posting`, and carries on with the next item. A failed item is in no queue, so the next cycle cannot meet it again — no crash loop.
78. **The card moves the item to `awaiting_approval` before it sends anything**: previously the videos went out first. If the database refuses the item, nothing has reached the chat; if the process dies after the move, `next_item_for_review` re-offers `awaiting_approval` items first, so the card is simply sent again.
79. **The card shows the item's own `style_preset` renders**: `telegram_bot.card_renders()` returns the render paths recorded on the item (the files made with `items.style_preset`, and the files that will be posted) plus the preset name, which is printed on the card and on the Variant A message. It never picks a `media/previews/` render of another preset and never follows `style.preset` if that changed after the item was rendered. An item rendered before the style layer (NULL preset) is labelled P0, which is what those renders were.
80. **Reddit caption template removed, Twitch template added, templates in config**: `posting.caption_templates` (`youtube`, `twitch`); the render stage now builds its caption through the same `posting.build_caption()` instead of a hard-coded string, so a Twitch item gets the Twitch credit. Twitch credits the *broadcaster* (the streamer whose content it is), not the viewer who pressed "clip".

### Known issues from HANDOFF §7

81. **Downloads capped at 1080p H.264, with no "anything" fallback**: `download.format_selector()` builds the chain avc1 ≤1080p + m4a → avc1 ≤1080p + any audio → progressive avc1 → any non-AV1 ≤1080p (VP9) + audio → a single non-AV1 ≤1080p file with unknown codec allowed (Twitch clips report none). A source that exists only as AV1 or only above 1080p **fails the download** rather than being picked: a slow-decoding source is what made dedupe time out (so no fingerprints were stored and dedupe was silently skipped) and what pushed scene detection to its limit. The chain is tested against yt-dlp's real selector, offline.
82. **A finished download is probed and failed closed if it is AV1 or above the cap** (`download.source_is_acceptable`, `download.reject_codecs`): step 5 of the chain accepts files whose codec the site does not report, so the file itself is the only proof. Height is compared on the short side, so a vertical 1080×1920 clip is not rejected as "1920p".
83. **Tests write media under `tmp_path` through one switch**: `config.set_media_root()` (and `$GTA6_MEDIA_DIR`) redirect `config.media_dir()`; an autouse fixture points it at `tmp_path/media` for every test. `tests/test_zz_media_isolation.py` runs last and fails if any file under the real `media/` was added, removed or changed during the session (snapshot taken in `pytest_sessionstart`), so it also holds on the laptop where `media/` has real content.

### CP4: scheduler, approval switch, slot-driven posting, runner loop (see §10b)

84. **One replaceable clock (`pipeline/clock.py`)**: `db.now()`, `rate_limit` and every CP4 module read time from `clock.utcnow()`. Production never replaces it; the tests and `scripts/cp4_dry_run.py` install a `FakeClock` and walk ten days in seconds. The alternative (passing `now` through every function of CP1-CP3) would have touched every call site for no behavioural gain.
85. **Posting is slot-driven; only the scheduler turns `approved` into `queued`**: `run_posting_stage` now posts `queued` items whose slot has come and nothing else (CP3 posted every approved item immediately). `run_posting_for_item` is unchanged and remains the immediate primitive that `scripts/cp3_dry_run.py` uses.
86. **A missed slot is re-slotted, not sent late** (`posting.missed_slot_grace_minutes: 90`): a queued post that has not started within 90 minutes of its slot moves to the next free slot, earliest missed first, so a laptop that slept three days posts its four waiting items one per slot over the next two days. 90 minutes is under the 3 h minimum gap, so a late post can never run into the next slot. The check exists twice on purpose: the scheduler re-slots, and the posting stage independently refuses to start a post past its grace, so the order the two stages run in cannot cause a burst.
87. **At most one new post is begun per posting pass, and never within `min_gap_hours` of another item's live post** — measured against real `posted_at`, not just slot arithmetic.
88. **A post that is underway is never re-slotted**: once any row is `publishing` or `live` the item keeps its slot and the two-phase machine finishes or settles it (e.g. Facebook catches up after Instagram went out). Re-slotting it would mean starting a second container for something that may already be public.
89. **Bug fixed — an "unknown remote state" row was re-published on the next pass.** CP3's `settle()` correctly failed such a row without publishing, but the row was then plain `failed`, and the next posting pass picked it up and ran the upload + publish again: exactly the duplicate post call #60 exists to prevent, one cycle later. Rows now carry `posts.no_retry`; `settle()`'s unknown verdict and "gave up after N attempts" set it, and `publish()` returns at once for such a row.
90. **A given-up item leaves the queue**: when every enabled platform row is `live` or failed for good, the item becomes `posted` if anything went live (it is measured like any other post) or `failed` at `posting` if nothing did. Before this an exhausted row was "failed again" every cycle forever and kept its slot.
91. **Bug fixed — items that completed a dry run were never posted after going live.** Their rows end as `dry_run`, and `publish()` returns immediately for a `dry_run` row, so flipping `app.dry_run: false` would have posted nothing that was already queued. `posting.reset_dry_run_rows()` (a no-op while `dry_run` is true) turns them back into `pending`; their slots are in the past by then, so call #86 re-slots them one per slot instead of posting them all at once.
92. **Synthetic dry-run items are never scheduled or posted for real**: `scripts/cp3_dry_run.py` leaves `synthetic-cp3-*` items in `rendered` with a live Approve button. `db.is_releasable()` / `RELEASABLE_SQL` exclude them from the card queue, the auto gate, the ready buffer, the scheduler and (outside a dry run) `ensure_post_rows`; an approved one is `rejected` by the scheduler. `ApprovalBot.release_stuck_card()` also frees the one-card slot when the open card is a synthetic test card (after collecting a waiting tap) or its item was decided elsewhere — otherwise one untapped test card would have blocked every real card under the one-at-a-time rule.
93. **Slot order is discovery score, highest first, within a scheduling pass**; missed items are re-slotted before new approvals so they keep their place. Trending clips decay with a 36 h half-life, so the best-scoring approved clip should take the earliest slot.
94. **"Epsilon-greedy with a 20% floor" = leader 80%, other 20%**, i.e. ε = 0.4 spread evenly over two variants. It stays 50/50 until *both* variants have `autopilot.bandit_min_samples_per_variant` (3) measured posts and whenever the scores tie, so one lucky early post cannot tilt the split.
95. **Week 1 is "no live post yet, or fewer than 7 days since the first one"**, and the first live time is pinned in `kv` (`launch:first_live_at`) the first time it is read from `posts`, so cleanup or a later failure can never move the launch date. In `dry_run` nothing is ever live, so allocation stays 50/50 and approval stays on Telegram until the real launch.
96. ~~**The variant is fixed when the item is queued**~~ — superseded by call #140: the variant is drawn when the slot comes due.
97. **Approval switch is pinned, announced once, and reversible**: the switch moment is stored in `kv` (`approval:auto_since`); the notice flag is set only after Telegram accepted the message (or can never accept one: Telegram off / no credentials), so a network blip retries and can never send two. A card that is open at the switch is edited to "auto approval took over" and its item goes through the gate. `approval.switch_to_auto_after_days: null` keeps manual approval and also undoes a switch.
98. **Alerts are rate-limited per key**: CP3 remembered only the last alert key, so two different failures alternating every cycle would each have alerted every cycle. `kv` now holds a time per key (entries older than 30 days are pruned), and `repeat_gap_s=None` means "once, ever".
99. **Runner: every stage in its own try/except; the loop holds the lock**: `run_stages` logs and alerts (once an hour per stage) a failing stage and runs the next one. `runner loop` takes the existing kv lock once, keeps it alive with the existing heartbeat thread for the life of the process and releases it on exit, so a second `cycle`/`approve`/`post` started by hand is refused instead of interleaving. The render stage now renders every releasable item waiting in `cut` up to the room left in the ready buffer (CP3's cycle only rendered items cut in that same cycle, so an interrupted render was never resumed).
100. **`tzdata` added to requirements**: `zoneinfo` has no timezone database on Windows without it, and the laptop is Windows.
101. **New columns**: `posts.no_retry` (call #89) and `posts.style_preset` (captured at scheduling for the `style_preset` learning dimension), both via the idempotent migration.

### CP4: analytics

102. **A snapshot is only taken inside its window (`H` to `H + 12 h`)**: insights are cumulative, so a "24h" number read at hour 60 is really a 60h number and would flatter that post against every post measured on time. A snapshot whose window was slept through is skipped for good; the post is ranked on the snapshots it does have. A fetch that *fails* inside the window is simply retried on the next cycle.
103. **ASSUMPTION, not verified — Instagram Reels insight names**: `GET /{ig-media-id}/insights?metric=views,likes,comments,shares,saved,reach,total_interactions`. `views` is the metric Meta introduced in 2025 when it deprecated `plays` / `impressions` for Reels; the mapping reads `views` and falls back to `plays` if that is what comes back. `developers.facebook.com` is **blocked from the build sandbox** (the egress proxy lets almost nothing but GitHub through), so this could not be checked against the current docs. **Check it on the laptop before the first 24h snapshot is due** (Graph API Explorer → `{media-id}/insights?metric=views`); the names are config (`analytics.instagram`), not code.
104. **ASSUMPTION, not verified — Facebook Reels insight names**: `GET /{video-id}/video_insights?metric=fb_reels_total_plays,blue_reels_play_count,post_impressions_unique,post_video_likes_by_reaction_type,post_video_social_actions`; views = `fb_reels_total_plays` (else `blue_reels_play_count`), likes = the reaction breakdown summed, comments / shares = the `COMMENT` / `SHARE` keys of `post_video_social_actions`. Same caveat and same remedy as #103 (`analytics.facebook`). **Update 2026-11-10 (call #172): all five names are listed in Meta's Video Insights reference; the permissions they need are in call #169.**
105. **A refused metric costs one metric, not the snapshot**: Graph fails the whole insights call when one name in the list is unknown. On that error every metric is requested on its own, the refused ones are dropped and alerted **once** (naming the config key to edit), and the snapshot is recorded from the rest. Without this, one renamed metric would silently stop all learning.
106. **Ranking unit and order**: (1) each post is percentile-ranked (mid-rank, so ties share and a group of one is 0.5) within its **platform and snapshot window**; (2) a post's score is its longest window (72h replaces 24h — a 72h number never competes with 24h numbers); (3) an item's score is the mean over its platforms; (4) a dimension value's score is the mean over its items, and `sample_count` counts **items**, not post rows, so posting to two platforms does not double the evidence.
107. **The ranked quantity is views** (`analytics.score_weights`, views 1.0 and the rest 0.0), as §10 specifies. The weights are config so shares or comments can be mixed in later without a code change; likes/comments/shares are stored on every snapshot either way.
108. **Authenticity is learned as official vs unverified**: `leak`, `rumor`, `fan_made` and `unverified` all bucket to `unverified`, matching the two-value tag the hook stage actually assigns.
109. **`posted` → `measured`** when every live row has its last (72h) snapshot, or when the last window has lapsed (72 h + 12 h) so an item slept through measurement does not wait forever and block cleanup's bookkeeping.
110. **`ab_learning.weight`**: for `variant` it is the allocation probability the scheduler will really use (0.5/0.5, or 0.8/0.2), written from the same `scheduler.variant_probabilities()`; for the other dimensions it is that value's share of the dimension's total score. Only `variant` drives a decision today; the other four dimensions are measured for the weekly read-out and for later use in discovery weights.
111. **Analytics requests are GETs through the posting client**, so they share its pacing, retries and the source-wide 429 pause, and they are allowed in a dry run — where there are no live posts, so nothing is requested.

### CP4: cleanup

112. **"Renders" means everything made for the post**: 7 days after the live post the two renders go together with the cut, the voiceover wav and the item's subtitle files — none of them has a use once the renders are gone. The 7 days count from the **last** platform's `posted_at`.
113. **What "still needed by a post" means**: an item with any row `pending`, `uploading`, `container_ready`, `publishing`, or `failed` with retries left keeps every file, under both the 7-day and the 3-day rule (e.g. Instagram live a month ago but Facebook still open, or an item marked `failed` while a row is mid-publish). A row that failed for good (#89/#90) no longer protects anything.
114. **Cleanup only deletes what it can prove is ours**: only paths recorded on the item row (plus `media/subtitles/<item_id>_*`), only if the resolved path lies inside the media folder (a `..` or an absolute path elsewhere is logged and left, pointer intact), and never a file another item row still points at (the CP3 dry-run items all share one synthetic render pair). The column is set to NULL only once the file is really gone.
115. **`cleaned` is only reached from `measured`**: a `posted` item past retention loses its files but keeps its stage until analytics has finished with it (only matters if `purge_renders_after_days` is set below the 72 h + 12 h measurement window), then becomes `cleaned` on a later pass.
116. **Cleanup runs in dry run too, and skips `preview_only`**: it only touches local files and `app.dry_run` is about public posting. In a dry run nothing is ever `posted`, so only the source-after-render and rejected/failed rules can fire. `preview_only` items belong to the style previews and are never cleaned by the pipeline.

### CP4: Twitch discovery

117. **Twitch switches itself on by date**: `sources.twitch.enable_from: "2026-11-19"` is compared with the current **UTC** date (so it begins 05:30 on launch day in India); `enabled: true` forces it on earlier, `enable_from: null` with `enabled: false` keeps it off. No config edit is needed on launch day.
118. **Missing Twitch credentials = skip + one alert, ever**: `discover_twitch` never raises. Without `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` it returns no candidates and sends a single Telegram alert (key remembered permanently, `repeat_gap_s=None`); YouTube discovery carries on. A category that does not exist under the configured name is alerted once the same way; a 429 pauses the `twitch` source for 1 h; any other error is logged and returns nothing.
119. **The app access token is kept in memory only**, never in `pipeline.db`: it is a credential, and the long-running loop needs a new one only every ~60 days (it is also refreshed once on a 401). The resolved game id *is* cached in `kv`.
120. **Percentile pools are per source**: `run_discovery` used one pool seeded with YouTube velocities for every candidate. Twitch clip view-velocities are a different scale from YouTube's, so each source is now ranked within its own rolling pool before the shared formula applies `weight` (Twitch 1.10) and decay.
121. **Twitch candidates**: top clips by views for the game id over `lookback_hours` (48), `limit` 20, `languages: ["en"]`; credit goes to the **broadcaster**. They are downloaded by yt-dlp from the clip URL with the same capped format selector (step 5 of #81 exists for them) and go through the same download- and cut-level dedupe.
122. **A global test guard forbids real HTTP**: an autouse fixture makes `requests.Session.request` raise in every non-`live` test, so a test that forgets to inject a transport fails loudly instead of talking to Telegram, Meta, Twitch or Gemini (relevant once `.env` on the laptop holds real keys).

### Fix pass 2026-11-10 (laptop): endings, SFX density, CP4 completion

123. **A2 music bed moved from FreePD to OpenGameArt**: freepd.com closed permanently in 2025 (its home page is now a closure notice, and every candidate URL returned 404), so `fetch_assets.py` could never succeed. The candidates are now OpenGameArt pages, and a track is downloaded only if its page's licence field reads exactly `CC0` and the page links that file; the page URL is recorded in `assets/music/LICENSES.md`. Installed: "Trace Agent" by Vitalezzz (158 s). Checking the licence on the page, not trusting a hard-coded list, is what makes "real CC0" verifiable.

**Task 1: endings cut off mid-sentence** (`pipeline/endings.py`; supersedes #23 for speech)

124. **What counts as a completed sentence**: a word ending in `.` `?` `!` (closing quotes/brackets stripped; an ellipsis or `Mr.`/`Dr.`/`vs.`/`etc.` does not count) followed by ≥ **0.15 s** of silence, or any word gap ≥ **0.6 s**. 0.15 s because whisper `base.en` puts only ~0.2 s between sentences in continuous speech (measured on the laptop's cuts: `that.` → `He` 0.20 s, `level.` → `They're` 0.20 s); a higher punctuation gap would reject most real sentence ends. A gap ≥ 0.6 s counts without punctuation because base.en often omits it after a pause. All in `cut.ending`.
125. **Where in the gap**: never closer than 0.07 s to the last word or to the next one; in a gap with room to spare the end moves toward the target but keeps 0.35 s clear of both words; in a short gap it sits in the middle of the safe part. The old snap placed ends 0.1 s after a word *without* checking what followed.
126. **"Nearest the target, else the latest before it"** is implemented as: among the sentence breaks inside [min, max] take the one nearest the target; on equal distance take the earlier one. A fallback to a break *before* `min_duration` was not taken: it would break the configured duration bounds, and the next-best window exists for exactly that case.
127. **Window + 2.0 s tail, transcribed as its own 16 kHz WAV**: the tail is what shows whether a candidate's last word is followed by silence or by more speech (item_5a67's old cut ended 0.02 s after `ass.`). Transcribing only the span (≈24 s) instead of the 10-minute source keeps each window to a few seconds of CPU.
128. **One loop for "no clean ending" and "duplicate cut"**: windows are tried best score first, up to 8. A window with no sentence break, or whose cut matches a prior cut, moves on to the next. PLAN §5/§6 always said a duplicate cut tries the next window, but the code rejected the item on the first match; it now does what the plan said. Every tried window a duplicate → `rejected: duplicate_cut` as before; otherwise → `failed` at `cut` with `no_clean_ending`.
129. **Whisper failing at the cut stage fails the item**: an end that cannot be checked is not shipped.
130. **The cut stage owns the transcript cache**: it writes `items.transcript_words` in the same UPDATE that moves `cut_start`/`cut_end`, keyed on that window and the model, with `ending: sentence | no_speech`. It is always rewritten (also `[]` for no speech), so an older window's words can never survive a re-cut, and hooks reuses the words instead of transcribing again. A re-cut also replaces the item's cut fingerprints instead of appending a second set (`db.delete_fingerprints`).
131. **Render trusts the cut's own end only with the cut-stage verdict** (`render.cut_end_verified`). The cut stage saw the audio after the cut; render only sees the cut, where the silence after the last word can be as short as 0.07 s. A cut made before this change gets the strict rule inside the cut alone, and fails render with `no_clean_ending` if it holds no sentence end.
132. **Render snap aims at the cut's end**, so it never ends earlier than the last completed sentence in the cut. **A4** picks the dHash-closest frame only among those safe ends; a safe span too narrow to contain a sampled frame is scored with the nearest sampled frame (≤ half a 0.2 s step away). With speech, every A4 candidate is in a pause, so the pause bonus no longer changes the choice.
133. **A3 drops a quiet, still stretch only in a sentence break**: it must touch no word (±0.05 s) and the gap holding it must be a sentence break, or lie before the first / after the last word. The loudness meter and whisper disagree often enough (quiet words) that loudness alone could cut into a sentence.
134. **The end fade never covers the last word**: it is shortened to the silence after the last spoken word (floor 0.05 s), because an end 0.1 s after a word with a 0.25 s fade would audibly fade the word itself.
135. **`render_previews.py`**: re-cuts any cut the current cut stage did not verify; re-downloads a source the current downloader would refuse (the laptop's three were AV1 1440p, which is what made scene detection take ~4 min each) and keeps the old file if that fails; with no `--items`, uses the CP2 ids present in `pipeline.db`, topped up with the best real items (the hard-coded ids are the cloud database's and do not exist on the laptop); an item that fails with `no_clean_ending` is replaced by the next real item.
136. **Whisper loads from the default cache when `HF_HOME` points at a missing drive**: on the laptop `HF_HOME` is set machine-wide to `D:\huggingface_cache` and there is no D: drive, while `base.en` sits in `~/.cache/huggingface/hub`. Now that the cut stage needs whisper, that would fail every cut. `render.whisper_download_root()` falls back to the default cache (with a warning) only when HF_HOME's drive/root does not exist; the machine setting itself was left alone (it is the user's system configuration).

**Task 2: SFX density**

137. **One rule, one place**: `audio_mix.plan_sfx_cues` is the only code that decides SFX cues, and it gets times already on the variant's final output timeline (punch-ins are planned after A3's remap and with the B freeze offset; the hook starts at 0 in both variants). Boom first; a whoosh only where it starts ≥ `style.sfx.min_gap_s` 5.0 s from every placed cue, start to start; at most `style.sfx.max_per_clip` 2. `audio_mix.sfx_spacing_problems` re-checks a cue list and is used by the tests and the real-clip verification; it never decides anything.
138. **The whoosh goes to the strongest clear punch-in, not the first**: `edits.Punch.rank` records M3's loudness order, and whooshes are offered in that order. With the boom at 0 s and a cap of 2, a clip gets one whoosh, so it should mark the biggest moment rather than whichever came first after 5 s.
139. **Bug found and fixed: Variant B punch-ins were spaced on mixed timelines.** `plan_punch_ins` compared each candidate's cut-timeline time with already-taken punch-ins stored on the output timeline (+freeze), so on B the 3 s spacing was measured against the wrong times (B got punch-ins 1.5 s apart, at different moments than A). Both sides of the comparison are now output time; B's punch-ins are A's shifted by the freeze.

**Task 4: finishing CP4**

140. **The A/B variant is drawn when the slot comes due, not at queue time** (supersedes #96). `schedule_approved` creates the `posts` rows with the variant `?` (`posting.UNDECIDED_VARIANT`) and `posts.variant_decided_at` NULL; `posting.decide_variant` draws it from the week-1 status and `ab_learning` *at that moment*, right before the first upload (`run_posting_for_item`, and `Publisher.publish` as a safety net for direct callers). So an item queued on day 6 for a day-8 slot uses the bandit and the learning of day 8. "Approve A only" is still fixed as A at once. The draw is written to every row of the item in one UPDATE and logged to `stage_log`; it is frozen once any row is `uploading`/`container_ready`/`publishing`/`live` or holds a container id, so a crash, a retry, or Facebook catching up after Instagram can never switch the video. A missed slot (re-slotted) and a dry run (reset when going live) clear an undrawn-and-unstarted decision, so it is drawn again at the real slot. An undecided row can never be uploaded: `video_for_post` refuses `?`. The migration marks rows that had already begun as decided and leaves pending ones to be drawn at their slot.
141. **Bug found and fixed: a lost reply to the publish call could lead to a retry of a public post.** `MetaClient.request` retried *every* POST on a network error or 5xx, including `media_publish` and the Facebook `finish` (the publish itself). If Meta published and the reply was lost, the publish was re-sent in flight; and if that errored, `publish()` marked the row plain `failed`, so a later pass re-ran the flow on a container that was already public. Publish calls are now sent once (`retry=False`), and an error *during* the publish call leaves the row `publishing` (not `failed`) so the next pass settles it from the remote state, which never publishes twice (call #60). Covered by `test_a_lost_publish_response_is_never_retried_and_settles_live_without_a_second_publish` and by the CP4 dry run's lost-reply scenario.
142. **Healthchecks.io: a success ping only, after a fully successful cycle.** `pipeline/healthcheck.py` pings `HEALTHCHECK_URL` after every `loop` / `cycle` whose stages all succeeded; unset URL = no request; one try, 10 s timeout, never raises, and the URL (it embeds the check's secret UUID) is never logged. No `/fail` ping: a failing stage already alerts on Telegram, and Healthchecks.io raises its own alarm when success pings stop - a `/fail` on every partial cycle would page for a transient YouTube hiccup. `.env.example`'s comment said "/fail on error" and was corrected.
143. **Server move**: `deploy/gta6-pipeline.service` runs `python -m pipeline.runner loop` with `KillSignal=SIGINT` (the loop turns it into KeyboardInterrupt and releases its database lock; a SIGTERM would skip that, and adding a handler was unnecessary) and `Restart=always` (a crash leaves a dead PID, which the next start detects at once). The README section is provider-agnostic, says only one machine may run at a time (the lock lives in each machine's own `pipeline.db`), lists what to copy, and explains `posting.profile: "server"`: slots are stored in UTC, so already-queued posts keep their moment and only new approvals get New York times.
144. **`scripts/cp4_dry_run.py` drives the real stages, fakes only the network**: the real settle / approval bot / scheduler / two-phase publisher / analytics / cleanup run through `runner.run_stages` on a temporary database and media folder, on a `FakeClock` in 30-minute ticks over 10 days. Telegram is a scripted reviewer (taps ~45 min after a card), Meta is an in-process Graph fake that refuses a second publish of a used container (as Instagram does) and keeps a ledger of every publish *call*, Healthchecks.io a recorder; any other HTTP request raises and the secrets are fakes. The invariants are read from the fake remote's ledger, not from our own database, so a bug that hides a double post in our rows would still be caught. Inside the simulation `app.dry_run` is false in memory (otherwise nothing goes live, so week 1, the bandit, analytics and cleanup could not be exercised); `config.yaml` still says true.
145. **The dry run checks it touched nothing real**: it compares the real `pipeline.db` and `media/` before and after (skipped under pytest, where `test_zz_media_isolation.py` already guards `media/`), and restores every global it patched so it can also run as a test (`tests/test_cp4_dry_run.py`, 3.5 days).

**Task 3: real-clip verification, and what it changed**

146. **The first verification found one mid-sentence end, and it changed the rule (`endings.RULE_VERSION` 2).** item_1032's cut ended where whisper had reported a ≥ 0.15 s gap after "online.", but the audio at the cut point was **+2 dB** relative to the speaker: "online." runs straight into "So". Whisper base.en's word timestamps are not precise enough for short gaps in either direction (item_00983: "so." -> "The" reported 0.00 s apart over a real 0.16 s pause, which had made that item fail `no_clean_ending` on all 8 windows). With audio available (always, in the cut and render stages), a punctuation break now counts only where the audio actually pauses right after the word; ≥ 0.6 s pauses stay whisper's call (its VAD is reliable there, and music under a long pause would fail a level test). Cut-stage verdicts carry `ending_rule`, and render trusts only verdicts made under the current rule, so the cuts made under rule 1 were re-cut.
147. **Pause thresholds: ≥ 0.06 s at ≥ 12 dB under the clip's speech level, the EARLIEST such stretch from 0.1 s before whisper's word end to 0.4 s after it (and before the next word is 0.25 s in)**, end placed in its middle. Measured on the laptop's sources: real sentence pauses in continuous narration were 0.15-0.9 s at 13-26 dB down; 15 dB / 0.08 s rejected item_5a67's genuine ~0.15 s pause (its frames hover at -13..-20 dB). Earliest, not quietest, because a sentence-final pause comes first and a later dip can be a stop-consonant closure inside the next word.
148. **The end fade is also capped by the measured quiet tail** (floor 0.03 s), not only by whisper's word end, since whisper's ends can be ~0.1 s off: a 0.25 s fade on an end 0.1 s after a word would audibly fade the word.
149. **Words that finish after the end are dropped from the cut's cached words**: at a measured pause whisper often starts the next word inside the pause; that word is not in the cut, must not be captioned, and must not make render think the end is inside a word.
150. **Windows that snap to the same start are tried once**: snapping starts to scene cuts gave item_00983 the window 273.1 s twice in its top 8; the next-best window now really is a different one.
151. **`scripts/verify_previews.py` reports two checks per render and judges on the ground truth**: (1) whisper on the last 3 s of the render (what was asked for), and (2) the end put back into the source, re-transcribed with 2 s after it and measured: the sentence-break rule plus the audio level at the cut point relative to the speaker. Whisper on a 3 s snippet is unreliable on its own: it hears "wanted level." as "wanna love." in the source as well, and "-lup"/"-lope" when P2/P3 add SFX or music. A row passes when the source check passes and the snippet's last word ends a sentence (or the source shows a ≥ 0.6 s pause, the rule's no-punctuation case). SFX gaps are read from the cue list each variant was mixed with (`P{n}.plan.json`, written by `render_previews.py`).
152. **The second verification found one more, and it changed the rule again (`RULE_VERSION` 3): every end must sit in measurably quiet audio.** item_1032's P1-P3 renders ended at 18.80 s, where the source is **+6 dB** loud ("download," -> "one"). Two holes: the cut's verified end had been widened into the whole range "from the last whisper word to the cut end" on the assumption that it was silence (whisper ended "download," at 18.62 s; the audio was still loud at 18.85 s, and A4 picked a frame there), and ≥ 0.6 s pauses were still whisper-only (whisper's word ends moved ~0.3 s between two transcriptions of the same audio). Now a long pause only counts where the audio is ≥ `pause_drop_db` 8 dB under the speaker for ≥ 0.06 s (8, not 12: music or game sound under a real pause should not veto it, but continuing speech, which sits within ±6 dB of the speaker's median, does), and the verified end reaches back only over its measured quiet tail (just the point when there is no audio to measure). The cuts were re-cut again under rule 3.
153. **Bug found and fixed: P0 rejected a verified cut end because of rounding.** `choose_end` rounded the chosen end to the millisecond; a cut's own end such as 18.98567 s became 18.986 s, 0.3 ms outside its one-point verified span, and the render guard failed the item with `no_clean_ending` (P0 on item_1032 and item_5a67; P1-P3 escaped because A4 picks sampled frame times). The rounded end is now clamped into its span, and `span_at` tolerates 0.1 ms.
154. **The third verification refined the long-pause case (`RULE_VERSION` 4): "a gap ≥ 0.6 s" is measured in the audio.** Under rule 3 item_1032 ended in real quiet (−24 dB), but measuring the stretch showed the speaker's pause after "download," was **0.32 s, after a comma** - not a sentence end by the rule; the cut stage had accepted it because whisper reported a 0.63 s gap. Now that case is a quiet stretch at least `pause_gap_s` long at ≥ `pause_drop_db` under the speaker, wherever it sits (whisper sometimes starts the next word inside such a pause). Whisper's own gaps count only when it hears nobody for ≥ `no_speech_gap_s` 2 s (music or gameplay with no speech, which may be too loud for a level test), and then not closer than 0.35 s to the last word. `verify_previews.py` likewise fails an end that sits in sound, and no longer fails one only because whisper stretches the next word back over a measured pause.

### Captions: GTA glossary and model choice (2026-11-10)

155. **One transcription path, the glossary on every call.** `pipeline/transcribe.py` is the only place faster-whisper is called (cut-stage spans, hooks, render's cut fallback, the VO, `render_previews.py`, `verify_previews.py`); `render.transcribe_words` is a thin wrapper. `transcribe.glossary` in `config.yaml` holds the terms asked for (Leonida, Lucia, Jason, Vice City, Port Gellhorn, Ambrosia, wanted level, Rockstar, GTA 6, GTA VI, Trevor, Michael, Franklin) plus the names and places from `keywords.GTA_TERMS` written as people say them (Leonida Keys, Grassrivers, Mount Kalaga, Everglades, Miami, Cal, Boobie, Dre'Quan, Raul, Brian, Grand Theft Auto, Rockstar Games). The generic words in `GTA_TERMS` (TRAILER, MONEY, CAR...) were left out: whisper already knows them, and every extra term biases it more. Bug fixed on the way: `render.get_whisper_model` kept the FIRST model it loaded and silently ignored any other name, so a model change could never take effect in a running process; there is now one loaded model per name.
156. **small.en chosen over base.en; measured on this laptop** (`python scripts/benchmark_captions.py`, mean wall time per clip, model load excluded, 2 runs, the 3 preview items):

    | | 24 s cut-stage span (feeds the captions) | ~20 s cut file | "wanted level" in 3 short snippets* |
    |---|---|---|---|
    | base.en (the old captions) | 2.31 s | 1.41 s | 0/3 |
    | base.en + glossary (hotwords) | 7.42 s | 3.46 s | 0/3 |
    | base.en + glossary (initial_prompt) | 6.12 s | 6.25 s | 0/3 |
    | small.en | 5.91 s | 4.54 s | 1/3 |
    | **small.en + glossary (hotwords) - chosen** | **8.29 s** | **5.85 s** | **3/3** |
    | small.en + glossary (initial_prompt) | 8.42 s | 5.83 s | 3/3, but dropped 42 of 79 words on item_1032 (call #157) |

    \*the 5 s source snippet around the end and the last 3 s of P0_A / P3_B of item_5a67, i.e. the audio where "wanna love" / "wanna -lup" appeared. On the long 24 s spans every configuration already heard "wanted level": base.en's error is a short-context error, and the glossary does not fix it for base.en, while small.en + glossary fixes all three. "Under ~2x base.en's time" was taken against base.en **with** the glossary, the alternative this choice replaces now that the glossary is on every call: 1.1x on the spans, 1.7x on cut files. Against the old glossary-free base.en it is 3.6x / 4.1x (≈ 8 s instead of ≈ 2 s per 24 s span; the cut stage transcribes up to 8 windows, so worst case ≈ 1 min per item at two posts a day). Word diffs (small.en + hotwords) on the spans the captions come from, vs the old base.en captions: item_00983 none; item_1032 "+he" at the start, "to" -> "the", "licensed," -> "license,"; item_5a67 "Just" dropped (the clip starts mid-word), "or revamp" -> "are revamped,", "revamp" -> "revamped", "+and", "managed" -> "manage". On the cut-file benchmark it also wrote the spoken "GTA 6" as "GTA VI" once (call #157).
157. **The glossary goes in as `hotwords`, not `initial_prompt`** (`transcribe.glossary_as`). initial_prompt was chosen first (it did not turn "6" into "VI"), and the re-render then showed small.en + initial_prompt transcribing only **37 of 79 words** of item_1032's new window - everything after "online." vanished (a known whisper failure with list-like prompts), while small.en + hotwords, small.en and base.en all kept every word. A missing caption is as wrong as a misheard one, and the cut stage's ending check depends on complete words, so hotwords it is. Its known side effects, accepted: it may write a spoken "GTA 6" as "GTA VI" (both are in the glossary as asked; removing "GTA VI" from `transcribe.glossary` stops it), and it dropped the first, half-cut word of a clip. With base.en, both modes appended invented text at the end of cut files ("So. . . .", "Thank you. . . .", "All right."); small.en did not.
158. **The transcript cache key is model + glossary + how it is passed** (`transcribe.cache_key()`, e.g. `small.en|<12-hex sha1>`), stored as `items.transcript_words.key`. Any change to the three re-transcribes; payloads written before the key existed never count, so every item re-cuts once (the cut stage's verdict travels with its words). Bug fixed on the way: a NEW model could not be downloaded on this laptop because huggingface_hub's xet cache also lives under the stale `HF_HOME=D:\...`; when that drive is missing the process now points `HF_HOME` at `~/.cache/huggingface` before huggingface_hub loads.
159. **Ending rule 5: no end without measured quiet, even when whisper hears nobody** (`cut.ending.no_speech_gap_s: null`). Rule 4 let a whisper gap of ≥ 2 s count without a level test (for music/gameplay under no speech). The verification of the small.en + initial_prompt render showed why that is unsafe: whisper had dropped the speech after "online.", reported "nobody speaks", and item_1032's end landed at **+4 dB**, mid-sentence. Now every end, in every stage, needs a measured quiet stretch; a clip whose speech is followed only by loud music or game sound fails `no_clean_ending` instead. Set `no_speech_gap_s` to a number to allow it again.
160. **Ending rule 6: a full stop needs a pause of at least 0.2 s** (`cut.ending.audio_gap_min_s`, was 0.06). item_5a67 ended after "…your wanted level" + a 0.15 s pause; small.en wrote "level." with 24 s of context from the cut start and "level," + "they're" with 20 s from 2 s earlier - whisper's punctuation flips at a comma-length pause, so the punctuation alone cannot be trusted there. Sentence-final pauses in the three sources measured 0.3-0.9 s; requiring 0.2 s keeps those and rejects the ambiguous ones. The verifier's source check also now transcribes up to 20 s before the end (it used 5 s), so it is not weaker than the decision it checks.
161. **Retry windows are spread out** (`cut.ending.retry_spacing_s: 2.0`): a window starting within 2 s of one that had no sentence end is skipped without using up one of the 8 tries. item_5a67's 8 tries had been 326, 327, 328, 329.3, 331 s... - near-copies with the same end range. With the spacing they covered 147.6-331 s. **Outcome:** under rule 6 the Hazard narration (fast, continuous) has no clean ending in any of 8 distinct windows, so item_5a67 now fails `no_clean_ending` and is not previewed; its earlier previews ended at "…your wanted level" + 0.15 s before "they're still going to be able to identify you…", which small.en with more context reads as one sentence, and were deleted from `media/previews/`. `pipeline.db` holds no other real item to swap in (that needs a discovery + download run).

### Sentence-aligned cut windows (2026-11-10)

162. **Windows are now built from sentence ends, not the other way round** (`pipeline/sentence_windows.py`). The old stage scored fixed 20 s windows on audio/scene and then hunted for a clean end inside each one's 18-22 s end range, so a fast talker failed every window although the source has many sentence ends (Hazard: 0 of 8). Now every clean sentence end E found in the source - by the **unchanged** rules (`endings.safe_spans`: an audio pause ≥ `audio_gap_min_s` after . ? !, or a measured quiet stretch ≥ `pause_gap_s`) - gives one candidate: the best start in [E − max_duration, E − min_duration], scored with the **same** audio/scene formula (now `sentence_windows.Scorer`, which `scan_cut_windows`' formula is identical to: 0.75·loudness + 0.25·min(1, scene cuts/5) − 0.05 per touched 5 % edge). Start points are a 0.5 s grid, the scene cuts in range (the old scene-cut snap) and sentence starts; a start `start_lead_s` 0.15 s before a sentence's first word earns `start_bonus` 0.05 - soft (scores run 0-1, so a clearly louder/busier window still wins; tested). End point per sentence break: the middle of a measured pause after a full stop, or the comfortable point nearest the last word in a long pause.
163. **The source is transcribed once and cached** (`items.source_transcript`, key = source + source id + downloaded file size + `transcribe.cache_key()` + the regions transcribed), so a re-cut, a dedupe retry or a preview re-render never transcribes it again, and a model/glossary change always does. Up to `sentence_windows.full_transcribe_max_s` 660 s (discovery caps sources at 600 s) the whole source is transcribed; longer sources get the top `region_count` 6 non-overlapping scored windows, each widened by `region_margin_s` 10 s plus the max length and the 2 s tail. The wall time is logged on the cut's stage_log row and stored in the cut transcript's `selection`.
164. **Dedupe stays on top; no speech keeps the old logic.** Candidates are cut best first, up to `max_windows` 8 cuts; a cut that matches an earlier item's cut is dropped and every remaining candidate overlapping it by half or more is skipped without being cut (same footage). All candidates duplicates -> `rejected: duplicate_cut` as before. A source with no transcribed speech keeps the old loudness-trough windows. The fixed-window retry loop and its `retry_spacing_s` (call #161) are gone.
165. **Short sources (Twitch clips) take the same path**: a 25 s or 45 s clip is transcribed whole and its windows come from its sentence ends like any source; the old "≤ target_duration -> the whole clip" shortcut only still skips scene detection and the static-source floor. Sources shorter than `min_duration` are still refused before transcription.
166. **"GTA VI" removed from `transcribe.glossary`** (as a hotword it turned a spoken "GTA 6" into "GTA VI", call #157). The glossary is part of the transcription key, so every cached cut and source transcript was invalidated and re-made.
167. **The no_clean_ending rate is visible.** Every cut attempt now leaves exactly one `stage_log` row (`success` / `failure` / `rejected`, also for missing downloads, whisper/ffmpeg errors and duplicates); `db.cut_outcome_stats()` counts them and the `no_clean_ending` failures over the last 24 h and all time, shown in `python -m pipeline.runner status`. There is no daily Telegram summary, so per the brief: once the 24 h rate reaches `cut.no_clean_ending_alert.rate` 40 % (after at least `min_attempts` 5 attempts) `telegram_bot.send_alert` appends one line with the rate to **every** failure alert, and a cut that fails with no_clean_ending sends its own alert (at most every 12 h) - below the threshold such a failure stays quiet, as before.
168. **Proof on the three real sources (laptop, small.en + glossary, whole sources transcribed):**

    | item | source | transcription | clean sentence ends | candidate windows | chosen window | ends with | endings + SFX check |
    |---|---|---|---|---|---|---|---|
    | item_00983 (GamingBolt) | 535 s | 222 s | 85 | 85 | #1, 285.84-304.48 s (18.64 s), starts at a sentence | "...the dangers that await them." | 8/8 OK |
    | item_1032 (Tridzo GTA) | 539 s | 267 s | 31 | 30 | #1, 113.11-135.01 s (21.90 s) | "...no extra download, one megabyte pack." | 8/8 OK |
    | **item_5a67 (Hazard)** | 599 s | 263 s | **2** | **2** | #1, 163.31-184.81 s (21.50 s) | "...pre-order benefits, like the Vice City Vintage Pack." | **8/8 OK** |

    Hazard, which failed all 8 fixed windows, now renders: its 2,347 words hold only two sentence ends that pass the current rules, and the old windows' 18-22 s end ranges simply never landed on them. SFX on every P2/P3 render: the boom at 0 s plus one whoosh at 9.7-20.3 s. Three fixes came out of this run: a window exactly `max_duration` long encoded to 22.005 s and fell outside render's 18-22 s range (windows now stop `length_margin_s` 0.1 s short, and render tolerates 0.15 s of encode slack on a verified end); a measured pause after a full stop is now safe across its whole length, not just ±0.05 s around its middle (A4 picked a silent frame 0.1 s earlier); and `verify_previews.py` transcribes 6 s after the end (with 2 s whisper pulled "And", really +0.40 s, into the silence before the cut) and ignores words it places entirely inside measured silence.

### Meta account setup and permissions (2026-11-10; guide in SETUP.md, policy in PRIVACY.md)

169. **Nine Meta permissions, all on one Page token.** Read from Meta's own docs on 2026-11-10: Instagram publishing with Facebook Login takes a *Page* access token with `instagram_basic`, `instagram_content_publish`, `pages_read_engagement`; Page Reels take a Page token with `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`; IG media insights take `instagram_manage_insights`; and the Video Insights reference requires **`read_insights` and `pages_manage_engagement`** (plus the `ANALYZE` Page task) for `/{video-id}/video_insights`. The last two were missing from `.env.example`, HANDOFF and the dry run, so every Facebook snapshot would have been refused after the first live post. `business_management` is added by the Instagram use case and kept. The list lives in `scripts/cp3_dry_run.py` as `REQUIRED_SCOPES`; one Page token from `me/accounts` carries all nine, so `FB_PAGE_ACCESS_TOKEN` stays blank (call #73's fallback is unchanged). `ads_management` / `ads_read` are only needed when Page access comes through a business portfolio role, so SETUP.md says to skip the portfolio.
170. **The dry run fails instead of warning.** `check_meta_tokens` now fails its row, and the run (exit 1), when a token is missing any required permission (each listed on its own summary line with its use), is not a PAGE token, is invalid, names a different Page in `profile_id`, or when `GET /{FB_PAGE_ID}?fields=instagram_business_account` is empty or not `IG_USER_ID`. The old check read `if scopes and s not in scopes`, so an answer with **no** scopes passed; it now counts all nine missing. With a separate `FB_PAGE_ACCESS_TOKEN`, both tokens are checked: the IG one for `IG_SCOPES`, the FB one for `FB_SCOPES` (their union is the nine). Still read-only GETs behind the guarded transport.
171. **The Meta app must be Live before the first real post.** Meta's app-modes page: "Any data generated while an app is in Development mode, such as test posts, can only be seen by role users." Going Live needs a privacy policy URL and data-deletion instructions, so `PRIVACY.md` was written from what the pipeline actually stores and sends (the `pipeline.db` tables, `retention`, the Gemini payload). Its GitHub URL is the one to paste (HANDOFF step 5, SETUP.md 2e). Permissions stay at Standard Access with no App Review; Meta's docs do not spell out that a Live *Business* app keeps Standard Access for role users, so HANDOFF says to stop and report if going Live asks for App Review.
172. **Facebook insight names confirmed; Instagram names still not.** All five `analytics.facebook` metrics (`fb_reels_total_plays`, `blue_reels_play_count`, `post_impressions_unique`, `post_video_likes_by_reaction_type`, `post_video_social_actions`) appear in the Reels metrics table of Meta's Video Insights reference. The `analytics.instagram` names (call #103) were not checked in this pass; HANDOFF step 6 still asks for that.

### Pre-launch fix pass 2026-11-12 (office PC): hook claims, Gemini outages, captions under music, burned-in text

Known cases from the 11 Nov office preview run, matched in this PC's `pipeline.db` / `media/renders/preview_2026-11-11/_work/preview.db` by creator: Tridzo GTA = **item_763c2e191a1f**, Rockstar "Trailer 2" = **item_9eb56ea0f951**, GamingBolt = **item_a35aead652d3**, Hazard = **item_4622278cd0fc**. The Gemini 503 is `preview.db` stage_log row 18 (item_a35aead652d3: a grounding check's HTTP 503 was counted as attempt 1 of 3).

**Fix 1: hook / voiceover claims not in the segment** (`pipeline/hooks.py`, `config.yaml -> hook_check`)

173. **The Tridzo hook is rebuilt synthetically.** Neither database on this PC holds the "LEAKED ..." hook (the 11 Nov run here only previewed GamingBolt, Trailer 2 and XBOX; item_763c2e191a1f still has its 9 Nov `IS THIS REALLY GTA 6?`), so `tests/test_hook_claims.py` rebuilds it from the report: a LEAKED hook whose only word shared with the segment is VERSION, over the Tridzo window's words ("PC version ... no extra download, one megabyte pack"), and a VO that says the creator "posted this leak". The test first shows the old rule passing it on VERSION, then the new checks failing it, then the stage regenerating once with the reason and sending it to `needs_review`.
174. **"Content words" = what is left after three config lists**: `hook_check.stopwords` (the old hard-coded generic list: function words and hype words), `generic_topic_words` (gta, vi, 6, six, game, rockstar, version, trailer, new, video, plus grand/theft/auto/games/gameplay/clip/footage that were already generic) and `claim_words` (judged by their own rule, so they never count as overlap). At least `min_shared_content_words` 2 must be said in the segment (stems: plural, -ed, -ing, 's). **Judgement call:** a line with only ONE content word passes when that word is said (`single_word_lines_ok: true`), because "WHO IS LUCIA?" can never share two; the Tridzo failure was a multi-word hook carried by one incidental word, which this still catches. Set it to false for a strict 2. The existing test case "BANK HEISTS ARE BACK?" over "you can rob banks again" was PASS on one word and is now FAIL (its expectation was changed, and a both-words-said case added).
175. **Claim words are matched by stem, plus the longer form of a 5+ letter claim** ("leaked"/"leaks" ~ leak, "officially" ~ official, "confirms" ~ confirmed), so "REALLY" is not the claim "real" and the old fallback `IS THIS REALLY GTA 6?` still passes. Unverified: any claim word in the hook or the VO fails, even if the segment says it. Official: each claim word must be said (same stem) in the segment transcript. The check is lexical: a segment saying "this is not a leak" counts as saying "leak"; the Gemini PASS/FAIL that runs after it is the semantic backstop.
176. **The voiceover gets the same two checks as the hook** (claims, then the 2-word overlap): the Tridzo VO was the line that stated the leak as fact. Both run before the Gemini check, so a FAIL costs no Gemini call, and the existing path does the rest (one regeneration with the reason in `PREVIOUS ATTEMPT REJECTED`, then `needs_review`). The generation prompt now lists the claim words with the rule for the item's tag and asks for 2 said words; its old example hook `COULD THIS LEAK BE REAL?` was replaced, since it would now fail.

**Fix 2: Gemini 503 / 5xx / network errors** (`pipeline/hooks.py`, `config.yaml -> gemini`)

177. **An outage is retried inside the call and is never a hook attempt.** HTTP 5xx and requests' `Timeout`, `ConnectionError` and `ChunkedEncodingError` are retried after `gemini.transient_backoff_s` [10, 30, 90]: read as **3 retries after the first try** (up to 4 requests, ~130 s of waiting), each still behind the 12 s pacing. Still failing -> `GeminiTransientError`, from the generation or from the grounding check (which used to swallow it as an "ERROR" verdict; that is how the 11 Nov 503 became attempt 1). The hooks stage then stops for this cycle: the item keeps its stage (`cut`), is never failed or rejected, gets a `stage_log` row with status `transient`, and `items.transient_errors` (new column, migrated by `init_db`) goes up by one. **Judgement call:** a regeneration in progress is not carried over; the next cycle starts at attempt 1 with a fresh check count, because the outage may have lasted hours and the frames/transcript are re-read anyway. `render.run_render_item` returns without failing such an item; `render_previews.py` reports it as "waiting".
178. **One alert at the 6th consecutive cycle** (`gemini.transient_alert_after_cycles`): sent once when the count reaches 6, through `telegram_bot.send_alert` (the existing failure-alert path: it never raises, and only logs when Telegram is off), key `gemini_unavailable:<item>`; cycles 7, 8, ... stay quiet and the item keeps waiting. The count is reset to 0 whenever Gemini answers (a hook, `needs_review`, or a real failure). In the runner loop the hooks stage only runs from the render stage, once per item per cycle, so the count is a cycle count.
179. **Unchanged on purpose:** 429 handling (a per-minute 429 waits the server's retryDelay; a daily-quota 429 still fails fast, so attempts 2 and 3 fail at once and the item is failed, as before), truncated or unparseable answers (a failed attempt), and other 4xx answers (a failed attempt: a bad request does not fix itself).

---

## 14. Checkpoint 1 Re-Proof Verification Data

### Discovery Top 10 with Real Recency Decay
| Video Title | Channel | Views | Age (h) | Velocity | Pct Rank | Decay | Weight | Final Score |
|---|---|---|---|---|---|---|---|---|
| GTA 6 Is All Set To Do The Unbelievable | GamingBolt | 160,484 | 22.0h | 7,284.9/h | 0.90 | 0.654 | 1.00 | 0.579 |
| BREAKING: GTA 6 ONLINE MULTIPLAYER LEAKED.. | Tridzo GTA | 4,622 | 1.5h | 3,038.1/h | 0.65 | 0.971 | 1.00 | 0.574 |
| 15 Things YOU NEED TO DO Before GTA 6 Releases | Hazard | 35,154 | 12.4h | 2,824.4/h | 0.61 | 0.787 | 1.00 | 0.437 |
| I Became A "HITMAN" In GTA 6 VR! (Shiesty) | Kexi | 17,463 | 11.3h | 1,538.9/h | 0.55 | 0.804 | 1.00 | 0.391 |
| GTA 6 Box has Leaked (Poster Included) | GameRiot | 28,411 | 35.5h | 799.5/h | 0.45 | 0.504 | 1.00 | 0.194 |
| GTA 6 "Gas" Leaked Gameplay \| 1080p HDR | CYBERLEEK | 13,705 | 27.9h | 490.5/h | 0.35 | 0.584 | 1.00 | 0.168 |
| Rockstar Updated the GTA 6 Release Time | TmarTn2 | 544,103 | 134.1h | 4,056.4/h | 0.71 | 0.076 | 1.00 | 0.050 |
| GTA 6 GAMEPLAY & LEAKS \| CLIP ESCLUSIVE | GtaVI Clip | 5,973 | 48.0h | 124.3/h | 0.16 | 0.397 | 1.00 | 0.044 |
| GTA 6 - Dev. Build LEAKED (CyberLeek Update!) | GTA6Videos | 26,272 | 102.7h | 255.7/h | 0.29 | 0.138 | 1.00 | 0.031 |
| GTA 6 Gameplay: Jason Free Roam in Vice City | Gar Studio | 199,610 | 148.2h | 1,346.8/h | 0.48 | 0.058 | 1.00 | 0.024 |

*Note: Evergreen Trailer 1 (Rockstar Games, Dec 2023, ~25,000h old) receives decay $0.5^{25000/36} \approx 0.0$ and final score $0.000$, allowing fresh creator uploads to dominate the top ranks.*

### Real Repost Deduplication Proofs
| Comparison Pair | Video Types | Matching Frames | Min Length | Coverage | Result |
|---|---|---|---|---|---|
| Trailer 2 Rockstar vs Trailer 2 PlayStation | Same Trailer (Repost) | 166 | 167 | 99.4% | **CAUGHT AS DUPLICATE** ($\ge 65\%$) |
| Launch Trailer Rockstar vs Launch Trailer Xbox | Same Trailer (Repost) | 86 | 90 | 95.6% | **CAUGHT AS DUPLICATE** ($\ge 65\%$) |
| Trailer 1 Rockstar vs Trailer 2 Rockstar | Different Trailers | 10 | 90 | 11.1% | **NOT FLAGGED** ($< 65\%$) |
| GamingBolt Breakdown vs Trailer 2 Rockstar | Creator video using trailer footage | 18 | 167 | 10.8% | **NOT FLAGGED** ($< 65\%$) |
| GamingBolt Breakdown vs Launch Trailer Rockstar | Creator video using trailer footage | 7 | 90 | 7.8% | **NOT FLAGGED** ($< 65\%$) |

### Real Crash & Resume Test Results
- **Process Spawned**: PID `14592` via CLI `python -m pipeline.runner --db crash_proof.db download`.
- **Intervention**: Killed mid-download via `proc.kill()` during active network payload.
- **Immediate State Post-Crash**:
  - Item row count: `1` (unchanged).
  - Duplicate rows: `0`.
  - Item stage: `discovered`.
  - Stale temp file: `tmp_crash_test_item.f401.mp4.part`.
- **Immediate Rerun**:
  - Dead PID detected on host: `Breaking stale lock: process PID 14592 on host is dead`.
  - Rerun acquired lock in `0.02s` without waiting 600s.
  - Stale temp file cleaned up.
  - Download resumed and completed successfully in 39.15s.
  - Final item stage: `downloaded`.
  - Final media verified on disk via ffprobe.

---

## 15. Checklist of Required Items from User by Checkpoint

| Checkpoint | Item Needed from User | Where to Provide | Status |
|---|---|---|---|
| **CP 0** | Plan approval ("go") | Chat response | Completed. |
| **CP 1** | None (public YouTube) | N/A | **Re-Proof Completed & Passing.** |
| **CP 2** | `GEMINI_API_KEY` | `.env` | Set on the laptop. Style preset still to be picked from the previews (`style.preset`, default P0). |
| **CP 3** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IG_USER_ID`, `IG_ACCESS_TOKEN`, `FB_PAGE_ID`, optionally `FB_PAGE_ACCESS_TOKEN` | `.env` | **Built and tested (mocked).** As of 2026-11-10 none of these is set in the laptop's `.env`; they are needed to run `python scripts/cp3_dry_run.py`. |
| **CP 3** | Explicit "go" before the first live post | Chat response | Pending. `app.dry_run: true` stays until then. |
| **CP 4** | `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` (before 19 Nov), `HEALTHCHECK_URL` (optional) | `.env` | **Built and tested (mocked); `scripts/cp4_dry_run.py` passes every invariant over 10 simulated days.** Scheduler, slot-time variant draw, approval switch, analytics, cleanup, Twitch, healthcheck and the server unit are on `main`. As of 2026-11-10 the laptop's `.env` has none of these keys set. Not yet run against the real APIs. |
| **Go live** | Explicit "go", then `app.dry_run: false` on 19 Nov | Chat response, `config.yaml` | Pending. Needs the CP3 keys (SETUP.md), a passing CP3 real-token dry run, and the Meta app switched to **Live** with the PRIVACY.md URL (HANDOFF step 5). |

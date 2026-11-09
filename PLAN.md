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

### Cut End Snapping (Variant A audio cut-off fix)
- `cut.snap_end_to_pause`: the END is snapped like the start: (1) a whisper word gap ≥ 0.25 s inside [start+18, start+22] (end placed in the gap, ≥0.1 s after the last word, closest to the proposed end); else (2) the ebur128 momentary-loudness trough (3-point smoothed, ties within 1 LU → closest to proposed); else (3) the proposed end.
- Cut stage: loudness trough on the source (no transcript yet). Render stage: `render.snap_render_end` refines inside the existing cut with whisper words (bounds 18 s → cut length, e.g. 18-20 s for a fixed 20 s cut), falling back to the cut's loudness trough.

### Variant B: Freeze-Frame Voiceover (replaces ducking)
- **VO Script**: one punchy 3-6 s line (Gemini, grounded); **Kokoro TTS** (`am_adam`).
- **Video**: the cut's FIRST frame is held as a still (`tpad=start=N:start_mode=clone`) for `freeze = VO duration + 0.3 s` (rounded up to a whole frame), then the cut plays from its start.
- **Audio**: VO only during the freeze (padded with silence to exactly the freeze), then the cut's original audio at full level (`concat`), then the shared loudnorm/48 kHz/fade tail. No sidechaincompress/amix (sidechaincompress ended at its shortest input, which cut CP2's B audio to 3.1 s).
- **Length**: B = freeze + cut (≈ 23 s for a 20 s cut), intended.
- **Captions**: VO words (whisper on the VO wav, else evenly spread script words) during the freeze, then the cut's speech captions offset by the freeze.

### Verification Script
- `python scripts/render_check.py` renders A_v2/B_v2 for every `samples/cp2/<id>/` (cut.mp4, vo.wav, item.json) or, if no pack exists, a synthetic 20 s clip; writes `contact_sheet.png` (A @1 s, B mid-freeze, B freeze+2 s, A last 0.5 s, plus A/B waveforms) and `render_report.json`, prints the ffprobe table and measured cap heights, exits non-zero on any failure.

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
- **Reddit items**:
  `{hook_text}\n\nCredit: {author_name}\nFound via r/{subreddit}\n.\n#gta6 #grandtheftauto #gta #gaming #rockstargames`
- **YouTube items**:
  `{hook_text}\n\nCredit: {author_name}\n.\n#gta6 #grandtheftauto #gta #gaming #rockstargames`

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
publishing limit, the Page) and then runs the real posting flow in dry-run mode. It cannot publish:
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
33. **A `FINISHED` Container Counts as Unknown (CP3)**: on resume in `publishing`, only `ERROR` / `EXPIRED` (publishing impossible) lets a fresh container be started. A container reading `FINISHED` with no matching recent media is treated as *unknown* and the row is failed and alerted, never re-published: Instagram's `/media` list is eventually consistent, so "FINISHED and not in `/media`" cannot distinguish a publish that never happened from one whose media has not yet appeared. A duplicate public post is worse than a post that needs one manual retry.
34. **Recent-Media Matching by Caption and Time**: Graph gives no way to ask "which media came from container X", so a published media is identified by its caption's first line matching `posts.caption_text` and its timestamp being at or after `posts.publish_started_at`. The caption is therefore frozen onto the row when the row is created, not rebuilt at publish time.
35. **Dry Run Defaults to `plan_only` (CP3)**: "everything except the publish call" was read conservatively. Creating a container and uploading bytes are real writes against the live account that leave unpublished containers behind, so the default dry run sends nothing and instead walks the whole state machine while writing a redacted plan of every request. `posting.dry_run_mode: up_to_publish` gives the literal reading (real container, real upload, no publish) as a one-line opt-in.
36. **A Dry Run Never Advances an Item to `posted`**: `run_posting_for_item` marks the item `posted` only when every platform row is `live`. All-`dry_run` leaves the stage alone, otherwise flipping `dry_run: false` would find the item already "posted" and it would never actually go out.
37. **`max_duration_seconds` Applies to Variant A Only (CP3)**: PLAN.md section 7 / call #25 make Variant B a freeze-frame plus the cut (approximately 23 s), which the 22.0 s gate would have rejected on every item. The gate now caps Variant A at `max_duration_seconds` and allows Variant B up to `approval.auto_quality_gate.max_b_extra_seconds` (8.0 s) longer than A.
38. **The Reviewer Does Not Pick the Variant**: `Approve` leaves `chosen_variant` NULL for the CP4 scheduler's bandit to assign. Only `Approve A only` pins a variant, and it sets `items.ab_excluded = 1` so that item never enters the A/B comparison.
39. **New Columns for CP3**: `items.ab_excluded`, and on `posts`: `ab_excluded`, `attempt_count`, `publish_started_at` and `caption_text`. All added with `ALTER TABLE` migrations in `db.init_db`, so an existing `pipeline.db` upgrades in place.
40. **Stale Protection Keys on the Message Id and the Item Id**: both the recorded `mid` and the recorded `item_id` must match the click. The message id alone would let a tap on a re-sent card decide the wrong item; the item id alone would let an old message decide a current item.
41. **Strict One-at-a-Time Enforced in `kv`, Not in Memory**: the open card lives in the database, so a restarted runner still refuses to send a second card and an item whose decision was lost is re-offered rather than skipped.
42. **The `getUpdates` Offset Is Saved Before the Update Is Handled**: a crash mid-handling therefore skips that tap instead of replaying it. Replaying a decision is worse than losing one, which the user can simply tap again.
43. **Telegram Alerts Are Deduplicated for an Hour per Key**: a failure that recurs every cycle would otherwise fill the approval chat and bury the cards.
44. **Meta Request Pacing Lives in the Database**: `kv:meta_api:next_free` keeps `posting.meta_min_gap_seconds` between any two Meta requests across processes, so the publisher, the bot and the dry-run script cannot collectively trip the limit.
45. **Rate-Limit Pause Is Source-Wide**: a 429 or block pauses the whole `meta_graph` source for `posting.rate_limit_pause_hours` (3 h), not just the failing row, and the row is left at its current status rather than failed, so it resumes cleanly when the pause lifts.
46. **Facebook Can Use Its Own Page Token**: `FB_PAGE_ACCESS_TOKEN` is read first and falls back to `IG_ACCESS_TOKEN`, since Page Reels need a PAGE token with `pages_manage_posts` while Instagram publishing needs `instagram_content_publish`; one token often covers both, but not always.
47. **Graph API v25.0 Pinned in Config**: `posting.api_version`, `posting.graph_base` and `posting.rupload_base` are config keys, so a version bump is an edit rather than a code change.

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
| **CP 2** | `GEMINI_API_KEY` | `.env` | Needed for hook generation and VO scripting. |
| **CP 3** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IG_USER_ID`, `IG_ACCESS_TOKEN`, `FB_PAGE_ID`, optionally `FB_PAGE_ACCESS_TOKEN` | `.env` | **Built and tested (mocked).** Keys needed to run `python scripts/cp3_dry_run.py` on the laptop. |
| **CP 3** | Explicit "go" before the first live post | Chat response | Pending. `app.dry_run: true` stays until then. |
| **CP 4** | `HEALTHCHECK_URL` (optional), `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` (before 19 Nov) | `.env` | Needed for dead-man switch and Twitch Helix. |

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

- **A1 SFX**: boom on the hook's first frame, whoosh `whoosh_lead_s 0.06` before each
  punch-in. Each cue is gain-staged so its peak lands `headroom_db 6.0` **under the clip's
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
- **Reddit items**:
  `{hook_text}\n\nCredit: {author_name}\nFound via r/{subreddit}\n.\n#gta6 #grandtheftauto #gta #gaming #rockstargames`
- **YouTube items**:
  `{hook_text}\n\nCredit: {author_name}\n.\n#gta6 #grandtheftauto #gta #gaming #rockstargames`

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
| **CP 3** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IG_USER_ID`, `IG_ACCESS_TOKEN`, `FB_PAGE_ID` | `.env` | Needed for Telegram card & social publishing. |
| **CP 4** | `HEALTHCHECK_URL` (optional), `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` (before 19 Nov) | `.env` | Needed for dead-man switch and Twitch Helix. |

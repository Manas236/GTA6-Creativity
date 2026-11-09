# GTA 6 Clipping Channel: Fully Automated Short-Video Pipeline
## Architecture Plan & Engineering Specification (PLAN.md - Revision 2)

---

## 1. System Overview & Core Principles

This pipeline automates the end-to-end lifecycle of short vertical videos (Reels) covering Grand Theft Auto VI:
1. **Discover**: Find viral, trending content from YouTube and Reddit (and Twitch post-launch).
2. **Download & Dedupe**: Fetch source media idempotently to a temp file with atomic rename; deduplicate by source ID and multi-frame 1 fps perceptual hashing with black-bar cropping and consecutive subsequence matching.
3. **Pull-Based Buffer**: Maintain a capped buffer of ready items (default 6), pulling and processing the highest-scoring items first to conserve disk and API quotas.
4. **Cut**: Select optimal ~20s highlight windows via audio loudness peak analysis and scene-change density. Re-check perceptual deduplication at the cut level.
5. **Enrich & Render**:
   - Generate curiosity, question, or hype/analysis hooks via Gemini free tier, strictly grounded in the video (verified by a yes/no grounding check against transcript, 4 frames, and title; falling back to a plain question hook after 2 failures).
   - Hook burned via ASS subtitle file (not drawtext) to support line wrapping and special characters.
   - Transcribe word-level timestamps via local `faster-whisper`. If no speech is detected, Variant A displays the hook only.
   - Synthesize a punchy 3–6s voiceover line near the start via local `Kokoro TTS`.
   - Render two 1080x1920 (9:16) variants (**Variant A**: clip + hook + captions; **Variant B**: A + ducked original audio during VO + Kokoro voiceover + combined VO/speech captions).
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
   - Crash resilience via SQLite WAL mode and atomic file swaps.

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
│   ├── render.py             # Stage 7: ffmpeg 9:16 compositing, ASS captions & hook, asplit ducking
│   ├── telegram_bot.py       # Stage 8: Single self-editing Telegram approval & alert card
│   ├── posting.py            # Stage 9: Two-phase Meta Graph API Reel uploader (IG + FB)
│   ├── scheduler.py          # Stage 10: 2 daily slots, A/B bandit allocator, healthchecks ping
│   ├── analytics.py          # Stage 11: 24h/72h metric scraping & normalized bandit cache rebuild
│   ├── cleanup.py            # Stage 12: Disk retention manager (source, render, rejected/failed purge)
│   └── runner.py             # Single long-running orchestrator with stale-lock detection
├── assets/
│   └── fonts/                # Bundled open-source fonts (Montserrat-Bold.ttf)
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
    ├── test_voiceover.py     # 3-6s TTS generation, audio timing, ducking asplit parameters
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
    source TEXT NOT NULL,                      -- 'youtube', 'reddit', 'twitch'
    source_id TEXT NOT NULL,                   -- e.g. YouTube video ID or Reddit submission ID
    source_url TEXT NOT NULL,                  -- Original URL
    author_name TEXT NOT NULL,                 -- Original creator/channel for credit
    original_title TEXT,                       -- Source title
    original_duration REAL,                    -- Duration in seconds
    view_count INTEGER DEFAULT 0,              -- Raw view count at discovery
    upvote_count INTEGER DEFAULT 0,            -- Reddit upvotes / YT likes
    comment_count INTEGER DEFAULT 0,           -- Reddit / YT comments
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

## 4. Discovery Score Formula (Normalized Percentile & Single Source Weight)

Per user requirements:
- Raw velocity is computed per source.
- Raw velocity is converted to a percentile rank within that source's recent items (last 100 items discovered from that source).
- Recency decay and the source weight (`sources.<name>.weight`) are applied.
- Source weights live in ONE place: `sources.<name>.weight`.

### Velocity Formulas by Source
- **YouTube**:
  $$\text{RawVelocity} = \frac{\text{views}}{\max(\text{age\_hours}, \text{min\_age\_hours})}$$
- **Reddit**:
  $$\text{RawVelocity} = \frac{\text{upvotes} + (\text{comments} \times 2.0)}{\max(\text{age\_hours}, \text{min\_age\_hours})}$$
- **Twitch**:
  $$\text{RawVelocity} = \frac{\text{views}}{\max(\text{age\_hours}, \text{min\_age\_hours})}$$

### Percentile Rank Calculation
Within the recent rolling pool for source $S$:
$$\text{PercentileRank} = \frac{\text{rank of item's RawVelocity in pool}}{N_{\text{pool}}} \in [0.01, 1.0]$$

### Overall Discovery Score Formula in `config.yaml`
```yaml
discovery:
  min_age_hours: 0.5
  half_life_hours: 36.0
  # Formula evaluated dynamically via safe expression evaluator:
  score_formula: >
    percentile_rank * (0.5 ** (age_hours / half_life_hours)) * source_weight
```

---

## 5. Deduplication: Black-Bar Cropping, 1 fps Hashing & Consecutive Matching

Per user requirements:
1. **Black-Bar Cropping**: Run ffmpeg `cropdetect=24:16:0` on sample frames to identify letterboxing/pillarboxing, and crop active video content before generating hashes.
2. **1 fps Fingerprinting**: Extract frames at 1 frame per second (`fps=1`).
3. **64-bit dHash Generation**: Convert cropped frame to $9\times8$ grayscale; calculate difference between adjacent pixels to yield a 64-bit integer hash.
4. **Consecutive Subsequence Matching**:
   - Compare the candidate sequence of hashes against stored sequences.
   - If there exist $N$ consecutive frames ($N=3$ by default, configurable) where every corresponding frame has a Hamming distance $\le \text{hamming\_threshold}$ (8 bits by default), the candidate is flagged as a duplicate.
5. **Two-Level Application**:
   - **Download-Level (CP1)**: Run on the full downloaded video against all previously seen download fingerprints. If duplicate, mark item `rejected: duplicate_download`.
   - **Cut-Level (CP2)**: Run on the chosen 20s cut window against all previously rendered/queued/posted cuts. If duplicate, attempt the next-best 20s cut window. If all candidate windows match prior cuts, mark item `rejected: duplicate_cut`.

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

### Canvas Layout (1080x1920, 9:16)
- **Background Layer**: Source video scaled to cover 1080x1920, cropped, blurred with `boxblur=25:25`, and darkened by -22% brightness.
- **Foreground Layer**: Source video centered vertically.
  - A standard 16:9 widescreen video at 1080px wide is exactly **607.5px (608px) tall** ($1080 \times 9 / 16$).
  - Vertical position: $Y = (1920 - 608) / 2 = 656\text{px}$.
- **Hook Title Overlay**:
  - Embedded inside the **ASS subtitle file** (not `drawtext`) with `\pos(540, 240)` and text alignment, avoiding apostrophe and colon escaping bugs and ensuring auto-wrapping.
- **Word-by-Word Highlighted Captions**:
  - Transcribed by `faster-whisper`.
  - Positioned at $Y \approx 1540\text{px}$ in the ASS file.
  - Active word highlighted in yellow/cyan (`{\c&H00FFFF&}`), inactive words in white (`{\c&HFFFFFF&}`).
  - **No speech fallback**: If whisper finds no speech (music/gameplay only), Variant A displays the hook only.

### Voiceover & Audio Ducking (Variant B)
- **VO Script**: A single punchy line of **3–6 seconds** near the start, written by Gemini based on grounded clip action.
- **Kokoro TTS**: Generates local voiceover WAV (`am_adam` or `af_heart`).
- **Audio Ducking**:
  - Split TTS stream using `asplit`: one stream feeds the sidechain compressor input, the other mixes into the output.
  - Original audio is ducked by $-14\text{dB}$ only while the TTS line plays, returning to full volume immediately afterwards:
    ```
    [1:a]asplit=2[vo_duck][vo_mix];
    [0:a][vo_duck]sidechaincompress=threshold=0.08:ratio=8:attack=40:release=350[ducked];
    [ducked][vo_mix]amix=inputs=2:duration=first:dropout_transition=2[a_out]
    ```
- **Variant B Captions**: The VO line's words (timestamps from whisper run on the TTS wav) followed by the original speech captions.

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
  font_path: "assets/fonts/Montserrat-Bold.ttf"
  whisper_model: "base.en"
  target_lufs: -14.0
  ducking_attenuation_db: -14.0
  voiceover_target_duration_s: 4.5

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
  stale_lock_seconds: 600
```

---

## 12. Kick Streaming API & Cloud IP Blocking Notes

### Kick Streaming API Assessment
Kick does provide an official public API (`api.kick.com/public`) requiring application credentials, but it currently lacks a clips endpoint. Clips are only served via undocumented website endpoints, which constitutes scraping. Per project rules (*"do not scrape around it"*), Kick remains disabled until official clips support is released.

### Cloud Server IP Blocking Note
Upon deploying to a cloud Linux server (AWS/Hetzner/DigitalOcean), YouTube and Reddit frequently block datacenter IP ranges (HTTP 429 / CAPTCHA). Per project rules (*"do not add cookies or proxies to get around it"*), the pipeline must detect blocking, pause the affected source, emit a single Telegram alert, and continue operating autonomously using remaining unblocked sources (e.g. Reddit JSON or Twitch Helix).

---

## 13. Calls I Made (Comprehensive Engineering Audit)

1. **Virtual Environment Python Version**: Selected `Python 3.11` for `.venv` because `Kokoro TTS` requires `numpy 1.26.4` which lacks binary wheels for Python 3.14 on Windows and triggers C-compilation failures, whereas Python 3.11 provides fully compatible prebuilt binary wheels for Kokoro, Torch, and faster-whisper.
2. **Audio/Scene Cut Weights**: Standardized cut scoring to 0.75 audio loudness weight and 0.25 visual scene-cut density.
3. **Intro/Outro Penalty Window**: Applied a 5% duration penalty to the beginning and end of long-form videos to avoid channel intros and outro slates.
4. **Target Cut Duration Range**: Hard-bounded between 18.0s and 22.0s with a 20.0s target window.
5. **YouTube Query Duration Ceiling**: Capped search results at 600s (10 minutes) to avoid downloading multi-hour livestreams.
6. **Local Whisper Model**: Selected `base.en` as default for sub-second CPU transcription latency while retaining high word-timestamp accuracy.
7. **Font Choice**: Bundled open-source `Montserrat-Bold.ttf` for high vertical readability.
8. **Audio Loudness Target**: Standardized at $-14.0\text{ LUFS}$ per Instagram and Facebook Reels ingest specs.
9. **Audio Ducking Depth**: Standardized at $-14.0\text{ dB}$ attenuation during the 3–6s voiceover line.
10. **Ready Buffer Size**: Fixed at 6 ready rendered items to prevent unbounded disk usage and conserve Gemini API calls.
11. **Deduplication Thresholds**: Set consecutive matching frames $N=3$ at 1 fps with Hamming distance $\le 8$ bits on cropped frames.
12. **Bandit Exploration Floor**: Set minimum exploration floor at 20% for each variant to guarantee ongoing statistical validation.
13. **Stale Lock Timeout**: Set orchestrator lock timeout at 600 seconds (10 minutes) to safely recover from unexpected process crashes.
14. **Failed Media Retention**: Set retention for rejected and failed item media at 3 days before disk purge.

---

## 14. Checklist of Required Items from User by Checkpoint

| Checkpoint | Item Needed from User | Where to Provide | Status |
|---|---|---|---|
| **CP 0** | Plan approval ("go") | Chat response | Completed. |
| **CP 1** | None (public YouTube and Reddit JSON) | N/A | Current checkpoint. |
| **CP 2** | `GEMINI_API_KEY` | `.env` | Needed for hook generation and VO scripting. |
| **CP 3** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IG_USER_ID`, `IG_ACCESS_TOKEN`, `FB_PAGE_ID` | `.env` | Needed for Telegram card & social publishing. |
| **CP 4** | `HEALTHCHECK_URL` (optional), `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET` (before 19 Nov) | `.env` | Needed for dead-man switch and Twitch Helix. |

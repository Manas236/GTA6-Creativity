# GTA 6 Clipping Channel: Fully Automated Short-Video Pipeline
## Architecture Plan & Engineering Specification (PLAN.md)

---

## 1. System Overview & Core Principles

This pipeline automates the end-to-end lifecycle of short vertical videos (Reels) covering Grand Theft Auto VI:
1. **Discover**: Find viral, trending content from YouTube and Reddit (and Twitch post-launch).
2. **Download & Dedupe**: Fetch source media idempotently; deduplicate by source ID and multi-frame perceptual hashing.
3. **Cut**: Select optimal ~20s highlight windows via audio loudness peak analysis and scene-change density.
4. **Enrich & Render**: Generate curiosity/question hooks via Gemini free tier; transcribe word-level timestamps via local `faster-whisper`; synthesize local TTS voiceovers via local `Kokoro TTS`; render two 1080x1920 (9:16) variants (**Variant A**: clip + hook + captions; **Variant B**: A + ducked original audio + Kokoro TTS voiceover).
5. **Approve & Schedule**: Single self-editing Telegram status card (Week 1 review) transitioning via a single config flag (`AUTO_APPROVE=true`) to autonomous publishing; queue with 2 scheduled daily slots.
6. **Publish & Track**: Resumable chunked upload to Instagram Reels and Facebook Page Reels via Meta Graph API; pull 24h/72h performance metrics; update Bayesian A/B weights across variant, hook style, and source.
7. **Maintenance & Safety**: Dead-man switch via healthchecks.io; failure-only Telegram alerts; automatic disk cleanup (source deleted post-render, renders deleted 7 days post-publication); crash resilience with SQLite WAL mode.

---

## 2. Folder & File Layout

All code and assets reside inside this project directory (`c:\Users\Admin\Desktop\Creativity`). Code is strictly cross-platform using `pathlib.Path` so it can run interchangeably on Windows and Linux.

```
Creativity/
├── .env.example              # Secret key template with one-line descriptions
├── .env                      # Real secrets (gitignored, never committed or printed)
├── .gitignore                # Git ignore rules for media, db, cache, secrets
├── PLAN.md                   # This master architectural blueprint
├── README.md                 # Setup, operations, and Linux deployment guide (CP4)
├── config.yaml               # Pipeline settings, scoring formula, schedules, fonts
├── requirements.txt          # Python dependencies
├── pipeline/
│   ├── __init__.py           # Package marker
│   ├── config.py             # Config loader, env validator, brand constants
│   ├── db.py                 # SQLite WAL state store, migrations, transactions
│   ├── errors.py             # Custom exceptions, error categorizations
│   ├── rate_limit.py         # Per-source rate limiting and cooldown state
│   ├── discover.py           # Stage 1: YouTube, Reddit JSON, Twitch Helix discovery
│   ├── download.py           # Stage 2: Resumable, crash-safe media fetch via yt-dlp
│   ├── dedupe.py             # Stage 3: Perceptual hash generation & Hamming deduplication
│   ├── cut.py                # Stage 4: Audio loudness peaks & scene cut window selection
│   ├── hooks.py              # Stage 5: Gemini hook generation & style taxonomy
│   ├── voiceover.py          # Stage 6: Gemini VO script writing & local Kokoro TTS synthesis
│   ├── render.py             # Stage 7: ffmpeg 9:16 compositing, whisper captions, audio ducking
│   ├── telegram_bot.py       # Stage 8: Single self-editing Telegram approval & alert card
│   ├── posting.py            # Stage 9: Meta Graph API resumable Reel uploader (IG + FB)
│   ├── scheduler.py          # Stage 10: 2 daily slots, A/B queue allocator, healthchecks ping
│   ├── analytics.py          # Stage 11: 24h/72h metric scraping & bandit weight updates
│   ├── cleanup.py            # Stage 12: Disk retention manager (source & render purge)
│   └── runner.py             # Pipeline orchestrator and CLI entry points (`run`, `doctor`, `test`)
├── assets/
│   └── fonts/                # Bundled open-source fonts (e.g. Montserrat-Bold.ttf)
├── media/                    # Local storage (gitignored)
│   ├── downloads/            # Source media (purged immediately post-render)
│   ├── cuts/                 # 20s cropped clips
│   ├── audio/                # Kokoro TTS audio clips
│   └── renders/              # Final Variant A & B MP4s (purged 7 days post-publish)
└── tests/
    ├── conftest.py           # Mock fixtures: DB, yt-dlp, Reddit, Gemini, Meta, Telegram
    ├── test_config.py        # Config & env validation tests
    ├── test_db.py            # WAL mode, state transitions, idempotency, crash resume
    ├── test_discover.py      # Source parsing and discovery score formula calculation
    ├── test_download.py      # Download retry, rate limit pause, partial resume
    ├── test_dedupe.py        # Perceptual hash collision & duplicate rejection
    ├── test_cut.py           # Audio peak + scene density scoring and boundary snap
    ├── test_hooks.py         # Hook style prompts, Gemini parsing, fallback hooks
    ├── test_voiceover.py     # TTS generation, audio timing, ducking parameters
    ├── test_render.py        # ffmpeg filtergraph syntax, ASS subtitle generation
    ├── test_telegram.py      # Self-editing status card, approval flow, stale clicks
    ├── test_posting.py       # Meta Graph resumable upload flow, rate limit gap
    ├── test_scheduler.py     # Slot math, missed slots, A/B variant rotation
    ├── test_analytics.py     # Metrics ingestion, bandit weight updates
    └── test_cleanup.py       # Source cleanup post-render, 7-day render cleanup
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
Items progress sequentially through forward stages:
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
    upvote_ratio REAL DEFAULT 1.0,             -- Reddit upvote ratio or YT like ratio
    upload_time TEXT NOT NULL,                 -- ISO-8601 UTC timestamp of creation
    discovered_at TEXT NOT NULL,               -- ISO-8601 UTC when discovered
    discovery_score REAL NOT NULL,             -- Calculated discovery score
    
    -- Download & Deduplication
    download_path TEXT,                        -- Local file path to raw download
    downloaded_at TEXT,                        -- Timestamp download finished
    perceptual_hash TEXT,                      -- Hex fingerprint string of keyframes
    is_duplicate INTEGER DEFAULT 0,            -- 1 if flagged duplicate
    
    -- Cut Stage
    cut_start REAL,                            -- Seconds into source video
    cut_end REAL,                              -- Seconds into source video
    cut_duration REAL,                         -- Target ~20s
    cut_path TEXT,                             -- Local file path to cut clip
    
    -- Content & Copy
    hook_style TEXT,                           -- 'curiosity', 'question', 'hype_analysis'
    hook_text TEXT,                            -- Hook title overlay
    voiceover_script TEXT,                     -- TTS script for Variant B
    voiceover_audio_path TEXT,                 -- Audio file for Variant B
    caption_text TEXT,                         -- Post description with creator credit & tags
    
    -- Renders
    render_a_path TEXT,                        -- Local path to Variant A MP4
    render_b_path TEXT,                        -- Local path to Variant B MP4
    chosen_variant TEXT,                       -- 'A' or 'B' (assigned by scheduler/user)
    
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

-- 2. Perceptual Fingerprints (for fast multi-frame deduplication)
CREATE TABLE IF NOT EXISTS fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    frame_pct REAL NOT NULL,                   -- e.g. 0.20, 0.40, 0.60, 0.80
    phash_uint64 INTEGER NOT NULL,             -- 64-bit perceptual hash integer
    created_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_fingerprints_item ON fingerprints(item_id);

-- 3. Social Media Posts
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id TEXT NOT NULL,
    platform TEXT NOT NULL,                    -- 'instagram', 'facebook'
    variant TEXT NOT NULL,                     -- 'A' or 'B'
    hook_style TEXT NOT NULL,                  -- Captured for learning
    source TEXT NOT NULL,                      -- Captured for learning
    slot_time TEXT NOT NULL,                   -- Target slot time, e.g. '14:30'
    status TEXT NOT NULL,                      -- 'pending', 'uploading', 'live', 'failed', 'dry_run'
    remote_id TEXT,                            -- IG Media ID or FB Video ID
    remote_url TEXT,                           -- Permalink to published Reel
    error_message TEXT,
    posted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(item_id) REFERENCES items(id) ON DELETE CASCADE,
    UNIQUE(item_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
CREATE INDEX IF NOT EXISTS idx_posts_posted_at ON posts(posted_at);

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

-- 5. Multi-Armed Bandit / Bayesian A/B Weights
CREATE TABLE IF NOT EXISTS ab_learning (
    dimension TEXT NOT NULL,                   -- 'variant', 'hook_style', 'source'
    dimension_value TEXT NOT NULL,             -- e.g. 'A', 'B', 'curiosity', 'youtube'
    sample_count INTEGER DEFAULT 0,            -- Number of published posts
    total_views INTEGER DEFAULT 0,             -- Cumulative views
    mean_log_views REAL DEFAULT 0.0,           -- Logarithmic mean (resilient to viral outliers)
    weight REAL DEFAULT 1.0,                   -- Sampling weight for allocation
    updated_at TEXT NOT NULL,
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

-- 7. Key-Value Store (Mutex locks, heartbeats, Telegram status card message ID)
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

## 4. Pipeline Stages: Inputs, Outputs & Processing

| Stage | Name | Input | Output | Details |
|---|---|---|---|---|
| **1** | `discover` | Search queries, Subreddit hot JSON, Twitch API | DB entries (`items`) in `discovered` | Computes discovery score from formula in `config.yaml`. Checks `source_id` uniqueness. |
| **2** | `download` | Top items in `discovered` | Raw video in `media/downloads/` | `yt-dlp` python interface with rate-limiting, socket timeouts, and exponential backoff. Marks stage `downloaded`. |
| **3** | `dedupe` | Raw video file | Perceptual fingerprints in DB | Extracts 4 keyframes; calculates 64-bit dHash/pHash. If Hamming distance $\le 8$ bits on $\ge 3$ frames against prior items, mark `rejected: duplicate_perceptual`. |
| **4** | `cut` | Downloaded video file | 20s cropped clip in `media/cuts/` | If $\le 30$s, trims to center/start. If $> 30$s, computes audio loudness peaks + scene-cut density sliding window, snaps to scene boundary. Marks stage `cut`. |
| **5** | `hooks` | Cut video transcript / source title & tags | Hook title & style tag in DB | Gemini free tier API formats curiosity/question hook. Style recorded separately for A/B tracking. |
| **6** | `voiceover` | Video transcript & hook | Kokoro TTS audio in `media/audio/` | Gemini generates a 15–18s punchy narration; Kokoro generates audio locally (for Variant B). |
| **7** | `render` | Cut video, hook, whisper words, Kokoro audio | `media/renders/<id>_A.mp4` and `<id>_B.mp4` | ffmpeg composites 1080x1920 (9:16) with blurred background fill, centered video, top hook title, bottom word-highlighted captions. Variant B ducks audio under TTS. Deletes raw download. Marks `awaiting_approval`. |
| **8** | `approval` | Rendered items in `awaiting_approval` | Item stage `approved` or `rejected` | **Week 1**: Telegram single self-editing status card with video, hook, variant buttons. **Auto mode** (`AUTO_APPROVE=true`): Automatically approves based on score threshold and A/B bandit. |
| **9** | `posting` | Approved items in `approved` | Meta Graph Reel publication | Resumable chunked upload (`rupload.facebook.com`) to Instagram Reels + Facebook Page Reels. Claims slot atomically. Sends healthchecks.io ping. Marks `posted`. |
| **10** | `measure` | Posts at 24h and 72h | Records in `metrics` & updates `ab_learning` | Fetches views, likes, comments via Graph API. Updates Bayesian weights for variant, hook style, and source. Marks `measured`. |
| **11** | `cleanup` | Source media & 7-day-old renders | Disk space reclaimed | Deletes downloads immediately post-render; deletes renders 7 days post-publish. Marks `cleaned`. |

---

## 5. Discovery Score Formula Specification

Per hard requirement: **"The formula lives in config, not code."**

### Formula in `config.yaml`
```yaml
discovery:
  min_age_hours: 0.5
  half_life_hours: 36.0
  velocity_power: 1.2
  reddit_score_weight: 1.5
  source_weights:
    youtube: 1.0
    reddit: 1.25
    twitch: 1.10
  # Evaluated dynamically via safe math parser:
  score_formula: >
    ( (views + (upvotes * reddit_score_weight)) / max(age_hours, min_age_hours) ) ** velocity_power
    * ( 0.5 ** (age_hours / half_life_hours) )
    * source_weight
```

### Mathematical Definitions
- $\text{age\_hours} = \max\left(\frac{t_{\text{now}} - t_{\text{upload}}}{3600}, \text{min\_age\_hours}\right)$
- $\text{views}$: Views count from YouTube/Twitch, or Reddit comment count + upvotes.
- Velocity: $\text{VPH} = \frac{\text{views} + (\text{upvotes} \cdot W_{\text{reddit}})}{\text{age\_hours}}$.
- Recency Decay: $D = 0.5^{\frac{\text{age\_hours}}{T_{1/2}}}$ (exponential half-life decay prioritizing fresh breaking clips over older saturated ones).
- Source Weight: $W_{\text{source}}$ boosts or balances platforms.
- Score: $\text{Score} = (\text{VPH})^{\alpha} \cdot D \cdot W_{\text{source}}$.

The evaluation engine uses Python's `ast` (or a restricted math dict) to parse and evaluate the formula string at runtime without `eval()`, ensuring no code modification is needed to adjust the formula.

---

## 6. Cut Window Selection Algorithm

The goal is to extract a captivating ~20s segment from raw videos that are often several minutes long.

### Algorithm Steps:
1. **Short Video Bypass**:
   - If source duration $T \le 30.0$ seconds (common for Reddit clips and Twitch clips):
     - If $T \le 22.0$s: keep full clip.
     - If $22.0\text{s} < T \le 30.0\text{s}$: trim $[0, 20.0]$ or center slice $[\frac{T - 20}{2}, \frac{T + 20}{2}]$.

2. **Long Video Analysis ($T > 30.0$s)**:
   - **Step A: Audio Loudness Profiling via ffmpeg**:
     - Run `ffmpeg -i input.mp4 -af ebur128=meter=100 -f null -` to extract momentary loudness ($M$ in LUFS) sampled every 100ms.
     - Normalize loudness values $L(t)$ into relative energy: $E_{\text{audio}}(t) = \frac{L(t) - L_{\min}}{L_{\max} - L_{\min}} \in [0, 1]$.
   - **Step B: Visual Scene-Change Density**:
     - Run `ffmpeg -i input.mp4 -vf "select='gt(scene,0.3)',showinfo" -f null -` to capture scene-cut timestamps $S = \{t_1, t_2, \dots, t_k\}$.
     - Visual activity score $A_{\text{scene}}(t, t+W)$ counts scene transitions in the 20s window, penalizing dead stretches ($>8$s without a cut) and hyper-erratic montages ($>12$ cuts in 20s).
   - **Step C: Sliding Window Scoring**:
     - Target window width $W = 20.0$s, evaluated at 1.0s increments from $t = 0$ to $t = T - W$:
       $$\text{WindowScore}(t) = 0.45 \cdot \text{MeanAudioEnergy}(t, t+W) + 0.35 \cdot \text{PeakAudioEnergy}(t, t+W) + 0.20 \cdot A_{\text{scene}}(t, t+W) - \text{Penalty}_{\text{intro\_outro}}(t)$$
       *where $\text{Penalty}_{\text{intro\_outro}}$ discourages channel intros (first 5% of video) and subscribe outros (last 5% of video).*
   - **Step D: Boundary Snapping**:
     - Let $t^*$ be the highest scoring start time. Snap $t^*$ to the nearest preceding scene cut in $[t^* - 1.5\text{s}, t^* + 0.5\text{s}]$, or to the nearest audio energy trough (speech pause).
     - Clip exact segment: `ffmpeg -ss <snapped_start> -i input.mp4 -t 20.0 -c:v libx264 -c:a aac cut.mp4`.

---

## 7. Render Layout & Audio Engineering

### Visual Canvas Composition (1080x1920, 9:16)

```
+---------------------------------------------------+  Y = 0
| [TOP SAFE ZONE - Avoid Reels channel UI]          |
|                                                   |
| +-----------------------------------------------+ |  Y = 180
| |               HOOK TITLE OVERLAY              | |
| |   "NEW GTA 6 LEAK REVEALS UNDERWATER MAP?!"   | |
| +-----------------------------------------------+ |  Y = 340
|                                                   |
| + - - - - - - - - - - - - - - - - - - - - - - - + |  Y = 460
| |                                               | |
| |           FOREGROUND CENTERED VIDEO           | |
| |           (1080px wide, original AR)          | |
| |                                               | |
| |     [Behind: Blurred & Darkened Fill of       | |
| |      the exact same video stretched to 9:16]  | |
| |                                               | |
| + - - - - - - - - - - - - - - - - - - - - - - - + |  Y = 1460
|                                                   |
| +-----------------------------------------------+ |  Y = 1480
| |     WORD-BY-WORD HIGHLIGHTED CAPTIONS         | |
| |         "LOOK AT THIS SECRET DETAIL"          | |
| +-----------------------------------------------+ |  Y = 1640
|                                                   |
| [BOTTOM SAFE ZONE - Avoid caption, sound & icons] |
+---------------------------------------------------+  Y = 1920
```

### ffmpeg Complex Filtergraph
```
[0:v]split=2[fg_in][bg_in];
[bg_in]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=25:25,eq=brightness=-0.22[bg];
[fg_in]scale=1080:-2[fg];
[bg][fg]overlay=(W-w)/2:(H-h)/2[comp];
[comp]drawtext=fontfile='assets/fonts/Montserrat-Bold.ttf':text='%{hook_text}':fontcolor=white:fontsize=52:box=1:boxcolor=black@0.65:boxborderw=20:x=(w-text_w)/2:y=220[v_hook];
[v_hook]subtitles=captions.ass[v_out]
```

### Captions Engine: `faster-whisper`
- Local model: `small.en` or `base.en` (run on CPU/CUDA).
- Outputs word-level timestamps: `(word, start_time, end_time)`.
- Converts word streams into `.ass` subtitle format where each active spoken word is wrapped in an override tag: `{\c&H00FFFF&}` (vibrant yellow/gold or green highlight), with inactive words in `{\c&HFFFFFF&}` (bold white with black outline).

### Audio Engineering & Variant Split
- **Variant A (Original Audio Highlight)**:
  - Original audio normalized to $-14.0\text{ LUFS}$ via `loudnorm=I=-14:TP=-1.5:LRA=11`.
- **Variant B (Narration + Ducked Audio)**:
  - Gemini drafts an engaging 15–18s voiceover line based on clip action.
  - Kokoro TTS generates clean voiceover WAV locally (`af_heart` or `am_adam` voice).
  - Original audio is ducked by $-14\text{dB}$ during speech using ffmpeg `sidechaincompress` or `volume` ducking:
    ```
    [orig_a][tts_a]sidechaincompress=threshold=0.08:ratio=8:attack=40:release=450[ducked];
    [ducked][tts_a]amix=inputs=2:duration=first:dropout_transition=2[a_out]
    ```

---

## 8. Configuration File Shape (`config.yaml`)

```yaml
app:
  brand_name: "GTA 6 Clipping Pipeline"
  dry_run: false

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
    enabled: false # Set true from launch (19 Nov 2026)
    game_id: "506442" # GTA VI Helix game ID
    weight: 1.10
    limit: 20

discovery:
  min_age_hours: 0.5
  half_life_hours: 36.0
  velocity_power: 1.2
  reddit_score_weight: 1.5
  source_weights:
    youtube: 1.0
    reddit: 1.25
    twitch: 1.10
  score_formula: >
    ( (views + (upvotes * reddit_score_weight)) / max(age_hours, min_age_hours) ) ** velocity_power
    * ( 0.5 ** (age_hours / half_life_hours) )
    * source_weight

cut:
  target_duration: 20.0
  min_duration: 18.0
  max_duration: 22.0
  short_clip_threshold: 30.0
  scene_threshold: 0.3
  audio_weight: 0.8
  scene_weight: 0.2

render:
  width: 1080
  height: 1920
  fps: 30
  font_path: "assets/fonts/Montserrat-Bold.ttf"
  hook_font_size: 52
  hook_top_y: 220
  caption_font_size: 46
  caption_bottom_y: 1540
  highlight_color: "&H00FFFF&" # Yellow highlight in ASS format
  base_color: "&HFFFFFF&"
  target_lufs: -14.0
  ducking_attenuation_db: -14.0

posting:
  slots:
    - "14:30"
    - "19:00"
  timezone: "UTC"
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
  auto_mode: false # Overridden by .env AUTO_APPROVE
  alert_every_hours: 12

retention:
  purge_downloads_immediately: true
  purge_renders_after_days: 7
```

---

## 9. Test Plan (Deterministic, Zero Network)

Testing adheres strictly to: **"pytest, every stage gets tests with fixtures (no network in tests). Report the exact command and pass/fail counts."**

### Fixtures Strategy (`tests/conftest.py`)
1. **Mock DB Fixture**: In-memory or temporary SQLite database initialized with the full WAL schema.
2. **Mock yt-dlp Fixture**: Monkeypatches `yt_dlp.YoutubeDL.extract_info` returning realistic YouTube metadata and simulated downloaded dummy video.
3. **Mock Reddit JSON**: Loads local fixture JSON files simulating Reddit `r/GTA6/hot.json` submissions.
4. **Mock Synthetic Media Fixture**: Generates tiny 5-second 320x240 MP4 test clips via ffmpeg (`testsrc=duration=5:size=320x240:rate=30` and `sine=frequency=1000:duration=5`) for fast deterministic rendering tests without large binary blobs in git.
5. **Mock faster-whisper Fixture**: Returns synthetic timestamped segments: `[Word(word="GTA", start=0.0, end=0.4), Word(word="6", start=0.4, end=0.8)]`.
6. **Mock Gemini Fixture**: Returns structured mock hook generation outputs without invoking AI Studio API.
7. **Mock Kokoro Fixture**: Generates a 2-second silent or sine WAV file simulating TTS.
8. **Mock Telegram Bot API**: Simulates Bot API HTTP endpoints (`sendMessage`, `editMessageText`, `editMessageCaption`, `getUpdates`).
9. **Mock Meta Graph API**: Simulates `/media`, `rupload.facebook.com`, and `/media_publish` responses.

### Test Matrix

| Test Suite | Coverage Area | Key Verification Scenarios |
|---|---|---|
| `test_config.py` | Config & Environment | Checks `.env.example` keys exist, config YAML parser, default fallbacks. |
| `test_db.py` | DB & State Machine | WAL mode active, forward state transitions, foreign keys, idempotency on duplicate insert. |
| `test_discover.py` | Discovery & Scoring | Dynamic scoring formula evaluation, ranking items, ignoring invalid/blacklisted URLs. |
| `test_download.py` | Downloader & Resilience | Resumable chunk logic, backoff on 429 response, socket timeouts. |
| `test_dedupe.py` | Fingerprinting & Hash | Exact source ID match rejection; perceptual dHash comparison rejecting duplicate videos with flipped bits. |
| `test_cut.py` | Cut Algorithm | Trimming clips $\le 30$s; sliding window loudness + scene density peak selection for clips $> 30$s; boundary snapping. |
| `test_hooks.py` | Hooks & Voiceover Copy | Gemini prompt construction, hook style extraction, regex formatting, fallback hooks. |
| `test_voiceover.py` | Kokoro Audio Generation | Output WAV formatting, sample rate matching, duration checks. |
| `test_render.py` | Compositing & Subtitles | ffmpeg filtergraph construction, ASS subtitle formatting with word highlight tags, ducking filter generation. |
| `test_telegram.py` | Telegram Approval Card | Single status card rendering, Approve/Reject callbacks, stale button protection, rate-limit edit throttling. |
| `test_posting.py` | Meta Graph API Uploader | Chunked resumable upload to IG and FB Reels, token expiration alerts, rate-limit gap enforcement. |
| `test_scheduler.py` | Cadence & A/B Allocation | Daily slot claiming, missed slot rolling, bandit-weighted Variant A vs B selection. |
| `test_analytics.py` | Engagement & Learning | 24h/72h metrics parsing, Bayesian mean log views update, weight rebalancing. |
| `test_cleanup.py` | Storage Garbage Collection | Instant source deletion post-render; 7-day retention deletion for published MP4s. |

---

## 10. Build Order & Implementation Milestones

```
+--------------------------------------------------------------------------+
| Checkpoint 0 (Current): Architecture Plan (PLAN.md), .env.example, git   |
+--------------------------------------------------------------------------+
                                     |
                                     v
+--------------------------------------------------------------------------+
| Checkpoint 1: Discovery, Download, Dedupe & State Machine                |
| - SQLite DB in WAL mode                                                  |
| - YouTube (yt-dlp search) & Reddit public JSON discovery                 |
| - Config-driven discovery scoring engine                                 |
| - Resumable downloader & perceptual hash deduplication                   |
| - Live run: 10 discovered items, duplicate caught, crash test pass       |
| - Full unit test suite with fixtures                                     |
+--------------------------------------------------------------------------+
                                     |
                                     v
+--------------------------------------------------------------------------+
| Checkpoint 2: Cut, Enrich & Render Engine                                |
| - ffmpeg audio loudness & scene-change cut window selector               |
| - Gemini hook generation + Kokoro TTS voiceover                          |
| - faster-whisper word-highlighted ASS subtitles                         |
| - Dual variant renderer: Variant A & Variant B (ducked audio)            |
| - Live run: 3 real clips rendered into output folder + laptop benchmarks |
| - Tests for cut & render                                                 |
+--------------------------------------------------------------------------+
                                     |
                                     v
+--------------------------------------------------------------------------+
| Checkpoint 3: Approval Interface & Social Publishing                     |
| - Telegram single self-editing review card (one item at a time)          |
| - Meta Graph API Instagram Reels + Facebook Page Reels uploader          |
| - Dry-run upload verification + single live post on explicit user "go"   |
| - Tests for Telegram & Meta Graph                                        |
+--------------------------------------------------------------------------+
                                     |
                                     v
+--------------------------------------------------------------------------+
| Checkpoint 4: Autonomous Scheduler, Learning, Health & Migration        |
| - 2 posts/day scheduler with atomic slot claiming                        |
| - 24h & 72h metrics ingestion & Bayesian A/B weight updater              |
| - Healthchecks.io dead-man switch & failure-only alerts                  |
| - Auto-mode flip switch (`AUTO_APPROVE=true`)                            |
| - Disk cleanup daemon (instant download purge, 7-day render purge)       |
| - Linux server migration README & full test pass                         |
+--------------------------------------------------------------------------+
```

---

## 11. Kick Streaming API Evaluation

The requirement states:
> *"plus Kick only if a working public endpoint exists (if not, say so; do not scrape around it). No X."*

**Evaluation**:
Kick currently protects its endpoints behind Cloudflare Turnstile bot detection and does not provide an official, sanctioned public clips REST API for developers. Accessing Kick clips without private user session tokens requires bypassing bot challenges, which violates our hard guidelines ("Never solve, click through, wait out, compute, or bypass a CAPTCHA, 'verify you are human' or anti-bot challenge... Use documented, sanctioned paths only").
**Decision**: Kick integration will remain disabled by default with an explicit notice in config and logs. If Kick launches an open, sanctioned public API endpoint in the future, it can be plugged in without architectural changes.

---

## 12. Calls I Made (Engineering Judgement Calls)

1. **dHash / pHash for Perceptual Fingerprinting**:
   - Sample 4 frames at 20%, 40%, 60%, and 80% duration; compute 64-bit difference hash (dHash) on each. Videos sharing $\ge 3$ matching frame hashes within a Hamming distance $\le 8$ bits are classified as duplicate clips.
2. **Dynamic Math Expression Parser for Scoring**:
   - To keep the scoring formula in `config.yaml` rather than hardcoded in Python, we implement a safe AST-based formula evaluator supporting basic arithmetic, `max`, `min`, `**`, and exponential functions without using unsafe `eval()`.
3. **ASS (Advanced SubStation Alpha) Subtitles**:
   - Rather than burning multiple individual `drawtext` filters in ffmpeg for every word (which is slow and complex), we generate standard `.ass` subtitle files with `\k` karaoke or `\c` color-override tags. ffmpeg compiles this in a single fast subtitle pass.
4. **Hook Styling Taxonomy**:
   - Standardized three hook styles: `question` (e.g., "IS THIS REAL?"), `curiosity` (e.g., "NOBODY NOTICED THIS DETAIL"), and `hype_analysis` (e.g., "GTA 6 GRAPHICS JUST BROKE THE INTERNET"). This taxonomy is stored in `items.hook_style` and tracked in `ab_learning`.
5. **Local Kokoro TTS Voice Selection**:
   - Defaulted to `am_adam` or `af_heart` 24kHz American English voices for high naturalness without external API calls or latency.
6. **Slot Schedule**:
   - Defaulted the 2 daily slots to `14:30` and `19:00` UTC (or configurable local timezone) to match peak global gaming engagement on Instagram Reels and Facebook.
7. **Credit in Caption**:
   - Structured caption template:
     `{hook_text}\n\nCredit: {author_name} ({source})\nFound via r/GTA6\n.\n#gta6 #grandtheftauto #gta #gaming #rockstargames`
8. **Atomic Slot Claiming**:
   - Using SQLite transactions on the `kv` table (`posting:slot:<date>T<HH:MM>`) to guarantee two processes or a crash restart can never double-post in the same slot window.

---

## 13. Required User Inputs & Credentials by Checkpoint

| Checkpoint | Item Needed from User | Where to Provide | Notes |
|---|---|---|---|
| **CP 0** | Explicit confirmation ("go") on `PLAN.md` | Chat response | Required to proceed to Checkpoint 1. |
| **CP 1** | None (public YouTube and Reddit JSON require no keys) | N/A | Tests run against public endpoints. |
| **CP 2** | `GEMINI_API_KEY` | `.env` | Free tier key from [AI Studio](https://aistudio.google.com/apikey). |
| **CP 3** | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `IG_USER_ID`, `IG_ACCESS_TOKEN`, `FB_PAGE_ID` | `.env` | Needed to test interactive approval card and dry-run social posting. |
| **CP 4** | `HEALTHCHECK_URL` (optional) | `.env` | Dead-man switch ping from [healthchecks.io](https://healthchecks.io). |

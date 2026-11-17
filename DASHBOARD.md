# Local control panel

Run from the repository root, with Python 3.11 or newer:

```powershell
python -m pip install -r requirements.txt
python -m dashboard
```

Open **http://127.0.0.1:8765** and use **Start runner** on Overview, or run
`python -m pipeline.runner loop` from a second terminal. Restart a runner from
an earlier checkout so it loads the current command consumer. The dashboard
displays `app.dry_run`; publishing runs in the runner process.

On Windows, double-click **setup.bat** once to create `.venv` if necessary,
upgrade pip and install `requirements.txt`. It is safe to rerun. Then double-click
**dashboard.bat** to activate that environment, start the dashboard and open its
loopback URL in your browser. The launcher runs setup if `.venv` is absent and
pauses on errors so the message stays visible. Both scripts work from Explorer
regardless of its current directory. Stop the dashboard with Ctrl-C in its window.

## Stack

Flask provides small Python routes, signed sessions and confirmation tickets.
Waitress serves them on Windows and Linux, bound explicitly to 127.0.0.1.
Plain HTML/CSS/JavaScript and Canvas charts need no Node/npm build or CDN.
SQLite remains the source of truth; existing pipeline functions own decisions,
checks, scheduling and publishing.

## Pages

- **Overview:** all `db.STAGES`, ready buffer/target, approval mode and first-live
  age, next five queued items, A/B allocation, runner heartbeat, source/Meta
  cooldowns, and a prominent dry-run banner. Times use the active posting
  profile's timezone, shown beside every calendar. With a running instrumented
  runner, its observed scheduling settings stay visible until the next cycle
  reloads the updated configuration.
- **Make:** discover a batch or paste a source URL with optional time hints.
  Job progress lists the stage and result for each requested item.
- **Review:** compare A/B renders and apply decisions, remake a clip, or edit
  its hook or caption.
- **Library:** search or filter all items and select eligible clips for bulk
  deletion or rejection.
- **Queue & calendar:** chronological local-day timeline, A/B variant or pending
  draw, per-platform status, search and a filter for every stage. Approved items
  without a slot appear separately. The scheduler owns actual slot allocation.
- **Item detail:** both render players, cut player, hook, voiceover script,
  cut window, source authenticity, binding diagnostics and full stage history.
  Missing/purged files are marked unavailable. Media requests support seeking
  and are restricted to registered paths inside the configured media root.
- **Analytics:** platform views by publish date, a selectable post's snapshots
  over observation time, scheduler variant statistics and allocation reason,
  plus hook-style/source bars. Totals use the latest snapshot per platform post;
  they never add cumulative 24h and 72h snapshots together. These are descriptive
  totals, not age-normalized causal comparisons.
- **Failures & review:** failed/rejected items with stage/reason, needs-review
  items, and the existing 24h `cut_outcome_stats` no-clean-ending rate.
- **Settings:** local scheduling, publishing, Telegram, buffer and discovery
  preferences. Includes per-service connection Test buttons and a parallel Test all, using the
  current local `.env` without sending its values to the browser. Loading,
  refreshing and polling never run probes. Results show masked API errors,
  status and last-check time; they remain in memory until the dashboard stops.
  Green means OK, red failed, amber a Meta expiry/scope warning, grey unset or
  not checked. Each check has a 10-second deadline. Empty credentials skip HTTP.
  Gemini only lists models, Telegram only reads bot/chat details, Meta reads
  identity/token metadata using the configured Graph version, and Twitch obtains
  an in-memory app token and reads the games endpoint. Meta may require a separate
  app/developer credential to inspect some token types; its actual error is shown
  if the configured Page token cannot inspect itself. No validity is inferred.
  **Healthchecks Test and Test all send a real success ping**, which can reset
  its alert timer. Only HTTPS ping URLs ending at a check UUID are accepted;
  status suffixes, queries and redirects are rejected. **Send test message** is
  a separate, explicit Telegram action and is never part of Test all. Connection
  checks and this send action are immediate and do not need a running runner.

The UI polls every 2.5 seconds and pauses polling while a confirmation is open.
Item players keep playing while status refreshes.
Gemini's remaining daily allowance is not available locally: the panel displays
only a cooldown observed by the runner, or **Unknown**. A heartbeat on a different
host proves recent activity, not a local PID check. No secrets are requested.

## How controls work

Every control first prepares a read-only preview via POST, then needs a second
**Confirm** POST. Confirmations expire after 15 minutes and are bound to
that browser session. CSRF tokens, strict same-site cookies, Origin/Host checks
and a local-only content security policy protect the loopback UI. This is a
single-user local tool, not an authenticated multi-user server: do not expose it
through a proxy or tunnel.

Confirmation records the request in `dashboard_commands`. If the runner has a
fresh heartbeat and holds the lock, it applies quick commands between stages.
The UI shows queued, running, applied, refused and failed commands and their
messages. The loop interval is an estimate because a long active stage can
take longer. When the runner is stopped, the dashboard acquires the same
runner lock, applies the quick command in one transaction and releases the
lock. If the runner wins the lock race, the command stays queued. Repeated
confirmation uses the same command ID.
Changed item state invalidates the command; refresh and confirm again. Unexpected
application errors roll back that command's effects and record a terminal failure.
The runner logs dashboard errors and runs the stage even if the mailbox or the
dashboard import fails. If SQLite cannot open a write transaction at all, the
runner cannot record an outcome, and the unprocessed command stays pending.

| Control | Behavior |
| --- | --- |
| Approve both / A only / B only / Reject | Calls `telegram_bot.decide()` under the runner lock. Revalidates stage, bindings, files and publishing history. Releases the matching Telegram card state in the same transaction. |
| Hook is right | Calls `decide(..., "hookok")`, preserving the existing human-review semantics. Returns the item to rendering; this is not publication approval. |
| Pause posting | Sets an indefinite dashboard hold. The runner's settlement stage also respects it. A request already in flight may finish. |
| Resume posting | Lifts an active dashboard hold. Any remaining Meta cooldown stays in effect. Normal slot rules still apply. |
| Edit hook | Invalidates approval, post plans, renders and bindings, and returns an unstarted item to `cut`. See below. |
| Re-slot | Recomputes `scheduler.next_free_slot()` at application time, honors the configured gap and caps the local-day target at two. The preview is an estimate; another reservation can move the final slot. |
| Delete | Soft-rejects an unposted item, releases its Telegram card, removes guarded local media and retains discovery history. |
| Unapprove | Releases pending post rows and sends the item back to review. |
| Move up/down | Swaps two unstarted slots after checking the daily cap and minimum gap. |
| Post now | Confirms the earliest gap- and cap-compliant local slot, bypassing the usual slot list. Upload happens during the runner's posting stage and can start later than the confirmed slot. |
| Edit caption | Previews the caption that will post, validates the combined length and hashtag limits and appends locked attribution and unverified-footage disclosure. |
| Retry | Only failed `download`, `cut`, `render` items with no post history. Required source/cut files must exist. Returns to the preceding runner stage. |

Started, ambiguous or failed publishing is display-only. Even a dry-run row with
an upload/container/attempt record is conservatively treated as started. No
control resets publishing state or retries Meta. Preview-only/synthetic items
are refused cleanly before approval; existing database triggers remain intact.
The physical Telegram approval message is not edited by this app: its stored card
is released, and a later button tap is stale. Only the separate Connections
**Send test message** button sends a Telegram message.

### Creation and runner controls

Use **Make** to discover 1–10 new sources or paste a supported YouTube or Twitch
URL. Optional `mm:ss` hints limit the cut search. Title, channel and creator
are read from the downloader, and the usual channel allowlist and hook
grounding rules apply. A source already in the database links back to its item.
With a loop running, creation starts at the next discover stage. Otherwise a detached
`python -m pipeline.runner make --job ...` process acquires the lock and takes
the requested clips through download, cut, hook, voiceover, render and the
review hand-off. That worker has no posting, measuring or cleanup phase.
**Remake cut** excludes windows that overlap at least half of the new window
with the old cut. It downloads the source again if render retention purged it.
**Regenerate hook** keeps the cut, regenerates the hook/voiceover, and renders
the variants again so the video and caption stay in sync.

**Stop runner** waits for the current stage. **Force stop** becomes available
after two minutes and names the stage it would interrupt. It is blocked during
the posting and publishing-settlement stages.

### Engagement signals

The default `cut.heatmap_weight` is **0.25**. The original audio/scene score
roughly spans 0–1, so a sustained maximum replay peak adds at most a quarter
of that range. This can break close calls while still rewarding stronger
audio and scene evidence. The mean is weighted by the time each replay bin
overlaps the candidate window. A missing signal leaves the previous score
unchanged, and a candidate must still pass the clean-ending rule.

Manually pasted Twitch VODs can use chat replay. Ten-second bins with at least
three messages and three times the VOD's average bin count feed the same
weighted score after a five-second default reaction offset
(`cut.chat_offset_seconds`). Twitch clips skip chat lookup. Replay failures
fall back to audio and scene scoring. A cut records the engagement signal when
it contributes to the chosen window and records `audio_scene` otherwise.

### Local settings

Settings save to gitignored `config.local.yaml`; each save first writes a
dated backup. The overlay merges into `config.yaml` and the runner reloads it
at the beginning of each cycle. Turning Telegram approval cards off requires
dashboard review, even if the base configuration would switch to automatic
approval. Connection requests run in child processes. The demo database uses
an isolated settings overlay and stubbed media jobs.

### Posting pause ownership

The dashboard hold lives in the `dashboard:meta_pause` key. Meta's own
`meta_graph` rate-limit row keeps its original reason, error count and deadline,
including cooldowns recorded while a dashboard hold is active. The pipeline
checks both sources before making Meta calls. Resume releases the hold and the
rate limiter continues to enforce any remaining Meta cooldown. An expired
cooldown has no effect. Overview shows Meta's reason and remaining minutes,
refreshed with the normal polling cycle. Only an active dashboard hold offers
a resume control. Existing indefinite dashboard pauses are recognized by their
original reason and far-future timestamp and converted when the hold is changed.

### Hook edits and binding safety

Editing is available once the runner has produced an original hook and voiceover
script. The dashboard only stores a candidate and a `dashboard_edit` review marker.
It clears both render paths, the old hook/voiceover stamps, the spoken-audio path,
caption, approval and variant choice. Unstarted post plans are removed. Old
files are not deleted by the request. A missing cut prevents the edit.

The runner's hooks stage recognizes the marker and keeps the user's candidate
instead of generating a replacement. It uses the **same** transcript, source
trust, claim-word and overlap rules, then Gemini segment/honesty verification.
The existing voiceover script is retained and checked alongside the edited hook.
The local keyword rules extract terms from the edited hook for styling.
Only that runner path stamps `segment_check`; a failed verdict goes to the
existing manual needs-review queue, and a transient outage keeps the candidate
pending. Human “hook is right” remains an explicit existing override, visible in
binding history. Voiceover and renders must be regenerated before approval.

### Concurrent access and minimal pipeline changes

`pipeline/db.py` adds a context-local caller-owned connection scope. Existing
helpers can participate in one atomic command transaction without nested commits.
Heartbeat threads keep separate connections. GETs copy SQLite into an in-memory
snapshot because some existing “read” helpers pin first-live/auto-mode clocks;
those incidental writes stay in RAM. SQLite WAL and the existing busy timeout
remain enabled. Startup initializes schema and the dashboard mailbox.

`pipeline/runner.py` drains the mailbox before each stage, records non-secret
runtime settings/quota observations, and honors Meta pause during settlement.
`pipeline/hooks.py` accepts a dashboard candidate through the normal validation
path. `pipeline/rate_limit.py` tracks dashboard holds independently from Meta
cooldowns, and `pipeline/posting.py` shares its Meta source key.

## Demo

Use a **new path outside the repository**, never a file named `pipeline.db`:

```powershell
python scripts/dashboard_demo_db.py "$env:TEMP\gta-dashboard-demo.db"
python -m dashboard --db "$env:TEMP\gta-dashboard-demo.db" --media-root "$env:TEMP\gta-dashboard-demo-media"
```

Linux equivalent:

```sh
python scripts/dashboard_demo_db.py /tmp/gta-dashboard-demo.db
python -m dashboard --db /tmp/gta-dashboard-demo.db --media-root /tmp/gta-dashboard-demo-media
```

The generator refuses existing files and symlinks. It seeds 85 clearly fictional
items across every stage, multiple weeks of A/B platform metrics, failures,
reviews and tiny local videos (requires the pipeline's FFmpeg). The UI displays
a DEMO banner and forces dry run. Make and URL jobs use stubbed media stages.
The demo runner handles dashboard commands and skips the live pipeline stages.
Settings saves use an isolated overlay beside the demo database.

## Validation and limits

Run the default suite with `python -m pytest -q`. Dashboard tests use temporary
databases and exercise mailbox isolation, stopped-runner locking, creation-only
workers, approval and queue guards, captions, settings and engagement scoring.
Run `python -m pytest -o addopts= -m live tests/test_live_crash.py` separately
for the YouTube crash/recovery check in an isolated working directory.

The default suite was exercised on Linux with Python 3.13 and FFmpeg 9.0.2.
Chromium screenshots and interaction checks use the isolated demo database.
Windows 11 setup and worker detachment still need a native smoke test.

Snapshots copy the whole DB into RAM and send all stored metrics to the browser.
This is suitable for the initial local workload, but needs pagination/aggregation
for a large archive. Command history is retained; no new cleanup policy deletes
it. Existing media retention remains with the pipeline. An edited item's old
render files can remain until overwritten or existing retention removes them.

Run `scripts\windows_smoke.bat [database.db] [--youtube "URL"] [--twitch "VOD_URL"]` after `setup.bat`; the default DB is the repo’s `pipeline.db`.
The script opens the source read-only, then runs a private dry-run dashboard and workers on a temporary copy.
Make N uses real discovery, so it needs an eligible new source; omit either replay URL to skip that check.
See the console summary and `media/smoke/windows_smoke_<timestamp>.txt`.

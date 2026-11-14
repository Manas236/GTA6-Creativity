# Local control panel

Run from the repository root, with Python 3.11 or newer:

```powershell
python -m pip install -r requirements.txt
python -m dashboard
```

Open **http://127.0.0.1:8765**. Start the normal runner in a second terminal with
`python -m pipeline.runner loop`. Restart an already-running runner after checking
out this branch so it loads the command consumer. The dashboard never starts it,
takes its lock, changes `app.dry_run`, or publishes anything.

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
  runner, its observed settings take precedence over changed config on disk;
  a banner asks for a restart when they differ.
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
- **Connections:** per-service Test buttons and a parallel Test all, using the
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

The UI polls every 15 seconds, pausing while a confirmation dialog is open.
Item media is not reset by background polling; use Refresh to reload details.
Gemini's remaining daily allowance is not available locally: the panel displays
only a cooldown observed by the runner, or **Unknown**. A heartbeat on a different
host proves recent activity, not a local PID check. No secrets are requested.

## How controls work

Every control first prepares a read-only preview via POST, then needs a second
**Confirm & queue** POST. Confirmations expire after 15 minutes and are bound to
that browser session. CSRF tokens, strict same-site cookies, Origin/Host checks
and a local-only content security policy protect the loopback UI. This is a
single-user local tool, not an authenticated multi-user server: do not expose it
through a proxy or tunnel.

Confirmation adds an immutable command to `dashboard_commands`. The runner
consumes commands **between stages**, under its existing serialization. An
active render or publish finishes before a new command is applied. A stopped
runner leaves controls pending; the UI shows pending/applied/refused/failed results.
Do not interpret “queued” as “applied.” Commands and their effects commit in one
short SQLite transaction, with no HTTP, rendering or model work inside it.
Repeated confirmation and runner restarts do not apply a command twice.
Changed item state invalidates the command; refresh and confirm again. Unexpected
application errors roll back that command's effects and record a terminal failure.
The runner logs dashboard errors and runs the stage even if the mailbox or the
dashboard import fails. If SQLite cannot open a write transaction at all, the
runner cannot record an outcome, and the unprocessed command stays pending.

| Control | Behavior |
| --- | --- |
| Approve both / A only / Reject | Calls `telegram_bot.decide()` in the runner. Revalidates stage, bindings, files and publishing history. Releases the matching Telegram card state in the same transaction. |
| Hook is right | Calls `decide(..., "hookok")`, preserving the existing human-review semantics. Returns the item to rendering; this is not publication approval. |
| Pause posting | Sets an indefinite dashboard hold. The runner's settlement stage also respects it. A request already in flight may finish. |
| Resume posting | Lifts an active dashboard hold. Any remaining Meta cooldown stays in effect. Normal slot rules still apply. |
| Edit hook | Invalidates approval, post plans, renders and bindings, and returns an unstarted item to `cut`. See below. |
| Re-slot | Recomputes `scheduler.next_free_slot()` at application time, honors the configured gap and caps the local-day target at two. The preview is an estimate; another reservation can move the final slot. |
| Retry | Only failed `download`, `cut`, `render` items with no post history. Required source/cut files must exist. Returns to the preceding runner stage. |

Started, ambiguous or failed publishing is display-only. Even a dry-run row with
an upload/container/attempt record is conservatively treated as started. No
control resets publishing state or retries Meta. Preview-only/synthetic items
are refused cleanly before approval; existing database triggers remain intact.
The physical Telegram approval message is not edited by this app: its stored card
is released, and a later button tap is stale. Only the separate Connections
**Send test message** button sends a Telegram message.

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
a DEMO banner. **Do not run the full pipeline against the demo database:** its
source URLs and media are examples. HTTP controls can be inspected and queued
without processing them. Automated tests exercise command application on temp DBs.

## Validation and limits

Run `python -m pytest tests/test_dashboard.py -q` and the unchanged full suite with
`python -m pytest -q`. Every dashboard test uses a temporary database and refuses
the production path. Tests block external API calls and runner-lock acquisition,
exercise concurrent confirmation, atomic rollback, stale Telegram callbacks,
preview refusal, binding/check outcomes, pause/resume, scheduling and retries.

Implementation was exercised on Linux/Python 3.12, not a Windows machine. Windows
3.11 compatibility follows the existing pipeline plus Waitress; a Windows smoke
test is still required. Browser screenshots could not be captured in this
execution environment: Chromium socket creation was denied and the cloud browser
could not access the local app. No mockups are presented as screenshots.

The unchanged baseline has three render failures under FFmpeg 7.1.5 (freeze-frame
variant B timing); FFmpeg 7.0.2 reproduces them and its bundled build also lacks a filter
needed by six source-text fixtures. FFmpeg 4.2.2 passes those three but
fails other existing filter tests, so downgrading is not a complete fix. See the
handoff report for exact observed test counts; no existing tests were weakened.

Snapshots copy the whole DB into RAM and send all stored metrics to the browser.
This is suitable for the initial local workload, but needs pagination/aggregation
for a large archive. Command history is retained; no new cleanup policy deletes
it. Existing media retention remains with the pipeline. An edited item's old
render files can remain until overwritten or existing retention removes them.

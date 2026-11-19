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
Plain HTML, CSS and JavaScript need no Node/npm build or CDN.
SQLite remains the source of truth; existing pipeline functions own decisions,
checks, scheduling and publishing.

## Pages

- **Overview** shows clips that need review, the next post, runner controls
  and problems. The compact health row includes the ready buffer and Gemini quota.
- **Make** supports a batch or a source URL with optional time hints. Job
  progress lists each clip. **Discovered** shows candidates, their scores and ages.
- **Review** has To approve, Needs a decision, Failed and Rejected tabs.
  The old `/failures` URL opens Failed, which includes the no-clean-ending
  rate over the last 24 hours. New clips have P1 Clean, P2 Punchy and P3 Full
  hype. The version switcher keeps the player's position. Use 1–3 to switch
  presets on new clips. On legacy clips, 1 and 2 approve the Captions version
  or Voiceover version respectively. The question-mark button lists the
  other shortcuts. Use Z to undo reversible changes. Items archived with
  `python scripts/archive_review.py --before YYYY-MM-DD` (stage `archived`, files
  moved to `media/archive/<date>/`, never deleted) are in no Review tab; the
  Library still lists them. All pre-launch Review items were archived on 19 Nov.
- **Library** supports search, filters, and bulk deletion or rejection.
- **Queue** shows a seven-day calendar with open times. Drag an approved
  clip onto an empty slot, or switch to List view. Scheduled times use the
  active profile's timezone.
- **Item detail** shows previews, cut context, binding diagnostics and stage
  history. Published posts link to their Reels and show recorded metrics.
- **Analytics** shows views, likes, comments and shares. Totals use the latest
  snapshot per platform post. Missed windows and late snapshots are labeled.
- **Logs** shows redacted runner output. Pause holds the view in place.
  Actions can start analytics or cleanup.
- **Settings** groups Posting, Look and sound, Sources, Approval, Voiceover,
  Retention and Connections. Each editable group saves independently. The
  Posting tab includes the switch between dry run and live posting.
  Connections checks run on request using the current local `.env`. Results
  show masked API errors, status and last-check time.
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

The dashboard polls every 30 seconds. Make also polls job progress every
2.5 seconds. Polling pauses while a confirmation dialog is open. Theme defaults
to Light. Dark and System are available in the top bar. Gemini quota shows
**not reported** when the runner has no active cooldown to report.

## Remote access

The owner can open the dashboard from a phone through a **Cloudflare quick tunnel**.
`cloudflared tunnel --url http://127.0.0.1:8765` needs no Cloudflare account and
prints a random `https://<words>.trycloudflare.com` address. The dashboard itself
still binds to 127.0.0.1 only. cloudflared connects out to Cloudflare, so no router
port is opened. Once signed in you have full control, the same as locally: the same
CSRF tokens, prepare/confirm tickets and confirmation dialogs apply.

**Why a password:** anyone who learns the link could otherwise approve, post, delete
or switch to live posting. The tunnel therefore never starts unless a password is set.

### Password

```powershell
python -m dashboard set-password     # asks twice; at least 12 characters
python -m dashboard clear-password   # removes it (the remote link then stays off)
```

`set-password` stores only a scrypt hash, as `DASHBOARD_PASSWORD_HASH=` in `.env`.
It makes a dated backup `.env.YYYY-MM-DD_HHMMSS.bak` first, and the hash is never
printed. Both commands sign out every device. Restart the dashboard afterwards so
the remote link picks up the change.

With a password set, every page, `/api/*` and `/media/*` needs a signed-in session,
locally as well as remotely. Only the sign-in page and its stylesheet are public.
Sessions end after 12 hours without activity. **Keep me signed in on this device**
extends that to 30 days. **Log out** in the sidebar signs out every device, since
there is one user. Cookies are HttpOnly, SameSite=Lax (a dashboard link tapped in
Telegram opens signed in) and Secure over the tunnel's HTTPS.

Brute-force limits: 5 wrong passwords from one IP within 15 minutes block that IP
for 15 minutes. 20 failures across all IPs within an hour block every remote sign-in
for 1 hour. Sign-in on the PC itself (http://127.0.0.1:8765) keeps working during
that hour. Every failure shows the same message. Over the tunnel, the visitor's IP
comes from Cloudflare's `CF-Connecting-IP` header.

### Turning it on

1. Install cloudflared once: `winget install --id Cloudflare.cloudflared`.
2. `python -m dashboard set-password`.
3. In `config.local.yaml`:
   ```yaml
   dashboard:
     remote:
       enabled: true
       cloudflared_path: null   # null: PATH, then the winget install folders
   ```
4. Start the dashboard as usual (`dashboard.bat` or `python -m dashboard`).
   The console prints `REMOTE LINK (password protected): https://….trycloudflare.com`,
   and the same link arrives in Telegram.

The link changes every time the dashboard (or cloudflared) restarts. Only this run's
exact hostname is accepted; other `trycloudflare.com` hosts are refused. If
cloudflared exits, the dashboard restarts it with backoff (5, 10, 20, 40 and 80 s,
up to 5 tries) and sends the new link. cloudflared is a child process: it stops when
the dashboard stops, including on Ctrl-C or closing the window. The status line at
the top of the dashboard shows the current link, `remote off`, or
`remote waiting for a password`.

If `enabled: true` but no password is set, the console says the remote link is
waiting for a password and the dashboard runs locally only. If cloudflared prints no
link within 60 seconds, the error is printed and the dashboard stays local-only.

### Telegram messages

These go through the pipeline's normal alert path, to the same chat, prefixed 🚨:

| Message | Meaning |
| --- | --- |
| `Dashboard link: https://….trycloudflare.com` | The tunnel started or restarted. This is the current link. |
| `Dashboard sign-in through the remote link` with time (IST), IP and browser | Someone signed in over the tunnel. If it was not you, run `set-password` right away (this signs everyone out) and restart the dashboard. Local sign-ins send nothing. |
| `Dashboard: 20 failed sign-ins in the last hour…` | Someone is guessing passwords. Remote sign-in is refused for an hour. Consider turning remote off. |

### Turning it off

Set `dashboard.remote.enabled: false` in `config.local.yaml` (or delete that block;
the shipped default is off) and restart the dashboard. The old link stops working
when cloudflared stops. `clear-password` also keeps the tunnel from starting and
returns the dashboard to the unauthenticated, loopback-only mode.

## Mock mode

Start the dashboard with a temporary database and media directory:

```bash
demo_dir=$(mktemp -d)
mkdir -p "$demo_dir/media"
python -m dashboard --db "$demo_dir/pipeline.db" --media-root "$demo_dir/media"
```

Open [Overview in mock mode](http://127.0.0.1:8765/?mock=1). Add `?mock=1`
to any dashboard page, or `&mock=1` when it already has a query. The browser
uses fixtures and a local sample video. Mock changes apply in memory for the
current page session, including the simulated publishing-mode switch. Regular
pages use the active backend and omit controls when their APIs are unavailable.

## How controls work

Ticketed actions send a prepare POST and a confirm POST. The UI asks for
confirmation when changing publishing mode, posting now or deleting. Tickets
expire after 15 minutes and are bound to that browser session. CSRF tokens, same-site (Lax) cookies, Origin/Host checks
and a local-only content security policy protect the UI. It is a single-user
tool. The only supported way to reach it from another device is the
password-protected Cloudflare quick tunnel described in
[Remote access](#remote-access). Do not put any other proxy or tunnel in front of it.

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

**N asked = N delivered.** Make keeps going until N clips passed every gate and
rendered. A failed attempt is replaced by the next discovery candidate; when the
candidates run out, discovery widens (more queries, a longer Twitch age window;
`discovery.widen`). It stops early only after `make.sources_per_clip` (5) x N
candidate sources, or when Gemini's daily quota is gone. Job progress shows
"x of N done · k of B source attempts" and the attempt in hand, live, and one row
per attempt with its own source length, cut window, time and the reason it failed.
Every candidate window must pass the content gate (real motion and mostly gameplay,
`pipeline/content_gate.py`): a tweet, article, menu, text card or facecam window is
skipped for the next window of the same source, then for the next source.
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

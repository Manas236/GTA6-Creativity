# GTA 6 pipeline control panel

Install the requirements with Python 3.11+ and run from the repository root.

```powershell
python -m pip install -r requirements.txt
python -m dashboard
```

Open **http://127.0.0.1:8765**. On Windows, `start-dashboard.bat` runs setup on first use and opens the browser. `setup.bat` also supports the existing `dashboard.bat` launcher. The dashboard is bound to loopback. Overview starts and stops the separate runner. If you already run the loop as a service, restart that service on this version before using the controls.

| Page | Controls and information |
| --- | --- |
| Overview | Runner start, graceful stop, conditional force stop, Meta pause/resume, heartbeat, funnel and recent command outcomes |
| Make | Discover 1–10 new clips, or paste a YouTube video/Short or Twitch clip/VOD. Optional `mm:ss` hints constrain cutter search. Each job shows its items and their progress. |
| Review | A/B players, approve both, approve A only, reject, confirm hook, regenerate hook, choose another cut and edit captions |
| Queue | Calendar in the active profile timezone, adjacent slot swaps, unapprove, re-slot and post now |
| Library | Search and stage filters, bulk select, reject and delete |
| Item detail | Exact posting caption, locked credit/disclosure, source, cut signal, stage history and available controls |
| Analytics | Stored platform/variant results, hook and source views, and counts by selected cut signal |
| Failures | Failed and rejected items, stage and reason, and retry for recoverable pipeline stages |
| Settings | Per-profile slots, destination and discovery switches, approval mode, ready buffer target, weights and explicit connection checks |

## Execution and posting

The signed confirmation and CSRF token bind each action to the current session. A command is validated against current item state both at submission and at application. Fresh heartbeat plus the shared SQLite runner lease means the loop owns the mailbox. Quick actions can otherwise take that same lease and use the same apply function in the request. Contention queues the command. The previous `/api/confirm` endpoint remains a mailbox-only interface for compatibility. The panel uses `/api/execute`.

The status view reports queued, running, applied, refused or failed outcomes with messages. The estimate for a queued command is the configured loop interval plus time spent finishing the current stage. Quick actions commit in a short SQLite transaction. Database or stage changes that invalidate the confirmation will refuse the command.

Make and remake use the next discover boundary when the runner is active. Otherwise, the panel launches a detached `python -m pipeline.runner make COMMAND_ID` worker. It takes the lease and runs the job’s discovery/manual insertion, download, cut, hook, voiceover, render and approval hand-off. Logs are written in the media root under `jobs/`. Hook and voiceover use the normal segment bindings and checks. A remake that changes the cut excludes windows overlapping the previous cut by half or more. Source downloads purged by retention can be downloaded again.

Graceful stop records a stop request. The runner finishes its current stage, avoids starting another, and releases the lease. Force stop becomes available after two minutes and validates the lease and stage at confirmation. Publishing and publishing recovery are protected from force stop. The runner reads local settings at the start of each cycle.

Post now reserves the earliest instant consistent with the minimum gap and maximum of two clips per local day. It skips the profile slot list, shows the exact local time before confirmation, and offers to start a stopped runner. The runner owns Meta publication and continues to observe dry run and Meta cooldowns. Reorder swaps two unstarted neighbours only when the resulting slots pass the same constraints. Unapprove removes unstarted post rows and frees the slot.

Delete moves an item to Rejected with its source identity intact. It then removes media using the retention guard, which protects paths outside the media root and media shared by another item. Approval and delete release the matching Telegram card state, so later callbacks on the old card are stale. Published items remain display-only. Preview-only items and items with started post rows have no mutation controls.

Caption overrides are limited to 2,200 characters and 30 hashtags, counting locked text. Posting uses the override for new rows and updates already-queued, unstarted rows. The renderer and transcript checks continue to use the hook and voiceover associated with the specific cut.

## Cut scoring

YouTube’s full download metadata can include a replay heatmap. The default `cut.heatmap_weight` is **0.20**. The existing audio and scene weights contribute up to 0.75 and 0.25 respectively. A bounded 0.20 bonus lets a strong replay peak break a close ranking without replacing audio and scene scoring. The mean is duration-weighted over the window. Unavailable heatmap data leaves the original score and candidate ordering intact.

Only pasted Twitch VODs attempt chat replay. Message counts use ten-second bins, with a 3× mean-rate threshold and a six-second lead correction so that a delayed chat reaction boosts the triggering moment. `cut.chat_weight`, `cut.chat_offset_seconds` and `cut.chat_spike_multiplier` are optional local overrides. Chat failures leave audio/scene scoring in use. All speech candidates still require a clean sentence ending. The stored cut signal records when engagement changes the first-ranked eligible window relative to audio and scene scoring in the searched regions. Older cuts can have no recorded signal.

## Settings and checks

The dashboard atomically writes only `config.local.yaml` and saves the previous bytes in a dated `config.local.yaml.YYYY-MM-DD_HHMMSS.bak`. Both are gitignored. Settings merge over `config.yaml`. Dry run is shown in the banner and remains display-only.

Explicit checks with configured credentials run in a separate process and display masked results. Test all includes a real Healthchecks success ping. Sending a Telegram test message uses its own button. Opening or refreshing Settings does not contact the services.

## Demo and tests

Use a fresh path outside the repo with FFmpeg installed.

```powershell
python scripts/dashboard_demo_db.py "$env:TEMP\gta-dashboard-demo.db"
python -m dashboard --db "$env:TEMP\gta-dashboard-demo.db" --media-root "$env:TEMP\gta-dashboard-demo-media"
python -m pytest -q
```

The seeded demo stages, metric history, creation job and local previews are fictional. Keep the runner stopped for visual inspection unless you have substituted offline worker stages. Live service credentials are unnecessary for the test suite, which uses temporary SQLite files.

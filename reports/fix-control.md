# fix/control

Base `ad1aeb8e9819193a886bbd4c9a099d236e57955a`. The fetched `origin/main` had `style.preset: "P2"`. Work took place in the dedicated `GTA6-fix-control` worktree.

## Done

| Task | Changes and files |
| --- | --- |
| C1 | Added idempotent version, override and metrics migrations. New items share transcription and cut analysis across verified P1/P2/P3 voiceover outputs. Legacy items keep A/B. Retention removes version MP4s and sidecars. `pipeline/db.py`, `pipeline/render.py`, `pipeline/cleanup.py`. |
| C2 | Added version approvals, separate legacy controls, Telegram preset albums and buttons, and auto-approval of usable versions after the configured switch. `dashboard/commands.py`, `pipeline/telegram_bot.py`, `config.yaml`. |
| C3 | The slot-time decision uses only approved preset arms. The bandit gives each eligible arm its configured minimum share, subject to the number of arms. Legacy A/B uses its existing allocation. `pipeline/scheduler.py`, `pipeline/posting.py`, `pipeline/analytics.py`. |
| C4 | Added grouped settings, validation, dated backups, atomic saves, go-live and go-dry tickets, and the Twitch date preservation regression test. `dashboard/settings.py`, `dashboard/app.py`, `dashboard/commands.py`, `pipeline/runner.py`, `tests/test_dashboard_control.py`, `tests/test_dashboard_controls.py`. |
| C5 | Recovered missed windows on the next analytics pass. Stored actual age, late flag, saves and reach, added manual snapshots and surfaced post metrics and warnings. `pipeline/analytics.py`, `pipeline/db.py`, `pipeline/runner.py`, `dashboard/views.py`, `dashboard/app.py`, `tests/test_analytics.py`. |
| C6 | Submitted caption-word and voiceover edits as creation jobs, persisted overrides, regenerated TTS for voiceover edits, and returned rerenders to review. Item details expose versions, sidecar metadata and voiceover. `dashboard/commands.py`, `dashboard/views.py`, `pipeline/creation.py`, `pipeline/render.py`, `pipeline/voiceover.py`. |
| C7 | Added schedule, discovered items, redacted logs, detached operations, state summaries, undo for immediately applied actions, and preset media routes. `dashboard/app.py`, `dashboard/commands.py`, `dashboard/views.py`, `pipeline/creation.py`, `pipeline/runner.py`, `pipeline/scheduler.py`. |
| C8 | Documented the HTTP endpoints, fields, action payloads and refusals in `docs/API.md`. |

Approving exactly one version pins it and excludes the result from the variant dimension. Preset learning therefore waits for auto mode or for a reviewer to approve **All** or another multi-version selection, and requires on-time metrics from those scheduler draws. Late metrics remain visible and are excluded from learning across dimensions.

## Not done / partial

- **C1/C6** — The control branch records and reads the contract sidecar path. Full sidecar generation and populated metadata in Review require the render branch.
- **C4** — Grouped settings use the current application config keys. The source-duration field currently controls the existing YouTube acquisition limit. Twitch clips use their existing acquisition rules.
- **C7** — Confirm responses include undo when the command has already applied and a reversal is available. A pending acknowledgement has no undo entry. An exact move without a previous queue slot has no previous slot to restore.

## Deviations

- **C2** — The new quality-gate key is `require_at_least_one_version`. The legacy gate still reads `require_both_renders` and checks A/B. Preset approval always requires at least one usable render, including when optional grounded-hook and probe checks are switched off. Telegram puts the inline keyboard on a companion message because media groups do not accept inline keyboards. A partial set of one version uses a single video.
- **C4** — The two settings POST routes apply the explicit direct-save interface with the existing CSRF protection. The go-live and go-dry actions use the signed ticket flow. The grouped GET includes the legacy flat values so the existing form can run until the merge. Passing Meta checks are held in the dashboard process for 15 minutes and invalidated by credential changes.
- **C5** — A pass that misses both windows fetches the current insights once, records the same actual age against both missing windows and issues one alert per live post. Manual snapshots outside a window use separate keys so they cannot fill future scheduled windows.
- **C7** — An active loop handles analytics and cleanup requests at its analytics boundary. With the runner stopped, a detached worker calls the same stage functions as the CLI. An active one-shot worker returns a busy refusal.
- **Tests** — Updated the Twitch save assertion to require preservation of the scheduled date, the legacy analytics test to require backfill instead of silently skipping, and three orchestration mocks to return verified preset rows. Three existing video integration checks failed with the host FFmpeg 7.1.5 and passed using FFmpeg 9.0.2. The final full run used FFmpeg 9.0.2 on PATH.

## Needs from other branches

- **fix/render** — Emit the sidecar next to the P1/P2/P3 render output using the shared schema. Review edits pass corrected `StylePlan.words` to Variant B so the sidecar and video can use matching captions. Confirm the `style.sfx.enabled_categories` names, music toggle and hook warning threshold match the settings UI.
- **fix/ui** — Read `versions`, use lowercase preset IDs for preview and approval, and retain A/B controls for legacy rows. Poll queued operations for command completion. Render late metrics using the supplied actual age and official window, when present.

## How to verify by hand

1. Run `pytest -q tests/test_dashboard_control.py tests/test_dashboard_controls.py tests/test_analytics.py tests/test_scheduler.py tests/test_posting.py` to verify the disposable settings, dry/live tickets, scheduler and analytics paths.
2. Inspect the historical Reel fixture with `pytest -q tests/test_analytics.py -k legacy_reel -vv`. It uses `item_a35aead652d3` and `manual_B:previous_A`, with a fake Instagram insights response. Both 24 h and 72 h rows appear as late at 96 h, a repeat fetch produces no new rows, and no learning rows are created.
3. In the disposable dashboard demo, inspect `/api/state`, `/api/settings`, `/api/schedule?days=7`, `/api/discovered` and `/api/items/<id>`. Check that empty slots carry null item IDs, Twitch's date survives a toggle-only save and the run state stays dry.
4. After the merge, render a test cut in isolated media and inspect the item detail. Review should show available P1/P2/P3 outputs, labels and sidecars. Confirm one version and check that the queue pins it. Then approve multiple versions on another item and confirm that its platform rows agree on the preset selected at posting time. Use plan-only posting and mocked Telegram and Meta transports.

## Tests

`pytest -q` with FFmpeg 9.0.2 on PATH.

```
672 passed, 3 skipped, 1 deselected in 268.92s (0:04:28)
```

# Dashboard UI rework

## Done

| Task | Change | Files |
| --- | --- | --- |
| U1 | Light default, Dark and System modes, saved theme selection, 15 px body text and a 12 px type floor. Canvas colors come from the theme. Body text and secondary text on page and panel backgrounds range from 5.72:1 to 16.87:1. | `dashboard/static/index.html`, `style.css`, `workspace.js`, `app.js` |
| U2 | Overview shows four next steps, runner controls and a compact health row with Gemini quota shown as “not reported” when appropriate. | `dashboard/static/app.js`, `style.css` |
| U3 | Review includes To approve, Needs a decision, Failed and Rejected tabs. The `/failures` alias opens Failed with the 24-hour no-clean-ending rate. | `dashboard/static/app.js`, `workspace.js`, `index.html` |
| U4 | One portrait preview carries playback position across versions. Review shows render metadata and warning thresholds, version approval, caption-word and voiceover editors, and post text editing. Legacy approval choices and shortcuts remain available. | `dashboard/static/app.js`, `workspace.js`, `style.css` |
| U5 | Grouped settings, editable time chips, publishing-mode dialogs, Twitch start date, disk usage and Connections. The current backend still has its original settings form. | `dashboard/static/app.js`, `workspace.js`, `style.css` |
| U6 | Seven-day calendar with visible empty slots, drag from approved items, week navigation, list toggle, Post now, Re-slot and the overflow menu. | `dashboard/static/app.js`, `workspace.js`, `style.css` |
| U7 | Uses server undo payloads when returned, retains existing fallback undo, renders a slim status line, removes browser confirmation popups, and patches refreshed rows in place. | `dashboard/static/app.js`, `workspace.js`, `style.css` |
| U8 | Item details link back to the origin and show published Reels, metrics and snapshot actions. Analytics labels missed windows and late snapshots. Logs has Pause and stage actions. Make includes Discovered. | `dashboard/static/app.js`, `workspace.js`, `index.html`, `style.css` |
| U9 | Documented polling and mock mode. Mock fixtures exercise new and legacy renders, failed and pending decisions, analytics, a seven-day schedule, discovered clips and logs. The test suites passed with the runtime described below. | `DASHBOARD.md`, `dashboard/static/mock-loader.js`, `mock.js`, `mock-preview.mp4` |

## Not done / partial

- **U9** — The 20 screenshots and pixel-level layout checks remain pending. The headless Chromium process aborted at startup because the execution environment denied a socket call. The cloud browser could not reach the workspace’s loopback server. A browser at the running dashboard can perform the checks below.
- **U4–U8** — The new contract actions and data were exercised with fixtures. A merged integration run needs the render and control branches.

## Deviations

- GitHub confirmed that `main` was `ad1aeb8e9819193a886bbd4c9a099d236e57955a` and its config blob matched the cached Git objects, which report `style.preset: "P2"`. Direct Git fetch timed out in this environment, so the isolated worktree used the verified local objects.
- Logs lives at `/?page=logs` because the base Flask app does not serve a `/logs` document route. The UI fetches the contract’s `/api/logs` route.
- Capabilities with dedicated GET endpoints are detected with GET. For new actions the UI can issue a prepare-only capability check, which leaves the ticket unused. Grouped settings fall back to the original form when the grouped GET response is absent.
- The default FFmpeg produced three failures in existing Variant B duration tests. The full suite passed with FFmpeg 9.0.2 on `PATH`. The UI branch has no render changes.
- The HTTPS push requested a GitHub username and the environment had no Git credentials. The branch remains committed locally.

## Needs from other branches

- **fix/control** — Implement the shared state and detail fields, grouped settings, schedule, discovered and logs endpoints, and ticket actions including undo data. Review can use item actions advertised by the control backend. Serve `/logs` if a dedicated path is desired.
- **fix/render** — Supply P1/P2/P3 render files and sidecars. Review uses sidecar metadata for the hook warning, cues, music and caption-word edits.

## How to verify by hand

Run in the `fix/ui` worktree:

```bash
demo_dir=$(mktemp -d)
mkdir -p "$demo_dir/media"
python -m dashboard --db "$demo_dir/pipeline.db" --media-root "$demo_dir/media"
```

- Open `http://127.0.0.1:8765/?mock=1`. In Light and Dark at 1440×900 and 1280×720, capture Overview, Review, Queue, Settings → Posting, and Analytics under `reports/fix-ui/`. Check for overflow, overlapping controls and text smaller than 12 px.
- In Review, play the preview and press 1, 2 and 3. Check that the playhead carries over and that P2 shows the 118 px warning. Open the legacy item and confirm the two version names and three approval choices. Edit a caption word or voiceover, approve a version, then use the Undo toast.
- Open `/failures?mock=1` and verify the Failed tab and cut rate. In Queue, open an overflow menu, refresh, then drag an approved item onto an open slot and undo the move. Test the list toggle and inline Re-slot.
- In mock Settings, add a posting time, save the group, then go live and return to dry run through the in-app dialogs. Open Sources for the Twitch start date.
- In Analytics, inspect the late snapshot and missed 72-hour window. Follow a published item, check its Reel link and return via Back. Logs should pause and resume. Make should list 10 discovered candidates.
- Reopen the dashboard without the mock query to inspect the legacy backend and confirm optional controls are absent.

## Tests

- `pytest -q` with FFmpeg 9.0.2 on `PATH`

  `668 passed, 3 skipped, 1 deselected, 1 warning in 250.67s (0:04:10)`

- `pytest -q tests/test_dashboard*.py`

  `101 passed, 1 warning in 15.04s`

- `node --check` passed on the four static JavaScript files. A Node smoke run exercised fixture responses, approval and undo, caption-word edits, schedule moves, discovery, logs, late metrics and publishing-mode actions.

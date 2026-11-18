# Integration report

18 November 2026

## Merge

Verified the complete input bundle and the four branch tips before merging. The three `--no-ff` merges completed without conflicts.

| Branch | Input tip | Merge commit |
| --- | --- | --- |
| `main` | `ad1aeb8` | Integration base |
| `fix/control` | `d7229f0` | `7a5753e` |
| `fix/render` | `25c4060` | `4f8e70b` |
| `fix/ui` | `7cb1e80` | `274020b` |

The integration configuration has `app.dry_run: true` and `style.preset: "P2"`. Verification used generated media, disposable databases and mocked Meta transport. It made no social posts or Telegram sends.

## Findings

| Finding | Status | Outcome and files |
| --- | --- | --- |
| F1 Caption indices | Done | The ASS builder assigns indices after normalizing and filtering words. The same index selects the edit and goes into the sidecar. Real P2 and legacy B render tests verify that `STRAUSS` replaces `STROWEL`, `PRE-DOWNLOAD` stays intact, and every other text/index pair is preserved. `pipeline/render.py`, `tests/test_render.py`. |
| F2 Music toggle | Done | Missing `style.music_bed.enabled` reads as true. False disables the preset flag before planning or mixing. True restores an installed catalog track in the P3 plan. Verified both overlay saves through Flask. `dashboard/settings.py`, `pipeline/style.py`, `pipeline/plan.py`, `pipeline/render.py`, `tests/test_integration_contract.py`, `tests/test_style.py`. |
| F3 Audio decoding | Done | PyAV 19.0.0, 19.0.1 and 19.0.2 all rejected `metadata_errors` with faster-whisper 1.2.1. The next highest version, 18.1.0, decoded the generated WAV to 16,000 samples. The requirements cap is 18.1.0. `requirements.txt`, `tests/test_transcribe.py`. |
| F4 Review controls and preview | Done | The action bar sticks to the bottom of Review. The 9:16 player measured 230.4 × 409.6 px at 1280 × 720 and 360 × 640 px at 1440 × 900. The full player and action bar were visible together in both themes. `dashboard/static/app.js`, `dashboard/static/workspace.js`, `dashboard/static/style.css`. |
| F5 Caption templates | Done | Empty and whitespace-only values are refused with “Caption templates cannot be blank.” GET fills unset source templates from the publishing defaults so the form can save an unrelated change. `dashboard/settings.py`, `tests/test_integration_contract.py`. |
| F6 Blank approval switch | Done | A blank field submits JSON null and a saved null reloads as blank. Verified through the real approval settings form. The reported zero behavior is addressed under Disputed. `dashboard/static/workspace.js`. |
| F7 Analytics chart | Done | Mock metrics lacked `posted_at`, which the chart uses for its date axis. Added the timestamp to fixture metrics and verified plotted dates against mock and Flask-backed data. `dashboard/static/mock.js`, `dashboard/static/app.js`, `tests/test_integration_contract.py`. |
| F8 Small polish | Done | Analytics renders Views, Likes, Comments and Shares. The breadcrumb stays on one line at the requested widths. `dashboard/static/app.js`, `dashboard/static/style.css`. |
| F9 Real backend contract | Done | Demo data includes both preset and legacy previews. Flask tests cover contract reads, single and multiple approvals, undo, the caption edit worker's transition to `cut`, the three-hour gap, and a mocked Meta snapshot that writes metrics. Go-live refuses both a missing check at prepare and a stale check at confirm. Firefox also exercised version selection, approval and undo against the demo server. `scripts/dashboard_demo_db.py`, `tests/test_integration_contract.py`, `reports/integration/`. |
| F10 Documentation | Done | Documented override indices, the music toggle, caption template validation, asset fetching and the database backup before automatic migration. `docs/API.md`, `HANDOFF.md`. |

### Disputed

F6 attributed immediate auto-approval to zero. In this version `pipeline.telegram_bot.effective_mode` keeps manual approval for both zero and null when configured for manual mode. The UI now preserves null for blank input, and the existing zero behavior is preserved.

## Tests

FFmpeg `n9.0.2-17-g2a571b6068-20260930`, FFprobe `7.1.5-0+deb13u1`, Python 3.12.14, and PyAV 18.1.0. The FFmpeg executable was placed first on PATH for rendering and pytest.

`pytest -q`

```text
689 passed, 3 skipped, 1 deselected, 1 warning in 258.71s (0:04:18)
```

The warning came from the installed AnyIO pytest plugin.

`python scripts/dashboard_demo_db.py <tmp>/demo.db` seeded 90 items. The real dashboard served the temporary database and its separate demo media root. The browser loaded every dashboard route and every settings group, verified the two Analytics date axes, submitted a blank approval switch, and checked approval and undo from Review.

`python scripts/fetch_assets.py` and `python scripts/fetch_assets.py --check` succeeded. Checks covered 20 automatic effects, two synthesized fallback sounds, five music beds and 13 emoji. Three effects and four music beds are optional manual Pixabay downloads.

`python scripts/render_previews.py` generated all three synthetic previews. `python scripts/verify_previews.py` independently passed 3/3 stream and sidecar checks. Sidecar summaries:

```text
synthetic_P1.mp4: hook 146 px | cues none | music none | 36 caption words
synthetic_P2.mp4: hook 146 px | cues 0s impact/impact_03.wav (hook), 15.067s cash/cash_02.wav (emoji) | music none | 36 caption words
synthetic_P3.mp4: hook 146 px | cues 0s impact/impact_01.wav (hook), 15.067s cash/cash_01.wav (emoji) | music silent_dance_floor.wav | 36 caption words
```

## Screenshots

Captured and inspected 20 Firefox screenshots in [`reports/integration/`](integration/). The set covers Overview, Review, Queue, Settings → Posting and Analytics at 1280 × 720 and 1440 × 900 in Light and Dark. System Chromium was tried with `--no-sandbox` and stopped at startup when the environment denied a socket call.

The first Review screenshots showed the sticky bar covering the bottom of the preview. The final layout puts the Review title and inbox tabs on one row, moves version tabs alongside the video, and compacts the action bar. The final capture confirmed the full player, a visible action bar and a single-line breadcrumb at both sizes. Analytics screenshots show populated series from the demo database.

## Open items

- On the pipeline PC, back up the database before the first start and follow the asset instructions in `HANDOFF.md`. Optional Pixabay downloads are listed in the asset manifests' companion `NEEDED.md` files.
- The bundle is ready for the owner to import and push from their PC after verification.

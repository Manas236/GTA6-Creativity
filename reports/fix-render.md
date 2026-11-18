# Fix render

18 November 2026. Based on `ad1aeb8` from `origin/main` with the P2 preset and dry run enabled.

## Done

- **R1** — Enforced the hook font floor, added two width retries per generation with a measured character budget, and routed oversized hooks to review. `pipeline/rendercore.py`, `pipeline/hooks.py`, `pipeline/render.py`, `config.yaml`, hook and render tests.
- **R2** — Fitted oversized single-word caption chunks down to 70%, with a warning at the floor. `pipeline/rendercore.py`, `pipeline/render.py`, `pipeline/overlay.py`, `tests/test_render.py`.
- **R3** — Catalogued 23 effects across nine categories and installed and checked 20 automatic CC0 downloads. `assets/sfx/`, `scripts/fetch_assets.py`.
- **R4** — Picked unique seeded cues in priority order and enforced the existing gap and cap on final output timestamps. `pipeline/audio_mix.py`, `pipeline/render.py`, `pipeline/overlay.py`, `pipeline/keywords.py`, `config.yaml`, SFX and style tests.
- **R5** — Added five automatically fetched CC0 beds and four optional Pixabay beds. The render selects from installed tracks by item ID and keeps the existing ducking and fallback. `assets/music/`, `scripts/fetch_assets.py`, `pipeline/audio_mix.py`, `pipeline/render.py`.
- **R6** — Wrote atomic JSON sidecars for verified A and B renders, including the final mix and caption words. `pipeline/render_meta.py`, `pipeline/render.py`, `tests/test_render.py`.
- **R7** — Added the config defaults and the P1, P2, P3 voiceover preview entry point. Both verification scripts report sidecar summaries. `config.yaml`, `pipeline/style.py`, `scripts/render_previews.py`, `scripts/verify_previews.py`, `scripts/style_check.py`, `tests/test_scripts.py`.

## Not done / partial

- **R3, R5** — Three Pixabay effects and four Pixabay beds require manual downloads. Their manifests leave SHA-256 blank until the original MP3s are supplied. The asset instructions give the source pages and staging paths. The 20 automatic effects and five automatic beds passed the local checksum, duration, 48 kHz PCM, and −3.0 dBFS checks.

## Deviations

- The measured hook budget is 15 characters per line with the checked-in Montserrat font. The quality-of-life example just fits at 96 px on this build, so it requests a rewrite even though it can pass the hard floor. The Lucia hook uses 150 px and the release-date hook cannot fit at 96 px.
- The final SFX and music selection happens in the owned render helpers. The planner still produces its earlier estimate, so the sidecar is the source for what was mixed. The SFX downloader uses pinned CC0 mirror URLs for Kenney and OpenGameArt originals.
- FFmpeg 7.1 emitted overlapping timestamps when padding the start of Variant B. Rebuilding the constant-rate video timestamps after padding preserves the full freeze and cut on the expected timeline.
- The synthetic preview uses the repository’s generated tone for the voiceover input. It exercises the freeze, captions and mix without requiring a speech model.
- Existing hook-grounding and libass tests use shorter sample hooks so they still exercise their original checks under the new width threshold and floor. The density tests now expect a single fallback whoosh when only one file is available. The legacy preview swap test explicitly opts into the database-backed preview path.

## Needs from other branches

- **fix/control** — Call `render_variant_b` with one plan per configured preset and write the agreed `<item_id>_<PRESET>.mp4` output path. Pass `item_id` explicitly when the filename uses a different convention. Keep the bound-item guard and review handling via `assert_item_caption_binding` and `send_collision_to_review`. Persist the version and sidecar paths under the shared contract.
- **fix/ui** — Display the review reason `hook too long` from the API and the actual audio and caption data from the version sidecar.

## How to verify by hand

1. Run `python scripts/fetch_assets.py` and then `python scripts/fetch_assets.py --check`. Look for normalized and checked files under `assets/sfx/` and `assets/music/`. The optional downloads are in the corresponding `NEEDED.md` files.
2. Run `python scripts/render_previews.py` and `python scripts/verify_previews.py --out samples/synthetic/presets`. Open `synthetic_P1.mp4`, `synthetic_P2.mp4`, and `synthetic_P3.mp4` with their adjacent `.meta.json` files. P1 is clean, P2 adds effects, and P3 adds a ducked bed.
3. Run `python scripts/style_check.py --presets P1 P2 P3 --no-sheet` for the legacy A and B render stream checks.
4. Run `pytest -q tests/test_render.py tests/test_hooks.py tests/test_caption_binding.py tests/test_sfx_density.py` for the hard floor, review path, libass caption bounds, cue priority and sidecar assertions.

## Tests

`pytest -q`

```text
675 passed, 3 skipped, 1 deselected, 1 warning in 317.57s (0:05:17)
```

`python scripts/render_previews.py`

```text
synthetic_P1.mp4: hook 146 px | cues none | music none | 36 caption words
synthetic_P2.mp4: hook 146 px | cues 0s impact/impact_03.wav (hook), 15.067s cash/cash_02.wav (emoji) | music none | 36 caption words
synthetic_P3.mp4: hook 146 px | cues 0s impact/impact_01.wav (hook), 15.067s cash/cash_01.wav (emoji) | music silent_dance_floor.wav | 36 caption words
```

`python scripts/verify_previews.py --out samples/synthetic/presets`

```text
synthetic_P1.mp4: hook 146 px | cues none | music none | 36 caption words
synthetic_P2.mp4: hook 146 px | cues 0s impact/impact_03.wav (hook), 15.067s cash/cash_02.wav (emoji) | music none | 36 caption words
synthetic_P3.mp4: hook 146 px | cues 0s impact/impact_01.wav (hook), 15.067s cash/cash_01.wav (emoji) | music silent_dance_floor.wav | 36 caption words
3/3 renders pass.
```

`python scripts/style_check.py --presets P1 P2 P3 --no-sheet`

```text
P1_A.mp4: hook 134 px | cues none | music none | 28 caption words
P1_B.mp4: hook 134 px | cues none | music none | 36 caption words
P1: 101.7s  A=21.300s B=24.800s  modes=['fill', 'fit'] segments=2 punches=0 emoji=3 sfx=0 dead_air=[(5.12, 5.78)] fade=0.15s
P2_A.mp4: hook 134 px | cues 0s impact/impact_01.wav (hook), 11.567s cash/cash_02.wav (emoji) | music none | 28 caption words
P2_B.mp4: hook 134 px | cues 0s impact/impact_01.wav (hook), 15.067s cash/cash_02.wav (emoji) | music none | 36 caption words
P2: 102.6s  A=21.300s B=24.800s  modes=['fill', 'fit'] segments=2 punches=4 emoji=3 sfx=2 dead_air=[(5.12, 5.78)] fade=0.15s
P3_A.mp4: hook 134 px | cues 0s impact/impact_01.wav (hook), 11.567s cash/cash_02.wav (emoji) | music trace_agent.wav | 28 caption words
P3_B.mp4: hook 134 px | cues 0s impact/impact_01.wav (hook), 15.067s cash/cash_02.wav (emoji) | music trace_agent.wav | 36 caption words
P3:  99.9s  A=21.300s B=24.800s  modes=['fill', 'fit'] segments=2 punches=4 emoji=3 sfx=2 dead_air=[(5.12, 5.78)] fade=0.15s
| preset | out | video s | audio s | fps | sample rate | ch | size | guard |
|---|---|---|---|---|---|---|---|---|
| P1 | A | 21.300 | 21.300 | 30 | 48000 | 2 | 1080x1920 | PASS |
| P1 | B | 24.800 | 24.800 | 30 | 48000 | 2 | 1080x1920 | PASS |
| P2 | A | 21.300 | 21.300 | 30 | 48000 | 2 | 1080x1920 | PASS |
| P2 | B | 24.800 | 24.800 | 30 | 48000 | 2 | 1080x1920 | PASS |
| P3 | A | 21.300 | 21.300 | 30 | 48000 | 2 | 1080x1920 | PASS |
| P3 | B | 24.800 | 24.800 | 30 | 48000 | 2 | 1080x1920 | PASS |
```

The speech probe in the legacy style check was unavailable because the installed audio dependency rejected `metadata_errors`. The synthetic fixture timings and FFprobe stream checks completed.

# GTA 6 Clipping Pipeline

Fully automated short-video (Reels) pipeline for GTA 6 content: discover → download →
dedupe → cut → hook → voiceover → render → approve → post → measure.

* **`PLAN.md`** — the architecture, every algorithm and every number, plus the
  "Calls I made" engineering log.
* **`HANDOFF.md`** — current state, what is not in the repository and why, and the
  exact steps to continue on the laptop.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                                 # add GEMINI_API_KEY
python scripts/fetch_assets.py                       # CC0 music bed + asset verification
pytest                                               # zero-network suite
python -m pipeline.runner status
```

`ffmpeg` and `ffprobe` must be on `PATH`, built with **libass**
(`ffmpeg -filters | grep ass`).

## Style layer

Edit effects are independently toggleable flags grouped into presets, all configured
under `style:` in `config.yaml`:

| Preset | Contents |
|---|---|
| **P0** Control | zoom-to-fill framing (F1) — **the pipeline default** |
| **P1** Clean | P0 + keyword colour (C1), keyword emoji (C2), progress bar (C3), credit tag (C4), caption pop-in (M1), dead-air removal (A3), loop-friendly end (A4) |
| **P2** Punchy | P1 + hook pop/shake (M2), punch-ins (M3), SFX (A1) |
| **P3** Full hype | P2 + slow push-in (M4), music bed (A2) |

Verify locally with `python scripts/style_check.py` (synthetic clip, all presets,
writes `samples/synthetic/presets_sheet.png`) and render real previews with
`python scripts/render_previews.py`.

## Asset credits and licenses

| Asset | Source | License |
|---|---|---|
| `assets/fonts/Montserrat-*.ttf` | [JulietaUla/Montserrat](https://github.com/JulietaUla/Montserrat) | SIL Open Font License 1.1 — see `assets/fonts/OFL.txt` |
| `assets/emoji/*.png` | **[Twemoji](https://github.com/jdecked/twemoji)** (jdecked/twemoji, 72×72 PNG set) | **Graphics © Twitter, Inc and other contributors, licensed under [CC-BY 4.0](https://creativecommons.org/licenses/by/4.0/)** |
| `assets/sfx/*.wav` | Synthesized in this repository with ffmpeg | CC0 1.0 — see `assets/sfx/LICENSES.md` |
| `assets/music/bed.mp3` | [FreePD](https://freepd.com) (fetched by `scripts/fetch_assets.py`, not committed) | CC0 1.0 — see `assets/music/LICENSES.md` after fetching |

The Twemoji PNGs are used by style element **C2** (keyword emojis), because libass
cannot rasterise colour emoji — they are composited as image overlays instead. Only the
codepoints referenced by `style.keyword_emoji.map` are committed.

## Tests

```bash
pytest           # zero-network: unit, libass raster and small ffmpeg renders
pytest -m live   # live network / external process integration tests
```

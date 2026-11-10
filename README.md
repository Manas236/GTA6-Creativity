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
| `assets/music/bed.mp3` | "Trace Agent" by Vitalezzz, [OpenGameArt](https://opengameart.org/content/trace-agent) (fetched by `scripts/fetch_assets.py`, not committed; FreePD closed in 2025) | CC0 1.0 — see `assets/music/LICENSES.md` after fetching |

The Twemoji PNGs are used by style element **C2** (keyword emojis), because libass
cannot rasterise colour emoji — they are composited as image overlays instead. Only the
codepoints referenced by `style.keyword_emoji.map` are committed.

## Tests

```bash
pytest           # zero-network: unit, libass raster and small ffmpeg renders
pytest -m live   # live network / external process integration tests
python scripts/cp4_dry_run.py   # ~10 simulated days of approvals/slots/posts/metrics/cleanup, no network
```

## Moving to a server (Linux)

Any always-on Linux machine works (a VPS, a home server); nothing here is provider-specific.
**Only one machine may run the pipeline at a time**: the runner's lock lives in `pipeline.db`,
so two machines with two databases would not see each other and could both post.

1. **Install** Python 3.11, `ffmpeg` built with libass (`ffmpeg -filters | grep ass`), and git.
   Create a user and the folder:
   ```bash
   sudo useradd --system --create-home --shell /usr/sbin/nologin gta6
   sudo mkdir -p /opt/gta6-pipeline && sudo chown gta6:gta6 /opt/gta6-pipeline
   sudo -u gta6 git clone https://github.com/Manas236/GTA6-Creativity.git /opt/gta6-pipeline
   cd /opt/gta6-pipeline
   sudo -u gta6 python3.11 -m venv .venv
   sudo -u gta6 .venv/bin/pip install -r requirements.txt
   sudo -u gta6 .venv/bin/python scripts/fetch_assets.py
   sudo -u gta6 .venv/bin/python -m pytest -q            # must be green before going further
   ```
2. **Stop the laptop**: Ctrl-C its `python -m pipeline.runner loop` and make sure it is not
   started again (scheduled task, autostart).
3. **Copy the state** from the laptop to `/opt/gta6-pipeline/` (scp, rsync, a USB stick):
   `.env` (secrets: copy it as a file, never paste it into a chat or a ticket), `pipeline.db`,
   and `media/renders/` + `media/audio/` + `media/cuts/` so queued items still have their
   videos. Then `sudo chown -R gta6:gta6 /opt/gta6-pipeline` and `chmod 600 .env`.
   Starting with an empty database instead is fine too; the launch date, the A/B learning and
   the approval switch then start from scratch.
4. **Flip the timezone profile** in `config.yaml`:
   ```yaml
   posting:
     profile: "server"      # America/New_York 18:00 and 21:00 (was "local": Asia/Kolkata 13:00, 18:00)
   ```
   Slots are stored as UTC, so posts already queued keep the moment they were given; every new
   approval is slotted in the server profile's times. Edit `posting.profiles.server` to change them.
5. **Run it as a service**: `sudo cp deploy/gta6-pipeline.service /etc/systemd/system/`, edit
   `User=` and the paths if yours differ, then
   `sudo systemctl daemon-reload && sudo systemctl enable --now gta6-pipeline`.
   Logs: `journalctl -u gta6-pipeline -f`. Status: `sudo -u gta6 .venv/bin/python -m pipeline.runner status`.
6. **Dead-man switch**: set `HEALTHCHECK_URL` in `.env` to a Healthchecks.io check (period =
   `orchestrator.loop_interval_seconds`, grace ≥ 30 min, since a cycle that renders can take
   minutes). The runner pings after every fully successful cycle; if pings stop, Healthchecks.io
   alerts you. A failed ping never affects the pipeline.

Datacenter IP ranges are often throttled by YouTube (PLAN.md §12): the pipeline then pauses
that source and alerts on Telegram; it does not use cookies or proxies to get around it.

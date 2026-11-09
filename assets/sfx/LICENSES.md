# SFX sources and licenses (style element A1)

| File | Source | License | Notes |
|---|---|---|---|
| `boom.wav` | Synthesized in this repository by `pipeline.audio_mix.synthesize_sfx("boom")` (ffmpeg `aevalsrc`) | CC0 1.0 / public domain — original output of code in this repository, no third-party sample used | 0.70 s, 48 kHz mono, peak-normalized to −1.0 dBFS. Low sine thump with a pitch drop (110 → 40 Hz) plus a short noise transient, low-passed at 320 Hz. |
| `whoosh.wav` | Synthesized in this repository by `pipeline.audio_mix.synthesize_sfx("whoosh")` (ffmpeg `aevalsrc`) | CC0 1.0 / public domain — original output of code in this repository, no third-party sample used | 0.45 s, 48 kHz mono, peak-normalized to −1.0 dBFS. Band-limited noise sweep (320 Hz–7 kHz) with a rising 300 → 4500 Hz chirp. |

## Why these are synthesized rather than downloaded

The preferred CC0 source was **Kenney.nl** ("Impact Sounds", "UI Audio", CC0 1.0 —
<https://kenney.nl/assets>). That host, along with OpenGameArt, Freesound and
Pixabay, is **not reachable from the cloud build sandbox** this layer was built in:
the egress proxy answered `403` to `CONNECT kenney.nl:443` (and to
`opengameart.org`, `freesound.org`, `cdn.pixabay.com`). Only `github.com` /
`raw.githubusercontent.com` and the package registries are allowed, so the brief's
documented fallback was used: the two cues were synthesized with ffmpeg and committed.

Both files are therefore original output of code in this repository and carry no
third-party rights. They are safe to ship as-is.

## Replacing them with the Kenney CC0 originals (optional, on the laptop)

`python scripts/fetch_assets.py --prefer-download` attempts the Kenney CC0 pack and,
only if the download succeeds and the license file is present, installs
`boom.wav` / `whoosh.wav` from it and rewrites this table. Without
`--prefer-download` the script leaves the synthesized files in place (it is
idempotent and never overwrites a good asset).

## Level staging

Rendered SFX never compete with the voice: `pipeline.audio_mix.sfx_gain_db` measures
the clip's speech peak with `volumedetect` and gains each cue so its peak lands
`style.sfx.headroom_db` (default **6 dB**) below that peak.

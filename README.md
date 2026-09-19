<h1 align="center">Doblarr</h1>

<p align="center">
  <strong>AI dubbing for your media library.</strong><br/>
  The missing link in your *arr stack — turn a foreign-language film into an added,
  translated audio track, voiced by cloned speaker voices.
</p>

<p align="center">
  <em>Doblaje</em> (Spanish: dubbing) + <code>-arr</code>. Sits next to Bazarr:
  Bazarr does subtitles, Doblarr does dubs.
</p>

---

## What it does

Given a video (e.g. a Korean movie) and optionally its subtitles, Doblarr produces
a new audio track — say English or Spanish — spoken in voices cloned from the
original actors, and muxes it back in as **"AI - ES"** without touching the
original. Plex/Jellyfin then just show it as another audio option.

Doblarr owns the movie-specific pipeline. **[voicebox](https://github.com/jamiepine/voicebox)**
(MIT) is the voice-cloning + TTS engine, called over HTTP — not vendored — so the
two stay decoupled and voicebox upgrades come for free.

## Pipeline

| # | Stage | Tool | Status |
|---|-------|------|--------|
| 1 | Extract audio | ffmpeg | ✅ real |
| 2 | Separate dialogue vs music+FX | Demucs `htdemucs_ft` | 🚧 stub |
| 3 | Timed transcript | subtitles (pysubs2) / WhisperX | ✅ subs · 🚧 whisper |
| 4 | Speaker diarization | pyannote 3.1 | 🚧 stub |
| 5 | Translate (dubbing-aware, length-budgeted) | Claude | ✅ wiring · 🚧 impl |
| 6 | Clone voices + synthesize lines | **voicebox** | ✅ wiring |
| 7 | Fit timing (isochrony) | rubberband / ffmpeg | 🚧 stub |
| 8 | Mix dialogue over M&E + ducking | ffmpeg | 🚧 stub |
| 9 | Mux new track back | ffmpeg | ✅ real |

The whole thing runs end-to-end today in **`--dry-run`** (prints the plan, no heavy
deps). Stubs marked 🚧 are the build-out work, each isolated in its own module
under `doblarr/stages/`.

## Quickstart

```bash
# 1. install core deps (skeleton + dry run)
pip install -r requirements.txt

# 2. copy and edit config
cp config.example.yaml config.yaml

# 3. make sure voicebox is running, then:
python -m doblarr check

# 4. dry-run the full pipeline on one file
python -m doblarr dub "M:/Plex/Movies/SomeKoreanFilm (2022)/film.mkv" \
    --from ko --to es --subs "film.ko.srt" --dry-run
```

## Project layout

```
doblarr/
  cli.py            # command-line interface
  config.py         # YAML config + defaults
  models.py         # DubJob / Segment / Speaker
  pipeline.py       # runs the stages in order
  clients/
    voicebox.py     # HTTP client (health, transcribe, profiles, generate, audio)
    translator.py   # Claude / passthrough translation
  stages/           # one module per pipeline step (see table above)
```

## Roadmap

- [ ] Implement the 🚧 stages (separate → diarize → fit_timing → mix)
- [ ] Wire `ClaudeTranslator` (load the `claude-api` skill for current model ids)
- [ ] Web UI + job queue (make it a real *arr)
- [ ] Radarr/Sonarr/Plex webhook trigger → auto-dub new foreign titles overnight
- [ ] Borrow & re-implement the duration-matching + ducking approach proven by
      [neutrinus/dubarr](https://github.com/neutrinus/dubarr) (GPL — study, don't copy)

## License

MIT — see [LICENSE](LICENSE). voicebox is MIT; neutrinus/dubarr is GPL-3.0 (used
only as a reference to re-implement from, never copied in).

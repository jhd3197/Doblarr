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

## What's real today

The **app** is live — a real backend + web UI you can run and use:

- **Web UI + API** (`doblarr serve`) — serves the interface and a REST API.
- **Library scan** — reads your **Radarr (movies) + Sonarr (shows)** and classifies
  every title as **needs-dub / partial / available** by looking at its actual audio
  tracks vs. your target languages. Shows in the Library page with live counts.
- **Settings** — edit the config from the UI; saved to `config.yaml` (secrets redacted,
  never clobbered).
- **Job queue** — enqueue a dub from the Library; a background worker runs it and the
  Dubs page + Overview update live.

What's **not** real yet is the **dub output itself** — the worker runs the pipeline in
**dry-run** (it plans every stage but produces no audio) until the heavy stages are
implemented and voicebox is running (see Pipeline + Roadmap). This is the
`dub.dry_run: true` default in config — flip it to `false` once voicebox + the heavy
deps are in place.

## Running it

```bash
pip install -r requirements.txt          # core + FastAPI/uvicorn
cp config.example.yaml config.yaml        # set Radarr/Sonarr URLs + API keys
python -m doblarr serve                    # http://127.0.0.1:6363
```

### API authentication

Set `web.api_key` in `config.yaml` to lock the API: every `/api/*` route (except
`/api/health` and `/api/health/ready`) then requires the `X-Api-Key: <key>` header
(or `?api_key=<key>`). The web UI prompts for the key once and remembers it. With no
key configured the API stays open (fine for a trusted home network) and a warning is
logged at startup.

### Webhooks (Radarr/Sonarr → Doblarr)

Doblarr accepts the standard *arr webhook JSON at `POST /api/webhooks/radarr` and
`POST /api/webhooks/sonarr`. A **Download** (import) event schedules a library rescan —
a burst of webhooks coalesces into one scan (`discovery.webhook_debounce`, default 30s);
**Test** events just return 200; other event types are ignored. If `filtering.auto_label`
is on, the rescan also syncs Plex labels.

Setup in Radarr/Sonarr: **Settings → Connect → Add → Webhook** —
URL `http://<doblarr-host>:6363/api/webhooks/radarr` (or `.../sonarr`), trigger
**On Import/On Upgrade**. If you set `web.api_key`, add a header `X-Api-Key: <key>`
in the webhook settings (no key configured → webhooks are open like the rest of the API).

Open the UI, go to **Library** to see your real collection, and **Queue dub** on a
needs-dub title to watch it flow through the queue. The CLI still works too:
`python -m doblarr dub "<file>" --from ko --to es --subs film.srt --dry-run`.

### Docker

Runs next to your other -arrs; reaches Radarr/Sonarr/Plex via `host.docker.internal`.

```bash
cp config.docker.example.yaml config/config.yaml   # fill in URLs + keys
docker compose up -d --build                         # http://localhost:6363
```

`config/` holds `config.yaml`; `data/` holds the job store + generated Kometa
fragment. The image is the app only (no ML stack) — the worker runs dry-run until
Demucs/voicebox are added.

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

## Project layout

```
doblarr/
  cli.py            # CLI: serve / dub / check
  server.py         # FastAPI app: serves the UI + REST API
  config.py         # YAML config + defaults, read/write, secret redaction
  discovery.py      # library scan: needs-dub / partial / available
  jobs.py           # persistent job queue + background worker
  models.py         # DubJob / Segment / Speaker
  pipeline.py       # runs the stages in order (with progress callback)
  clients/
    radarr.py       # Radarr API (movies)
    sonarr.py       # Sonarr API (shows)
    voicebox.py     # voicebox HTTP client (transcribe, profiles, generate, audio)
    translator.py   # Claude / passthrough translation
  stages/           # one module per pipeline step (see table above)
web/index.html      # the web UI (Overview / Library / Dubs / Voices / Settings)
```

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | liveness (process up) |
| GET | `/api/health/ready` | readiness (voicebox up + a source configured), 503 otherwise |
| GET | `/api/library` | scan Radarr+Sonarr, classify every title (`?refresh=true` bypasses the scan cache) |
| GET/POST | `/api/config` | read (redacted) / save config |
| GET/POST | `/api/jobs` | list / enqueue dub jobs |
| POST | `/api/jobs/clear-finished` | remove done+failed+cancelled jobs |
| DELETE | `/api/jobs/{id}` | remove one job (a *running* job is cancelled instead) |
| GET | `/api/events` | SSE stream of job/scan progress (replay + live; use `?api_key=` from browsers) |

The UI consumes `/api/events` via `EventSource` for live job progress (slow polling
as a fallback). Cancelling a running job stops it between pipeline stages and kills
any in-flight ffmpeg process; a cancel while waiting on a voicebox generation aborts
the wait but leaves the remote generation running (voicebox has no cancel endpoint).

## Development

```bash
pip install -e ".[dev]"   # app + pytest/ruff/mypy/httpx
python -m pytest -q       # test suite (no network or *arr services needed)
ruff check .              # lint
mypy doblarr/             # type check
```

## Roadmap

- [x] Web UI + REST API + job queue (a real *arr shell)
- [x] Library discovery from Radarr + Sonarr
- [x] Settings read/save from the UI
- [x] Plex labeling + hide (Kometa handoff) + scheduled auto-sync
- [x] Docker packaging
- [ ] **The real dub** — flip the worker to `dry_run=False` once these land:
  - [ ] `separate` (Demucs), `diarize` (pyannote), `whisper` transcribe, `fit_timing`, `mix`
  - [ ] wire `ClaudeTranslator` (load the `claude-api` skill for current model ids)
  - [ ] voicebox running locally on `17493`
- [ ] Voices page from real diarization/cloning data
- [ ] Radarr/Sonarr/Plex webhook trigger → auto-dub new foreign titles overnight
- [ ] Borrow & re-implement the duration-matching + ducking approach proven by
      [neutrinus/dubarr](https://github.com/neutrinus/dubarr) (GPL — study, don't copy)

## License

MIT — see [LICENSE](LICENSE). voicebox is MIT; neutrinus/dubarr is GPL-3.0 (used
only as a reference to re-implement from, never copied in).

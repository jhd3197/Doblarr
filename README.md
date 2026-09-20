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

The worker defaults to **dry-run** (`dub.dry_run: true`), which plans stages without
producing audio. Real extraction, separation, subtitle transcription, translation,
speech generation, timing, mixing and muxing are implemented. Set dry-run to false
when the required local services and dependencies are ready.

For an interrupted first episode with existing audio stems, the explicit
`scripts/finish_episode.py` runner saves translation batches and individual voice
clips, then assembles a full-length video. See [episode recovery](docs/episode-recovery.md).

## Running it

```bash
pip install -r requirements.txt          # core + FastAPI/uvicorn
cp config.example.yaml config.yaml        # set Radarr/Sonarr URLs + API keys
python -m doblarr serve                    # http://127.0.0.1:6363
```

### AI translation

Prompture is the shared AI translation layer for every real translation provider.
It handles structured JSON generation and provider capabilities; Doblarr validates
nonempty translated text and an exact one-to-one mapping of segment IDs before TTS.
Malformed responses get two attempts, followed by individual-line attempts for a
failed batch. Exhausted attempts fail the job rather than substitute source dialogue.
Character budgets remain approximate dubbing guidance, not a guarantee of audio duration.

Existing configuration remains supported:

- `translate.provider: claude` uses Prompture's Claude driver with the existing
  model ID and `ANTHROPIC_API_KEY` (or Prompture's `CLAUDE_API_KEY`).
- `translate.provider: prompture` accepts `provider/model` and an optional endpoint.
- `translate.provider: voicebox` adapts the service's local LLM through the same
  Prompture schema and validation pipeline.
- `translate.provider: passthrough` is an explicit stub for development.

Prompture is installed with the core dependencies. Translation loads it lazily,
so dry runs do not initialize an AI provider. Parsed-response usage metadata is
available on the translator's `last_usage`; Voicebox does not report token usage.

### API authentication

Set `web.api_key` in `config.yaml` to lock the API: every `/api/*` route (except
`/api/health` and `/api/health/ready`) then requires the `X-Api-Key: <key>` header
(or `?api_key=<key>`). The web UI prompts for the key once and remembers it. With no
key configured the API stays open (fine for a trusted home network) and a warning is
logged at startup.

Security notes: `config.yaml` holds your *arr/Plex keys in plaintext — protect it with
filesystem permissions (it's gitignored). Doblarr serves plain HTTP; put it behind a
reverse proxy for HTTPS if you expose it beyond localhost/LAN.

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

### Persistence & resume

Jobs and the last library scan live in a SQLite database (`paths.db`, default
`<work_dir>/doblarr.db`; in Docker that's inside the mounted `/data`), so the Dubs
page and Overview survive restarts. A legacy `work/jobs.json` is imported once and
renamed to `jobs.json.migrated`. Jobs interrupted mid-run are re-queued at startup,
and the pipeline **skips stages whose output artifact already exists** and is newer
than the input file (extract/separate/synthesize/mix/mux) — resubmitting continues
where the artifacts stop. Enqueue with `"force": true` to redo every stage. After a
real (non-dry-run) mux, Doblarr asks Plex to refresh that item so the new
"`<Language>` AI" track shows up at once (`plex.auto_refresh`, default on; failures
never fail the job).

### Teasers & voice casting

Before committing to a full dub, queue a **tease** (Library card → "Tease", or
`POST /api/jobs` with `"kind": "tease"`): Doblarr dubs only the first
`dub.teaser_minutes` (default 10) into `<title>.tease.mkv` so you can audition the
voices. Tease artifacts live in a separate `.tease` namespace and never poison the
full dub's checkpoint cache.

Every detected speaker gets **one voice** from a per-title **voice cast**, labeled by
archetype ("Narrator", "Adult M 1", "Adult F 2", …) and auto-assigned on the first
tease. The cast persists (SQLite `voice_casts` table) and is reused by the full dub —
edit it from a Library card's "Cast" button (`GET`/`PUT /api/cast`, voices from
`GET /api/voices`, which proxies voicebox profiles or falls back to
`dub.preset_voices`). Multi-speaker casting lands with diarization (pyannote); until
then jobs fall back to a single narrator voice.

Open the UI, go to **Library** to see your real collection, and **Queue dub** on a
needs-dub title to watch it flow through the queue. The CLI still works too:
`python -m doblarr dub "<file>" --from ko --to es --subs film.srt --dry-run`.

### Docker

Runs next to your other -arrs; reaches Radarr/Sonarr/Plex via `host.docker.internal`.

```bash
cp config.docker.example.yaml config/config.yaml   # fill in URLs + keys
docker compose up -d --build                         # http://localhost:6363
```

Tagged releases (`git tag v0.2.0 && git push --tags`) build a multi-arch image to
`ghcr.io/<owner>/doblarr` (`latest` + the version tag) and cut a GitHub release with
auto-generated notes — see `.github/workflows/release.yml`.

`config/` holds `config.yaml`; `data/` holds the job store + generated Kometa
fragment. The image is the app only (no ML stack) — the worker runs dry-run until
Demucs/voicebox are added.

## Pipeline

| # | Stage | Tool | Status |
|---|-------|------|--------|
| 1 | Extract audio | ffmpeg | ✅ real |
| 2 | Separate dialogue vs music+FX | Demucs `htdemucs_ft` | ✅ real |
| 3 | Timed transcript | subtitles (pysubs2) / WhisperX | ✅ real |
| 4 | Speaker diarization | pyannote 3.1 | ✅ real |
| 5 | Translate (dubbing-aware, length-budgeted) | Claude | ✅ real |
| 6 | Clone voices + synthesize lines | **voicebox** | ✅ wiring |
| 7 | Fit timing (isochrony) | ffmpeg `atempo` | ✅ real |
| 8 | Mix dialogue over M&E + ducking | ffmpeg `sidechaincompress` | ✅ real |
| 9 | Mux new track back | ffmpeg | ✅ real |

The whole thing runs end-to-end today in **`--dry-run`** (prints the plan, no heavy
deps). Stubs marked 🚧 are the build-out work, each isolated in its own module
under `doblarr/stages/`.

## Project layout

```
doblarr/
  cli.py            # CLI: serve / dub / check
  server.py         # app assembly, lifespan, authentication, SSE and static UI
  routes/           # configuration, library, jobs and title API routers
  library_service.py # discovery cache, persisted scan state and webhook orchestration
  config.py         # YAML config + defaults, env overrides, secret redaction
  config_schema.py  # pydantic validation of config.yaml (warnings, never fatal)
  auth.py           # X-Api-Key dependency for /api/* (optional; web.api_key)
  discovery.py      # library scan: needs-dub / partial / available
  jobs.py           # job queue (SQLite) + background worker (cancel-aware)
  store.py          # sqlite3 Database: WAL, migrations, jobs + scan_state
  events.py         # EventBus: in-process pub/sub with replay buffer
  services.py       # lazy cached service clients (DI seam) from Config
  webhooks.py       # *arr webhook classification + debounced rescan
  cache.py          # TTL cache for library scans
  ffmpeg.py         # run_ffmpeg/run_ffprobe with FFmpegError + cancel
  logging_setup.py  # console + rotating file + uvicorn + SSE log stream
  scheduler.py      # periodic rescan thread
  models.py         # DubJob / Segment / Speaker
  pipeline.py       # runs the stages in order (progress, cancel, resume)
  clients/
    base.py         # ArrClient: Session + tenacity retry + uniform errors
    radarr.py       # Radarr API (movies)
    sonarr.py       # Sonarr API (shows)
    plex.py         # Plex API (labels; token in header)
    voicebox.py     # voicebox HTTP client (transcribe, profiles, generate, audio)
    translator.py   # shared Prompture structured translation
  stages/           # one module per pipeline step (see table above)
web/index.html      # application shell
web/styles.css      # shared styles
web/js/app.js       # routing, navigation and feature wiring
web/js/settings-model.js # field metadata shared by settings and title plans
web/js/api.js       # JSON requests, authentication and consistent errors
web/js/jobs-data.js # coalesced job requests shared across screens
web/js/             # settings, library, jobs and title feature controllers
```

## API

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | liveness (process up) |
| GET | `/api/health/ready` | readiness (voicebox up + a source configured), 503 otherwise |
| GET | `/api/library` | scan Radarr+Sonarr, classify every title (`?refresh=true` bypasses the scan cache) |
| GET/POST | `/api/config` | read (redacted) / save config |
| GET/POST | `/api/jobs` | list / enqueue dub jobs (`force: true` ignores cached artifacts) |
| POST | `/api/jobs/clear-finished` | remove done+failed+cancelled jobs |
| DELETE | `/api/jobs/{id}` | remove one job (a *running* job is cancelled instead) |
| GET | `/api/jobs/{id}/file` | stream the produced dub/tease (HTTP Range; only under output/work dirs) |
| GET | `/api/events` | SSE stream of job/scan/log events (replay + live; `?api_key=` from browsers) |
| POST | `/api/webhooks/radarr` | Radarr webhook (Download → debounced rescan; Test → 200) |
| POST | `/api/webhooks/sonarr` | Sonarr webhook (same) |
| GET/PUT | `/api/cast` | read / save a title's voice cast (`?key=` or `?path=`/`?tmdb_id=`/`?title=`) |
| GET/PUT | `/api/plan` | read / save per-title configuration overrides |
| GET | `/api/voices` | voice list (voicebox profiles, else `dub.preset_voices`) |

The UI consumes `/api/events` via `EventSource` for live job progress and a log
tail (slow polling as a fallback). Cancelling a running job stops it between
pipeline stages and kills
any in-flight ffmpeg process; a cancel while waiting on a voicebox generation aborts
the wait but leaves the remote generation running (voicebox has no cancel endpoint).

## Development

```bash
pip install -e ".[dev]"   # app + pytest/pytest-cov/ruff/mypy/httpx
python -m pytest -q       # test suite (no network or *arr services needed)
python -m pytest -q --cov=doblarr --cov-report=term-missing   # with coverage
ruff check .              # lint
mypy doblarr/             # type check
# pre-commit install      # optional: run ruff+mypy as git hooks (.pre-commit-config.yaml)
```

### Frontend checks

The UI uses native JavaScript modules. There is no frontend build step and Node
is needed only for development checks. Use Node 22 or newer:

```bash
npm ci
npx playwright install chromium
npm run check            # lint + API tests + browser regressions
npm test                 # fast API/helper tests only
npm run test:browser     # settings, queue errors and title-plan browser flows
```

Browser tests start an isolated FastAPI server with temporary configuration and
storage on port 8766; they do not use your media services or local config. They
use `.venv` when available, otherwise `python`; set `PYTHON` to choose another
interpreter. Python tests share a `client_factory` fixture in `tests/conftest.py`
for isolated API clients with automatic cleanup. Tests that exercise worker
startup use an explicit application lifespan.

For backend auto-reload during development:

```bash
python -m uvicorn doblarr.server:create_app --factory --reload --port 6363
```

Settings defaults belong in `ConfigModel` in `doblarr/config_schema.py`.
Presentation metadata belongs in `web/js/settings-model.js`; title plans reuse
those field definitions. Controls inherited from the design mockup that had no
backend setting have been removed. Add API calls through `api()` and shared job
reads through `getJobs()` so authentication, error handling and concurrent reads
stay consistent. Each feature controller receives navigation callbacks from
`app.js`, keeping feature imports free of circular dependencies.

CI checks Python lint/types/tests and the frontend checks on pull requests and
pushes to `dev`, `main` and `master`.

## Roadmap

- [x] Web UI + REST API + job queue (a real *arr shell)
- [x] Library discovery from Radarr + Sonarr
- [x] Settings read/save from the UI
- [x] Plex labeling + hide (Kometa handoff) + scheduled auto-sync
- [x] Docker packaging
- [ ] **The real dub** — flip the worker to `dry_run=False` once these land:
  - [x] `separate` (Demucs two-stems), `diarize` (pyannote), `whisper` transcribe
        (whisperx/faster-whisper), `fit_timing` (atempo stretch), `mix`
        (sidechain ducking) — `pip install doblarr[real]`
  - [x] wire `ClaudeTranslator` (Anthropic Messages API, numbered-lines protocol)
  - [x] voicebox running locally on `17493`
- [ ] Voices page from real diarization/cloning data
- [ ] Radarr/Sonarr/Plex webhook trigger → auto-dub new foreign titles overnight
- [ ] Borrow & re-implement the duration-matching + ducking approach proven by
      [neutrinus/dubarr](https://github.com/neutrinus/dubarr) (GPL — study, don't copy)

## License

MIT — see [LICENSE](LICENSE). voicebox is MIT; neutrinus/dubarr is GPL-3.0 (used
only as a reference to re-implement from, never copied in).

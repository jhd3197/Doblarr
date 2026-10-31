<p align="center">
  <img src="ui/public/logo.png" alt="Doblarr logo" width="160" />
</p>

<h1 align="center">Doblarr</h1>

[![Doblarr demo: dub your movie library, keep the original audio](https://img.youtube.com/vi/6qZMMPa4zk8/maxresdefault.jpg)](https://youtu.be/6qZMMPa4zk8)

<sub>Demo footage: *Tears of Steel* (CC) Blender Foundation | [mango.blender.org](https://mango.blender.org), licensed [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/).</sub>

<p align="center">
  <a href="https://github.com/jhd3197/Doblarr/pkgs/container/doblarr">
    <img src="https://ghcr-badge.elias.eu.org/shield/jhd3197/Doblarr/doblarr" alt="Docker pulls (GitHub Container Registry)" />
  </a>
</p>

<!-- The pulls badge requires the GHCR package to be published and publicly accessible. -->

<p align="center">
  <strong>AI dubbing for your media library.</strong><br/>
  Adds a translated audio track to a film or episode, voiced by clones of the
  original speakers, next to Radarr, Sonarr and Plex.
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
(MIT) is the voice-cloning + TTS engine, a separate service Doblarr calls over HTTP.
[VoiceStudio](https://github.com/debpalash/VoiceStudio) works the same way: set
`speech.backend: voicestudio` and Doblarr clones and speaks through its local API
instead (engines such as OmniVoice, VoxCPM2 and IndexTTS2; translation then needs a
provider other than `voicebox`).

## Status

Every stage of a dub runs for real: extraction, dialogue separation, transcription
from subtitles or Whisper, speaker grouping, translation, voice cloning and speech,
timing, mixing and muxing. Around it are a web UI, a job queue, a review editor and
an episode studio.

A new install starts in **dry run** (`dub.dry_run: true`): jobs plan their stages and
produce no audio, so you can check the library scan and settings first. Set it to
`false` once the services below are running.

For an interrupted first episode with existing audio stems, the explicit
`scripts/finish_episode.py` runner saves translation batches and individual voice
clips, then assembles a full-length video. See [episode recovery](docs/episode-recovery.md).

## What you need

- **FFmpeg and ffprobe** on the PATH (included in the Docker images).
- **A speech service:** [voicebox](https://github.com/jamiepine/voicebox), running on
  its default port 17493, or VoiceStudio (`speech.backend: voicestudio`).
- **A translation model:** Claude by default (`translate.provider: claude`, which
  needs `ANTHROPIC_API_KEY` and is billed per use), any Prompture model including
  local Ollama ones (`translate.provider: prompture`), or voicebox's local LLM.
- **For real dubs, the local models:** Demucs separation, WhisperX transcription and
  speaker grouping (`pip install "doblarr[real]"`, or the GPU Docker image). They run
  on a CPU, much faster on an NVIDIA GPU. Speaker grouping needs no account; only
  `transcribe.diarizer: pyannote` needs a HuggingFace token.
- **Disk space** for the work folder: separated stems and takes for each episode,
  plus a copy of every saved dub version. `retention.*` and `dub.versions_kept`
  limit what is kept.
- Radarr, Sonarr and Plex are optional: they feed the library page; a file can
  also be dubbed by path.

## Running it

### Install

Use Docker (below) or run from source. The `doblarr` package on PyPI is an early
0.1.0 build that lacks the web UI and much of what is described here.

### Run from source

```bash
pip install -r requirements.txt          # core + FastAPI/uvicorn
npm ci && npm run build:ui                # the web UI (Node 22+), into ui/dist
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

Every `/api/*` route (except `/api/health` and `/api/health/ready`) requires the
`X-Api-Key: <key>` header (or `?api_key=<key>`). On first start `doblarr serve`
generates a key, saves it as `web.api_key` in `config.yaml` and prints it; the web UI
asks for it once and remembers it. To run without a key, set
`web.allow_unauthenticated: true`.

Requests must also be addressed to a name Doblarr expects. IP addresses, `localhost`
and local names (`nas`, `nas.local`, `.lan`, `.home.arpa`, `.internal`) always work.
Any other domain, such as a reverse proxy's `dub.example.com`, must be listed in
`web.allowed_hosts` (or the `DOBLARR_ALLOWED_HOSTS` environment variable, comma
separated). This stops a web page from reaching the API through DNS rebinding.

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
**On Import/On Upgrade**, and add a header `X-Api-Key: <key>` with the key from
`web.api_key`.

### Persistence & resume

Jobs and the last library scan live in a SQLite database (`paths.db`, default
`<work_dir>/doblarr.db`; in Docker that's inside the mounted `/data`), so the Dubs
page and Overview survive restarts. A legacy `work/jobs.json` is imported once and
renamed to `jobs.json.migrated`. Jobs interrupted mid-run are re-queued at startup,
and the pipeline reuses artifacts whose input and configuration manifests still
match. Source artifacts are shared across target languages; translated scripts and
outputs use separate language namespaces. Completed TTS clips are verified by
content fingerprints, and interrupted waits resume the saved remote generation ID. Enqueue with `"force": true` to redo every stage. After a
real (non-dry-run) mux, Doblarr asks Plex to refresh that item so the new
"`<Language>` AI" track shows up at once (`plex.auto_refresh`, default on; failures
never fail the job).

### Faster generation and dialogue review

Choose **Custom**, **Preview**, or **Final** in generation settings. Preview uses
preset voices, the preview engine, faster separation, and no translation repair
retries; assign existing compatible Voicebox profile IDs first. Final enables
duration fitting. Custom respects your individual settings. Engine availability
and throughput depend on your Voicebox installation.

Use **Audition voices** on a title, or `--kind audition` on the CLI, for a short
WAV montage covering speakers, fast dialogue, quiet/loud passages, and different
points in the source. Transcription and speaker detection still inspect the source;
separation and speech generation run on the selected excerpts.

Completed or failed jobs with dialogue snapshots expose **Review** on the Dubs page.
Listen to a line, edit its wording, timing, voice, or delivery, then render changes.
A new job reuses matching clips and rebuilds the mix/export; the previous review
snapshot stays available. **Generate a new take** invalidates that line explicitly.
Delivery instructions require a compatible Qwen engine.

Translation supports scene context, terminology dictionaries, and bounded shortening
of overlong lines. Speech checks flag silence, clipping, duration problems, and
optional ASR mismatches. Loudness normalization and configurable ducking preserve
background dynamics. Unresolved flags remain visible for human review.

Each run writes stage timings and cache/retry counters to `work/reports`, available
through `GET /api/jobs/{id}/report`.

### Shows and narrator voices

Click an episode title to open its own page at
`/title/tvdb-<show-id>/episode/<sonarr-episode-id>/voices`. Each title tab has
its own URL (`plan`, `voices`, `jobs`, or `meta`; shows also have `episodes`),
so refresh, shared links, and browser back/forward preserve the current workspace.
Movies use `/title/tmdb-<movie-id>/<tab>` and shows use `/title/tvdb-<show-id>/<tab>`.
Older links without a tab automatically open the appropriate default tab. Voice assignments, narrator
settings, audition actions, plans, and jobs are scoped to that episode; the back
button returns to its show. Episode plans initially inherit the show's saved plan.

**Browse all voices & samples** and **Find matching voice** expose saved profiles
and both preset catalogs provided by the connected Voicebox version (Kokoro and
Qwen CustomVoice). Filtering and ranking use language, declared voice gender, and
listening tags. Set a character role such as **Older man**, audition a candidate,
then save the cast. Unknown ages stay unknown, and diarization creates neutral
speaker labels rather than guessing age/gender. Voice traits can be tagged after
listening. Each cast assignment saves its engine so mixed-engine casts work.

Catalog browsing is read-only. Selecting a preset registers it as a Voicebox
profile if necessary; **Generate sample** submits a short TTS request. Qwen accepts
delivery directions, while Kokoro presets require their declared language. Model
availability and the resulting age/timbre still need auditioning. Text-only voice
design is not enabled: the installed Voicebox exposes its metadata but does not
implement the full generation path. Closing the picker stops polling/playback;
a submitted preview can finish in Voicebox history.

TV show pages open on **Episodes**, grouped by season, including episodes Sonarr
knows about that are not downloaded. Select the dub language to see source audio,
completed AI outputs, active jobs, and missing dubs separately. Queue individual
files, selected episodes, or missing dubs; shared files and active jobs are skipped.
A series folder is never sent to the media pipeline. Refresh episodes to fetch new
Sonarr inventory or the latest generation status.

In **Speakers & voices**, pick a saved narrator voice and optionally a Qwen delivery
direction, then **Save narrator**. These are defaults for new jobs in that show or
movie. Explicit episode character assignments take precedence. Use **Voices** on
an episode row to rename discovered speakers, choose their roles and voices, and
set delivery direction. Use **Audition** on that episode to hear the result before
queueing its full dub. Create/clone additional profiles in Voicebox and refresh the
voice list. When no way of telling voices apart can run, every line gets one narrator
voice; the job's message says so, and the next run tries again.

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
`dub.preset_voices`).

Open the UI, go to **Library** to see your real collection, and **Queue dub** on a
needs-dub title to watch it flow through the queue. Or do it all from a terminal:
`doblarr make "<file or season folder>" --to es-419` checks the machine, analyses,
looks the title up, dubs, re-voices flagged lines and reports. Every step is also a
command of its own; see [docs/cli.md](docs/cli.md).

### Docker

Runs next to your other -arrs; reaches Radarr/Sonarr/Plex via `host.docker.internal`.

```bash
cp config.docker.example.yaml config/config.yaml   # fill in URLs + keys
docker compose up -d --build                         # http://localhost:6363
```

Publishing follows the `dev` → `main`/`master` promotion process automatically:

- Push to `dev`: lint, type checks, tests, frontend checks, and a Docker build must
  pass before publishing `ghcr.io/jhd3197/doblarr:dev` and a numbered dev image.
- Merge a same-repository `dev` PR into `main` or `master`: the same checks run on
  the merged commit, then publish `latest` and a stable version, and create its
  Git tag and GitHub release with generated notes. Direct stable-branch pushes
  do not publish; the release workflow verifies the merged promotion.
- The first stable release uses the package version (`0.1.0` initially); later
  promotions increment the latest stable tag's patch number automatically.
  Retrying a released commit reuses its version. Dev images never update `latest`.

Images support `linux/amd64` and `linux/arm64` and include a full `sha-<commit>`
tag. No manual version edits or tag pushes are needed. Publication uses the
built-in `GITHUB_TOKEN` with `contents: write` and `packages: write`; a separate
GitHub key is not required. See [the release workflow](.github/workflows/release.yml).

GHCR creates new packages as private by default. After the first publication,
the package must be **Public** in its GitHub package settings for anonymous pulls
and the pull-count badge to work. The workflow checks anonymous access and reports
an error if publication succeeded but the image cannot be read publicly.

`config/` holds `config.yaml`; `data/` holds the job store, the work folder and the
generated Kometa fragment. This image has no local models; real dubs need the GPU
image below (or those models installed some other way) and a speech service.

#### GPU

`Dockerfile.gpu` adds the local models (Demucs separation, whisperx
transcription, pyannote diarization) on a CUDA build of PyTorch. The host needs
an NVIDIA driver and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Speaker grouping runs locally without an account. Only `transcribe.diarizer: pyannote`
needs `HF_TOKEN` set, with the model's terms accepted on HuggingFace.

Then open **Settings → Hardware**. It shows the GPU the container sees, its free
memory and which libraries are installed, and lets you pick the device for each
stage. Downloaded models stay under `data/models`. voicebox still runs as its own
service. The image is large (CUDA wheels) and is built locally, not published.

## Pipeline

| # | Stage | Tool |
|---|-------|------|
| 1 | Extract audio | ffmpeg |
| 2 | Separate dialogue vs music+FX | Demucs `htdemucs_ft` |
| 3 | Timed transcript | subtitles (pysubs2) / WhisperX |
| 4 | Tell voices apart | local voice grouping, or pyannote |
| 5 | Translate (dubbing-aware, length-budgeted) | Prompture: Claude, local models, voicebox |
| 6 | Clone voices + synthesize lines | voicebox or VoiceStudio |
| 7 | Fit timing | ffmpeg `atempo`, by line or by phrase |
| 8 | Mix dialogue over M&E + ducking | ffmpeg `sidechaincompress` |
| 9 | Mux the new track | ffmpeg |

Each stage is a module under `doblarr/stages/`; `doblarr/pipeline.py` runs them in
order and resumes a stopped job from its last finished stage.

## Project layout

```
doblarr/
  cli.py            # the `doblarr` command; subcommands live in commands/
  server.py         # app assembly, lifespan, authentication, SSE and static UI
  routes/           # configuration, library, jobs and title API routers
  library_service.py # discovery cache, persisted scan state and webhook orchestration
  config.py         # YAML config + defaults, env overrides, secret redaction
  config_schema.py  # settings, defaults and validation (load warns, save refuses)
  auth.py           # X-Api-Key and Host checks for /api/* (web.api_key, web.allowed_hosts)
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
ui/                 # web UI (React + Vite), built into ui/dist
  src/router.jsx    # every route, with the loader that fetches its data first
  src/shell/        # sidebar, header, boot screen, new-dub dialog
  src/pages/        # one folder per area: library + title, episode, dubs, voices, studio
  src/components/   # pieces several pages use (media player, character picker, …)
  src/lib/          # API transport, query cache, live events, pure helpers
  src/styles/       # shared CSS (tokens, layout, one file per page)
```

## API

The main endpoints are below; every endpoint and its parameters are listed by the
running server at `/docs`.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/health` | liveness (process up) |
| GET | `/api/health/ready` | readiness (voicebox up + a source configured), 503 otherwise |
| GET | `/api/library` | scan Radarr+Sonarr, classify every title (`?refresh=true` bypasses the scan cache) |
| GET | `/api/library/item/{key}` | one title from the last scan by its URL key (`tmdb-…`, `tvdb-…`, `t-<title>-<year>`) |
| GET | `/api/series/{tvdb_id}/episodes/{episode_id}` | one episode and its show, without building the whole series |
| GET/POST | `/api/config` | read (redacted) / save config |
| GET/POST | `/api/jobs` | list / enqueue dub jobs (`force: true` ignores cached artifacts) |
| POST | `/api/jobs/clear-finished` | remove done+failed+cancelled jobs |
| DELETE | `/api/jobs/{id}` | remove one job (a *running* job is cancelled instead) |
| GET | `/api/jobs/{id}/report` | stage timings and generation counters |
| GET / POST | `/api/jobs/{id}/review` | read dialogue snapshot / queue line edits |
| GET | `/api/jobs/{id}/clips/{index}` | listen to a generated line (HTTP Range) |
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
any in-flight ffmpeg process. Cancelling a Voicebox wait also requests remote
cancellation; if the server cannot be reached, remote generation may continue.

## Development

```bash
pip install -e ".[dev]"   # app + pytest/pytest-cov/pytest-xdist/ruff/mypy/httpx
python -m pytest -q -n auto            # test suite on every core (no network or *arr services needed)
python -m pytest -q -n auto -m "not slow"   # skip the end-to-end audio renders (~1 min)
python -m pytest -q -n auto --cov=doblarr --cov-report=term-missing   # with coverage
ruff check .              # lint
mypy doblarr/             # type check
# pre-commit install      # optional: run ruff+mypy as git hooks (.pre-commit-config.yaml)
```

`.\dev.ps1 validate` watches `doblarr/`, `tests/` and `ui/` and reruns the
checks on every save: ruff (safe fixes applied), mypy and the fast pytest tier
when Python changes; eslint, the node tests and the UI build when the UI
changes. `.\dev.ps1 validate once` runs them a single time (`./dev.sh validate`
on Linux/macOS). `check` runs everything CI does, slow tests and browser specs
included.

Tests never reach the network: `tests/conftest.py` makes every real HTTP request
fail at once, so a test that forgets to fake a service fails fast instead of
waiting out a connection timeout. Tests that render real audio end to end are
marked `slow` by file (`SLOW_FILES` in the same conftest).

### Frontend checks

The UI is React, built with Vite. Use Node 22 or newer:

```bash
npm ci
npx playwright install chromium
npm run dev:ui           # live-reload UI on :5363, /api proxied to a running server on :6363
npm run build:ui         # production build into ui/dist (what `doblarr serve` serves)
npm run check            # lint + unit tests + build + browser regressions
npm test                 # fast API/helper tests only
npm run test:browser     # builds the UI, then runs the browser specs
```

Every route in `ui/src/router.jsx` has a loader that fetches what the page
needs before it renders: the first load shows one boot screen and later
navigations a progress bar, so a deep link never paints a half-filled page.
Data lives in one TanStack Query cache (`ui/src/lib/queries.js`); the SSE
stream refreshes what an event changed.

Browser tests start an isolated FastAPI server with temporary configuration and
storage on port 8766 (`PW_PORT` changes it); they serve `ui/dist` (`UI_DIR`
picks another build) and do not use your media services or local config. They
use `.venv` when available, otherwise `python`; set `PYTHON` to choose another
interpreter. Python tests share a `client_factory` fixture in `tests/conftest.py`
for isolated API clients with automatic cleanup. Tests that exercise worker
startup use an explicit application lifespan.

For backend auto-reload during development:

```bash
python -m uvicorn doblarr.server:create_app --factory --reload --port 6363
```

Settings defaults belong in `ConfigModel` in `doblarr/config_schema.py`.
Presentation metadata belongs in `ui/src/lib/settings-model.js`; title plans
reuse those field definitions. Make API calls through `api()` in
`ui/src/lib/api.js` and read shared data through the query objects, so
authentication, errors and caching stay consistent. A page keeps its own
classes in a CSS file next to it and reuses `ui/src/styles` for the rest.

CI is three workflows that run side by side: Backend CI (ruff, mypy, and pytest
in a fast and a slow tier across Ubuntu/Windows and Python 3.11/3.12), Frontend
CI (eslint, unit tests, UI build, Playwright) and Docker CI. Pull requests run
the ones whose paths they touch; every push to `dev`, `main` and `master` runs
all three through `release.yml` before anything publishes.

## Recipes, the studio and media knowledge

### Share a dub recipe

Movies and episodes have a **Recipes** tab with a persistent `/recipes` route.
Export a `.dobdub` file containing saved generation settings, pronunciation rules,
character directions and voice names. Import it on the matching local title,
review its contents, choose local voices and engines, then apply it. Queue generation
separately after checking the character assignments.

Version 1 is recipe-only JSON: no audio, video, dialogue, subtitles, cloned voice
samples, credentials, or local file paths. Translation services and model locations
remain local. Different models, source cuts and speaker detection can produce different
results. Expected runtime is optional release information, not automatic verification.
See [the recipe format and API](docs/dub-recipes.md).

### Episode studio

Open **Studio** on a run (or **Open studio** on an episode) to work on one episode in
one place: listen to the original, the dub, saved versions and other dubs at the same
moment, mark problems where you hear them, edit and re-render single lines, audition a
character's voice, and export the chosen version without generating speech by surprise.
It also runs controlled writing experiments — does an English dub reference improve the
Spanish? — with the official Spanish adaptation held out of every request and revealed
only after the candidates are frozen. See [the studio guide](docs/studio.md).

### Media knowledge and voice envelopes

An episode's **Analysis** tab ties everything it learns to the file's content rather than
its name: characters of the series, why each line got its voice, which dub tracks were
safe to listen to, optional picture evidence (faces, mouth movement) and title knowledge
proposals you review before anything uses them. See [media knowledge](docs/media-knowledge.md).

Character voice profiles live under **Voices → Characters**. Voice envelopes (a line's
emphasis inside itself) and background policies are data templates under
**Knowledge → Audio templates**, recommended per line and chosen or changed in review;
changing one reprocesses existing takes and never generates speech. See
[voice envelopes and background policies](docs/adaptive-audio.md).

A **voice study** learns each character's voice from one film and tests it, held out, on a
second film (`doblarr voice-study run`, `/api/voice-studies`). See [voice study](docs/voice-study.md).

## License

MIT — see [LICENSE](LICENSE). voicebox is MIT; neutrinus/dubarr is GPL-3.0 (used
only as a reference to re-implement from, never copied in).

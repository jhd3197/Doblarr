# Doblarr improvement review

A read-only review of the `dev` branch (49 commits ahead of `master`, where it was
first written). Sections 1-3 were re-checked against the code on `dev`: the sidebar,
`window.alert`, README and table-wrapper findings from the first pass were already
fixed there and are marked **fixed on dev**. Sections 4-5 (the voice-study design
and roadmap) were not re-checked line by line against `dev`; treat their line
references as approximate. Nothing was run for the review itself (no tests, no
model downloads). Where something comes from general knowledge rather than this
repo, it says so.

Per repo convention this file uses generic wording ("film 1 / film 2 of a series")
and no real titles.

---

## 1. What the project is, and how it is laid out

**Doblarr** turns a foreign-language video in a Radarr/Sonarr library into an
added, translated audio track voiced by cloned speaker voices, muxed back in as an
extra track (`README.md:28-33`). It sits next to Bazarr in an *arr stack. MIT
licensed, Python >= 3.11 (`pyproject.toml:11`), React 19 + Vite 8 UI
(`package.json`).

### Backend (`doblarr/`, about 40k lines in the top-level, `stages/`, `studio/` and `clients/` modules)

| Area | Where | Notes |
|---|---|---|
| App assembly | `server.py`, `routes/*.py`, `auth.py`, `events.py` | FastAPI; optional `X-Api-Key`; SSE event bus for live UI |
| Library | `discovery.py`, `library_service.py`, `clients/{radarr,sonarr,plex}.py`, `webhooks.py`, `scheduler.py` | needs-dub / partial / available classification |
| Jobs and storage | `jobs.py`, `store.py`, `background.py` | SQLite (WAL, ordered `MIGRATIONS`); tables include `jobs`, `voice_casts`, `title_plans`, `knowledge_*`, `studio_records` |
| Pipeline | `pipeline.py` (1400 lines), `stages/` | extract, separate (Demucs), transcribe (subtitles / WhisperX), diarize, translate, synthesize, fit_timing, mix, mux, plus `quality`, `audition`, `treatment` |
| Speech engines | `clients/voicebox.py`, `clients/voicestudio.py`, `clients/speech.py` | voice cloning and TTS over HTTP, not vendored |
| Translation | `clients/translator.py`, `llm.py` | Prompture, structured output, validated segment mapping |
| Identity and knowledge | `identity.py`, `knowledge/`, `published_cast.py`, `catalogues/`, `research/` | series, media revision, character records; title knowledge; AniList/ANN/Jikan/Bangumi/Wikidata/TMDB |
| Analysis | `analysis.py`, `cues.py` (2119 lines), `levels.py`, `speaker_memory.py`, `vision/` | per-episode breakdown without dubbing |
| Studio | `studio/*`, `routes/studio.py` | listening and comparison workspace, held-out evaluation experiments (`docs/studio.md`) |
| CLI | `cli.py`, `commands/*.py` | `doblarr make` chains doctor, analyze, lookup, voices, dub, fix, report (`docs/cli.md`) |
| Config | `config.py`, `config_schema.py` (pydantic, warnings not fatal) | defaults live in `ConfigModel` |

### Frontend (`ui/`)

- `ui/src/router.jsx`: one route table; every route has a loader, a boot screen covers first load, a progress bar later ones.
- `ui/src/shell/`: `Shell`, `Sidebar`, `BootScreen`, `NewDubModal`.
- `ui/src/pages/`: `Overview`, `Settings`, and folders `library/`, `dubs/`, `voices/`, `studio/`, `episode/`. About 8.9k lines of JSX in pages and components.
- `ui/src/lib/`: `api.js`, `queries.js` (TanStack Query), `events.js` (SSE), `notice.js` (app-wide error line), pure helpers with node unit tests in `tests/frontend/*.test.js`.
- Tests: node unit tests plus Playwright specs (`tests/frontend/*.spec.js`) against an isolated server.

### Pipeline, in the README's own words
Extract, separate, timed transcript, diarize, translate, clone and synthesize, fit timing, mix with ducking, mux (`README.md`, "Pipeline"). A new install defaults to `dub.dry_run: true`.

### Deploy
`Dockerfile` (app only, no ML stack), `Dockerfile.gpu` (CUDA PyTorch + Demucs/whisperx/pyannote), `docker-compose.yml` and `docker-compose.gpu.yml`. CI is three workflows (backend, frontend, docker) gated through `release.yml`, with `dev` to `main`/`master` promotion publishing GHCR images. `dev.ps1` / `dev.sh` wrap the local checks.

### Documentation drift
The first pass found five README problems on `master`. On `dev` all five claims are **fixed**: the dead `docs/generation-roadmap.md` link, the stale "serve / dub / check" CLI line, the "diarization lands later" line, the unchecked Voices roadmap item, and the License section in the middle of the document (it is now last). One minor item remains: `README.md:51-52` sends interrupted-episode recovery to `scripts/finish_episode.py`, a thin wrapper over `doblarr/recovery.py` (left unfixed; a `doblarr` subcommand would be a CLI change).

---

## 2. UI/UX consistency issues

Priority is for a showcase demo: P0 breaks trust or looks broken in the first minute, P1 is visible polish a reviewer will notice, P2 is system hygiene.

### P0

1. **The sidebar fakes its status and version.** **Fixed on dev.** `Sidebar.jsx:33-36` now reads `statusQuery` and shows "server unreachable", "worker stopped" or "worker online · <device>"; the version comes from `status.version`.
2. **Failure is not visually distinct from anything else.** *Still true on dev.* There are no success / warning / danger tokens (`ui/src/styles/base/tokens.css` defines accent and neutrals only). A failed, cancelled or planned job all get `tag-outline`, the accent outline (`ui/src/lib/jobs.js:12-13`); analysis "failed" and "stale" share one style (`ui/src/styles/pages/analysis.css:58`). The accent coral is also the primary button and link colour, so an error looks like a call to action.
3. **No page title or heading on the busiest list pages.** *Still true on dev.* Overview has an `<h1>` (`Overview.jsx:332`), Watch an `<h1>` (`Watch.jsx:215`), Voices an `<h2>` plus a one-line description (`VoicesList.jsx:90-91`); Library, Dubs and Settings start straight with a toolbar or tabs. Heading levels skip.
4. **Dubs empty state lies when searching.** *Still true on dev.* `Dubs.jsx:77` filters by the header search, then `Dubs.jsx:93` shows "No jobs yet — queue one from the Library." for any empty result. Library says "No titles match." correctly.
5. **Native browser dialogs.** **Mostly fixed on dev.** Failures from queueing, queue pause/resume/clear, Watch, studio and template flows go through `notify()` (`lib/notice.js`, rendered by `Shell.jsx` as `role="alert"`). Five native dialogs remain, all needing text input or a destructive confirmation: the API-key prompt (`lib/api.js:40`), `StudioExperiments.jsx:45` (prompt) and `:101` (confirm to reveal the official dub), `StudioOverview.jsx:162` (prompt) and `voices/Templates.jsx:134` (prompt). Replacing them needs a small input-dialog component.

### P1

6. **Empty and loading states use several different treatments.** *Still true on dev.* `.muted-note` (`shell/shell.css:40`) and `.dubs-empty` (`dubs/dubs.css:15`) are the same rule; `.ov-empty` (`overview.css:7`) and `.title-muted` (`library/title.css:31`) are near-copies at 13px; about 40 call sites use the four names. No skeleton or spinner exists.
7. **Design tokens cover colour only.** *Still true on dev.* 36 distinct `font-size` values across the CSS; radius tokens exist (`tokens.css:23`) but are used 9 times against many literal radii; `.btn` and `.input` hard-code 10px (`elements.css`) while `.panel` and `.card` hard-code 14px. No spacing scale.
8. **Button heights differ by variant.** *Still true on dev.* `.btn` is `padding: 9px 15px; line-height: 1`; `.btn-ghost` overrides to `7px 11px`, so mixed toolbars (`Library.jsx`, `Dubs.jsx`) misalign.
9. **Two stylesheet trees with the same names.** *Still true on dev.* `ui/src/styles/pages/*.css` coexist with page-local files (`pages/library/library.css`, `title.css`, `voices/voices.css`, `overview.css`, `settings.css`). Left unfixed (large reorganisation).
10. **Wide tables not all in `overflow-x: auto` wrappers.** **Fixed on dev.** `.table-wrap` (`styles/base/layout.css:51`) exists and every `<table className="table">` in `ui/src` sits inside it. Research tables use their own `.research-table-wrap`.
11. **Unknown URLs silently redirect to Overview** (`router.jsx:32`). *Still true on dev.*
12. **No system theme and the toggle sits in the top bar.** *Still true on dev.* Theme defaults to light (`Shell.jsx:13`); no `prefers-color-scheme` anywhere in `ui/src`.
13. **Dark-mode colour exceptions are hand-written.** *Still true on dev* for `.tag-accent` (literal `rgba(255,106,80,0.16)` / `#ffb4a3` in `elements.css`). `.btn-primary` already uses `--color-on-accent` tokens (improved on dev).

### P2

14. **Inconsistent naming** (ids and classes mixed, per-page prefixes, generic `.m`, `.opt`, `.dot9`). *Still true;* many ids are Playwright hooks. Left unfixed.
15. **Sidebar "Star on GitHub" nudge** (`Sidebar.jsx:55-63`) is shown only while due and is snoozable with "Not now"; there is also a small star link by the brand. Acceptable; left alone.
16. **Breakpoints are scattered:** 560, 650, 720, 760, 860, 900, 980, 1000px. *Still true;* left unfixed.
17. **Accessibility is mostly good.** Remaining gap: clickable voice rows (`VoicesList.jsx:117`) are mouse-only `onClick` rows; the inner `<Link>` is the keyboard path.
18. **Large page components:** `ScenePanel.jsx` (556 lines), `CharacterProfile.jsx` (536), `Breakdown.jsx` (472), `StudioOverview.jsx` (463). *Still true;* left unfixed.

---

## 3. Quick polish wins

Status is relative to `dev` plus the `doblarr-polish` branch.

| # | Win | Where | Status |
|---|---|---|---|
| 1 | Real worker state and version in the sidebar | `Sidebar.jsx` | Fixed on dev |
| 2 | `--ok`, `--warn`, `--danger` tokens; map job/analysis states | `tokens.css`, `lib/jobs.js`, `analysis.css` | Fixed on doblarr-polish |
| 3 | Dubs empty state: "no jobs" vs "no match" | `Dubs.jsx` | Fixed on doblarr-polish |
| 4 | One `PageHeader` (title + hint, one `h1`) | Library, Dubs, Settings, Voices, Knowledge (`components/PageHeader.jsx`) | Fixed on doblarr-polish |
| 5 | Replace native dialogs with toast / inline errors | see P0-5 | Alerts fixed on dev; five input/confirm dialogs left |
| 6 | Merge duplicate empty / loading classes | four CSS files | Partly: `.empty` and `.note` replace the four classes; the many bare "Loading…" paragraphs are not unified |
| 7 | Radius tokens and a small type scale on `.btn`, `.input`, `.panel`, `.card` | `elements.css`, `layout.css`, `tokens.css` | Partly: tokens used in `.btn`, `.input`, `.panel`, `.card`, `.table`, `.tag`; the other ~100 literal radii and sizes remain |
| 8 | Equalise `.btn-ghost` height | `elements.css` | Fixed on doblarr-polish |
| 9 | Wrap wide tables | all `.table` users | Fixed on dev |
| 10 | Real 404 route | `router.jsx` | Fixed on doblarr-polish |
| 11 | Follow the OS theme on first visit | `Shell.jsx` | Fixed on doblarr-polish |
| 12 | README repair | `README.md` | Fixed on dev |
| 13 | Keyboard-complete clickable rows | `Knowledge.jsx` rows | Fixed on doblarr-polish; `VoicesList.jsx` rows keep their inner link as the keyboard path |
| 14 | Collapse the nav star nudge | `Sidebar.jsx` | left alone (already snoozable) |

---

## 4. Feature: "voice study"

**Goal.** Take one film (film 1 of a series), find each distinct voice, tie each to a character (and where possible to a credited actor), build a voice profile per character, then run those profiles against film 2 and report how well voices are recognised: similarity scores, a confusion matrix, and which characters drift or collide.

### 4.1 What already exists (this is most of the pipeline)

The repo has more of this than the feature name suggests. Verified by reading the code:

| Need | Already in the repo | Reference |
|---|---|---|
| Speaker embeddings | WeSpeaker ResNet34 (via pyannote, ungated), 3D-Speaker CAM++ / ERes2NetV2, NeMo TitaNet L/S, CN-Celeb ResNet34, user-registered sherpa-onnx models, several joined | `doblarr/voice_models.py:65-84`, `:87-102`; default pair at `:39`; embedding at `speakers.py:148-182` |
| Segmenting by subtitle cue, not by VAD | Lines come from subtitles or ASR; embeddings are taken per line on separated dialogue | `speakers.py:1-26`, `:322-327` |
| Clustering into voice groups | Agglomerative, cosine, average linkage, tuned thresholds, small-group merging, per-line decision record (`clustered`, `merged_small`, `nearest`, `inherited`) | `speakers.py:197-297` |
| Full diarization alternative | pyannote 3.1 pipeline (needs HF token) with word-level speaker splits | `stages/diarize.py:29-124` |
| Per-line embeddings persisted | `<stem>.speakers.json` sidecar | `speakers.py:351-367` |
| Using dub tracks as extra evidence | Aligned other audio tracks joined into the vectors | `speakers.py:76-145`, `doblarr/track_alignment.py` |
| Manual correction | Move lines, regroup, carry names across a regroup | `speakers.py:370-449`, `speaker_memory.py`, `voice_tags.py:374-389` |
| Names from dialogue | Vocatives/addressing clues | `doblarr/dialogue_clues.py`, used at `routes/analysis.py:527-530` |
| Characters as first-class records | Series-scoped characters with aliases, variants, merges, references | `identity.py:461-560`, `routes/characters.py` |
| Published cast and original voice actors | AniList / ANN / Jikan / Bangumi links and import | `doblarr/published_cast.py`, `docs/media-knowledge.md:28-44` |
| Per-character voice profile that persists across episodes | Duration-weighted prototypes (1-3 per character), stored per series, voice model and language | `voice_tags.py:201-272` (`prototypes`, `remember_series`); `PRINTS_VERSION = 2` |
| Recognition of unnamed groups in a new episode | Cosine to each character with a floor (0.3, `voice_tags.py:24`) and a margin to the runner-up | `voice_tags.py:327-371`, called from `routes/analysis.py:518` |
| Held-out evaluation episodes | An episode can be declared evaluation-only and is excluded from memory | `doblarr/feedback.py:136-150`, `speaker_memory.py:50-54`, `routes/analysis.py:520` |
| Evaluation metrics | B-cubed P/R/F1, incorrect merges, missed brief characters, naming precision, leave-one-out or held-out recognition with abstention | `doblarr/speaker_eval.py:27-157` |
| Voice similarity to a reference | Cosine of a generated take against its reference clip | `doblarr/voice_check.py` |
| Voice cloning / TTS / audition | voicebox and VoiceStudio clients, character auditions | `clients/voicebox.py:99-184`, `studio/auditions.py` |
| Hardware selection | Per-stage device choice, GPU probe | `doblarr/hardware.py`, `README.md:291` |
| UI | Voices > Characters profile pages; Analysis tab with suggestions and margins | `ui/src/pages/voices/CharacterProfile.jsx`, `ui/src/pages/episode/*` |

### 4.2 Gaps (verified absent)

1. **No cross-film evaluation.** `speaker_eval.recognition()` supports `held_vectors` / `held_truth` (`speaker_eval.py:89-131`), but a search of the package finds no caller outside `tests/test_speaker_eval.py`. Nothing runs "profiles from film 1 against film 2" end to end.
2. **No confusion matrix.** `recognition()` returns only counts (`top1`, `answered`, `precision`, `coverage`, `speaker_eval.py:147-150`). Nothing in `doblarr/` or `ui/src` mentions a confusion matrix.
3. **Film 1 and film 2 are different "series" today.** `identity.series_id_for` gives `movie:tmdb:<id>` per film (`identity.py:234-237`), and voice memory is keyed by series id (`voice_tags.py:184-185`). Radarr/TMDB collection ids are not read (no `collection` handling in `identity.py`, `catalogues/tmdb.py` or `clients/radarr.py`). The nearest mechanism is `identity.link_series` (`identity.py:299-333`), which merges characters by name into a target, but it is designed for "local id to provider id", with a per-pair confirmation, and nothing builds a franchise target.
4. **Actors are not modelled.** Characters exist; the person who voices them (and so the same actor across two languages or two films) is not a record. `published_cast.py` carries voice-actor leads for anime but not for live action: `catalogues/tmdb.py:3` states it is "Not a cast source".
5. **Cross-language is partly handled but not measured.** Memory is partitioned by model and language (`voice_tags.py:188-189`), so a film 2 in another language gets an empty partition; `voice_check.py:5-9` notes cross-language similarity is lower. There is no cross-language calibration.
6. **Prototypes are off by default** because they scored lower on the one measured episode (`voice_tags.py:334-337`); the study would produce the larger evaluation that note asks for.
7. **No overlap handling for embeddings.** Cross-talk is flagged for levels (`speaker_memory.py:35`), but overlapped lines are still embedded as if single-speaker.
8. **No privacy/retention controls on embeddings**: the sidecar stores 192+-dim vectors per line beside the media (`speakers.py:363`).

### 4.3 Concrete design

**Principle: reuse `speakers.py`, `voice_tags.py` and `speaker_eval.py` as the engine; add a thin "study" layer that orchestrates and reports. Do not fork the embedding or clustering code.**

#### Stages

1. **Pick the pair.** User picks film 1 (profile source) and film 2 (probe). A study record links them; a franchise id can optionally be proposed from the TMDB collection (needs a new catalogue call).
2. **Analyse film 1** with the existing analysis (`routes/analysis.py` `/api/analysis`, `analysis.py`): separate dialogue, cut lines (subtitles preferred), embed, cluster. Output: groups and `<stem>.speakers.json`.
3. **Map groups to characters.** In order of trust:
   - the user's names (existing `PUT /api/analysis/names`, `routes/analysis.py:577`),
   - dialogue clues (`dialogue_clues.read`),
   - subtitle roles/styles (`subtitle_roles.py`; speaker tags in some subtitle formats),
   - published or catalogue cast (`published_cast.py`),
   - picture evidence if the optional `vision` extra is on (`doblarr/vision/association.py`).
   Everything stays a *proposal* until confirmed, as with title knowledge today. Output: `labels` (group to character id), as already passed to `voice_tags.remember_series`.
4. **Build profiles.** `remember_series` already stores up to three duration-weighted prototypes per character (`voice_tags.py:201-272`). The study adds, per character: lines and seconds used, within-character spread (mean pairwise cosine), an outlier list, and a quality grade (see data model). Overlap and very short lines are excluded.
5. **Probe film 2.** Run the same analysis on film 2, but with its *own* grouping kept separate from film 1's memory. Two probe modes:
   - **Blind**: cluster film 2, then match each group to film 1 characters (this is what `suggest_series` does today).
   - **Line-level**: match each line to film 1 characters (what `recognition(held_vectors=…)` does), which is what a confusion matrix needs.
6. **Score.** Ground truth for film 2 is the user's own naming (partial is fine), because the held-out principle matters: film 2 must be marked evaluation-only (`feedback.hold_out`) *before* matching so it can never teach the profiles it is tested on (`speaker_memory.held_out_revisions`).
   Report: top-1 accuracy, answered/abstained, precision at the chosen floor and margin, per-character precision/recall, a **confusion matrix** (rows = true character, columns = predicted, plus an "abstained" column), the score distribution for same-character vs different-character pairs, and the closest-confusable pairs.
7. **Review.** A person confirms or corrects, which feeds back as ground truth and (only if they opt in) as new profile material from film 2.

#### Libraries

Prefer what is already a dependency:

- Embeddings, clustering: `pyannote.audio` (WeSpeaker), `sherpa-onnx`, `scikit-learn`, `numpy` (`scikit-learn` is a core dependency, `pyproject.toml:22`; `pyannote.audio` and `sherpa-onnx` are in the `real` extra, `:32-33`; `numpy` comes in transitively).
- New and optional: `matplotlib` or plain JSON + a UI heatmap (prefer the latter; the UI is React, no chart library is installed); `umap-learn` or `scikit-learn` TSNE/PCA for a 2-D voice map (PCA is already available through scikit-learn and is enough for a first version).
- Optional quality upgrades, not needed at first: pyannote's segmentation model for overlap detection; a speech-quality gate (SNR from the separated stem; `levels.py` already measures per-line level).
- Calibration: score normalisation (AS-norm / z-norm) against an impostor cohort built from the other characters; small, local, and no new dependency beyond `numpy`.

#### Data model

Use the existing SQLite `studio_records` / `records` store (`store.py:190`, `doblarr/studio/records.py`) rather than a new table; it already handles revisions and scope.

| Record | Key fields |
|---|---|
| `voice_study` | `id`, `name`, `source_revision` (film 1), `probe_revisions` [film 2...], `model` (combined id, `voice_models.combined_id`), `language_pair`, `thresholds` {floor, margin}, `created_at`, `status` |
| `voice_profile` | `study_id`, `character_id`, `actor` (optional free text or catalogue ref), `protos` (as in `voice_tags.prototypes`), `lines`, `seconds`, `spread`, `grade`, `excluded_lines` |
| `voice_probe_result` | `study_id`, `probe_revision`, `per_line` [{cue, true, pred, score, margin, abstained}], `matrix` {labels, counts}, `summary` {top1, precision, coverage, per_character} |

Embeddings stay in the existing sidecar; the study stores references (cue ids), not copies of vectors, except the prototype sums.

#### Data flow

```
film 1 file -> analysis (separate, cut, embed, cluster) -> groups + sidecar
   -> naming (user / dialogue clues / cast / vision) -> labels
   -> remember_series(...) -> profiles (+ spread, grade)
film 2 file -> analysis -> sidecar2 (hold_out first)
   -> match lines to profiles (floor + margin, abstain) -> per_line
   -> speaker_eval (new confusion_matrix + per-character P/R) -> study report
   -> UI review -> corrections -> optional promotion to profile material
```

#### API (new routes file, same style as `routes/characters.py`)

```
POST   /api/voice-studies                       {name, source, probes[], models?, floor?, margin?}
GET    /api/voice-studies                       list
GET    /api/voice-studies/{id}                  study + profiles + latest results
POST   /api/voice-studies/{id}/profiles/build   (re)build from confirmed names
POST   /api/voice-studies/{id}/probe            {revision}  -> queues a job (kind: "voice_probe")
GET    /api/voice-studies/{id}/matrix?probe=…   confusion matrix + per-character table
PUT    /api/voice-studies/{id}/lines            corrections to ground truth
DELETE /api/voice-studies/{id}                  removes profiles and results (privacy)
```

Heavy work (embedding a film) runs as a job so it appears on the Dubs page and in SSE like any other, using the existing queue (`jobs.py`) and `analysis` job path.

#### UI screens (under Voices, using existing pages as the pattern)

1. **Studies list**: pair of films, model, status, headline numbers (top-1, coverage).
2. **Profile view** (extends `CharacterProfile.jsx`): per character, lines used, spread, grade, outliers with play buttons (reuse `MediaPlayer`), the audio clips that define the profile, and a "this voice is confusable with X" warning.
3. **Probe results**: confusion-matrix heatmap with a table fallback (rows = true, columns = predicted + abstained), click a cell to list and play the lines; per-character precision/recall bars; score histograms of same vs different character.
4. **Review lines**: low-margin and wrong lines first, with the existing line-move interaction.
5. **Settings/help**: which models, thresholds, caveats ("this measures this pair of films, not general accuracy" is already the repo's own wording in `speaker_eval.py:18-19`).

Because of P0/P1 items in section 2, add the status colours, `EmptyState`, `PageHeader` and table wrapper first; a confusion matrix needs a defined colour scale (use the sequential palette approach, not the accent) and a non-colour cue for accessibility.

### 4.4 Phased plan

| Phase | Deliverable | Effort | Risk |
|---|---|---|---|
| 0. Groundwork | `confusion_matrix` and per-character P/R in `speaker_eval.py`; unit tests with synthetic vectors; CLI `doblarr voice-study run FILM1 FILM2 --json` that wires existing pieces (names from a JSON file) | 3-4 days | Low. Pure functions on existing data. |
| 1. Study records + API | Study / profile / probe records, `voice_probe` job, held-out enforcement, endpoints above | 1 week | Medium. Revision semantics in `records`; make sure film 2 never teaches film 1's profiles. |
| 2. UI v1 | Studies list, results page with matrix and per-character table, line playback | 1-1.5 weeks | Medium. Needs shared components from section 2 first. |
| 3. Cross-film identity | Optional franchise grouping (TMDB collection id) feeding `link_series`-like merge; actor record; actor-vs-character view | 1 week | Medium. Needs a new catalogue call and a policy for conflicting character names across films. |
| 4. Quality | Overlap-aware embeddings, impostor-cohort score normalisation, evaluation of prototypes on/off, model comparison table (extends `voice_models` `scores`) | 1-2 weeks | Medium. Gains need a hand-labelled pair; the repo's existing score evidence comes from a single episode (`voice_models.py:37-38`). |
| 5. Cross-language | Compare original-language film 1 with a dubbed film 2 using aligned dub tracks; calibrate floors per language pair | 1-2 weeks | High. `voice_check.py:5-9` already documents lower scores across languages. |

Roughly 5-7 engineer-weeks for phases 0-3; phases 4-5 are research-flavoured.

### 4.5 Risks and constraints

- **GPU / compute.** Embedding is light. WeSpeaker and sherpa-onnx models run on CPU (`speakers.embed(device="cpu")` default, `voice_models.py:202`); the heavy parts are dialogue separation (Demucs) and ASR (WhisperX), which the repo already schedules per stage via `hardware.py`. A feature-length film is the same cost as an analysis run today. pyannote full diarization needs a GPU for good speed and an HF token (`diarize.py:3-10`). Not measured by this review.
- **Model licensing** (from general knowledge, not verified in this repo, which does not record licences; check each model card before shipping): WeSpeaker/pyannote weights and the VoxCeleb training data carry non-trivial terms (research-oriented dataset licence); 3D-Speaker is published by Alibaba under a permissive licence; NVIDIA NeMo TitaNet has its own model licence; pyannote diarization 3.1 is gated behind accepting terms. A study that only compares voices does not ship weights, but a hosted product would. Recommended: add a `license` and `source_url` field to `VoiceModel` (`voice_models.py:43-52`) and show it in the model picker.
- **Privacy.** A voice embedding is biometric data about a real person. Keep it local (already true: everything stays on the machine, `docs/media-knowledge.md:3`), do not export it in `.dobdub` recipes (the recipe format already excludes voice samples, `README.md:487-490`), add a "delete study" action that removes profiles, results and the sidecars derived from them, and keep any identification of a *real actor* to published credits the user chose to link, never inferred from the voice.
- **Copyright.** Studies read copyrighted audio the user already owns. The output must contain statistics, cue ids and short user-initiated clips only; do not store or export full separated stems or transcripts in study exports. This matches the recipe policy (no audio, subtitles or dialogue).
- **Accuracy claims.** Treat results as a measurement of this pair of films. Different mixes, different actors for the same character (recasting, child to adult, aged voices, dubs), shouting vs calm, and music bleed after separation all lower scores; the repo already documents the shouting/calm split (`speakers.py:11-15`). Show the abstained share next to precision, as `speaker_eval.recognition` already does.
- **Name collisions.** Two films may name characters differently or reuse names; never merge across films automatically (same policy as `identity.link_series` requiring confirmation).
- **Scope creep.** Do not mix this with the dub pipeline's job path. A study must never trigger translation, cloning or speech generation (as analysis already promises, `docs/media-knowledge.md:3`).

---

## 5. Roadmap

### Now (this sprint; demo-readiness)
- Real worker status and version in the sidebar (3 / quick win 1).
- Status colour tokens and mapped job/analysis states (quick win 2).
- Page headers for Library, Dubs, Settings, Voices, Knowledge; fix the Dubs empty state (wins 3-4).
- Replace native alerts with toasts / inline errors in the queue and library flows (win 5).
- README repair: dead link, License placement, stale roadmap lines (win 12).
- Wrap the wide tables, per `CLAUDE.md` (win 9).

### Next (1-2 sprints; system consistency and the study's foundation)
- Shared `EmptyState`, `Loading`, `Toast`, `PageHeader`, `TableWrap` components, then migrate pages.
- Type, radius and spacing scales in `tokens.css`; collapse the two CSS trees under one convention.
- Follow the OS theme; a 404 route; consistent breakpoints.
- Voice-study phases 0-1: confusion matrix, per-character metrics, CLI runner, study records and API with held-out enforcement.
- A hand-labelled film pair under `work/` (git-ignored, real names allowed there per `CLAUDE.md`) for evaluation; commit only invented-name fixtures.

### Later (quarter; depth)
- Voice-study UI (phase 2) and cross-film/franchise identity plus actor records (phase 3).
- Overlap-aware embeddings, score normalisation, prototype evaluation, model comparison with licence column (phase 4).
- Cross-language study against dub tracks (phase 5).
- Break up the 450-540 line page components while migrating them to the shared set.
- Consider surfacing the existing `voice_check` similarity inside the study (does a generated take sound like the profile it was cloned from), closing the loop between analysis and dubbing.

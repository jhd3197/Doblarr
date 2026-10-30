# Doblarr gap analysis

What Doblarr lacks to be impressive and competitive, ranked. Written from the code on
`dev`, the open issues, the existing improvement review and a look at comparable
projects. Competitor facts come from their public READMEs and from third-party
roundups, not from running them. Doblarr facts come from this repository.

## Where Doblarr stands

Doblarr is the only project found that is built around the *arr library workflow
(Radarr/Sonarr/Plex scan, needs-dub / partial / available, queue, "AI - ES" track
muxed back in). Its depth is real: dialogue separation, subtitle-first transcription,
speaker grouping, dubbing-aware translation, timing fit, ducked mix, quality report,
review editor, episode studio, held-out voice evaluation, dub recipes.

| Project | What it does well | What it lacks that Doblarr has |
|---|---|---|
| pyVideoTrans (GPL-3.0, desktop + CLI + web) | many ASR/TTS/LLM backends, per-speaker voices, manual proofreading between stages, F5-TTS cloning | no *arr integration, no library view, no job queue or webhooks |
| Open Dubbing (Apache-2.0, CLI) | simple single command pipeline | no UI, no library, no voice casting or review |
| Linly-Dubbing (Apache-2.0) | built-in lip-sync option | older stack, one video at a time |
| VideoLingo (Apache-2.0) | translate-reflect-adapt subtitle quality | no per-speaker voices |
| Plex AI Dubber (Dubber_LXC), dubarr | Plex label trigger, ducking, multi-track | Proxmox-only or hobby scope, little UI |
| ElevenLabs Dubbing Studio (commercial) | polished editor, 90+ languages, lip-sync, zero setup | cloud, per-minute cost, not self-hosted |

So the gap is not engine depth. It is first impression, reach, onboarding and a few
visible features the competitors advertise.

## Prioritised gaps

Effort: S under a day, M a few days, L one to two weeks, XL more. Impact is for
"impressive and competitive".

### P0 - first impression and adoption

| # | Gap | Evidence | Effort | Status |
|---|---|---|---|---|
| 1 | No screenshots, demo GIF or hero section in the README; `ui/public` has only icons | README has no images; the repo has 2 stars | S | open |
| 2 | UI still has no shared empty/loading/dialog set; five native `prompt`/`confirm` dialogs remain | `docs/IMPROVEMENT_REVIEW.md` | M | branch `feat/ui-shared-states-dialogs` |
| 3 | Voice study: the "learn film 1, test on film 2" idea has most of its parts but no confusion matrix, no runner, no records | `docs/IMPROVEMENT_REVIEW.md` section 4 | L | branch `feat/voice-study-core` (phases 0-1) |
| 4 | First-run is a config file and a dry-run flag; no in-app checklist of what is missing (ffmpeg, speech service, models, Radarr/Sonarr) | `doctor` exists as a CLI command only | M | open |
| 5 | Project reads as "in development" with a default of no audio; no hosted demo dub or sample clip | README Status | S | open |

### P1 - features competitors advertise

| # | Gap | Evidence | Effort |
|---|---|---|---|
| 6 | Jellyfin and Emby are not integrated (only a README mention); `clients/` has Plex, Radarr, Sonarr | `doblarr/clients/` | L |
| 7 | One notification channel (Discord webhook); no ntfy, Telegram, Apprise or per-event routing | `config.py` `notify.discord_webhook` | S-M |
| 8 | No Plex-label or "dub this" trigger flow documented as a feature (Dubber_LXC advertises it); auto-label exists but is opt-in and little shown | `scheduler.py`, `plex_library.py` | S |
| 9 | Translated script cannot be exported as SRT/ASS alongside the dub, a cheap bonus that pyVideoTrans and VideoLingo have | no subtitle writer for translated lines found | S-M |
| 10 | One API key for everyone; no read-only or per-user keys, so a shared household box cannot lock down Settings | `auth.py` | M |
| 11 | No Prometheus/health metrics endpoint for the stack that already runs Grafana beside the *arrs | no metrics export found | S |
| 12 | Voice engines are external services (voicebox, VoiceStudio); a single compose file that brings one up with Doblarr would remove the biggest setup hurdle | README "What you need" | M |

### P2 - depth and polish

| # | Gap | Evidence | Effort |
|---|---|---|---|
| 13 | Shared type, radius, spacing scales applied everywhere; two CSS trees; scattered breakpoints | review section 2 | M-L |
| 14 | Large files (`cues.py`, `pipeline.py`, `routes/*.py`) and closure-heavy routers are hard to unit test | ponytail audit | L |
| 15 | Broad `except Exception` handlers (about 80) that can hide a failed write or mux | ponytail audit | M |
| 16 | Docker image installs from a hand-synced copy of the dependencies | ponytail audit | S |
| 17 | Lip-sync is absent (Linly-Dubbing has it; most tools do not); an optional post-step is a "later" item | competitor table | XL |
| 18 | Translation review step between stages is present as a review editor, but there is no "pause after translation for approval" job mode like pyVideoTrans | `jobs.py` | M |
| 19 | Accessibility: voices list rows are mouse-only; focus handling in modals is not shared | review P2 | S |
| 20 | Dependabot PRs (#17-#28) are open and unreviewed; CI gating for Python 3.14 and Node 25 images is unproven | open PRs | S |

## What is being built first

1. `feat/ui-shared-states-dialogs` - gap 2: `EmptyState`, `Loading`, a promise-based
   `ConfirmDialog`/`PromptDialog`, and the remaining native dialogs removed.
2. `feat/voice-study-core` - gap 3, phases 0-1: confusion matrix and per-character
   metrics, a `doblarr voice-study` CLI runner, study records and API.

## Suggested next

README screenshots and a demo clip (gap 1) are the cheapest high-impact change and
need a running instance with real data, so they are left for the maintainer.
After that, onboarding checklist (4), Jellyfin (6) and notifications (7).

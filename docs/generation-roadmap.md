# Generation roadmap

Implementation order and acceptance gates:

1. Run reports: record stage durations, failure state, cache reuse, and counts.
2. Reliability: per-speaker synthesis, configuration-aware artifacts, resumable requests.
3. Dialogue: contextual batches, speech cue cleanup, bounded duration repair.
4. Throughput: explicit engines/presets, bounded requests, adaptive polling, model controls.
5. Sound: configurable mix, clip checks, pronunciation and selective repair.
6. Review: representative auditions, line editing/requeue, reusable character assignments.

All six implementation phases are present. Real-source throughput and listening
acceptance are still required; automated inference fixtures cannot establish those.

## Controls and workflow

1. Start with `dub.preset: custom`, `voicebox.concurrency: 1`, and a short audition.
   Use `doblarr dub film.mkv --from ja --to es --kind audition` with
   `dub.dry_run: false`, or select Audition voices on a library title.
2. Pick an engine installed in Voicebox. Preview switches to `preview_engine`
   (default `kokoro`), preset voices, `htdemucs`, and zero fit-repair attempts.
   It requires usable `dub.preset_voices` profile IDs. Final enables duration fitting;
   it does not automatically choose a larger model or raise concurrency.
3. Set `translate.glossary` for consistent names and terminology.
   `dub.pronunciations` changes only the text sent to speech generation.
4. Review flagged lines after the job stops. Text, timing, voice, delivery, exclusion,
   and new-take edits create a new queued job. Unchanged speech is reused when its
   fingerprint matches. Mixing/export still runs when its dependencies change.
5. To reuse recurring characters across episodes, set the same `dub.cast_group` and
   map each episode's detected speaker IDs to character names in `dub.character_map`.
   Speaker numbers alone are never treated as persistent identity. Explicit local
   cast choices take precedence over the recurring-character map.
6. Compare reports before increasing concurrency or retaining models. Model retention
   applies to managed transcription/alignment/diarization models, not the remote
   Voicebox service or Demucs. ASR checking (`quality.asr: suspicious` or `all`)
   adds recognition work and should be evaluated separately.

## Recovery and storage

Artifacts live beneath source-specific work/output directories and target-language
namespaces. Old filename-only caches are not blindly trusted. Each new review stores
an immutable line-audio snapshot using hardlinks where possible, with a copy fallback.
Reports, snapshots, and superseded artifacts need periodic storage management; this
change does not automatically delete them.

TTS creation is not automatically retried after an ambiguous response. Once an ID
has been saved, an interrupted poll/download resumes that request. A confirmed
terminal failure permits a bounded fresh attempt. Cancellation requests remote
cancellation best-effort. A lost creation response may require inspecting Voicebox
history because no reliable ID was returned.

Source-language word alignment can split dialogue at speaker changes. Translated
subtitles are not force-aligned against the original language. Ambiguous cues stay
in the script, and missing diarization dependencies fall back to one narrator.

Auditions export audio montages; source transcription and diarization are still
full-source work. This is soundtrack dubbing with the original video retained, not
video synthesis or lip-sync animation. Automated clip checks cannot judge acting,
character identity, or translation meaning; review and listening remain necessary.

## Benchmark protocol

A local Windows microbenchmark on 2026-09-20 measured 20 duration lookups on
the same synthetic 10-second PCM WAV: median 0.046 ms from its WAV header versus
53.003 ms through an FFprobe process. This validates avoiding per-line process
startup for supported WAVs; it does not measure generation or total job speed.
The local result is retained at `work/benchmarks/duration-lookup.json`.

Use the same source cut and settings for every comparison. Choose short scenes with
multiple speakers, rapid speech, quiet dialogue, and loud backgrounds. Run a cold
render, a warm rerun, then change one line. Keep the JSON reports under `work/reports`.
Compare stage seconds, generated/cached lines, retries, timing flags, and listening
notes. Dry-run reports are labelled and are not inference benchmarks. Never claim a
speedup from mock tests or compare different engines as equivalent quality.

Heavy models and listening quality require an installed Voicebox/ML stack and a
representative media sample; automated tests cover orchestration and real FFmpeg
processing where available. Production throughput and acting quality remain measured
acceptance gates rather than promises inferred from code.

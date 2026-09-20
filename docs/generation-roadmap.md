# Generation roadmap

Implementation order and acceptance gates:

1. Run reports: record stage durations, failure state, cache reuse, and counts.
2. Reliability: per-speaker synthesis, configuration-aware artifacts, resumable requests.
3. Dialogue: contextual batches, speech cue cleanup, bounded duration repair.
4. Throughput: explicit engines/presets, bounded requests, adaptive polling, model controls.
5. Sound: configurable mix, clip checks, pronunciation and selective repair.
6. Review: representative auditions, line editing/requeue, reusable character assignments.

## Benchmark protocol

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

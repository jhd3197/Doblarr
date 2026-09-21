"""Pipeline stages. Each stage is a small function that takes the DubJob
(and clients/config as needed), does one thing, and updates the job in place.

All stages share the `@stage` plumbing (common.py): a `dry()` sentinel for
dry-run planning and a `cached()` sentinel that skips stages whose declared
artifacts are fresh (checkpoint/resume; `force` bypasses).

Implementation status:
    extract    - real (ffmpeg)
    mux        - real (ffmpeg)
    transcribe - real (subtitles; whisper via whisperx / faster-whisper)
    separate   - real (Demucs two-stems; falls back to the original bed)
    diarize    - real (pyannote; falls back to a single narrator)
    translate  - real wiring; quality depends on the translator provider
                 (inline dry-run passthrough — no @stage short-circuit)
    synthesize - real wiring to voicebox
    fit_timing - real (ffprobe measure + atempo stretch to the slot)
    mix        - real (ffmpeg mix + duck)
"""

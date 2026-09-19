"""Pipeline stages. Each stage is a small function that takes the DubJob
(and clients/config as needed), does one thing, and updates the job in place.

Implementation status:
    extract    - real (ffmpeg)
    mux        - real (ffmpeg)
    transcribe - real for subtitles; whisper path is a stub
    separate   - stub (Demucs)
    diarize    - stub (pyannote)
    translate  - real wiring; quality depends on the translator provider
    synthesize - real wiring to voicebox
    fit_timing - stub (time-stretch / duration match)
    mix        - stub (ffmpeg sidechain ducking)
"""

"""Doblarr — AI dubbing for your media library.

Turns a foreign-language video into an added, translated audio track by
orchestrating: source separation, transcription/diarization, LLM translation,
voice-cloned TTS (via the voicebox service), time-fitting, mixing, and muxing.

Doblarr owns the movie-specific pipeline; voicebox owns voice cloning + TTS.
"""

__version__ = "0.1.0"

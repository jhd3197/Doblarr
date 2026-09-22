"""Separate fingerprints for generation, processing, mix and verification.

Each stage already writes a receipt describing the exact request it satisfied
(`artifacts.record`, the per-clip synthesis receipt, the quality receipt). This
module does not replace those receipts — it names the four independent caches
and derives one short id per request so cues, takes, artifacts and findings can
reference the exact inputs they came from.

Keeping the caches separate is what makes targeted invalidation possible: a gain
or fade change must invalidate processing and mix without touching generation,
and a checker-version change must invalidate verification without touching audio.
The `request` dictionaries are passed through unchanged, so existing on-disk
receipts keep matching and no cached work is thrown away by adopting this.
"""

from __future__ import annotations

from .artifacts import digest

GENERATION = "generation"      # a TTS request for one cue
PROCESSING = "processing"      # a derivative produced from an immutable input
MIX = "mix"                    # dialogue placed over the bed
VERIFICATION = "verification"  # acoustic/content checks over an artifact
KINDS = (GENERATION, PROCESSING, MIX, VERIFICATION)


def fingerprint(kind: str, request) -> str:
    """A short id for `request` inside one named cache namespace."""
    if kind not in KINDS:
        raise ValueError(f"unknown fingerprint kind {kind!r}")
    return digest({"kind": kind, "request": request})[:16]


def generation(request) -> str:
    return fingerprint(GENERATION, request)


def processing(request) -> str:
    return fingerprint(PROCESSING, request)


def mix(request) -> str:
    return fingerprint(MIX, request)


def verification(request) -> str:
    return fingerprint(VERIFICATION, request)

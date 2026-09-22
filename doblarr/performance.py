"""Acting direction: what to ask for, and what the engine can actually do.

D06's first half. A line's delivery is assembled from several layers — the
target locale's guidance, the character's standing direction, a speech mode, a
few delivery traits, and whatever the reviewer typed for this one line — and
the assembly rule matters more than any individual layer:

**A later layer replaces the same field, not the whole instruction.** Typing
"sound exhausted" on one line must not throw away the accent guidance that
keeps the character sounding like themselves. Each layer writes into named
fields, the fields are emitted in a fixed order, and the record says which
layer won each one.

Two things this deliberately refuses to do:

- It never reports an instruction as applied when the engine cannot take one.
  Capability comes from the adapter, and an unsupported instruction is listed
  on the intent as unsupported, so review shows "asked for, not honored"
  rather than pretending.
- A speech mode is not a character and not a gain. "Inner thought" changes how
  a line is asked for; it does not mint a new voice and it does not mandate a
  fixed dB reduction. Loudness belongs to `doblarr.levels`, and keeping them
  apart is what lets one character speak, think and shout in one voice.
"""

from __future__ import annotations

from .cues import SPEECH_MODES, PerformanceIntent, now

COMPOSER = "direction/1"

# The fields an instruction is built from, in the order they are spoken. The
# order is fixed so the same intent always composes to the same string, which
# is what makes a generation request cacheable.
FIELDS = ("locale", "character", "mode", "traits", "line")

# How each speech mode is asked for. Phrasing only — no mode carries a level,
# an effect or a voice change.
MODE_DIRECTION = {
    "thought": "as an inner thought: close, intimate, unprojected",
    "whisper": "whispering, breathy and unprojected",
    "shout": "shouting, projected and urgent",
    "call": "calling out to someone at a distance",
    "broadcast": "as if heard through a speaker or radio",
    "normal": "",
    "unknown": "",
}

# Delivery traits worth naming. Free text still works; a known trait simply
# composes predictably and can be offered as a control in review.
TRAITS = (
    "urgent", "restrained", "warm", "cold", "amused", "weary", "tender",
    "angry", "afraid", "formal", "casual", "breathless", "deadpan",
)


def known_trait(trait: str) -> bool:
    return str(trait).strip().lower() in TRAITS


def clean_traits(traits) -> list[str]:
    """Normalize a trait list without discarding an unfamiliar one."""
    seen: list[str] = []
    for trait in traits or []:
        value = str(trait).strip().lower()
        if value and value not in seen:
            seen.append(value)
    return seen


def normalize_mode(mode) -> str:
    value = str(mode or "").strip().lower() or "unknown"
    return value if value in SPEECH_MODES else "unknown"


def capability(engine: str, client=None) -> str:
    """Whether this engine accepts delivery instructions, per the adapter.

    Asks the client first — the adapter is the only thing that actually knows —
    and falls back to the client class's declared set. An engine nobody can
    speak for is `unknown`, never optimistically `supported`.
    """
    from .clients.voicebox import VoiceboxClient

    if not engine:
        return "unknown"
    for holder in (client, VoiceboxClient):
        check = getattr(holder, "supports_direction", None)
        if callable(check):
            try:
                return "supported" if check(engine) else "unsupported"
            except Exception:  # noqa: BLE001 - a broken adapter is not a capability
                continue
    return "unknown"


def compose(seg, *, engine: str = "", client=None, cast_delivery: str = "",
            locale_direction: str = "", character_note: str = "",
            narrator_delivery: str = "", scene_intent: str = "") -> PerformanceIntent:
    """Build the effective instruction for one cue from every layer that applies.

    Precedence, lowest first: locale guidance, the character's standing
    direction (a cast entry, a narrator default or an accepted character note),
    an accepted scene intent, the cue's speech mode and traits, and finally the
    reviewer's own words for this line. Each writes named fields; the rest of
    the instruction survives.
    """
    intent = seg.intent
    mode = normalize_mode(intent.mode)
    traits = clean_traits(intent.traits)
    fields: dict[str, str] = {}
    sources: list[dict] = []

    def put(field: str, text: str, origin: str) -> None:
        text = str(text or "").strip()
        if not text:
            return
        fields[field] = text
        sources.append({"field": field, "origin": origin, "text": text})

    put("locale", locale_direction, "locale")
    # One character direction slot: the most specific standing direction wins,
    # and the ones it displaces are still visible in `sources`.
    put("character", narrator_delivery, "narrator")
    put("character", character_note, "character-note")
    put("character", cast_delivery, "cast")
    put("mode", scene_intent, "scene")
    put("mode", MODE_DIRECTION.get(mode, ""), "mode")
    put("traits", ", ".join(traits), "traits")
    # The line's own direction. `seg.delivery` is the long-standing review
    # field and stays authoritative for the line slot; the structured
    # `intent.direction` is the same idea with an origin recorded.
    put("line", intent.direction, intent.origin or "manual")
    put("line", seg.delivery, "line")

    effective = "; ".join(fields[f] for f in FIELDS if fields.get(f))
    state = capability(engine, client)
    composed = PerformanceIntent(
        mode=mode,
        traits=traits,
        direction=intent.direction or seg.delivery,
        origin=intent.origin if intent.origin != "unknown" else (
            "manual" if seg.delivery else "unknown"),
        revision=intent.revision,
        treatment=intent.treatment,
        effective=effective,
        capability=state,
        sources=sources,
        at=now(),
    )
    if effective and state != "supported":
        # Recorded as asked-for-but-not-honored. The generation request below
        # will not carry it, and review shows exactly which layers were lost.
        composed.unsupported = [f"{s['field']}: {s['text']}" for s in sources]
    return composed


def requested_direction(intent: PerformanceIntent) -> str:
    """The instruction to actually send, or empty when the engine cannot take one."""
    return intent.effective if intent.capability == "supported" else ""


def from_edit(edit: dict | None, existing: PerformanceIntent | None = None
              ) -> PerformanceIntent:
    """Build an intent from a review edit, keeping whatever it does not mention."""
    base = existing or PerformanceIntent()
    data = dict(edit or {})
    if not data:
        return base
    mode = normalize_mode(data.get("mode", base.mode))
    traits = clean_traits(data.get("traits", base.traits))
    direction = str(data.get("direction", base.direction) or "")
    treatment = str(data.get("treatment", base.treatment) or "")
    changed = (mode, traits, direction, treatment) != (
        base.mode, base.traits, base.direction, base.treatment)
    return PerformanceIntent(
        mode=mode, traits=traits, direction=direction,
        origin=str(data.get("origin") or "manual"),
        revision=base.revision + 1 if changed else base.revision,
        treatment=treatment, at=now(),
    )


def describe(intent: PerformanceIntent) -> str:
    """One human-readable line for review, honest about what was honored."""
    if intent.empty:
        return "No delivery direction."
    parts = [intent.effective or "(nothing composed)"]
    if intent.capability == "supported":
        parts.append("applied")
    elif intent.capability == "unsupported":
        parts.append("not applied — this engine does not take delivery instructions")
    else:
        parts.append("engine capability unknown")
    return " · ".join(parts)

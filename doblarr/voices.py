"""Voice casting — archetype categories, per-title cast keys, default assignment.

A voice cast is per-title and reusable: every detected speaker gets ONE voice,
categorized by archetype, so the voice stays consistent across scenes and
episodes (and carries over from the teaser to the full dub). Cast entries are
`{speaker_id, label, category, voice, previewed}` — e.g. label "Adult M 1",
category "adult_m", voice a voicebox profile id ("" = unassigned).
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger("doblarr.voices")

# Archetype categories: (key, display label base). Entries are auto-numbered
# per category ("Adult M 1", "Adult M 2"); the narrator stays singular.
CATEGORIES = [
    ("speaker", "Speaker"),
    ("narrator", "Narrator"),
    ("child_f", "Child F"),
    ("child_m", "Child M"),
    ("young_f", "Young F"),
    ("young_m", "Young M"),
    ("adult_f", "Adult F"),
    ("adult_m", "Adult M"),
    ("elderly_f", "Elderly F"),
    ("elderly_m", "Elderly M"),
]
CATEGORY_LABELS = dict(CATEGORIES)


def ensure_cast(job, db, events=None) -> list[dict] | None:
    """Create/merge the title's voice cast after diarization (tease), or load
    the saved cast (full dub). Returns the cast list, or None without a db.

    Teases discover speakers and assign defaults; full dubs reuse whatever the
    teaser (or the user) established, so voices stay consistent.
    """
    key = cast_key(path=str(job.input_file))
    existing = db.load_cast(key)
    if job.speakers:
        cast = assign_default_cast(list(job.speakers), existing=(existing or {}).get("cast"))
        if not existing or len(cast) != len(existing["cast"]):
            db.save_cast(key, job.input_file.stem, cast)
            log.info("voice cast saved for %s (%d speakers)", key, len(cast))
            if events:
                events.publish("cast", {"type": "updated", "key": key, "speakers": len(cast)})
        return cast
    return (existing or {}).get("cast") or None


def cast_key(
    title: str | None = None,
    path: str | None = None,
    tmdb_id: int | None = None,
    tvdb_id: int | None = None,
) -> str:
    """Stable per-title cast key: media path if known, else tmdb/tvdb id, else title."""
    if path:
        return os.path.normcase(str(path))
    if tmdb_id:
        return f"tmdb:{tmdb_id}"
    if tvdb_id:
        return f"tvdb:{tvdb_id}"
    return f"title:{(title or '').strip().lower()}"


def assign_default_cast(speakers, existing: list[dict] | None = None) -> list[dict]:
    """Map speaker ids to default archetype assignments.

    A lone speaker is the Narrator; multiple speakers get neutral numbered labels.
    Existing entries keep the
    user's choices (voice, label); only genuinely new speakers get defaults.
    Nothing is dropped — a teaser re-run never loses full-dub assignments.
    """
    existing = [dict(e) for e in (existing or [])]
    known = {e["speaker_id"] for e in existing}
    counts: dict[str, int] = {}
    for e in existing:
        counts[e["category"]] = counts.get(e["category"], 0) + 1

    speakers = list(speakers)
    for spk in speakers:
        if spk in known:
            continue
        # Diarization identifies turns, not age or gender.
        category = "narrator" if len(speakers) == 1 else "speaker"
        counts[category] = counts.get(category, 0) + 1
        base = CATEGORY_LABELS[category]
        label = base if category == "narrator" else f"{base} {counts[category]}"
        existing.append(
            {
                "speaker_id": spk,
                "label": label,
                "category": category,
                "voice": "",
                "previewed": False,
            }
        )
    return existing


def merge_cast(own: list[dict] | None, inherited: list[dict]) -> list[dict]:
    """The title's cast over the series voices it inherits.

    A title entry with its own voice wins. One without a voice does not blank an
    inherited voice, but is otherwise kept: in clone mode its reference choice
    and fallback voice are the whole point of the entry.
    """
    assigned = {e["speaker_id"]: e for e in inherited}
    for entry in own or []:
        if entry.get("voice") or entry["speaker_id"] not in assigned:
            assigned[entry["speaker_id"]] = entry
    return list(assigned.values())


def assigned_cast(job, db, revision_id: str | None, series_id: str | None) -> list[dict]:
    """Voices cast for the characters this run's speakers were identified as.

    A speaker whose lines an analysis identified as a character (through the
    cues they share, doblarr.speaker_memory) inherits that character's series
    casting choice for the run's locale, or its "any" choice. Speakers nobody
    identified inherit nothing: a diarization number is not an identity.
    """
    if not revision_id or not series_id:
        return []
    from . import identity, speaker_memory
    from .studio import casting, records

    labels = speaker_memory.characters_for_labels(db, revision_id, job.segments)
    if not labels:
        return []
    series_ref = identity.casting_ref(series_id)
    choices = (records.get(db, "casting", casting.record_id("series", series_ref))
               or {}).get("characters") or {}
    out = []
    for label, character in sorted(labels.items()):
        choice = casting.pick(choices, character["name"], locale=job.target_locale or "")
        if not choice or not choice.get("voice"):
            continue
        out.append({"speaker_id": label, "label": character["name"], "category": "speaker",
                    "voice": choice["voice"], "previewed": True,
                    "character_id": character["id"],
                    **{k: choice[k] for k in ("engine", "pitch_semitones", "formant_semitones")
                       if choice.get(k)}})
    return casting.to_voice_cast([], {"speakers": [
        {"speaker": e["speaker_id"], "character": e["label"], "source": "series",
         **{k: v for k, v in e.items() if k not in ("speaker_id", "label")}}
        for e in out]}) if out else []


def character_cast(job, db, group, mapping):
    """Reuse only explicitly mapped characters; diarization numbers are not identities."""
    if not group or not mapping:
        return []
    saved = (db.load_cast(f"series:{group}") or {}).get("cast", [])
    voices = {e["speaker_id"]: e for e in saved}
    return [
        {**voices[mapping[label]], "speaker_id": label}
        for label in job.speakers
        if mapping.get(label) in voices
    ]


def save_characters(job, db, group, mapping):
    if not group or not mapping:
        return
    saved = (db.load_cast(f"series:{group}") or {}).get("cast", [])
    entries = {e["speaker_id"]: e for e in saved}
    for label, speaker in job.speakers.items():
        name = mapping.get(label)
        if name and speaker.voicebox_profile_id:
            entries[name] = {
                "speaker_id": name,
                "label": name,
                "category": "narrator",
                "voice": speaker.voicebox_profile_id,
                "previewed": True,
            }
    db.save_cast(f"series:{group}", group, list(entries.values()))

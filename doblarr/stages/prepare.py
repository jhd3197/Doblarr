"""Conservative speech cue cleanup. Ambiguous title cards remain reviewable.

Removing a cue from synthesis is not the same as not knowing something was
heard there: every dropped non-spoken cue is retained on the job as a typed,
timed `NonverbalEvent` for reaction-coverage review. Merging two cues retires
both IDs in favour of a new one, so an edit addressed to either half raises a
conflict instead of silently changing a different line.

Three things this deliberately will not do:

- **Speak a tag.** `[gasps]` becomes an event, never a line of dialogue for the
  engine to pronounce. That is the failure this parsing exists to prevent.
- **Guess a type it cannot read.** An unrecognised bracketed cue keeps
  `type="unknown"` and its original text. A subtitle tag is a label somebody
  typed, not a detector, so the recorded confidence never claims more.
- **Send a bare reaction to the engine.** A cue that is nothing but a laugh
  or a click written out as a word ("Tsk!", "Heh heh", "Oh!") is not a line
  an engine can act: asked to say "¡Tsk!", one produced a cheerful "Hah-ah",
  and asked for "¡Oh!" another held a vowel for half a minute. The original
  actor already made that sound, so it becomes an event too, and coverage can
  keep the real one. Only a cue made *entirely* of such sounds qualifies;
  "Huh? Yeah..." is speech.
- **Lose words.** A mixed cue (`[laughs] No puedo creerlo.`) is split only when
  the bracketed part matches the known vocabulary *and* real words remain. Any
  other shape is left exactly as it is and stays a spoken line.
"""

import re

from ..cues import SOURCE, NonverbalEvent, Span, event_id, merge_cues, now

# The non-spoken vocabulary this parser is willing to recognise, and what each
# entry actually is. A vocal reaction belongs to a person and may need to be
# retained or replaced; a background event belongs to the bed and is never a
# voice. Anything outside this table stays `unknown`.
_KNOWN: tuple[tuple[str, str, str], ...] = (
    (r"laughter|laughs?|laughing|chuckles?|chuckling|giggles?", "laugh", "vocal"),
    (r"sighs?|sighing", "sigh", "vocal"),
    (r"gasps?|gasping", "gasp", "vocal"),
    (r"sobs?|sobbing|crying|weeps?|weeping", "cry", "vocal"),
    (r"screams?|screaming|shouts?|shouting|yells?|yelling", "scream", "vocal"),
    (r"coughs?|coughing|clears? (?:his|her|their) throat", "cough", "vocal"),
    (r"breathes?|breathing|panting|pants|exhales?|inhales?", "breath", "vocal"),
    (r"grunts?|grunting|groans?|groaning|strains?|straining", "effort", "vocal"),
    (r"applause|clapping|cheering|cheers", "applause", "background"),
    (r"music|instrumental|singing|theme", "music", "background"),
    (r"footsteps|footfalls", "footsteps", "background"),
    (r"door (?:opens|closes|slams)|knocking", "door", "background"),
    (r"silence|no audible dialogue", "silence", "background"),
    (r"inaudible|unintelligible|indistinct", "inaudible", "unknown"),
)

# Sounds written out as words. Matched per token against a cue with its
# punctuation removed, and only when *every* token is one of them.
_INTERJECTIONS: tuple[tuple[str, str], ...] = (
    (r"(?:a|e)?(?:ha|he|hah|heh|ja|je|fu|hu|ho|hoh|hi|ku)(?:ha|he|hah|heh|ja|je|fu|hu|ho|hi|ku)+h?"
     r"|heh|hah|ahah+|ehe+h?|hehe+|fufu+|kuku+", "laugh"),
    (r"t+s+k+|t+c+h+|tut|hmph+|hmf|pf+t+|ps+h+|bah", "interjection"),
    (r"o+h+|a+h+|u+h+|e+h+|h+u+h+|h+m+|m+h+m*|w+h+o+a+|o+o+h+|w+a+h+|u+g+h+|a+r+g+h+|"
     r"g+a+h+|e+e+k+|a+y+|u+m+|e+r+m+", "interjection"),
)
_INTERJECTION_TYPES = tuple((re.compile(f"(?:{pattern})"), kind)
                            for pattern, kind in _INTERJECTIONS)
_INTERJECTION_TOKEN = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*", re.UNICODE)
_MAX_INTERJECTION_TOKENS = 4


def interjection(text: str) -> str | None:
    """The event type of a cue made only of reaction sounds, else None.

    "Tsk!" and "Heh heh..." qualify; "Oh, I see." does not, because "I see"
    is words. Returns `laugh` when every sound is laughter, `interjection`
    otherwise — a click and a gasp written as "Tch! Ah!" are one reaction.
    """
    body = str(text).strip()
    if not body or re.search(r"[\[\]()]", body):
        return None
    tokens = [t for token in _INTERJECTION_TOKEN.findall(body.casefold())
              for t in token.split("-") if t]
    if not tokens or len(tokens) > _MAX_INTERJECTION_TOKENS:
        return None
    kinds = set()
    for token in tokens:
        kind = next((k for pattern, k in _INTERJECTION_TYPES if pattern.fullmatch(token)),
                    None)
        if kind is None:
            return None
        kinds.add(kind)
    return "laugh" if kinds == {"laugh"} else "interjection"


_CUE = re.compile("(?:" + "|".join(pattern for pattern, _t, _c in _KNOWN) + ")", re.IGNORECASE)
_TYPES = tuple((re.compile(f"(?:{pattern})", re.IGNORECASE), kind, category)
               for pattern, kind, category in _KNOWN)
# A bracketed group at the very start or the very end of a cue. Only these two
# positions are split off: a tag in the middle of a sentence is far more likely
# to be a stage direction the parser would mangle.
_EDGE_TAG = re.compile(r"^\s*(?P<lead>[\[(][^\[\]()]{1,60}[\])])\s*(?P<rest>.+)$|"
                       r"^(?P<body>.+?)\s*(?P<tail>[\[(][^\[\]()]{1,60}[\])])\s*$",
                       re.DOTALL)
_WORD = re.compile(r"\w", re.UNICODE)


def classify(text: str) -> tuple[str, str]:
    """What a cue's wording says was heard, and whether it is a voice.

    Returns `("unknown", "unknown")` whenever the wording is not in the table
    above. That is the common case and it is not a failure: an unrecognised
    tag is still a timed piece of evidence a reviewer can act on.
    """
    inner = text.strip()
    if re.fullmatch(r"[♪♫\s]+", inner):
        return "music", "background"
    sound = interjection(inner)
    if sound:
        return sound, "vocal"
    if len(inner) >= 2 and (inner[0], inner[-1]) in {("[", "]"), ("(", ")")}:
        inner = inner[1:-1].strip()
    for pattern, kind, category in _TYPES:
        if pattern.fullmatch(inner):
            return kind, category
    return "unknown", "unknown"


def _nonspoken(text):
    if re.fullmatch(r"[♪♫\s]+", text):
        return True
    if len(text) >= 2 and (text[0], text[-1]) in {("[", "]"), ("(", ")")}:
        return bool(_CUE.fullmatch(text[1:-1].strip()))
    return False


def _split_mixed(text: str) -> tuple[str, str, str] | None:
    """Split `[laughs] words` or `words (sighs)` into (tag, spoken, position).

    Returns None unless the bracketed part is one of the known cues *and* what
    remains still contains words. An unrecognised tag, a tag in the middle, or
    a cue that would be left empty is not split at all — keeping a line intact
    is always safer than deleting speech to tidy a label.
    """
    match = _EDGE_TAG.match(text)
    if match is None:
        return None
    tag = match.group("lead") or match.group("tail") or ""
    rest = match.group("rest") or match.group("body") or ""
    if not tag or not _WORD.search(rest):
        return None
    if not _CUE.fullmatch(tag[1:-1].strip()):
        return None
    return tag, rest.strip(), "leading" if match.group("lead") else "trailing"


def _event(seg, text: str, ordinal: int, position: str, original: str) -> NonverbalEvent:
    """One timed piece of evidence, with the original wording kept verbatim."""
    kind, category = classify(text)
    spans = [s.as_dict() for s in seg.source.spans] or [Span(seg.start, seg.end, SOURCE).as_dict()]
    return NonverbalEvent(
        event_id=event_id(seg.cue_id, ordinal),
        cue_id=seg.cue_id,
        type=kind,
        category=category,
        # A subtitle names who is on screen at best. It is recorded as a
        # possible speaker and never treated as a proven one.
        speaker=seg.speaker if category == "vocal" else None,
        text=text,
        source=[Span.from_dict(s) for s in spans],
        evidence="subtitle",
        # Deliberately no number: a label somebody typed is not a measurement,
        # and a fabricated 0.8 here would be read as detector confidence.
        confidence=None,
        checks={"cue_text": original, "position": position,
                "parsed": kind != "unknown"},
        at=now(),
    )


def run(job, enabled=True, merge_gap=0.2, max_duration=12, split_mixed=True,
        interjections=True):
    if not enabled:
        return
    result = []
    removed = 0
    mixed = 0
    known = {event.event_id for event in job.nonverbal}
    for seg in job.segments:
        text = seg.text_src.strip()
        bare = interjections and interjection(text) is not None
        if seg.duration <= 0 or _nonspoken(text) or bare:
            removed += 1
            if seg.cue_id and seg.end > seg.start >= 0:
                event = _event(seg, text, 0, "whole", text)
                if event.event_id not in known:
                    job.nonverbal.append(event)
                    known.add(event.event_id)
            continue
        if split_mixed:
            parts = _split_mixed(text)
            if parts is not None:
                tag, spoken, position = parts
                mixed += 1
                event = _event(seg, tag, 1, position, text)
                if event.event_id not in known:
                    job.nonverbal.append(event)
                    known.add(event.event_id)
                # The words stay; only the label leaves. The cue keeps its
                # identity, because this is the same line with a tag removed.
                text = spoken
                seg.text_src = spoken
        previous = result[-1] if result else None
        if (
            previous
            and previous.speaker == seg.speaker
            and 0 <= seg.start - previous.end <= merge_gap
            and seg.end - previous.start <= max_duration
            and previous.text_src[-1:] not in ".!?。！？:"
            and not previous.text_translated
            and not seg.text_translated
        ):
            parents = [previous, seg]
            previous.text_src += " " + text
            previous.end = seg.end
            previous.words.extend(seg.words)
            # Keep both original intervals: a subtitle boundary is not a
            # guaranteed phrase boundary, and later plans need the real ranges.
            previous.source.spans = previous.source.spans + seg.source.spans
            merge_cues(job, parents, previous)
        else:
            seg.text_src = text
            result.append(seg)
    job.segments = result
    # A merged cue retires its id, but the evidence found on it still belongs
    # to the line that replaced it. The *event* id is left alone on purpose, so
    # a coverage decision recorded against it survives the merge.
    for event in job.nonverbal:
        for _hop in range(8):
            successors = job.cue_lineage.get(event.cue_id)
            if not successors:
                break
            event.cue_id = successors[0]
    job.metrics["nonspoken_cues_removed"] = removed
    job.metrics["nonverbal_events"] = len(job.nonverbal)
    job.metrics["mixed_cues_split"] = mixed
    job.metrics["interjection_cues"] = sum(
        e.type in ("laugh", "interjection") and e.checks.get("position") == "whole"
        and not str(e.text).startswith(("[", "(")) for e in job.nonverbal)

"""Conservative speech cue cleanup. Ambiguous title cards remain reviewable.

Removing a cue from synthesis is not the same as not knowing something was
heard there: every dropped non-spoken cue is retained on the job as timed
evidence for later reaction-coverage review. Merging two cues retires both IDs
in favour of a new one, so an edit addressed to either half raises a conflict
instead of silently changing a different line.
"""

import re

from ..cues import SOURCE, Span, merge_cues

_CUE = re.compile(
    r"(?:music|instrumental|silence|applause|laughter|laughs?|laughing|sighs?|"
    r"sighing|gasps?|gasping|sobs?|sobbing|crying|screams?|screaming|"
    r"coughs?|coughing|footsteps|door (?:opens|closes)|inaudible|unintelligible)",
    re.IGNORECASE,
)


def _nonspoken(text):
    if re.fullmatch(r"[♪♫\s]+", text):
        return True
    if len(text) >= 2 and (text[0], text[-1]) in {("[", "]"), ("(", ")")}:
        return bool(_CUE.fullmatch(text[1:-1].strip()))
    return False


def run(job, enabled=True, merge_gap=0.2, max_duration=12):
    if not enabled:
        return
    result = []
    removed = 0
    known = {event.get("cue_id") for event in job.nonverbal}
    for seg in job.segments:
        text = seg.text_src.strip()
        if seg.duration <= 0 or _nonspoken(text):
            removed += 1
            if seg.cue_id and seg.cue_id not in known and seg.end > seg.start >= 0:
                # Plan 08 decides coverage; Plan 01 only guarantees the evidence
                # survives. "unknown" is the honest reaction type here.
                job.nonverbal.append({
                    "cue_id": seg.cue_id,
                    "type": "unknown",
                    "speaker": seg.speaker,
                    "text": text,
                    "source": [s.as_dict() for s in seg.source.spans]
                              or [Span(seg.start, seg.end, SOURCE).as_dict()],
                    "coverage": "uncovered",
                })
                known.add(seg.cue_id)
            continue
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
    job.metrics["nonspoken_cues_removed"] = removed
    job.metrics["nonverbal_events"] = len(job.nonverbal)

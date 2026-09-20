"""Conservative speech cue cleanup. Ambiguous title cards remain reviewable."""

import re


def run(job, enabled=True, merge_gap=0.2, max_duration=12):
    if not enabled:
        return
    result = []
    removed = 0
    for seg in job.segments:
        text = seg.text_src.strip()
        if seg.duration <= 0 or re.fullmatch(r"\[[^\]]+\]|\([^()]+\)|[♪♫\s]+", text):
            removed += 1
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
            previous.text_src += " " + text
            previous.end = seg.end
            previous.words.extend(seg.words)
        else:
            seg.text_src = text
            result.append(seg)
    job.segments = result
    job.metrics["nonspoken_cues_removed"] = removed

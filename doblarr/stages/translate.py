"""Contextual translation with bounded batches and immediate checkpoints."""

from __future__ import annotations

from ..errors import JobCancelled

CHARS_PER_SECOND = 14


def run(
    job,
    translator,
    dry_run=False,
    progress=None,
    batch_size=12,
    glossary=None,
    chars_per_second=CHARS_PER_SECOND,
    checkpoint=None,
    cancel=None,
):
    if job.script_is_target or dry_run:
        for seg in job.segments:
            seg.text_translated = seg.text_translated or seg.text_src
        return
    size = max(1, min(32, int(batch_size)))
    pending = [s for s in job.segments if not s.text_translated]
    positions = {s.index: i for i, s in enumerate(job.segments)}
    batches: list[list] = []
    for seg in pending:
        if not batches or len(batches[-1]) >= size or seg.start - batches[-1][-1].end > 8:
            batches.append([])
        batches[-1].append(seg)
    for batch in batches:
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled before translation batch")
        payload = [
            {
                "text": s.text_src,
                "speaker": s.speaker,
                "duration": s.duration,
                "target_chars": max(1, int(s.duration * chars_per_second)),
            }
            for s in batch
        ]
        start, end = positions[batch[0].index], positions[batch[-1].index]
        context = [
            {"speaker": s.speaker, "text": s.text_src}
            for s in job.segments[max(0, start - 3) : end + 4]
        ]
        if hasattr(translator, "translate_batch"):
            results = translator.translate_batch(
                payload,
                job.script_lang or job.source_lang,
                job.target_lang,
                context=context,
                glossary=glossary,
            )
        else:
            results = [
                translator.translate(
                    s.text_src,
                    job.script_lang or job.source_lang,
                    job.target_lang,
                    target_chars=p["target_chars"],
                )
                for s, p in zip(batch, payload, strict=True)
            ]
        if len(results) != len(batch) or any(not str(t).strip() for t in results):
            raise ValueError("translation did not return every spoken line")
        for seg, text in zip(batch, results, strict=True):
            seg.text_translated = text
        job.metrics["translation_batches"] = job.metrics.get("translation_batches", 0) + 1
        if checkpoint:
            checkpoint()
        if progress:
            done = sum(bool(s.text_translated) for s in job.segments)
            progress(done, len(job.segments), f"translated {done}/{len(job.segments)}")

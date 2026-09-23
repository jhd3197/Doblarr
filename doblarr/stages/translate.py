"""Contextual translation with bounded batches and immediate checkpoints."""

from __future__ import annotations

import time

from ..errors import JobCancelled
from ..knowledge import memory

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
    memory_db=None,
    synopsis=None,
):
    if hasattr(translator, "repair_glossary"):
        translator.repair_glossary = dict(glossary or {})
    if (job.script_is_target and not job.translation_options.get("adapt_region")) or dry_run:
        for seg in job.segments:
            seg.text_translated = seg.text_translated or seg.text_src
        return
    size = max(1, min(32, int(batch_size)))
    for position, seg in enumerate(job.segments):
        context = memory.scene_context(job, position, glossary, synopsis)
        if ((seg.memory_context and seg.memory_context != context)
                or (job.script_is_target and job.translation_options.get("adapt_region")
                    and not seg.translation_provenance)):
            seg.text_translated = None
            seg.translation_provenance = {}
    pending = [s for s in job.segments if not s.text_translated]
    positions = {s.index: i for i, s in enumerate(job.segments)}
    started = time.perf_counter()
    reused = 0
    for seg in pending:
        if cancel is not None and cancel.is_set():
            raise JobCancelled("cancelled during memory lookup")
        seg.memory_context = memory.scene_context(job, positions[seg.index], glossary,
                                                  synopsis)
        reason = "reuse-disabled"
        match = None
        if memory_db is not None and job.translation_options.get("reuse_memory"):
            match, reason = memory.lookup(
                memory_db, source_lang=job.script_lang or job.source_lang,
                target_locale=job.target_locale or job.target_lang, source_text=seg.text_src,
                context=seg.memory_context, duration=seg.duration,
                target_chars=max(1, int(seg.duration * chars_per_second)),
                cutoff=(job.knowledge_snapshot or {}).get("memory_cutoff", 0),
            )
        seg.translation_provenance = {"method": "translation", "reason": reason}
        if match:
            seg.text_translated = match.target_text
            seg.translation_provenance = {
                "method": "memory", "reason": reason, "id": match.id, "revision": match.revision,
            }
            reused += 1
    job.metrics["memory_lookup_seconds"] = time.perf_counter() - started
    job.metrics["memory_checked_lines"] = len(pending)
    job.metrics["memory_reused_lines"] = reused
    if reused and checkpoint:
        checkpoint()
    pending = [s for s in pending if not s.text_translated]
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
            {"speaker": s.speaker, "text": s.text_src, "translated": s.text_translated}
            for s in job.segments[max(0, start - 3) : end + 4]
        ]
        if memory_db is not None and job.translation_options.get("reuse_memory"):
            retrieval_started = time.perf_counter()
            for seg in batch:
                context.extend(memory.suggestions(
                    memory_db, source_lang=job.script_lang or job.source_lang,
                    target_locale=job.target_locale or job.target_lang,
                    source_text=seg.text_src,
                    cutoff=(job.knowledge_snapshot or {}).get("memory_cutoff", 0),
                ))
            job.metrics["memory_lookup_seconds"] += time.perf_counter() - retrieval_started
        calls_before = getattr(translator, "provider_calls", None)
        batch_started = time.perf_counter()
        if hasattr(translator, "translate_batch"):
            results = translator.translate_batch(
                payload,
                job.script_lang or job.source_lang,
                job.target_lang,
                context=context,
                glossary=glossary,
                # only when there is one, so translators without it still work
                **({"synopsis": synopsis} if synopsis else {}),
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
        calls_after = getattr(translator, "provider_calls", None)
        job.metrics["translation_provider_calls"] = (
            (job.metrics.get("translation_provider_calls") or 0) + calls_after - calls_before
            if calls_after is not None and calls_before is not None else None
        )
        job.metrics["translation_seconds"] = (
            job.metrics.get("translation_seconds", 0) + time.perf_counter() - batch_started
        )
        # Preserve provider-reported usage, including repair attempts. Empty usage
        # means unknown (Voicebox); never estimate tokens from a line hit rate.
        job.metrics.setdefault("translation_usage", []).extend(
            getattr(translator, "last_usage", []) or [None]
        )
        job.metrics["translation_batches"] = job.metrics.get("translation_batches", 0) + 1
        if checkpoint:
            checkpoint()
        if progress:
            done = sum(bool(s.text_translated) for s in job.segments)
            progress(done, len(job.segments), f"translated {done}/{len(job.segments)}")

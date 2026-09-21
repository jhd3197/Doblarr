"""Offline exact-memory benchmark; synthetic measurements are not dubbing savings."""

import argparse
import json
import platform
import statistics
import tempfile
import time
import tracemalloc
from pathlib import Path

from doblarr.artifacts import digest
from doblarr.knowledge.memory import MemoryEntry, lookup
from doblarr.store import Database


def benchmark(size):
    context = {"register": "neutral", "speaker": "narrator", "before": [], "after": [],
               "settings": {}, "glossary": {}}
    with tempfile.TemporaryDirectory() as directory:
        db = Database(Path(directory) / "benchmark.db")
        started = time.perf_counter()
        with db._lock, db._conn:
            for i in range(size):
                language, locale, phrase = [("en", "es-MX", "Hello"), ("ja", "en", "こんにちは"),
                                             ("es", "fr-CA", "Hola")][i % 3]
                entry = MemoryEntry(
                    id=str(i), source_lang=language, target_locale=locale,
                    source_text=f"{phrase} {i}", target_text="test", context=context, duration=2,
                    status="reviewed", reviewer="synthetic", meaning_reviewed=True,
                    naturalness_reviewed=True, timing_reviewed=True,
                )
                db._conn.execute(
                    "INSERT INTO translation_memory"
                    " (id,revision,source_lang,target_locale,source_text,context_hash,document)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (entry.id, 1, language, locale, entry.source_text, digest(context),
                     entry.model_dump_json()),
                )
        build_seconds = time.perf_counter() - started
        db.close()
        tracemalloc.start()
        started = time.perf_counter()
        db = Database(Path(directory) / "benchmark.db")
        cold_seconds = time.perf_counter() - started
        timings = []
        for i in range(1000):
            index = (i * 97) % size
            language, locale, phrase = [("en", "es-MX", "Hello"), ("ja", "en", "こんにちは"),
                                         ("es", "fr-CA", "Hola")][index % 3]
            started = time.perf_counter()
            match, _ = lookup(db, source_lang=language, target_locale=locale,
                              source_text=f"{phrase} {index}", context=context, duration=2,
                              target_chars=28, cutoff=size)
            timings.append((time.perf_counter() - started) * 1000)
            assert match
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        disk_bytes = (Path(directory) / "benchmark.db").stat().st_size
        db.close()
    return dict(entries=size, build_seconds=build_seconds, cold_open_seconds=cold_seconds,
                warm_p50_ms=statistics.median(timings), warm_p95_ms=sorted(timings)[949],
                python_peak_bytes=peak, database_bytes=disk_bytes, lookups=len(timings))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = dict(platform=platform.platform(), processor=platform.processor(),
                  python=platform.python_version(),
                  note="Synthetic exact-memory lookup only; no provider calls or quality claims. "
                       "Python peak excludes SQLite native allocations and OS cache.",
                  runs=[benchmark(10000), benchmark(100000)])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))

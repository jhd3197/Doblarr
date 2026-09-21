"""Measure frozen phrase selection construction and warm speech matching."""

import json
import platform
import statistics
import time
import tracemalloc
from pathlib import Path

from doblarr.knowledge import Entry, KnowledgeSelection, Realization


def benchmark(size):
    tracemalloc.start()
    started = time.perf_counter()
    entries, realizations = [], []
    for i in range(size):
        locale, word = [("en", "Name"), ("es-MX", "Nombre"), ("ja", "名前")][i % 3]
        entries.append(Entry(id=str(i), phrase=f"{word}{i}", locale=locale))
        realizations.append(Realization(id=str(i), entry_id=str(i), engine="test",
                                        replacement=f"Spoken{i}"))
    selection = KnowledgeSelection(entries=entries, realizations=realizations, locale="en")
    selection.spoken("Name0", engine="test")
    cold_seconds = time.perf_counter() - started
    timings = []
    for i in range(1000):
        started = time.perf_counter()
        selection.spoken(f"Name{(i * 3) % size}", engine="test", line=f"scene#{i}")
        timings.append((time.perf_counter() - started) * 1000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return dict(entries=size, cold_construct_compile_seconds=cold_seconds,
                python_peak_bytes=peak, warm_p50_ms=statistics.median(timings),
                warm_p95_ms=sorted(timings)[949])


if __name__ == "__main__":
    result = dict(platform=platform.platform(), processor=platform.processor(),
                  python=platform.python_version(),
                  note="Synthetic mixed-locale pronunciation records. Cold time includes in-memory "
                       "record construction and first compile, excludes SQLite loading. "
                       "Warm samples use distinct line IDs without line overrides.",
                  runs=[benchmark(10000), benchmark(100000)])
    output = Path("docs/benchmarks/knowledge-matcher.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))

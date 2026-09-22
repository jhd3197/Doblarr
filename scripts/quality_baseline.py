#!/usr/bin/env python
"""Record or compare an offline audio-quality baseline.

    python scripts/quality_baseline.py record --name before
    python scripts/quality_baseline.py compare before after

Baselines land in `work/benchmarks/` and stay local — they name machine paths
and prove nothing outside this machine. The fixtures are tone, not speech: this
measures identity, artifact roles, cache reuse and request counts, not acting.

Needs FFmpeg. Needs no voicebox, no models and no network.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doblarr.benchmarks import compare, example_script, record  # noqa: E402
from doblarr.stages.common import save_script  # noqa: E402

DEFAULT_ROOT = Path("work") / "benchmarks"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    take = sub.add_parser("record", help="render the fixture scene and record observations")
    take.add_argument("--name", default="baseline", help="file name stem under work/benchmarks")
    take.add_argument("--out", type=Path, default=DEFAULT_ROOT)
    take.add_argument("--note", default="", help="what this baseline is for")
    take.add_argument("--keep", action="store_true",
                      help="keep this run's work and output directories")

    diff = sub.add_parser("compare", help="diff two recorded baselines")
    diff.add_argument("before")
    diff.add_argument("after")
    diff.add_argument("--out", type=Path, default=DEFAULT_ROOT)

    sub.add_parser(
        "example",
        help="print the worked cue example (two speakers, an overlap, an edit, a montage)",
    ).add_argument("--out", type=Path, default=DEFAULT_ROOT)

    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.command == "example":
        script = save_script(example_script(out / "example"), out / "example")
        print(script.read_text(encoding="utf-8"))
        return 0

    if args.command == "compare":
        result = compare(_resolve(out, args.before), _resolve(out, args.after))
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    if not shutil.which("ffmpeg"):
        print("ffmpeg is required to render the fixture scene", file=sys.stderr)
        return 2
    destination = out / f"{args.name}.json"
    # The fixture media lives at a stable path so two baselines describe the
    # same source document and therefore the same cue IDs. Each run still gets
    # a fresh work/output directory, so every baseline is a cold render.
    media_root = out / "fixture-media"
    root = out / args.name if args.keep else Path(tempfile.mkdtemp(prefix="doblarr-baseline-"))
    try:
        record(root, destination, note=args.note, media_root=media_root)
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)
    print(f"recorded {destination}")
    return 0


def _resolve(out: Path, name: str) -> Path:
    candidate = Path(name)
    if candidate.is_file():
        return candidate
    return out / (name if name.endswith(".json") else f"{name}.json")


if __name__ == "__main__":
    raise SystemExit(main())

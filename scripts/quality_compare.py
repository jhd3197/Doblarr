#!/usr/bin/env python
"""Build a bounded real-speech before/after comparison from a completed run.

    # what is in a completed run, and which windows are worth using
    python scripts/quality_compare.py scenes \
        --script work/media/<id>/es-MX/<name>.script.json \
        --clips  work/media/<id>/es-MX/clips/<name>-<hash>/es

    # render the comparison
    python scripts/quality_compare.py run --id 2026-09-22-mushi \
        --source work/input/<episode>.mkv \
        --script work/media/<id>/es-MX/<name>.script.json \
        --clips  work/media/<id>/es-MX/clips/<name>-<hash>/es \
        --vocals work/<name>.vocals.wav \
        --background work/<name>.background.wav \
        --scene 120:150 --scene 402:432 \
        --variant "baseline=" \
        --variant "improved=timing.mode=phrase,levels.mode=follow_source"

Unlike `quality_baseline.py`, this runs on **real speech**: the source episode,
the translated script and the generated takes a completed run already produced.
Every variant reuses those takes — the voicebox client handed to each run
refuses to generate at all, so a comparison that needed new speech fails
instead of quietly re-acting a line.

Results land in `work/benchmarks/quality-final/<id>/` and stay local: they name
machine paths and hold audio from the user's own library. Nothing is published.

Needs FFmpeg. Needs no voicebox, no models and no network.
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from doblarr import comparison  # noqa: E402
from doblarr.config import Config  # noqa: E402

DEFAULT_ROOT = Path("work") / "benchmarks" / "quality-final"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    look = sub.add_parser("scenes", help="list candidate windows in a completed run")
    look.add_argument("--script", required=True, type=Path)
    look.add_argument("--clips", required=True, type=Path)
    look.add_argument("--count", type=int, default=6)
    look.add_argument("--seconds", type=float, default=30.0,
                      help="longest window to suggest")

    go = sub.add_parser("run", help="render the bounded comparison")
    go.add_argument("--id", required=True, help="comparison id; its own directory")
    go.add_argument("--source", required=True, type=Path, help="the original media")
    go.add_argument("--script", required=True, type=Path)
    go.add_argument("--clips", required=True, type=Path)
    go.add_argument("--vocals", type=Path, help="this project's separated dialogue stem")
    go.add_argument("--background", type=Path, help="this project's separated bed")
    go.add_argument("--scene", action="append", default=[], metavar="START:END[:TITLE]",
                    help="a bounded window on the source timeline; repeatable")
    go.add_argument("--variant", action="append", default=[], metavar="NAME=K=V,K=V",
                    help="a settings variant; repeatable. An empty value means "
                         "'the configured defaults', which is the baseline.")
    go.add_argument("--config", type=Path, default=Path("config.yaml"))
    go.add_argument("--out", type=Path, default=DEFAULT_ROOT)
    go.add_argument("--source-lang", default="ja")
    go.add_argument("--target-lang", default="es")
    go.add_argument("--target-locale", default="")
    go.add_argument("--note", default="")
    go.add_argument("--open", action="store_true",
                    help="open the comparison page when it is ready")

    args = parser.parse_args(argv)

    if args.command == "scenes":
        payload = comparison.read_script(args.script)
        rows = comparison.inventory(payload, args.clips)
        playable = [r for r in rows if r["clip"]]
        print(f"{len(rows)} line(s) in the script, {len(playable)} with audio on disk")
        if not playable:
            print("No generated clips were found. Check --clips.", file=sys.stderr)
            return 2
        for row in comparison.suggest(rows, args.count, args.seconds):
            print(f"  --scene {row['start']:g}:{row['end']:g}"
                  f"    {row['lines']} line(s), {row['seconds']:.1f}s, "
                  f"{', '.join(row['speakers'])}")
        return 0

    if not args.scene:
        parser.error("at least one --scene is required")
    if len(args.variant) < 2:
        parser.error("a comparison needs at least two --variant values")

    windows, titles = [], {}
    for position, raw in enumerate(args.scene):
        parts = str(raw).split(":")
        if len(parts) < 2:
            parser.error(f"--scene needs START:END, got {raw!r}")
        try:
            windows.append((float(parts[0]), float(parts[1])))
        except ValueError:
            parser.error(f"--scene needs numeric seconds, got {raw!r}")
        if len(parts) > 2 and parts[2].strip():
            titles[position] = ":".join(parts[2:]).strip()

    variants: dict[str, dict] = {}
    for raw in args.variant:
        name, _, settings = str(raw).partition("=")
        name = name.strip()
        if not name:
            parser.error(f"--variant needs a name, got {raw!r}")
        if name in variants:
            parser.error(f"--variant {name!r} was given twice")
        variants[name] = _overrides(settings)

    stems = {}
    if args.vocals and args.background:
        stems = {"vocals": str(args.vocals), "background": str(args.background)}
    elif args.vocals or args.background:
        parser.error("--vocals and --background are given together or not at all")

    config = Config.load(args.config)
    try:
        manifest = comparison.run(
            config, comparison_id=args.id, source=args.source, script=args.script,
            clips=args.clips, windows=windows, titles=titles, variants=variants,
            root=args.out, stems=stems, source_lang=args.source_lang,
            target_lang=args.target_lang, target_locale=args.target_locale,
            note=args.note)
    except comparison.ComparisonError as exc:
        print(f"comparison not built: {exc}", file=sys.stderr)
        return 2

    root = Path(args.out) / args.id
    print(f"\ncomparison written to {root}")
    print(f"  page:     {root / 'index.html'}")
    print(f"  manifest: {root / 'manifest.json'}")
    print(f"  results:  {root / 'results.md'}")
    objective = manifest["objective"]
    print("\nobjective checks:", "ready" if objective["ready"] else "NOT ready")
    for problem in objective["problems"]:
        print(f"  ! {problem}")
    for note in objective["notes"]:
        print(f"  - {note}")
    for scene in manifest["scenes"]:
        index = scene["scene"]["index"]
        for variant in scene["variants"]:
            active = variant["active"]
            print(f"  scene {index} / {variant['name']}: "
                  f"timing={','.join(active['timing']['modes']) or '-'} "
                  f"levels={active['levels']['mode']} "
                  f"treatments={active['treatments'].get('mode', 'off')}"
                  f"/{active['treatments'].get('applied', 0)} applied "
                  f"tts={variant['tts_requests']}")
    print("\nNo speech was generated: every variant reused the imported takes.")
    print("Whether it sounds better is not established by any of this.")
    if args.open:
        webbrowser.open((root / "index.html").resolve().as_uri())
    return 0 if manifest["objective"]["ready"] else 1


def _overrides(text: str) -> dict:
    """Parse `key=value,key=value` into config overrides, keeping types honest."""
    values: dict = {}
    for pair in str(text).split(","):
        pair = pair.strip()
        if not pair:
            continue
        key, _, raw = pair.partition("=")
        key = key.strip()
        if not key or not _:
            raise SystemExit(f"a variant setting needs KEY=VALUE, got {pair!r}")
        value = raw.strip()
        if value.lower() in {"true", "false"}:
            values[key] = value.lower() == "true"
            continue
        if value.lower() in {"null", "none"}:
            values[key] = None
            continue
        try:
            values[key] = int(value)
            continue
        except ValueError:
            pass
        try:
            values[key] = float(value)
        except ValueError:
            values[key] = value
    return values


if __name__ == "__main__":
    raise SystemExit(main())

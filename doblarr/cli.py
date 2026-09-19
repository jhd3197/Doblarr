"""Doblarr command-line interface.

    doblarr check                         # ping the voicebox service
    doblarr dub MOVIE --to es --from ko [--subs FILE] [--dry-run]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .clients.voicebox import VoiceboxClient, VoiceboxError
from .config import Config
from .models import DubJob
from .pipeline import run_job


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _cmd_check(args: argparse.Namespace, config: Config) -> int:
    vb = VoiceboxClient(config["voicebox"]["base_url"])
    try:
        health = vb.health()
    except VoiceboxError as exc:
        print(f"voicebox NOT reachable: {exc}")
        return 1
    print(f"voicebox OK at {vb.base_url}: {health}")
    return 0


def _cmd_dub(args: argparse.Namespace, config: Config) -> int:
    src = Path(args.input)
    if not src.exists():
        print(f"input not found: {src}")
        return 1
    job = DubJob(
        input_file=src,
        source_lang=args.source,
        target_lang=args.to,
        subtitle_file=Path(args.subs) if args.subs else None,
    )
    run_job(job, config, dry_run=args.dry_run)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="doblarr", description="AI dubbing for your library")
    p.add_argument("--version", action="version", version=f"doblarr {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-c", "--config", default=None, help="path to config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="check the voicebox service is reachable")

    d = sub.add_parser("dub", help="dub a video into a target language")
    d.add_argument("input", help="path to the video file")
    d.add_argument("--to", required=True, help="target language code, e.g. es")
    d.add_argument("--from", dest="source", default="auto",
                   help="source language code, e.g. ko (default: auto)")
    d.add_argument("--subs", default=None, help="subtitle file for text + timing")
    d.add_argument("--dry-run", action="store_true",
                   help="print the plan without running heavy stages")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    config = Config.load(args.config)
    if args.command == "check":
        return _cmd_check(args, config)
    if args.command == "dub":
        return _cmd_dub(args, config)
    return 2


if __name__ == "__main__":
    sys.exit(main())

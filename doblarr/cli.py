"""Doblarr command-line interface.

    doblarr check                         # ping the voicebox service
    doblarr dub MOVIE --to es --from ko [--subs FILE] [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .clients.voicebox import VoiceboxClient, VoiceboxError
from .config import Config
from .logging_setup import setup_logging
from .models import DubJob
from .pipeline import run_job


def _cmd_check(args: argparse.Namespace, config: Config) -> int:
    vb = VoiceboxClient(config["voicebox"]["base_url"])
    try:
        health = vb.health()
    except VoiceboxError as exc:
        print(f"voicebox NOT reachable: {exc}")
        return 1
    print(f"voicebox OK at {vb.base_url}: {health}")
    return 0


def _cmd_serve(args: argparse.Namespace, config: Config) -> int:
    import uvicorn

    from .server import create_app

    host = args.host or config.get("web", {}).get("host", "127.0.0.1")
    port = args.port or config.get("web", {}).get("port", 6363)
    print(f"Doblarr serving on http://{host}:{port}  (UI + /api/library)")
    # log_config=None: logging_setup already configured uvicorn's loggers.
    uvicorn.run(create_app(config), host=host, port=int(port), log_config=None)
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
        kind=args.kind,
    )
    # --dry-run forces a plan; otherwise dub.dry_run from config decides.
    dry_run = args.dry_run if args.dry_run is not None else config["dub"].get("dry_run", True)
    from .artifacts import media_work, read_json
    from .knowledge import snapshot
    from .knowledge.packs import ensure_starter_pack
    from .languages import resolve_target_locale
    from .store import Database
    from .telemetry import write_json

    db = Database(config.db_path)
    try:
        ensure_starter_pack(db, config.get("knowledge", {}))
        job.target_locale = resolve_target_locale(config.as_dict(), job.target_lang)
        pin_file = media_work(config.work_dir, job) / job.target_locale / "knowledge.json"
        job.knowledge_snapshot = read_json(pin_file) if pin_file.is_file() else snapshot(db)
        write_json(pin_file, job.knowledge_snapshot)
        run_job(job, config, dry_run=dry_run, db=db)
    finally:
        db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="doblarr", description="AI dubbing for your library")
    p.add_argument("--version", action="version", version=f"doblarr {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-c", "--config", default=None, help="path to config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="check the voicebox service is reachable")

    s = sub.add_parser("serve", help="run the web UI + API server")
    s.add_argument("--host", default=None)
    s.add_argument("--port", default=None, type=int)

    d = sub.add_parser("dub", help="dub a video into a target language")
    d.add_argument("input", help="path to the video file")
    d.add_argument("--to", required=True, help="target language code, e.g. es")
    d.add_argument("--from", dest="source", default="auto",
                   help="source language code, e.g. ko (default: auto)")
    d.add_argument("--subs", default=None, help="subtitle file for text + timing")
    d.add_argument("--kind", choices=["full", "tease", "audition"], default="full",
                   help="full video, opening teaser, or representative audio audition")
    d.add_argument("--dry-run", action="store_true", default=None,
                   help="print the plan without running heavy stages "
                        "(overrides dub.dry_run in config)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.load(args.config)
    setup_logging(config, verbose=args.verbose, uvicorn=args.command == "serve")
    if args.command == "check":
        return _cmd_check(args, config)
    if args.command == "serve":
        return _cmd_serve(args, config)
    if args.command == "dub":
        return _cmd_dub(args, config)
    return 2


if __name__ == "__main__":
    sys.exit(main())

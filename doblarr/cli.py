"""Doblarr command-line interface.

    doblarr check                         # ping the voicebox service
    doblarr dub MOVIE --to es --from ko [--subs FILE] [--dry-run]
    doblarr cast series                   # series ids and titles
    doblarr cast search SERIES_ID [--query TITLE] [--source anilist|ann|jikan|bangumi|kitsu|all]
    doblarr cast suggest SERIES_ID --source ann   # entries matching the linked title
    doblarr cast link SERIES_ID URL [--season N] [--why TEXT]
    doblarr cast show SERIES_ID [--season N] [--episode N] [--merged]
    doblarr cast import SERIES_ID [--roles MAIN,SUPPORTING | --names A,B]
    doblarr research SERIES_ID "QUESTION" [--depth quick|standard|deep]
    doblarr scripts SERIES_ID [--sources fandom,screenplays,kitsunekko,opensubtitles,dubbing]
    doblarr titles SERIES_ID [--refresh]  # the title's ids and other names

The whole process, or one step of it (see doblarr.commands):

    doblarr make FILE_OR_FOLDER --to es-419   # doctor, analyze, lookup, voices, dub, fix, report
    doblarr doctor                        # every service and tool a run needs
    doblarr analyze FILE                  # separate, transcribe, group voices (once)
    doblarr lookup SERIES                 # catalogue ids, published cast, characters
    doblarr voices FILE [--name SPEAKER_01=NAME] [--move 12,14=NAME]
    doblarr queue FILE --to es-419 [--set key=value] [--wait]
    doblarr jobs [--watch JOB]
    doblarr report JOB                    # PASS/WARN/FAIL checks of a finished dub
    doblarr fix JOB                       # re-voice the lines the report flags
    doblarr line JOB INDEX [--text T | --retake | --voice V] [--clip out.wav]
    doblarr dubref FILE [--compare JOB]   # what the file's own dub says
    doblarr terms SERIES [add SOURCE=PHRASE | approve ID | retire ID]
    doblarr settings [get KEY | set KEY=VALUE ...]

Every one of these takes --json.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .clients.speech import SpeechError, build_speech_client
from .config import Config
from .errors import DoblarrError
from .logging_setup import setup_logging
from .models import DubJob
from .pipeline import run_job


def _cmd_check(args: argparse.Namespace, config: Config) -> int:
    speech = build_speech_client(config)
    try:
        health = speech.health()
    except SpeechError as exc:
        print(f"{speech.service} NOT reachable: {exc}")
        return 1
    print(f"{speech.service} OK at {speech.base_url}: {health}")
    return 0


def _cmd_serve(args: argparse.Namespace, config: Config) -> int:
    import uvicorn

    from .auth import ensure_api_key
    from .server import create_app

    host = args.host or config.get("web", {}).get("host", "127.0.0.1")
    port = args.port or config.get("web", {}).get("port", 6363)
    key = ensure_api_key(config)
    if key:
        print(f"Generated an API key and saved it as web.api_key in {config.path}.")
        print(f"Enter it when the web UI asks:  {key}")
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
    from .fileio import write_json
    from .knowledge import snapshot
    from .knowledge.packs import ensure_starter_pack
    from .languages import resolve_target_locale
    from .store import Database

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


def _print_hits(hits: list[dict]) -> None:
    for hit in hits:
        episodes = hit["episodes"] and f"{hit['episodes']} eps"
        facts = ", ".join(str(f) for f in (hit["format"], hit["year"], episodes) if f)
        why = f"  [{hit['why']}]" if hit.get("why") else ""
        print(f"  {hit['url']}  {hit['title']} ({facts}){why}")


def _cmd_cast(args: argparse.Namespace, config: Config) -> int:
    from . import catalogues, published_cast
    from .store import Database
    from .studio import records

    catalogues.configure_from(config)
    db = Database(config.db_path)
    try:
        if args.action == "series":
            for series in records.list_latest(db, "series"):
                if series.get("linked_to"):
                    continue
                linked = len(published_cast.links(db, series["id"]))
                print(f"{series['id']}  {published_cast.series_title(db, series['id']) or '?'}"
                      + (f"  [{linked} cast link(s)]" if linked else ""))
            return 0
        if args.action == "search":
            query = args.query or published_cast.series_title(db, args.series)
            if not query:
                print("no title known for this series; pass --query")
                return 1
            kind = "movie" if args.series.startswith("movie:") else "show"
            wanted = list(catalogues.PROVIDERS) if args.source == "all" else [args.source]
            hosts = ", ".join(catalogues.provider(n).host for n in wanted)
            print(f"searching for {query!r} (sends the title to {hosts})")
            found = published_cast.search_all(query, kind=kind, sources=wanted,
                                              max_results=8 if len(wanted) == 1 else 5)
            for name, hits in found["hits"].items():
                print(f"{catalogues.provider(name).label}:")
                _print_hits(hits)
            for name, why in found["skipped"].items():
                print(f"{catalogues.provider(name).label}: skipped ({why})")
            return 0
        if args.action == "suggest":
            print(f"searching {catalogues.provider(args.source).host} for the linked title")
            _print_hits(published_cast.suggest_links(db, args.series, args.source,
                                                     season=args.season))
            return 0
        if args.action == "link":
            published = published_cast.link(db, args.series, args.url, season=args.season,
                                            why=args.why or "")
            print(f"linked {published['title']}: {len(published['characters'])} characters"
                  + ("" if published["complete"] else " (more exist; raise the limit)"))
            return 0
        if args.action == "show":
            cast = published_cast.get(db, args.series, season=args.season)
            if cast is None:
                print("no published cast linked; run `doblarr cast search` then `link`")
                return 1
            if args.merged:
                merged = published_cast.merged_cast(db, args.series, season=args.season)
                for src in merged["sources"]:
                    print(f"{src['source']}: {src['title']} <{src['url']}>")
                for c in merged["characters"]:
                    voices = "; ".join(f"{v['language'] or '?'}: {v['name']}"
                                       for v in c["voice_actors"]) or "-"
                    flag = f"  (sources disagree on {', '.join(c['conflicts'])})" \
                        if c["conflicts"] else ""
                    print(f"  {c['name']:<24} [{'+'.join(c['sources'])}] {voices}{flag}")
                return 0
            print(f"{cast['title']} <{cast['url']}>")
            for c in published_cast.candidates(cast, episode=args.episode):
                voices = ", ".join(f"{v['name']} ({v.get('language') or '?'})"
                                   for v in c["voice_actors"]) or "-"
                print(f"  {c['name']:<24} {c['role']:<11} {c.get('gender') or '-':<7} "
                      f"voice: {voices}  ({c['why']})")
            return 0
        if args.action == "import":
            names = [n.strip() for n in args.names.split(",")] if args.names else None
            roles = [r.strip() for r in args.roles.split(",")]
            done = published_cast.import_characters(db, args.series, season=args.season,
                                                    roles=roles, names=names)
            print(f"created {len(done['created'])}: {', '.join(done['created']) or '-'}")
            print(f"matched {len(done['matched'])}: {', '.join(done['matched']) or '-'}")
            for item in done["ambiguous"]:
                print(f"skipped {item['name']}: the name also fits {', '.join(item['also'])}")
            return 0
    except (KeyError, ValueError, RuntimeError, LookupError) as exc:
        print(f"cast: {exc}")
        return 1
    finally:
        db.close()
    return 2


def _cmd_research(args: argparse.Namespace, config: Config) -> int:
    from .research import agent
    from .store import Database

    if args.depth:
        config = config.with_overrides({"research.depth": args.depth})
    db = Database(config.db_path)
    try:
        sent = agent.compose_query(db, args.series, args.question)
        model = agent.resolve_model(config)
        print(f"researching with {model} (sends this question out: {sent!r})")
        run = agent.research_title(db, args.series, args.question, config=config,
                                   target_locale=args.locale or "")
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f"research: {exc}")
        return 1
    finally:
        db.close()
    print(run["report"] or run["answer"] or "(no answer)")
    print(f"\nrun {run['id']} · ${run['cost']:.4f} · {run['elapsed_s']}s")
    if run["claim_id"]:
        print(f"external claim {run['claim_id']} waits in the narrative review")
    for t in run["terms"]:
        print(f"term for review: {t['source_form']} -> {t['phrase']}"
              + ("" if t.get("entry_id") else " (no show to keep it under; see the run folder)"))
    for lead in run["leads"]:
        print(f"cast lead: {lead['character']} ({lead['language']}): {lead['voice_actor']}")
    return 0


def _cmd_scripts(args: argparse.Namespace, config: Config) -> int:
    from . import catalogues
    from .research import probe, scripts
    from .store import Database

    catalogues.configure_from(config)
    db = Database(config.db_path)
    try:
        if args.sources:
            wanted = [s.strip() for s in args.sources.split(",") if s.strip()]
            print(f"looking in {', '.join(wanted)} (sends the title out)")
            found = probe.probe_all(db, config, args.series, wanted, wiki=args.wiki or "",
                                    languages=args.languages)
            for name, why in found["skipped"].items():
                print(f"{name}: skipped ({why})")
            print(f"kept {found['saved']} script(s)")
        for row in scripts.listing(db, args.series):
            where = (f"S{row['season'] or 0:02d}E{row['episode']:02d}"
                     if row["episode"] is not None else "-")
            print(f"  {row['id']}  {where:<7} {row['source']:<22} {row['kind']:<10} "
                  f"{row['lines']:>5} lines  {row['title']}")
    except (KeyError, ValueError, RuntimeError) as exc:
        print(f"scripts: {exc}")
        return 1
    finally:
        db.close()
    return 0


def _cmd_titles(args: argparse.Namespace, config: Config) -> int:
    from . import catalogues
    from .research import titles
    from .store import Database

    catalogues.configure_from(config)
    db = Database(config.db_path)
    try:
        if args.refresh:
            print("looking the title up on wikidata.org (and themoviedb.org with a key)")
            info = titles.refresh(db, args.series, tmdb_api_key=str(
                (config.get("research") or {}).get("tmdb_api_key") or ""))
        else:
            info = titles.get(db, args.series) or {"external_ids": titles.known_ids(
                db, args.series), "aliases": [], "skipped": {}}
    except KeyError as exc:
        print(f"titles: {exc}")
        return 1
    finally:
        db.close()
    for key, value in sorted(info["external_ids"].items()):
        print(f"  {key:<10} {value}")
    for alias in info["aliases"]:
        where = "-".join(x for x in (alias.get("language"), alias.get("region")) if x) or "?"
        print(f"  {where:<6} {alias['title']}")
    for name, why in (info.get("skipped") or {}).items():
        print(f"{name}: skipped ({why})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="doblarr", description="AI dubbing for your library")
    p.add_argument("--version", action="version", version=f"doblarr {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-c", "--config", default=None, help="path to config.yaml")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="check the speech service is reachable")

    s = sub.add_parser("serve", help="run the web UI + API server")
    s.add_argument("--host", default=None)
    s.add_argument("--port", default=None, type=int)

    d = sub.add_parser("dub", help="dub a video into a target language")
    d.add_argument("input", help="path to the video file")
    d.add_argument("--to", required=True, help="target language code, e.g. es")
    d.add_argument("--from", dest="source", default="auto",
                   help="source language code, e.g. ko (default: auto)")
    d.add_argument("--subs", default=None, help="subtitle file for text + timing")
    d.add_argument("--kind", choices=["full", "tease", "audition", "analyze"], default="full",
                   help="full video, opening teaser, representative audio audition, or "
                        "a line-by-line analysis without dubbing")
    d.add_argument("--dry-run", action="store_true", default=None,
                   help="print the plan without running heavy stages "
                        "(overrides dub.dry_run in config)")

    c = sub.add_parser("cast", help="link a series to its published cast (AniList, ANN, ...)")
    actions = c.add_subparsers(dest="action", required=True)
    actions.add_parser("series", help="list series ids and titles")
    cs = actions.add_parser("search", help="find catalogue entries for a series")
    cs.add_argument("series")
    cs.add_argument("--query", default=None, help="title to search (default: the series title)")
    cs.add_argument("--source", default="anilist",
                    choices=["anilist", "ann", "jikan", "bangumi", "kitsu", "all"])
    cg = actions.add_parser("suggest", help="entries of another catalogue that match the link")
    cg.add_argument("series")
    cg.add_argument("--source", required=True,
                    choices=["ann", "jikan", "bangumi", "kitsu", "anilist"])
    cg.add_argument("--season", type=int, default=None)
    cl = actions.add_parser("link", help="link a series (or one season) to an entry")
    cl.add_argument("series")
    cl.add_argument("url", help="a title URL from `cast search` or `cast suggest`")
    cl.add_argument("--season", type=int, default=None)
    cl.add_argument("--why", default=None, help="why this entry is the title (kept on record)")
    cw = actions.add_parser("show", help="show the linked cast, optionally for one episode")
    cw.add_argument("series")
    cw.add_argument("--season", type=int, default=None)
    cw.add_argument("--episode", type=int, default=None)
    cw.add_argument("--merged", action="store_true",
                    help="every linked catalogue side by side, voices by language")
    ci = actions.add_parser("import", help="create series characters from the linked cast")
    ci.add_argument("series")
    ci.add_argument("--season", type=int, default=None)
    ci.add_argument("--roles", default="MAIN", help="comma list of MAIN, SUPPORTING, BACKGROUND")
    ci.add_argument("--names", default=None, help="comma list of names (overrides --roles)")
    r = sub.add_parser("research", help="ask the web about a title (cited, for review)")
    r.add_argument("series")
    r.add_argument("question")
    r.add_argument("--depth", choices=["quick", "standard", "deep"], default=None)
    r.add_argument("--locale", default=None, help="dub locale for found terms, e.g. es-MX")

    sc = sub.add_parser("scripts", help="find and list a title's reference scripts")
    sc.add_argument("series")
    sc.add_argument("--sources", default=None,
                    help="comma list of " + ", ".join(
                        ["fandom", "screenplays", "kitsunekko", "opensubtitles", "dubbing"])
                    + "; without it, only lists what was found before")
    sc.add_argument("--wiki", default=None, help="the Fandom wiki's name, if not the title")
    sc.add_argument("--languages", default="en", help="OpenSubtitles languages, e.g. en,es")

    ti = sub.add_parser("titles", help="a title's catalogue ids and other names")
    ti.add_argument("series")
    ti.add_argument("--refresh", action="store_true", help="look them up again (online)")

    from . import commands
    commands.add_parsers(sub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = Config.load(args.config)
    setup_logging(config, verbose=args.verbose, uvicorn=args.command == "serve")
    try:
        return _dispatch(args, config)
    except (DoblarrError, KeyError, ValueError, OSError) as exc:
        if args.verbose:
            raise
        # A missing id or a bad value is the user's to fix: say what it was,
        # not where in the code it surfaced. --verbose keeps the traceback.
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        print(f"doblarr {args.command}: {message}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace, config: Config) -> int:
    if args.command == "check":
        return _cmd_check(args, config)
    if args.command == "serve":
        return _cmd_serve(args, config)
    if args.command == "dub":
        return _cmd_dub(args, config)
    if args.command == "cast":
        return _cmd_cast(args, config)
    if args.command == "research":
        return _cmd_research(args, config)
    if args.command == "scripts":
        return _cmd_scripts(args, config)
    if args.command == "titles":
        return _cmd_titles(args, config)
    from . import commands
    if args.command in commands.COMMANDS:
        return commands.run(args, config)
    return 2


if __name__ == "__main__":
    sys.exit(main())

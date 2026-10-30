"""The `doblarr` subcommands beyond `dub` and `serve`: one module per area.

`make` runs the whole process; every step is also a command of its own. Each
takes `--json` for scripts. See `doblarr --help` and `doblarr COMMAND --help`.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from ..config import Config
from . import doctor, episodes, jobs, make, research, settings, voice_study
from .base import ApiError, Ctx

MODULES = (make, doctor, episodes, research, jobs, settings, voice_study)
COMMANDS: dict[str, Callable[[Ctx], int]] = {
    name: fn for module in MODULES for name, fn in module.COMMANDS.items()}


def add_parsers(sub) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print the result as JSON")
    for module in MODULES:
        module.add_parsers(sub, common)


def run(args: argparse.Namespace, config: Config) -> int:
    try:
        return COMMANDS[args.command](Ctx(args, config))
    except ApiError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("stopped; a queued job keeps running on the server (`doblarr jobs`)",
              file=sys.stderr)
        return 130

"""Learn voice profiles from one film and test them held out on another.

    doblarr voice-study run FILM1 FILM2 --names names.json [--floor 0.3] [--margin 0.05]

FILM1 and FILM2 are an analysed media file or its `.speakers.json` sidecar.
`names.json` says which character each voice group is, per film:

    {"film1": {"SPEAKER_00": "Kaito", "SPEAKER_01": "Mina"},
     "film2": {"SPEAKER_00": "Mina",  "SPEAKER_03": "Kaito"}}

Profiles come from film 1 alone; film 2's lines are only compared with them,
so the result measures this pair of films. Nothing is translated, cloned or
spoken, and the server is not needed.
"""

from __future__ import annotations

import json
from pathlib import Path

from .. import voice_study
from .base import ApiError, Ctx


def table(result: dict) -> list[str]:
    labels = result["matrix"]["labels"]
    width = max([len(x) for x in labels] + [10])
    head = "".join(f"{x[:9]:>10}" for x in [*labels, "abstained"])
    out = [f"{result['lines']} lines of film 2: top-1 {result['top1']}, "
           f"precision {result['precision']} at coverage {result['coverage']} "
           f"(floor {result['floor']}, margin {result['margin']})", "",
           "true / said".ljust(width) + head]
    for label, row in zip(labels, result["matrix"]["counts"], strict=True):
        out.append(f"{label:{width}s}" + "".join(f"{n:10d}" for n in row))
    out += ["", f"{'character':{width}s}  precision  recall    f1  lines  abstained"]
    for label, m in result["per_character"].items():
        cells = [("-" if m[k] is None else f"{m[k]:.2f}") for k in
                 ("precision", "recall", "f1")]
        out.append(f"{label:{width}s}  {cells[0]:>9}  {cells[1]:>6}  {cells[2]:>4}  "
                   f"{m['support']:5d}  {m['abstained']:9.2f}")
    if result["unseen"]:
        out += ["", "not in film 1, so always wrong or abstained: " + ", ".join(result["unseen"])]
    return out


def cmd_voice_study(ctx: Ctx) -> int:
    a = ctx.args
    try:
        names = json.loads(Path(a.names).read_text(encoding="utf-8"))
        work = ctx.config.work_dir
        result = voice_study.run(voice_study.read(a.film1, work), names.get("film1") or {},
                                 voice_study.read(a.film2, work), names.get("film2") or {},
                                 floor=a.floor, margin=a.margin, use_prototypes=a.prototypes)
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        raise ApiError(f"cannot read the names file {a.names}: {exc}") from None
    except voice_study.StudyError as exc:
        raise ApiError(str(exc)) from None
    ctx.result(result, table(result))
    return 0


def add_parsers(sub, common) -> None:
    p = sub.add_parser("voice-study", parents=[common],
                       help="test voice profiles from one film on another, held out")
    steps = p.add_subparsers(dest="study_command", required=True)
    r = steps.add_parser("run", parents=[common], help="profiles from film 1, probe film 2")
    r.add_argument("film1", help="analysed media file or its .speakers.json")
    r.add_argument("film2", help="analysed media file or its .speakers.json")
    r.add_argument("--names", required=True, help='JSON: {"film1": {group: name}, "film2": ...}')
    r.add_argument("--floor", type=float, default=0.3, help="lowest similarity to answer")
    r.add_argument("--margin", type=float, default=0.05, help="lead over the runner-up")
    r.add_argument("--prototypes", action="store_true", help="match against several prototypes")


COMMANDS = {"voice-study": cmd_voice_study}

"""Is this machine ready to dub? Every service and tool a run needs, checked.

    doblarr doctor

PASS means ready, WARN means a run works but something is off (a slower
stretcher, little free memory), FAIL means a run would stop part-way.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.request
from pathlib import Path

from .base import Ctx


def _row(name: str, level: str, text: str) -> dict:
    return {"check": name, "level": level, "text": text}


def server(ctx: Ctx) -> dict:
    try:
        status = ctx.api.get("/api/status")
    except Exception as exc:  # noqa: BLE001 - every failure is a FAIL here
        return _row("server", "FAIL", str(exc))
    if status.get("queue_paused"):
        return _row("server", "WARN", f"{ctx.api.base} is up but its queue is paused")
    return _row("server", "PASS", f"{ctx.api.base} is up")


def speech(ctx: Ctx) -> dict:
    from ..clients.speech import SpeechError, build_speech_client

    client = build_speech_client(ctx.config)
    try:
        health = client.health(timeout=10)
    except (SpeechError, OSError) as exc:
        why = "connection refused" if "refused" in str(exc) else str(exc)[:80]
        return _row("speech", "FAIL", f"{client.service} at {client.base_url} is not "
                                      f"answering ({why}); start it before dubbing")
    gpu = health.get("gpu_type") or ("GPU" if health.get("gpu_available") else "CPU only")
    return _row("speech", "PASS", f"{client.service} at {client.base_url} ({gpu})")


def ollama_models(endpoint: str) -> list[str]:
    with urllib.request.urlopen(endpoint.rstrip("/") + "/api/tags", timeout=10) as resp:
        return [m["name"] for m in json.load(resp).get("models", [])]


def translator(ctx: Ctx) -> dict:
    settings = ctx.config.get("translate", {}) or {}
    provider = str(settings.get("provider") or "")
    model = str(settings.get("model") or "")
    if provider == "claude":
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY"):
            return _row("translator", "PASS", f"claude {model}")
        return _row("translator", "FAIL", "translate.provider is claude but neither "
                    "ANTHROPIC_API_KEY nor CLAUDE_API_KEY is set; set one or `doblarr settings set "
                    "translate.provider=prompture translate.model=ollama/<model>`")
    if provider == "prompture" and model.startswith("ollama/"):
        endpoint = str(settings.get("endpoint") or os.environ.get("OLLAMA_HOST")
                       or "http://127.0.0.1:11434")
        if not endpoint.startswith("http"):
            endpoint = "http://" + endpoint
        try:
            names = ollama_models(endpoint)
        except OSError as exc:
            return _row("translator", "FAIL", f"Ollama at {endpoint} is not answering ({exc})")
        wanted = model.split("/", 1)[1]
        if wanted not in names and f"{wanted}:latest" not in names:
            return _row("translator", "FAIL", f"Ollama has no {wanted}; `ollama pull {wanted}`")
        return _row("translator", "PASS", f"{model} via {endpoint}")
    if provider == "passthrough":
        return _row("translator", "WARN", "translate.provider is passthrough: nothing is "
                                          "translated")
    return _row("translator", "PASS", f"{provider} {model}")


def ffmpeg() -> list[dict]:
    from .. import stretch

    rows = []
    for tool in ("ffmpeg", "ffprobe"):
        rows.append(_row(tool, "PASS" if shutil.which(tool) else "FAIL",
                         shutil.which(tool) or f"{tool} is not on PATH"))
    if shutil.which("ffmpeg"):
        rows.append(_row("rubberband", "PASS" if stretch._has_rubberband() else "WARN",
                         "ffmpeg has the Rubber Band stretcher" if stretch._has_rubberband()
                         else "ffmpeg lacks librubberband; timing.stretcher falls back to "
                              "atempo"))
    return rows


def gpu() -> dict:
    if not shutil.which("nvidia-smi"):
        return _row("gpu", "WARN", "no NVIDIA GPU found; transcription and voices run on CPU")
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True,
                             text=True, timeout=15).stdout.strip().splitlines()[0]
        name, used, total = [x.strip() for x in out.split(",")]
    except (OSError, IndexError, ValueError, subprocess.SubprocessError) as exc:
        return _row("gpu", "WARN", f"nvidia-smi did not answer ({exc})")
    free = (int(total) - int(used)) / 1024
    level = "PASS" if free >= 6 else "WARN"
    hint = ""
    if level == "WARN":
        loaded = ollama_loaded()
        hint = ("; Ollama holds " + ", ".join(loaded) + " (`ollama stop MODEL`)" if loaded
                else "; unload voice or language models before a run")
    return _row("gpu", level, f"{name}: {free:.1f} GB free of {int(total) / 1024:.0f} GB"
                + hint)


def ollama_loaded(endpoint: str = "http://127.0.0.1:11434") -> list[str]:
    """Models Ollama keeps in memory right now, with their size."""
    try:
        with urllib.request.urlopen(endpoint + "/api/ps", timeout=5) as resp:
            models = json.load(resp).get("models", [])
    except (OSError, ValueError):
        return []
    return [f"{m['name']} {m.get('size_vram', m.get('size', 0)) / 1024 ** 3:.0f} GB"
            for m in models]


def disk(path: Path, name: str) -> dict:
    target = path if path.exists() else path.parent
    free = shutil.disk_usage(target).free / 1024 ** 3
    return _row(name, "PASS" if free >= 20 else "WARN" if free >= 5 else "FAIL",
                f"{free:.0f} GB free at {path}")


def checks(ctx: Ctx) -> list[dict]:
    rows = [server(ctx), speech(ctx), translator(ctx), *ffmpeg(), gpu(),
            disk(Path(ctx.config.work_dir), "work disk"),
            disk(Path(ctx.config.output_dir), "output disk")]
    mode = (ctx.config.get("translate", {}) or {}).get("published_dub", "follow")
    rows.append(_row("published dub", "PASS" if mode != "off" else "WARN",
                     f"translate.published_dub = {mode}"))
    return rows


def cmd_doctor(ctx: Ctx) -> int:
    rows = checks(ctx)
    ctx.result(rows, [f"  {r['level']:4s}  {r['check']:<14} {r['text']}" for r in rows])
    return 1 if any(r["level"] == "FAIL" for r in rows) else 0


def add_parsers(sub, common) -> None:
    sub.add_parser("doctor", parents=[common], help="check every service and tool a dub needs")


COMMANDS = {"doctor": cmd_doctor}

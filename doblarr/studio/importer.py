"""Bring earlier listening experiments into the studio without redoing any of them.

The Harbor Lights comparison and voice audition were built by scripts before the
studio existed. Their files stay exactly where they are; importing reads their
manifests, shows what is there and what is missing, and asks a person to map
each legacy identity (a speaker label, a source track) to a studio identity
before anything is recorded. Nothing is generated, re-rendered or moved.

Two things an import cannot do, and says so:

- A manifest records what was rendered, not what anybody concluded. The old
  page kept judgments in the browser's local storage; only a results file that
  was exported from that browser carries them, and only that can be imported.
- Experimental settings in an old comparison (a processing variant, a clone
  approach) are kept as the experiment's own settings. They do not become the
  product's defaults by being imported.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..artifacts import digest, stamp
from ..errors import DoblarrError, ForbiddenError
from ..fileio import inside
from . import records

MAX_MANIFEST_BYTES = 20 * 1024 * 1024
MAX_RESULTS_BYTES = 1024 * 1024
KNOWN_COMPARISON_VERSIONS = (1, 2)


class ImportError_(DoblarrError):
    http_status = 422


def _resolve(value: str | None, base: Path, repo: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    # Legacy manifests store paths relative to the repository the script ran in.
    for root in (repo, base):
        candidate = root / path
        if candidate.exists():
            return candidate
    return repo / path


def _read(path: Path, roots: list[Path]) -> dict:
    if not inside(path, roots):
        raise ForbiddenError("imports are read only from inside the configured work or "
                             "output directories")
    if not path.is_file():
        raise ImportError_(f"{path.name} does not exist")
    if path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ImportError_(f"{path.name} is larger than an experiment manifest should be")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ImportError_(f"{path.name} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ImportError_(f"{path.name} is not a manifest")
    return data


def preview(path: Path, roots: list[Path], repo: Path) -> dict:
    """Read-only: what a legacy artifact contains and what it would need mapped."""
    path = Path(path)
    data = _read(path, roots)
    base = path.parent
    missing: list[str] = []
    outside: list[str] = []

    def check(value) -> dict:
        found = _resolve(value, base, repo)
        if found is None:
            return {"name": "", "exists": False}
        ok = inside(found, roots)
        exists = ok and found.is_file()
        if not ok:
            outside.append(found.name)
        elif not exists:
            missing.append(str(value))
        return {"name": found.name, "exists": exists}

    if "comparison_id" in data and "scenes" in data:
        version = data.get("manifest_version")
        if version not in KNOWN_COMPARISON_VERSIONS:
            raise ImportError_(f"comparison manifest version {version!r} is not one this "
                               "Doblarr can read")
        source = data.get("source") or {}
        media = _resolve(source.get("media"), base, repo)
        media_state = ("missing" if media is None or not media.is_file() else
                       "modified" if stamp(media) != source.get("stamp") else "unchanged")
        speakers = sorted({sp for s in data["scenes"] for sp in s["scene"].get("speakers", [])})
        tracks = [{"key": f"stream-{t.get('audio_index')}", "language": t.get("language"),
                   "title": t.get("title", ""),
                   "legacy_role": ("original" if t.get("audio_index")
                                   == (source.get("stream") or {}).get("audio_index")
                                   else "reference dub")}
                  for t in (source.get("stream") or {}).get("available", [])]
        scenes = []
        for s in data["scenes"]:
            scene = s["scene"]
            scenes.append({
                "index": scene["index"], "title": scene.get("title", ""),
                "window": scene.get("window"), "duration": scene.get("duration"),
                "speakers": scene.get("speakers", []), "lines": len(scene.get("text", [])),
                "source": check(s.get("source_excerpt")),
                "references": [{"language": r.get("language"), "role": r.get("role"),
                                **check(r.get("path"))} for r in s.get("references", [])],
                "variants": [{"name": v["name"], "mixed": check(v.get("mixed")),
                              "matched": check(v.get("matched"))}
                             for v in s.get("variants", [])],
            })
        body = {"kind": "comparison", "legacy_id": data["comparison_id"],
                "labels": data.get("labels") or {}, "variants": data.get("variants") or {},
                "shared_takes": data.get("shared_takes", True),
                "media": {"name": media.name if media else "", "state": media_state},
                "speakers": speakers, "tracks": tracks, "scenes": scenes,
                "honesty": data.get("honesty") or []}
    elif "approaches" in data and "source_manifest" in data:
        approaches = []
        for name, row in (data.get("approaches") or {}).items():
            takes = _resolve(row.get("takes"), base, repo)
            lines = [k for k in row if k.isdigit()]
            present = 0
            if takes is not None and inside(takes, roots):
                present = sum(1 for k in lines if (takes / f"line_{int(k):04d}.wav").is_file())
            if takes and not inside(takes, roots):
                outside.append(takes.name)
            approaches.append({"name": name, "lines": len(lines), "present": present,
                               "missing": len(lines) - present,
                               "flagged": sum(1 for k in lines
                                              if not (row.get(k) or {}).get("clean", True)),
                               "base": row.get("base"), "speaker": row.get("speaker"),
                               "reference": row.get("reference")})
            if present < len(lines):
                missing.append(f"{len(lines) - present} take(s) of {name}")
        refs = []
        for speaker, row in (data.get("character_references") or {}).items():
            found = base / "references" / f"{speaker}.wav"
            refs.append({"speaker": speaker, "line": row.get("line"),
                         "seconds": row.get("seconds"),
                         "voice_over_bed_db": row.get("voice_over_bed_db"),
                         "exists": found.is_file()})
            if not found.is_file():
                missing.append(f"reference for {speaker}")
        body = {"kind": "voice_audition", "legacy_id": base.name,
                "speakers": sorted({r["speaker"] for r in refs}
                                   | {a["speaker"] for a in approaches if a["speaker"]}),
                "approaches": approaches, "references": refs,
                "profiles_note": ("Voice profile ids in this record belong to the voicebox "
                                  "server of the time; they are listed, not assumed to exist."),
                "directions": len(data.get("directions") or {})}
    else:
        raise ImportError_("this is neither a comparison manifest nor a voice audition record")
    body.update(
        manifest=str(path.resolve()), manifest_sha=digest(data)[:24],
        missing=missing, outside=outside,
        import_id="imp-" + digest([str(path.resolve()), digest(data)])[:12],
        generates="nothing: importing reads files and records a mapping; no speech, render "
                  "or recognition runs",
        judgments=("A manifest does not contain listening judgments. The old page kept them "
                   "in the browser's local storage; import an exported results file to "
                   "bring them in."))
    return body


def apply(db, session: str, found: dict, mapping: dict, actor: str = "") -> dict:
    """Record an import after every legacy identity has been mapped explicitly."""
    speakers = mapping.get("speakers") or {}
    unmapped = [s for s in found.get("speakers", []) if not str(speakers.get(s) or "").strip()]
    if unmapped:
        raise ImportError_("map every legacy speaker first: " + ", ".join(unmapped))
    if found["kind"] == "comparison":
        tracks = mapping.get("tracks") or {}
        unmapped = [t["key"] for t in found.get("tracks", []) if t["key"] not in tracks]
        if unmapped:
            raise ImportError_("say what each legacy source track is: " + ", ".join(unmapped))
    existing = records.get(db, "import", found["import_id"])
    if existing and existing.get("manifest_sha") == found["manifest_sha"]:
        return {**existing, "repeated": True}
    return records.put(db, "import", found["import_id"], {
        **found, "session": session, "mapping": mapping, "actor": actor,
        "experimental_settings": found.get("variants") or {},
        "settings_note": ("These are the settings this experiment compared. They are not "
                          "product defaults and are not applied to new runs."),
        "judgments_imported": []}, scope=session)


_SCENE = re.compile(r"^## Scene (\d+) — (.*?)(?: \(episode .*\))?$")
_BEST = re.compile(r"^- Best: \*\*(.*)\*\*$")
_FIELD = re.compile(r"^- (Issues|Note): (.*)$")
_ROW = re.compile(r"^\| (\d+:\d{2}\.\d) \| (.*?) \| (.*?) \| (.*?) \| (.*?) \| (.*?) \|$")


def parse_results(text: str) -> dict:
    """Judgments from a results file the old listening page exported."""
    if len(text.encode()) > MAX_RESULTS_BYTES:
        raise ImportError_("that results file is too large to be a listening export")
    lines = text.splitlines()
    if not lines or not lines[0].startswith("# Listening results — "):
        raise ImportError_("this is not a results file exported by the listening page")
    legacy_id = lines[0].split("— ", 1)[1].strip()
    scenes: list[dict] = []
    current: dict | None = None
    for line in lines[1:]:
        line = line.rstrip()
        if (m := _SCENE.match(line)):
            current = {"scene": int(m.group(1)) - 1, "title": m.group(2), "verdict": "",
                       "issues": [], "note": "", "marks": []}
            scenes.append(current)
        elif current is None:
            continue
        elif (m := _BEST.match(line)):
            current["verdict"] = "" if m.group(1) == "(not answered)" else m.group(1)
        elif (m := _FIELD.match(line)):
            value = m.group(2).strip()
            if m.group(1) == "Issues":
                current["issues"] = [] if value == "(none marked)" else [
                    v.strip() for v in value.split(",") if v.strip()]
            else:
                current["note"] = "" if value == "(none)" else value[:2000]
        elif (m := _ROW.match(line)) and m.group(1) != "At":
            minutes, rest = m.group(1).split(":")
            current["marks"].append({
                "at": int(minutes) * 60 + float(rest), "version": m.group(2).strip(),
                "what": [] if m.group(3).strip() == "—" else
                        [v.strip() for v in m.group(3).split(",")],
                "severity": m.group(4).strip(), "in_original": m.group(5).strip("— ").strip(),
                "note": m.group(6).strip()[:2000]})
        if len(scenes) > 200 or (current and len(current["marks"]) > 500):
            raise ImportError_("that results file has more rows than an export can")
    return {"legacy_id": legacy_id, "scenes": scenes,
            "marks": sum(len(s["marks"]) for s in scenes),
            "answered": sum(1 for s in scenes if s["verdict"])}


def attach_results(db, import_record: dict, parsed: dict, source_name: str) -> dict:
    """Store exported judgments with the import they belong to."""
    if parsed["legacy_id"] != import_record.get("legacy_id"):
        raise ImportError_(f"these results are for {parsed['legacy_id']}, not "
                           f"{import_record.get('legacy_id')}")
    key = digest(parsed)[:16]
    already = [j for j in import_record.get("judgments_imported") or [] if j["key"] == key]
    if already:
        return import_record
    entry = {"key": key, "file": source_name, "scenes": parsed["scenes"],
             "marks": parsed["marks"], "answered": parsed["answered"],
             "provenance": "imported from a results file exported by the listening page"}
    return records.update(db, "import", import_record["id"], {
        "judgments_imported": list(import_record.get("judgments_imported") or []) + [entry]})

"""Move name-keyed memory onto canonical identities, after a person has seen how.

Older Doblarr kept four kinds of memory under names:

- ``speaker-names:<file name>``: which voice group of an episode is which
  character, plus lines a person moved by hand;
- ``voice-tags:<folder name>``: each named character's voice print per show;
- ``voice-traits:<voice key>``: who a catalogue voice is (character, show);
- voice casts keyed by the media path a run used.

None of these names is an identity, so the migration never guesses. ``preview``
lists one action per legacy key with the evidence for it and a state:

- ``ready``: exactly one canonical target, reachable now;
- ``ambiguous``: several targets fit (two shows filed under one folder name,
  two files with one name); left alone until a person picks;
- ``unreachable``: the file it points at cannot be read here;
- ``applied``: already migrated, nothing to do.

``apply`` takes the preview's fingerprint. If anything changed in between (a
new name, a new file, another migration), the fingerprint differs and nothing
is written. Applying twice writes nothing the second time. Legacy rows are
kept: older routes and saved jobs still read them.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path

from . import identity, voice_tags
from .artifacts import digest
from .studio import records

log = logging.getLogger("doblarr.identity_migration")

MIGRATION_ID = "legacy-names/1"


class StalePreview(ValueError):
    """The legacy state changed since the preview a person approved."""


def _plans(db, prefix: str) -> list[dict]:
    rows = db.query("SELECT title_key, title, plan, updated_at FROM title_plans "
                    "WHERE title_key LIKE ? ORDER BY title_key", (prefix + "%",))
    out = []
    for row in rows:
        try:
            plan = json.loads(row["plan"] or "{}")
        except ValueError:
            plan = {}
        out.append({"key": row["title_key"], "title": row["title"], "plan": plan,
                    "updated_at": row["updated_at"]})
    return out


def _script_inputs(work_dir: Path) -> list[str]:
    """Every media path a saved script says it was made from."""
    found: list[str] = []
    media = Path(work_dir) / "media"
    if not media.is_dir():
        return found
    for script in media.glob("*/**/*.script.json"):
        try:
            data = json.loads(script.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        source = str((data.get("identity") or {}).get("input") or "")
        if source:
            found.append(source)
    return found


def _known_paths(db, work_dir: Path) -> list[str]:
    paths = list(_script_inputs(work_dir))
    for row in db.query("SELECT input_file FROM jobs WHERE input_file IS NOT NULL"):
        paths.append(row["input_file"])
    row = db.query_one("SELECT items FROM scan_state WHERE id = 1")
    if row is not None:
        with contextlib.suppress(ValueError):
            paths += [i["path"] for i in json.loads(row["items"] or "[]")
                      if isinstance(i, dict) and i.get("path")]
    unique: dict[str, str] = {}
    for path in paths:
        unique.setdefault(identity.norm_path(path), path)
    return list(unique.values())


def _by_name(paths: list[str], name: str) -> list[str]:
    wanted = name.casefold()
    return [p for p in paths if Path(str(p).replace("\\", "/")).name.casefold() == wanted]


def _by_folder(paths: list[str], folder: str) -> list[str]:
    wanted = folder.casefold()
    return [p for p in paths if identity.show_folder(p).name.casefold() == wanted]


def preview(db, work_dir: Path, *, cache_dir: Path | None = None) -> dict:
    """Every legacy key, what it would become, and why. Writes nothing."""
    paths = _known_paths(db, work_dir)
    actions: list[dict] = []
    basis: list = []
    resolved_series: dict[str, str] = {}   # show folder (folded) -> series id

    def resolve_path(path: str) -> dict | None:
        if not Path(path).is_file():
            return None
        try:
            hints = identity.library_hints(db, path)
            content = identity.content_identity(Path(path), cache_dir)
            series_id, _facts = identity.series_id_for(path, hints)
            known = identity.find_media_by_content(db, content["content_key"])
            return {"path": path, "series_id": known.get("series_id") if known else series_id,
                    "revision_id": f"rev-{content['content_key']}",
                    "media_id": known["id"] if known else None}
        except OSError:
            return None

    for plan in _plans(db, "speaker-names:"):
        name = plan["key"].removeprefix("speaker-names:")
        basis.append([plan["key"], digest(plan["plan"])])
        matches = sorted({identity.norm_path(p): p for p in _by_name(paths, name)}.values())
        names = dict(plan["plan"].get("names") or {})
        lines = dict(plan["plan"].get("lines") or {})
        action = {"key": plan["key"], "kind": "names", "names": names, "lines": lines,
                  "candidates": matches}
        if len(matches) > 1:
            action.update(state="ambiguous",
                          reason=f"{len(matches)} files share this name; pick the right one")
        elif not matches:
            action.update(state="orphan", reason="no known file has this name")
        else:
            target = resolve_path(matches[0])
            if target is None:
                action.update(state="unreachable",
                              reason="the file cannot be read here to identify it")
            else:
                action.update(target)
                done = _names_applied(db, target, names, lines)
                action["state"] = "applied" if done else "ready"
                resolved_series[identity.show_folder(matches[0]).name.casefold()] = \
                    target["series_id"]
        actions.append(action)

    for plan in _plans(db, voice_tags.KEY):
        folder = plan["key"].removeprefix(voice_tags.KEY)
        basis.append([plan["key"], digest(plan["plan"])])
        folders = {identity.norm_path(identity.show_folder(p)): identity.show_folder(p)
                   for p in _by_folder(paths, folder)}
        action = {"key": plan["key"], "kind": "prints",
                  "characters": sorted({n for by_ep in (plan["plan"].get("models") or {})
                                        .values() for sums in by_ep.values() for n in sums}),
                  "candidates": [str(f) for f in folders.values()]}
        series = {resolved_series.get(folder.casefold())} - {None}
        if not series and len(folders) == 1:
            sample = _by_folder(paths, folder)[0]
            series_id, _facts = identity.series_id_for(sample,
                                                       identity.library_hints(db, sample))
            series = {series_id}
        if len(folders) > 1:
            action.update(state="ambiguous",
                          reason=f"{len(folders)} show folders are called '{folder}'")
        elif not series:
            action.update(state="orphan", reason="no known show folder has this name")
        else:
            action["series_id"] = next(iter(series))
            action["state"] = ("applied" if _prints_applied(db, action["series_id"], plan)
                               else "ready")
        actions.append(action)

    for plan in _plans(db, "voice-traits:"):
        traits = plan["plan"]
        show = str(traits.get("show") or "")
        character = str(traits.get("character") or "").strip()
        basis.append([plan["key"], digest(plan["plan"])])
        action = {"key": plan["key"], "kind": "traits", "voice": plan["key"][13:],
                  "character": character, "show": show}
        if not show or not character:
            action.update(state="skipped", reason="not tied to a show and a character")
        elif not (show.startswith("tvdb-") and show[5:].isdigit()):
            action.update(state="ambiguous", reason=f"show '{show}' is not a provider id")
        else:
            action["series_id"] = f"show:tvdb:{int(show[5:])}"
            action["state"] = ("applied" if _traits_applied(db, action["series_id"],
                                                            character, action["voice"])
                               else "ready")
        actions.append(action)

    # A local series (identified by its folder path) that shares character
    # names with a provider series is probably that series. Never linked on
    # its own: the action waits for a person to confirm it by key.
    local_names: dict[str, set[str]] = {}
    for action in actions:
        series_id = str(action.get("series_id") or "")
        if action["kind"] == "names" and series_id.startswith(("show:local:", "movie:local:")):
            local_names.setdefault(series_id, set()).update(
                n.casefold() for n in action["names"].values())
    provider_names: dict[str, set[str]] = {}
    for action in actions:
        if action["kind"] == "traits" and action.get("series_id"):
            provider_names.setdefault(action["series_id"], set()).add(
                action["character"].casefold())
    for local, shared in sorted(local_names.items()):
        if identity.canonical_series(db, local) != local:
            continue
        overlaps = sorted(((len(shared & theirs), target) for target, theirs in
                           provider_names.items() if shared & theirs), reverse=True)
        if not overlaps:
            continue
        key = f"series-link:{local}"
        basis.append([key, overlaps[0][1]])
        state = ("confirm" if len(overlaps) == 1 or overlaps[0][0] > overlaps[1][0]
                 else "ambiguous")
        actions.append({"key": key, "kind": "series_link", "series_id": local,
                        "target": overlaps[0][1], "shared": overlaps[0][0], "state": state,
                        "reason": f"{overlaps[0][0]} character name(s) in common with "
                                  f"{overlaps[0][1]}; confirm to treat them as one series"})

    for row in db.list_casts():
        key = str(row["title_key"])
        if key.startswith(("series:", "tmdb:", "tvdb:", "title:")):
            continue
        cast = [e for e in row["cast"] if e.get("voice")]
        basis.append(["cast:" + key, digest(cast)])
        action = {"key": "cast:" + key, "kind": "cast", "path": key,
                  "entries": [{"speaker": e.get("speaker_id"), "voice": e.get("voice")}
                              for e in cast]}
        if not cast:
            action.update(state="skipped", reason="no voices chosen in this cast")
        else:
            target = resolve_path(key)
            if target is None:
                action.update(state="unreachable",
                              reason="the cast's file cannot be read here to identify it")
            else:
                links = identity.cluster_characters(db, target["revision_id"])
                linked = [e for e in action["entries"] if e["speaker"] in links]
                action.update(target)
                action["characters"] = {e["speaker"]: links[e["speaker"]]["name"]
                                        for e in linked}
                if not linked:
                    action.update(state="unlinked",
                                  reason="none of its voice groups is identified as a "
                                         "character yet; name them in the analysis first")
                else:
                    action["state"] = ("applied" if _cast_applied(db, target["series_id"],
                                                                  action) else "ready")
        actions.append(action)

    fingerprint = digest([MIGRATION_ID, basis,
                          [[a["key"], a["state"], a.get("series_id"), a.get("revision_id")]
                           for a in actions]])[:20]
    counts: dict[str, int] = {}
    for action in actions:
        counts[action["state"]] = counts.get(action["state"], 0) + 1
    return {"migration": MIGRATION_ID, "fingerprint": fingerprint, "counts": counts,
            "actions": actions}


def _names_applied(db, target: dict, names: dict, lines: dict) -> bool:
    links = {r["ref"]: r for r in identity.associations(db, target["revision_id"])}
    for label, name in names.items():
        row = links.get(label)
        character = identity.resolve_character(db, row["character_id"]) if row and \
            row.get("character_id") else None
        if character is None or identity._fold(character["name"]) != identity._fold(name):
            return False
    return all(cue in links for cue in lines)


def _prints_applied(db, series_id: str, plan: dict) -> bool:
    saved = (db.load_plan(voice_tags.series_key(series_id)) or {}).get("plan") or {}
    return digest(plan["plan"]) in (saved.get("migrated_from") or [])


def _traits_applied(db, series_id: str, character: str, voice: str) -> bool:
    found = identity.find_character(db, series_id, character)
    if found is None:
        return False
    asset = records.get(db, "voice_asset", voice) or {}
    return found["id"] in (asset.get("characters") or [])


def _cast_applied(db, series_id: str, action: dict) -> bool:
    from .studio import casting

    ref = casting.record_id("series", identity.casting_ref(series_id))
    chosen = (records.get(db, "casting", ref) or {}).get("characters") or {}
    return all(name in chosen for name in action["characters"].values())


def apply(db, work_dir: Path, fingerprint: str, *, cache_dir: Path | None = None,
          keys: list[str] | None = None) -> dict:
    """Apply the ready actions of a preview the caller has seen."""
    plan = preview(db, work_dir, cache_dir=cache_dir)
    if plan["fingerprint"] != fingerprint:
        raise StalePreview("the legacy names changed since this preview; preview again")
    applied = []
    # Confirmed series links go first, so names migrate into the right series.
    for action in plan["actions"]:
        if (action["kind"] == "series_link" and action["state"] == "confirm"
                and keys is not None and action["key"] in keys):
            identity.link_series(db, action["series_id"], action["target"])
            applied.append(action["key"])
    for action in plan["actions"]:
        if action["state"] != "ready" or (keys is not None and action["key"] not in keys):
            continue
        if action["kind"] == "names":
            _apply_names(db, action, cache_dir)
        elif action["kind"] == "prints":
            _apply_prints(db, action)
        elif action["kind"] == "traits":
            _apply_traits(db, action)
        elif action["kind"] == "cast":
            _apply_cast(db, action)
        applied.append(action["key"])
    records.put(db, "migration", MIGRATION_ID.replace("/", "-"), {
        "fingerprint": fingerprint, "applied": applied, "at": records.now_marker()})
    log.info("identity migration: applied %d legacy key(s)", len(applied))
    return {"applied": applied, "after": preview(db, work_dir, cache_dir=cache_dir)["counts"]}


def _apply_names(db, action: dict, cache_dir: Path | None) -> None:
    resolved = identity.resolve(db, action["path"], cache_dir=cache_dir)
    resolved["series_id"] = identity.canonical_series(db, resolved["series_id"])
    series_id, revision_id = resolved["series_id"], resolved["revision_id"]
    for label, name in sorted(action["names"].items()):
        character = identity.ensure_character(db, series_id, name, origin="migration")
        identity.associate(db, revision_id, "cluster", label, character["id"], state="manual",
                           evidence=[{"kind": "legacy", "key": action["key"]}])
    for cue, name in sorted(action["lines"].items()):
        found = identity.ensure_character(db, series_id, name, origin="migration") \
            if name else None
        identity.associate(db, revision_id, "line", cue, found["id"] if found else None,
                           state="manual", evidence=[{"kind": "legacy", "key": action["key"]}])


def _apply_prints(db, action: dict) -> None:
    legacy = (db.load_plan(action["key"]) or {}).get("plan") or {}
    series_id = action["series_id"]
    key = voice_tags.series_key(series_id)
    saved = (db.load_plan(key) or {}).get("plan") or {}
    partitions = dict(saved.get("partitions") or {})
    for model, by_episode in (legacy.get("models") or {}).items():
        # The legacy store has no source language; it is recorded as unknown
        # rather than assumed, so it never mixes with a known-language print.
        part = dict(partitions.get(voice_tags.partition(model, "")) or {})
        for episode, sums in by_episode.items():
            converted = {}
            for name, entry in sums.items():
                character = identity.ensure_character(db, series_id, name, origin="migration")
                converted[character["id"]] = entry
            part["legacy:" + episode] = converted
        partitions[voice_tags.partition(model, "")] = part
    saved.update(version=2, partitions=partitions,
                 migrated_from=[*(saved.get("migrated_from") or []), digest(legacy)])
    db.save_plan(key, series_id, saved)


def _apply_traits(db, action: dict) -> None:
    character = identity.ensure_character(db, action["series_id"], action["character"],
                                          origin="migration")
    current = records.get(db, "voice_asset", action["voice"]) or {}
    linked = list(dict.fromkeys([*(current.get("characters") or []), character["id"]]))
    records.put(db, "voice_asset", action["voice"], {**current, "characters": linked},
                base_revision=current.get("revision", 0))


def _apply_cast(db, action: dict) -> None:
    """A title's chosen voices become series choices for identified characters,
    only where the character has no series choice yet (an existing one wins)."""
    from .studio import casting

    series_ref = identity.casting_ref(action["series_id"])
    chosen = (records.get(db, "casting", casting.record_id("series", series_ref))
              or {}).get("characters") or {}
    for entry in action["entries"]:
        name = action["characters"].get(entry["speaker"])
        if not name or name in chosen:
            continue
        found = identity.find_character(db, action["series_id"], name)
        casting.decide(db, casting.ChoiceIn(
            character=name, scope="series", scope_ref=series_ref, voice=entry["voice"],
            character_id=found["id"] if found else ""))
        chosen[name] = True

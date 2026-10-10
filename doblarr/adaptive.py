"""Recommend, decide and prepare envelopes and the bed policy for one run.

Runs after timing (the takes are fitted) and before the level owner renders:

1. measure each line's original performance (source spans of the separated
   dialogue) and its fitted take — the take as it will be heard;
2. collect what else is known: speech mode, ending, traits, level band, the
   identified character, accepted narrative context, visual screen state;
3. rank templates by retrieval (doblarr.retrieval) over the shared catalogue
   and approved examples, with hard filters first;
4. build one bounded packet per line and let the configured judge choose
   (doblarr.judge), or abstain; a hand-chosen envelope is a lock and is not
   asked about;
5. write the chosen envelope onto the cue (`seg.envelope`) and the full
   recommendation (candidates, components, packet, decision) beside the
   script, plus one decision record per run.

`adaptive.mode`: ``off`` does nothing (hand-chosen envelopes still apply);
``suggest`` records recommendations and marks them as suggestions without
rendering; ``apply`` lets the level owner render them. Nothing here generates
speech: a template or strength change only reprocesses existing takes.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import features, judge, retrieval, speaking, templates
from .artifacts import digest, read_json, stamp
from .cues import LEVELED, EnvelopeDecision
from .fileio import write_json
from .studio import records

log = logging.getLogger("doblarr.adaptive")

RECOMMENDER = "adaptive/1"


def settings(options: dict | None) -> dict:
    values = dict(options or {})
    return {"mode": str(values.get("mode", "off")),
            "judge": str(values.get("judge", "retrieval") or "retrieval"),
            "judge_endpoint": values.get("judge_endpoint"),
            "judge_budget": int(values.get("judge_budget", 400)),
            "candidates": max(1, min(8, int(values.get("candidates", 4)))),
            "envelope_strength": float(values.get("envelope_strength", 1.0)),
            "max_envelope_db": float(values.get("max_envelope_db", 6.0)),
            "background_policy": str(values.get("background_policy") or ""),
            "max_bed_attenuation_db": float(values.get("max_bed_attenuation_db", 18.0)),
            "lines": {str(k): dict(v) for k, v in (values.get("lines") or {}).items()}}


def source_features(job, work: Path, cancel=None) -> dict[str, dict]:
    """The original performance of every line, from its source spans."""
    from .levels import dialogue_stream

    stream, origin, contaminated = dialogue_stream(job)
    if stream is None:
        return {}
    spans = []
    for seg in job.segments:
        if seg.source.spans:
            spans.append((min(s.start for s in seg.source.spans),
                          max(s.end for s in seg.source.spans)))
        else:
            spans.append((seg.start, seg.end))
    from .stages.common import work_stem

    path = Path(work) / f"{work_stem(job)}.source-features.json"
    request = digest([features.FEATURES, stamp(stream), spans])[:16]
    saved = read_json(path)
    if saved.get("request") == request:
        return saved["lines"]
    flags = [["contaminated"] if contaminated else [] for _ in spans]
    measured, _stats = features.measure_lines(stream, spans, flags=flags, cancel=cancel)
    lines = {seg.cue_id: row for seg, row in zip(job.segments, measured, strict=True)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"request": request, "source": origin, "lines": lines}),
                    encoding="utf-8")
    return lines


def take_features(job, work: Path) -> dict[str, dict]:
    """The fitted take of every line (the audio the level owner will read)."""
    from .stages.common import work_stem

    path = Path(work) / f"{work_stem(job)}.take-features.json"
    saved = read_json(path).get("lines") or {}
    out, changed = {}, False
    for seg in job.segments:
        upstream = seg.audio.upstream_of(LEVELED)
        if upstream is None or not upstream.exists():
            continue
        key = upstream.fingerprint or digest(stamp(Path(upstream.path)))[:16]
        cached = saved.get(seg.cue_id)
        if cached and cached.get("key") == key:
            out[seg.cue_id] = cached
            continue
        try:
            found = features.measure_file(Path(upstream.path))
        except (OSError, ValueError):
            continue
        out[seg.cue_id] = {**found, "key": key}
        changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": features.FEATURES, "lines": out}),
                        encoding="utf-8")
    return out


def _ending(seg) -> str:
    for finding in seg.findings:
        if finding.code == "decision_cutoffs" and finding.evidence.get("applied"):
            return str(finding.evidence.get("value") or "")
    from .decisions import cutoff_rule, spoken

    return cutoff_rule(spoken(seg)) or ""


def line_context(seg, *, character: dict | None, visual: dict | None,
                 claims: list[dict]) -> dict:
    words = []
    for claim in claims:
        if claim["kind"] in ("scene_intent", "speech_mode") and (
                not claim.get("subjects") or (character and f"character:{character['id']}"
                                              in claim["subjects"])):
            words += [w for w in claim["statement"].lower().split() if len(w) > 4][:4]
    return {"cue": seg.cue_id, "text": seg.text_src, "translated": seg.text_translated,
            "duration": seg.duration, "speaker_name": character["name"] if character else "",
            "mode": seg.intent.mode, "traits": list(seg.intent.traits),
            "band": speaking.band(seg.measurement.relative_db), "ending": _ending(seg),
            "screen": (visual or {}).get("screen"), "scene": ((visual or {}).get("context")
                                                              or {}).get("scene"),
            "intent_words": words[:8]}


def selection_from(decision: dict, catalogue: dict[str, dict], *, origin: str,
                   locked: bool = False, edit: dict | None = None) -> EnvelopeDecision:
    """The cue's envelope record from a judge decision or a hand edit."""
    chosen = (decision or {}).get("decision") or {}
    template_id = (edit or {}).get("template") or chosen.get("candidate")
    template = catalogue.get(template_id or "")
    if template is None:
        return EnvelopeDecision(origin=origin if template_id else "none", locked=locked,
                                outcome="unavailable" if template_id else "unknown",
                                reason=f"template {template_id} is not in the catalogue"
                                if template_id else "no envelope chosen",
                                decision=(decision or {}).get("record", ""))
    requested = (edit or {}).get("strength", chosen.get("strength"))
    overrides = dict((edit or {}).get("params") or {})
    if requested is not None and "strength" in (template.get("params") or {}):
        overrides["strength"] = float(requested)
    resolved = templates.resolve_params(template, overrides)
    return EnvelopeDecision(template=templates.pin(template), params=resolved["values"],
                            origin=origin, locked=locked,
                            requested=float(overrides.get("strength", resolved["values"].get(
                                "strength", 0.0))),
                            clamps=resolved["clamps"],
                            outcome="preserved" if template["family"] == "preserve"
                            else "unknown",
                            reason=(decision or {}).get("reason", "")[:300],
                            decision=(decision or {}).get("record", ""))


def recommend(job, config, *, db=None, work: Path, cancel=None, progress=None,
              budget=None, guard=None, claims: list[dict] | None = None,
              characters: dict | None = None, visual: dict | None = None,
              examples: list[dict] | None = None) -> dict:
    """Choose an envelope for every line (see module docstring)."""
    options = settings(config.get("adaptive", {}))
    edits = {**options["lines"], **{str(k): dict(v) for k, v in
                                    (job.envelope_edits or {}).items()}}
    mode = options["mode"]
    started = time.perf_counter()
    if db is not None:
        templates.ensure_builtins(db)
        voice_catalogue = templates.listing(db, "voice")
    else:
        voice_catalogue = [{**templates.validate(t).model_dump(), "version": 1}
                           for t in templates.builtins() if t["kind"] == "voice"]
    by_id = {t["id"]: t for t in voice_catalogue}
    for edit in edits.values():
        if edit.get("template") and edit.get("version") and db is not None:
            pinned = templates.get(db, edit["template"], int(edit["version"]))
            if pinned:
                by_id[edit["template"]] = pinned
    if mode == "off" and not edits:
        return {"mode": "off", "lines": 0}
    judge_engine = judge.build(options["judge"] if mode != "off" else "retrieval",
                               budget=options["judge_budget"],
                               endpoint=options["judge_endpoint"], request_budget=budget,
                               guard=guard)
    sources = source_features(job, work, cancel) if mode != "off" else {}
    from .stages.common import work_stem

    # Model answers are kept by engine and packet: a resumed or repeated run
    # with the same evidence never asks the provider again.
    judge_cache_path = Path(work) / f"{work_stem(job)}.judge-cache.json"
    judge_cache = read_json(judge_cache_path) if judge_engine.is_model else {}
    cache_hits = 0
    takes = take_features(job, work)
    profiles_by_character: dict[str, dict] = {}
    report: dict = {}
    states: dict[str, int] = {}
    examples_used: dict[str, list[str]] = {}
    for n, seg in enumerate(job.segments):
        if cancel is not None and cancel.is_set():
            from .errors import JobCancelled

            raise JobCancelled("cancelled while recommending envelopes")
        take = takes.get(seg.cue_id)
        edit = edits.get(seg.cue_id)
        if edit and edit.get("clear"):
            seg.envelope = EnvelopeDecision(origin="manual", locked=True, outcome="preserved",
                                            reason="cleared by hand: rendered as generated")
            states["manual"] = states.get("manual", 0) + 1
            continue
        if take is None:
            seg.envelope = EnvelopeDecision(outcome="unavailable",
                                            reason="no fitted take to shape")
            continue
        if edit and edit.get("template"):
            seg.envelope = selection_from({"decision": {}, "reason": "chosen by hand"}, by_id,
                                          origin="manual", locked=True, edit=edit)
            states["manual"] = states.get("manual", 0) + 1
            report[seg.cue_id] = {"locked": True, "edit": edit}
            continue
        if mode == "off":
            continue
        character = (characters or {}).get(seg.speaker)
        profile = None
        if character and db is not None:
            from . import profiles

            if character["id"] not in profiles_by_character:
                profiles_by_character[character["id"]] = profiles.get(db, character["id"])
            profile = profiles_by_character[character["id"]]
        source = sources.get(seg.cue_id) or {"quality": "missing"}
        line = line_context(seg, character=character,
                            visual=(visual or {}).get(seg.cue_id), claims=claims or [])
        ranked = retrieval.rank(line, source, take, voice_catalogue, profile=profile,
                                examples=examples, limit=options["candidates"],
                                query={"cue": seg.cue_id,
                                       "revision_id": (job.metrics.get("identity") or {})
                                       .get("revision_id"),
                                       "target_lang": job.target_lang,
                                       "held_out": list(job.metrics.get("held_out") or [])})
        packet = judge.build_packet(
            line=line, source=source, take=take, ranked=ranked,
            narrative=[{"kind": c["kind"], "statement": c["statement"]} for c in
                       (claims or [])][:12],
            profile=profile, identity={"character": line["speaker_name"] or None,
                                       "grouping": seg.speaker},
            background=(take or {}).get("bed") or {},
            limits={"max_envelope_db": options["max_envelope_db"],
                    "envelope_strength": options["envelope_strength"]})
        cache_key = f"{judge_engine.engine}|{packet['fingerprint']}"
        if judge_engine.is_model and cache_key in judge_cache:
            decided = dict(judge_cache[cache_key])
            cache_hits += 1
        else:
            decided = judge_engine.decide(packet, ranked)
            if judge_engine.is_model and decided["source"] == "model":
                judge_cache[cache_key] = decided
        decided["record"] = digest([seg.cue_id, packet["fingerprint"], decided["decision"]])[:16]
        origin = {"model": "judge", "rule": "retrieval"}.get(decided["source"], "default")
        seg.envelope = selection_from(decided, by_id, origin=origin)
        if mode == "suggest" and seg.envelope.active:
            seg.envelope.reason = f"suggestion: {seg.envelope.reason}"[:300]
        states[decided["state"]] = states.get(decided["state"], 0) + 1
        used = [e.get("id") for e in examples or [] if e.get("template") in
                {c["template"]["id"] for c in ranked["candidates"]}]
        if used:
            examples_used[seg.cue_id] = [u for u in used if u]
        report[seg.cue_id] = {"candidates": ranked["candidates"],
                              "excluded": ranked["excluded"], "warnings": ranked["warnings"],
                              "latency_ms": ranked["latency_ms"], "packet": packet,
                              "decision": decided,
                              "take": {k: take.get(k) for k in ("quality", "duration",
                                                                 "range_db", "active_seconds")},
                              "source": {k: source.get(k) for k in ("quality", "duration",
                                                                     "range_db")}}
        if progress is not None and n % 20 == 0:
            progress(n, len(job.segments), f"line {n}/{len(job.segments)}")
    if judge_engine.is_model and judge_cache:
        write_json(judge_cache_path, judge_cache, indent=None, default=str)
    out = Path(work) / f"{work_stem(job)}.recommendations.json"
    payload = {"recommender": RECOMMENDER, "mode": mode, "judge": judge_engine.engine,
               "judge_is_model": judge_engine.is_model, "namespace": retrieval.namespace(),
               "catalogue": templates.CATALOGUE, "lines": report,
               "states": states, "judge_calls": judge_engine.calls,
               "judge_cache_hits": cache_hits,
               "judge_failures": judge_engine.failures[:20],
               "seconds": round(time.perf_counter() - started, 2)}
    out.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    summary = {"mode": mode, "judge": judge_engine.engine, "states": states,
               "judge_calls": judge_engine.calls, "judge_cache_hits": cache_hits,
               "path": str(out),
               "seconds": payload["seconds"]}
    if db is not None:
        record_id = "dec-" + digest([str(job.input_file), job.target_locale or job.target_lang,
                                     job.kind])[:16]
        current = records.get(db, "decision", record_id)
        records.put(db, "decision", record_id, {
            "job_input": str(job.input_file), "locale": job.target_locale or job.target_lang,
            "summary": summary, "examples_used": examples_used,
            "pins": {cue: s.envelope.template for cue, s in
                     ((seg.cue_id, seg) for seg in job.segments) if s.envelope.template},
            "at": records.now_marker()},
            base_revision=current["revision"] if current else 0)
    log.info("adaptive: %s with %s → %s", mode, judge_engine.engine, states)
    return summary


def template_map(db, job) -> dict[str, dict]:
    """The pinned template definitions the level owner needs, by id@version."""
    out = {}
    for seg in job.segments:
        pin = seg.envelope.template
        if not pin or not pin.get("id"):
            continue
        key = f"{pin['id']}@{pin.get('version')}"
        if key in out:
            continue
        found = templates.get(db, pin["id"], pin.get("version")) if db is not None else None
        if found is None:
            found = next(({**templates.validate(t).model_dump(), "version": 1}
                          for t in templates.builtins() if t["id"] == pin["id"]), None)
        if found is not None:
            out[key] = found
    return out


def background(job, config, *, db=None, work: Path, cancel=None, scene_starts=None,
               activity: list[tuple[float, float]] | None = None) -> dict | None:
    """Plan and render the bed policy when one is selected; None keeps the legacy mix."""
    from . import bedpolicy

    options = settings(config.get("adaptive", {}))
    policy_id = options["background_policy"]
    if not policy_id or options["mode"] != "apply":
        return None
    bed = job.background if job.background and job.background != job.source_audio else None
    if bed is None or not Path(bed).is_file():
        job.metrics["background_policy"] = {"template": policy_id, "state": "unavailable",
                                            "reason": "no separated bed to shape"}
        return None
    template = (templates.get(db, policy_id) if db is not None else None) or next(
        ({**templates.validate(t).model_dump(), "version": 1} for t in templates.builtins()
         if t["id"] == policy_id), None)
    if template is None or template["family"] not in ("bed", "bed_legacy"):
        job.metrics["background_policy"] = {"template": policy_id, "state": "unavailable",
                                            "reason": "not a background template"}
        return None
    if template["family"] == "bed_legacy":
        job.metrics["background_policy"] = {"template": policy_id, "state": "legacy",
                                            "sidechain": "used"}
        return None
    params = templates.resolve_params(template)["values"]
    from .ffmpeg import media_duration

    duration = media_duration(Path(bed), cancel=cancel)
    if activity is None:
        activity = []
        takes = take_features(job, work)
        for seg in job.segments:
            take = takes.get(seg.cue_id)
            if take and take.get("intervals"):
                activity += [(seg.start + a, seg.start + b) for a, b in take["intervals"]]
            else:
                activity.append((seg.start, seg.end))
    levels_ = bedpolicy.bed_levels(Path(bed), duration, cancel) \
        if "transient_db" in params else None
    planned = bedpolicy.plan(params, activity, duration, scene_starts=scene_starts,
                             bed_level=levels_,
                             max_attenuation_db=options["max_bed_attenuation_db"])
    key = digest([templates.pin(template), params, planned["curve"], stamp(Path(bed))])[:16]
    from .stages.common import work_stem

    dest = Path(work) / f"{work_stem(job)}.bed.{key}.wav"
    if not dest.is_file():
        bedpolicy.render(Path(bed), dest, planned["curve"], cancel=cancel)
    job.metrics["background_policy"] = {"template": templates.pin(template), "params": params,
                                        "state": "applied", "sidechain": "bypassed",
                                        "stats": planned["stats"], "fingerprint": key}
    return {"path": dest, "fingerprint": key, "stats": planned["stats"]}

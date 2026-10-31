"""Voice envelopes, background policies and combined presets, as data.

A template is a reusable definition; a fitted instance (doblarr.envelopes) is
what one line actually gets. Three kinds:

- **voice** templates shape gain *inside* one line. They are rendered by the
  one post-fit level owner (doblarr.levels), in the same pass as the baseline
  and the performance gain, so there is never a second loudness stage.
- **background** templates are policies for the bed under the dialogue
  (doblarr.bedpolicy): how far it ducks, how fast, whether it holds through a
  hesitation, swells after a scene's dialogue or steps aside for an impact.
- **presets** pair one voice and one background template with bounds and the
  acoustic treatments (doblarr.treatments) they are meant to sit with.

What is *data* and what is *code* is the point of the design. Each template
belongs to a **signal family**, and a family is the only thing implemented in
code:

=================  =========================================================
``preserve``       no change, a first-class candidate (never a missing value)
``curve``          a gain curve from anchors over the line's active span
``flatten``        pull the take's own envelope partly toward its mean
``peak_linked``    bumps of gain on the take's own peaks or phrase starts
``bed``            a background policy (duck, hold, release, swell, protect)
``bed_legacy``     the existing sidechain ducking, unchanged
``preset``         a voice + background pairing
=================  =========================================================

Adding a curve (new anchors, a new shape) to ``curve`` is a data operation:
validate, store, pin by version, preview, retrieve and render through the same
code as every built-in. No template carries code, an FFmpeg filter string or a
path; anything outside the schema is refused.

Units are explicit: anchors and bounds are relative dB of linear gain; ``0.5``
linear gain is about −6.02 dB and is never a "50% loudness" target.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .artifacts import digest
from .studio import records

CATALOGUE = "templates/1"
FAMILIES = ("preserve", "curve", "flatten", "peak_linked", "bed", "bed_legacy", "preset")
VOICE_FAMILIES = ("preserve", "curve", "flatten", "peak_linked")
BACKGROUND_FAMILIES = ("preserve", "bed", "bed_legacy")
MAX_ANCHOR_DB = 12.0
TREATMENTS = ("dry", "room", "distant", "phone", "radio")
DATA = Path(__file__).resolve().parent / "data" / "templates.json"


def db_from_linear(gain: float) -> float:
    """Linear gain to dB (0.5 → −6.02). Gain must be positive."""
    if not gain > 0:
        raise ValueError("a linear gain must be positive")
    return 20 * math.log10(gain)


class Anchor(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    at: float = Field(ge=0.0, le=1.0)                 # position on the active span
    db: float = Field(ge=-MAX_ANCHOR_DB, le=MAX_ANCHOR_DB)


class Param(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    default: float
    min: float
    max: float
    units: str = ""

    @model_validator(mode="after")
    def ordered(self):
        if not self.min <= self.default <= self.max:
            raise ValueError("a parameter's default must sit between its min and max")
        return self


class Duration(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    min: float = Field(default=0.3, ge=0)
    max: float = Field(default=60.0, gt=0)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    active_seconds: float = Field(default=0.3, ge=0)
    quality: list[Literal["ok", "insufficient", "contaminated", "missing"]] = ["ok"]


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    origin: Literal["builtin", "local", "imported"] = "local"
    author: str = Field(default="", max_length=100)
    source: str = Field(default="", max_length=300)
    created_at: str = Field(default="", max_length=40)


class Template(BaseModel):
    """One template version. Unknown fields are refused, never ignored."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: str = Field(pattern=r"^(voice|background|preset)/[a-z0-9][a-z0-9-]{1,60}$")
    kind: Literal["voice", "background", "preset"]
    family: Literal["preserve", "curve", "flatten", "peak_linked", "bed", "bed_legacy",
                    "preset"]
    title: str = Field(min_length=1, max_length=80)
    purpose: str = Field(default="", max_length=400)
    anchors: list[Anchor] = Field(default_factory=list, max_length=24)
    interpolation: Literal["linear", "smooth", "hold"] = "smooth"
    smoothing_ms: float = Field(default=40.0, ge=0, le=500)
    silence: Literal["hold", "follow", "release"] = "hold"
    params: dict[str, Param] = Field(default_factory=dict)
    valid_duration: Duration = Field(default_factory=Duration)
    min_evidence: Evidence = Field(default_factory=Evidence)
    contraindications: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(default_factory=list, max_length=20)
    examples: dict[str, list[str]] = Field(default_factory=dict)
    voice: str = Field(default="", max_length=80)        # preset: voice template id
    background: str = Field(default="", max_length=80)   # preset: background template id
    treatments: list[str] = Field(default_factory=list, max_length=5)
    requires: list[Literal["numpy", "ffmpeg"]] = Field(default_factory=lambda: ["numpy"])  # type: ignore[arg-type]
    provenance: Provenance = Field(default_factory=Provenance)
    retired: bool = False

    @field_validator("contraindications", "tags")
    @classmethod
    def words(cls, value):
        for word in value:
            if not word or len(word) > 40 or not all(c.isalnum() or c in "-_" for c in word):
                raise ValueError("tags and contraindications are short words")
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.kind == "voice" and self.family not in VOICE_FAMILIES:
            raise ValueError(f"a voice template cannot be a {self.family} family")
        if self.kind == "background" and self.family not in BACKGROUND_FAMILIES:
            raise ValueError(f"a background template cannot be a {self.family} family")
        if self.kind == "preset" and self.family != "preset":
            raise ValueError("a preset belongs to the preset family")
        if self.family == "curve":
            if len(self.anchors) < 2:
                raise ValueError("a curve needs at least two anchors")
            positions = [a.at for a in self.anchors]
            if positions != sorted(positions) or len(set(positions)) != len(positions):
                raise ValueError("curve anchors must be in increasing position")
        elif self.anchors:
            raise ValueError(f"the {self.family} family takes no anchors")
        if self.valid_duration.max <= self.valid_duration.min:
            raise ValueError("valid_duration.max must exceed min")
        required = {"flatten": ("amount",), "peak_linked": ("bump_db", "width_ms"),
                    "bed": ("duck_db", "attack_ms", "release_ms")}.get(self.family, ())
        missing = [p for p in required if p not in self.params]
        if missing:
            raise ValueError(f"the {self.family} family needs parameters: {', '.join(missing)}")
        if "strength" in self.params and not (self.params["strength"].min >= 0
                                              and self.params["strength"].max <= 1.5):
            raise ValueError("strength is bounded to 0..1.5")
        if self.family == "preset":
            if not self.voice or not self.background:
                raise ValueError("a preset names one voice and one background template")
            unknown = [t for t in self.treatments if t not in TREATMENTS]
            if unknown:
                raise ValueError(f"unknown treatment(s): {', '.join(unknown)}")
        return self


def validate(document: dict) -> Template:
    try:
        return Template.model_validate(document)
    except ValidationError as exc:
        err = exc.errors()[0]
        where = ".".join(str(p) for p in err["loc"]) or "template"
        raise ValueError(f"{where}: {err['msg']}") from exc


def fingerprint(template: Template | dict) -> str:
    body = template.model_dump() if isinstance(template, Template) else dict(template)
    body.pop("provenance", None)
    return digest(body)[:16]


# --------------------------------------------------------------------------
# The catalogue (studio records kind "template"; revision = version)
# --------------------------------------------------------------------------

def _stored(template: Template) -> dict:
    # Record documents drop an "id" key, so the template id is kept twice.
    return {**template.model_dump(), "template_id": template.id,
            "fingerprint": fingerprint(template)}


def _record_id(template_id: str) -> str:
    return "tpl-" + template_id.replace("/", "-")


def builtins() -> list[dict]:
    return json.loads(DATA.read_text(encoding="utf-8"))["templates"]


def ensure_builtins(db) -> int:
    """Install built-ins once; a changed built-in becomes a new version."""
    added = 0
    for raw in builtins():
        template = validate({**raw, "provenance": {"origin": "builtin", "source": CATALOGUE}})
        current = records.get(db, "template", _record_id(template.id))
        if current is not None and current.get("fingerprint") == fingerprint(template):
            continue
        if current is not None and (current.get("provenance") or {}).get("origin") != "builtin":
            continue     # a person replaced it locally; never overwrite their version
        records.put(db, "template", _record_id(template.id),
                    _stored(template),
                    scope=template.kind, base_revision=current["revision"] if current else 0)
        added += 1
    return added


def get(db, template_id: str, version: int | None = None) -> dict | None:
    row = records.get(db, "template", _record_id(template_id), version)
    if row is None:
        return None
    body = {k: v for k, v in row.items() if k not in ("id", "revision", "scope",
                                                       "updated_at")}
    return {**body, "id": template_id, "version": row["revision"],
            "updated_at": row["updated_at"]}


def listing(db, kind: str | None = None, include_retired: bool = False) -> list[dict]:
    ensure_builtins(db)
    rows = records.list_latest(db, "template", scope=kind)
    out = []
    for row in rows:
        body = {k: v for k, v in row.items() if k not in ("id", "revision", "scope",
                                                           "updated_at")}
        found = {**body, "id": body.get("template_id") or row["id"],
                 "version": row["revision"], "updated_at": row["updated_at"]}
        if found.get("retired") and not include_retired:
            continue
        out.append(found)
    return sorted(out, key=lambda r: (r["kind"], r["id"]))


def history(db, template_id: str) -> list[dict]:
    return [{**{k: v for k, v in r.items() if k not in ("id", "scope")},
             "id": template_id, "version": r["revision"]}
            for r in records.history(db, "template", _record_id(template_id))]


def save(db, document: dict, *, base_version: int | None, author: str = "") -> dict:
    """Create or edit a template; every save is a new version (old ones stay)."""
    template = validate({**document, "provenance": {
        **dict(document.get("provenance") or {}), "origin": "local",
        "author": author or (document.get("provenance") or {}).get("author", ""),
        "created_at": records.now_marker()}})
    _check_references(db, template)
    saved = records.put(db, "template", _record_id(template.id),
                        _stored(template),
                        scope=template.kind, base_revision=base_version)
    return get(db, template.id, saved["revision"]) or {}


def _check_references(db, template: Template) -> None:
    if template.family != "preset":
        return
    voice, background = get(db, template.voice), get(db, template.background)
    if voice is None or voice["kind"] != "voice":
        raise ValueError(f"preset voice {template.voice!r} is not a voice template")
    if background is None or background["kind"] != "background":
        raise ValueError(f"preset background {template.background!r} is not a background "
                         "template")


def duplicate(db, template_id: str, new_id: str, *, title: str = "") -> dict:
    source = get(db, template_id)
    if source is None:
        raise KeyError(template_id)
    if get(db, new_id) is not None:
        raise ValueError(f"{new_id} already exists")
    body = {k: v for k, v in source.items() if k in Template.model_fields}
    body.update(id=new_id, title=title or f"{source['title']} (copy)", retired=False,
                provenance={"origin": "local", "source": f"duplicated from {template_id} "
                            f"v{source['version']}"})
    return save(db, body, base_version=0)


def retire(db, template_id: str, *, base_version: int | None) -> dict:
    """A retired template is no longer offered; saved jobs keep its versions."""
    current = get(db, template_id)
    if current is None:
        raise KeyError(template_id)
    body = {k: v for k, v in current.items() if k in Template.model_fields}
    body["retired"] = True
    template = validate(body)
    saved = records.put(db, "template", _record_id(template_id),
                        _stored(template),
                        scope=template.kind, base_revision=base_version)
    return get(db, template_id, saved["revision"]) or {}


def export(db, ids: list[str]) -> dict:
    """A portable bundle: definitions and provenance only, never paths or media."""
    rows = []
    for template_id in ids:
        found = get(db, template_id)
        if found is None:
            raise KeyError(template_id)
        rows.append({k: v for k, v in found.items() if k in Template.model_fields})
    return {"format": "doblarr-templates/1", "catalogue": CATALOGUE, "templates": rows}


def import_bundle(db, bundle: dict, *, author: str = "") -> dict:
    """Validate every template first; then add new ids and new versions of
    changed ones. A bundle with one invalid template imports nothing."""
    if bundle.get("format") != "doblarr-templates/1":
        raise ValueError("not a Doblarr template bundle")
    checked = []
    for raw in bundle.get("templates") or []:
        raw = {**raw, "provenance": {**dict(raw.get("provenance") or {}), "origin": "imported",
                                     "created_at": records.now_marker()}}
        checked.append(validate(raw))
    added: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    for template in sorted(checked, key=lambda t: t.family == "preset"):
        current = get(db, template.id)
        if current is not None and current.get("fingerprint") == fingerprint(template):
            unchanged.append(template.id)
            continue
        _check_references(db, template)
        records.put(db, "template", _record_id(template.id),
                    _stored(template),
                    scope=template.kind, base_revision=current["version"] if current else 0)
        (updated if current else added).append(template.id)
    return {"added": added, "updated": updated, "unchanged": unchanged}


def pin(template: dict) -> dict:
    """What a saved decision records about the template it used."""
    return {"id": template["id"], "version": template["version"],
            "fingerprint": template.get("fingerprint") or fingerprint(
                {k: v for k, v in template.items() if k in Template.model_fields}),
            "family": template["family"]}


def resolve_params(template: dict, overrides: dict[str, float] | None = None) -> dict:
    """Effective parameters, each clamped to its declared range (clamps reported)."""
    out, clamps = {}, {}
    for name, spec in (template.get("params") or {}).items():
        value = float((overrides or {}).get(name, spec["default"]))
        if not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")
        bounded = min(spec["max"], max(spec["min"], value))
        if bounded != value:
            clamps[name] = {"requested": value, "applied": bounded}
        out[name] = bounded
    unknown = set(overrides or {}) - set(out)
    if unknown:
        raise ValueError(f"unknown parameter(s): {', '.join(sorted(unknown))}")
    return {"values": out, "clamps": clamps}


def applicable(template: dict, *, duration: float, quality: str, active_seconds: float,
               tags: set[str] | None = None) -> tuple[bool, str]:
    """Whether a template may be offered for a line, and why not."""
    if template.get("retired"):
        return False, "retired"
    if template["family"] == "preserve":
        return True, ""
    limits = template.get("valid_duration") or {}
    if not limits.get("min", 0) <= duration <= limits.get("max", 1e9):
        return False, f"valid for lines of {limits.get('min')}–{limits.get('max')} s"
    evidence = template.get("min_evidence") or {}
    if quality not in (evidence.get("quality") or ["ok"]):
        return False, f"needs {'/'.join(evidence.get('quality') or ['ok'])} evidence, " \
                      f"the line is {quality}"
    if active_seconds < float(evidence.get("active_seconds", 0)):
        return False, f"needs {evidence.get('active_seconds')} s of speech"
    blocked = set(template.get("contraindications") or []) & (tags or set())
    if blocked:
        return False, f"contraindicated: {', '.join(sorted(blocked))}"
    return True, ""


def describe_curve(template: dict, strength: float = 1.0, points: int = 33) -> list[list[float]]:
    """The curve a template draws on a unit span, for previews and listings."""
    from .envelopes import template_curve

    return [[round(x, 3), round(y, 2)] for x, y in template_curve(template, points, strength)]

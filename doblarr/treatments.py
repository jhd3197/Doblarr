"""Where a line sounds like it is — without changing who is speaking.

D09 in one sentence: the same actor, standing further away, in a room, or on
the other end of a phone. A treatment is a *place or a device*, never a
performance, and that separation is the whole design. Acting direction lives in
`performance.py`, loudness in `levels.py`, and neither of them is allowed to
arrive here disguised as an effect.

Five presets, chosen because each one answers a question a reviewer actually
asks and none of them needs a model or a service:

    dry       nothing is applied; the explicit "leave it alone" answer
    room      a few early reflections, as if there were walls
    distant   the same voice from across the space: duller, quieter, wetter
    phone     band-limited and squashed, the way a handset sounds
    radio     band-limited more gently, with a little presence pushed up

Three rules the code enforces rather than documents:

- **Capability is asked, never assumed.** Every preset names the FFmpeg filters
  it needs. A build without one of them records the treatment as asked-for and
  `unsupported`; it never renders a quieter approximation and calls it applied.
- **The effect must not become a level decision.** Adding reflections raises a
  line's measured speech level and band-limiting lowers it. Each preset
  declares the offset it *intends* (a distant line should be quieter, a phone
  line should not be), the treated render is measured, and a bounded makeup
  gain puts the line where the preset said it should be. Anything left over is
  recorded, not hidden.
- **A tail is not speech.** The ring-out past the end of the words is recorded
  in seconds so the mix can leave room for it and the conversation check can
  exclude it. A reverb tail landing over the next speaker is not two people
  talking at once, and reporting it as a collision would be a lie with a
  timecode on it.

Nothing here infers a room from the picture, tracks a head, follows a camera
cut or decides on its own that a thought should echo. A preset is chosen by a
default, a scene rule, or a person.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field

from .cues import TREATMENT_PRESETS, Treatment

log = logging.getLogger("doblarr.treatments")

# Bump when a preset's parameters change: a saved version has to be able to say
# which catalogue produced the audio it holds.
CATALOGUE = "treatments/1"

# How far the measured treated level may sit from the preset's intent before a
# makeup gain is applied at all. Below this, correcting would be arguing with
# the measurement's own resolution.
LEVEL_DEADBAND_DB = 0.35
# The makeup gain is bounded in both directions. A preset that needs more than
# this to hit its own intent is misconfigured, and quietly spending 12 dB to
# rescue it would undo the level owner's work.
MAX_MAKEUP_DB = 6.0
# Two consecutive lines closer than this are a join worth listening to when
# their treatments differ.
TRANSITION_GAP = 1.2

DETECTOR = "treatment/1"


@dataclass(frozen=True)
class Preset:
    """One supported treatment: what it sounds like and exactly what it costs.

    `gentle` and `full` are the parameter sets at intensity 0 and 1. Intensity
    interpolates between them, so it is a preset-specific dial and not a
    dry/wet mix — a half-strength phone is a wider band, not a phone played
    quietly underneath the dry line.

    `echoes` is what makes the tail a fact instead of a guess. Each entry is one
    `aecho` stage's delays in milliseconds, and FFmpeg extends a stream by
    exactly the longest delay of each stage it passes through. The ring-out is
    therefore the sum of those maxima and nothing else — no padding constant,
    no estimate. Two stages rather than one because a single stage is two
    slaps; feeding the reflections back through a second gives a short decay
    that reads as a space.
    """

    name: str
    summary: str
    needs: tuple[str, ...] = ()
    echoes: tuple[tuple[float, ...], ...] = ()
    # dB this preset means the line to move by, relative to its dry level.
    offset: float = 0.0
    gentle: dict = field(default_factory=dict)
    full: dict = field(default_factory=dict)

    def at(self, intensity: float) -> dict:
        """The parameter set at `intensity`, linearly between gentle and full."""
        weight = max(0.0, min(1.0, float(intensity)))
        values = {}
        for key, high in self.full.items():
            low = self.gentle.get(key, high)
            values[key] = low + (high - low) * weight
        return values

    @property
    def tail(self) -> float:
        """Seconds of ring-out past the dry audio. Arithmetic, not an estimate."""
        return round(sum(max(stage) for stage in self.echoes) / 1000, 4)


PRESETS: dict[str, Preset] = {
    "dry": Preset(
        name="dry",
        summary="No acoustic treatment. The line is placed exactly as rendered.",
    ),
    "room": Preset(
        name="room",
        summary=("A few early reflections, as if the line were spoken indoors. "
                 "Short enough that it reads as a room rather than a hall."),
        needs=("aecho",),
        echoes=((17.0, 29.0), (43.0,)),
        offset=0.0,
        gentle={"wet": 0.10, "decay": 0.45},
        full={"wet": 0.22, "decay": 0.60},
    ),
    "distant": Preset(
        name="distant",
        summary=("The same voice further away: less high end, more reflection, "
                 "and genuinely quieter, because distance is mostly level."),
        needs=("aecho", "lowpass"),
        echoes=((31.0, 53.0), (83.0,)),
        offset=-3.5,
        gentle={"wet": 0.16, "decay": 0.50, "cutoff": 7200.0},
        full={"wet": 0.30, "decay": 0.70, "cutoff": 5200.0},
    ),
    "phone": Preset(
        name="phone",
        summary=("A handset: a narrow band and a firm compressor. No tail — a "
                 "phone line is not a room."),
        needs=("highpass", "lowpass", "acompressor"),
        offset=0.0,
        gentle={"low": 220.0, "high": 5200.0, "ratio": 2.5, "threshold": 0.20},
        full={"low": 330.0, "high": 3400.0, "ratio": 4.0, "threshold": 0.13},
    ),
    "radio": Preset(
        name="radio",
        summary=("Broadcast: a wider band than a phone, evened out, with a "
                 "little presence pushed up so it still cuts through."),
        needs=("highpass", "lowpass", "acompressor", "equalizer"),
        offset=0.0,
        gentle={"low": 120.0, "high": 6800.0, "ratio": 2.0, "threshold": 0.22,
                "presence": 1.2},
        full={"low": 190.0, "high": 5000.0, "ratio": 3.2, "threshold": 0.15,
              "presence": 2.6},
    ),
}

assert set(PRESETS) == set(TREATMENT_PRESETS)


def settings(options: dict | None) -> dict:
    """Effective treatment settings, with every default written down once."""
    values = dict(options or {})
    preset = str(values.get("default", "dry"))
    if preset not in PRESETS:
        log.warning("treatments: unknown default preset %r, using dry", preset)
        preset = "dry"
    scenes = []
    for row in values.get("scenes") or []:
        if not isinstance(row, dict):
            continue
        try:
            start, end = float(row.get("start", 0.0)), float(row.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        name = str(row.get("preset", "dry"))
        if end <= start or name not in PRESETS:
            continue
        scenes.append({
            "start": start, "end": end, "preset": name,
            "intensity": _intensity(row.get("intensity"), 1.0),
            "note": str(row.get("note", "")),
            # A rule's identity is its range and preset, so the same rule keeps
            # the same id when an unrelated one is added above it.
            "id": f"{start:.3f}-{end:.3f}:{name}",
        })
    lines = {}
    for key, row in (values.get("lines") or {}).items():
        if not isinstance(row, dict):
            continue
        lines[str(key)] = row
    return {
        "mode": "on" if str(values.get("mode", "off")) == "on" else "off",
        "default": preset,
        "intensity": _intensity(values.get("intensity"), 1.0),
        "scenes": sorted(scenes, key=lambda row: (row["start"], row["end"])),
        "lines": lines,
        "max_tail": max(0.0, float(values.get("max_tail", 1.5))),
    }


def _intensity(value, fallback: float) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return fallback


def enabled(options: dict | None) -> bool:
    """True when treatments own a `treated` derivative on this run."""
    return settings(options)["mode"] == "on"


# --------------------------------------------------------------------------
# Capability
# --------------------------------------------------------------------------

_available: set[str] | None = None


def available_filters(refresh: bool = False) -> set[str]:
    """The audio filters this FFmpeg build actually has.

    Read once per process from `ffmpeg -filters`. A build that cannot be asked
    reports an empty set, which makes every preset except `dry` unsupported —
    the honest answer, rather than a chain that fails at render time.
    """
    global _available
    if _available is not None and not refresh:
        return _available
    found: set[str] = set()
    try:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                                capture_output=True, text=True, timeout=30)
        for line in (result.stdout or "").splitlines():
            parts = line.split()
            # " T.C acompressor       A->A       Audio compressor."
            if len(parts) >= 4 and "->" in parts[2]:
                found.add(parts[1])
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("treatments: could not read the FFmpeg filter list: %s", exc)
    _available = found
    return found


def capability(preset: str) -> tuple[str, list[str]]:
    """Whether `preset` can be rendered here, and which filters are missing."""
    spec = PRESETS.get(preset)
    if spec is None:
        return "unsupported", [preset]
    if not spec.needs:
        return "supported", []
    have = available_filters()
    if not have:
        return "unknown", sorted(spec.needs)
    missing = sorted(name for name in spec.needs if name not in have)
    return ("supported" if not missing else "unsupported"), missing


# --------------------------------------------------------------------------
# Choosing
# --------------------------------------------------------------------------

def choose(seg, config: dict, edits: dict | None = None) -> dict:
    """Which preset this line gets, at what intensity, and why.

    Precedence is line, then scene, then the run default — the narrowest
    decision wins, because it is the one somebody made most deliberately.
    """
    override = {**(config.get("lines") or {}).get(seg.cue_id, {}),
                **((edits or {}).get(seg.cue_id) or {})}
    if override:
        if override.get("bypass"):
            return {"preset": "dry", "intensity": 1.0, "origin": "line",
                    "scene": "", "reason": "a reviewer bypassed treatment on this line"}
        name = str(override.get("preset", "")) or ""
        if name in PRESETS:
            return {"preset": name,
                    "intensity": _intensity(override.get("intensity"),
                                            config["intensity"]),
                    "origin": "line", "scene": "",
                    "reason": "chosen for this line in review"}
        if name:
            return {"preset": "dry", "intensity": 1.0, "origin": "line", "scene": "",
                    "reason": f"unknown preset {name!r} was requested for this line"}
    for rule in config["scenes"]:
        # A cue belongs to the scene its *start* is in. A line that straddles
        # the boundary is reported rather than split: half a sentence in a
        # room and half on a phone is not a treatment, it is a defect.
        if rule["start"] <= seg.start < rule["end"]:
            return {"preset": rule["preset"], "intensity": rule["intensity"],
                    "origin": "scene", "scene": rule["id"],
                    "reason": rule["note"] or f"scene rule {rule['id']}"}
    return {"preset": config["default"], "intensity": config["intensity"],
            "origin": "default" if config["default"] != "dry" else "none",
            "scene": "",
            "reason": ("the run default" if config["default"] != "dry"
                       else "no treatment was selected")}


def straddles(seg, config: dict) -> dict | None:
    """The scene rule this line starts inside but ends outside, if any."""
    for rule in config["scenes"]:
        if rule["start"] <= seg.start < rule["end"] < seg.end:
            return rule
    return None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def chain(preset: str, intensity: float, makeup_db: float = 0.0) -> list[str]:
    """The FFmpeg filter chain for one preset, as a list of filter strings.

    Nothing is padded. Each `aecho` stage extends its own output by its longest
    delay, which is exactly the ring-out the preset declares, so the rendered
    length is the dry length plus `tail` and the record never has to be
    reconciled with the file afterwards. The makeup gain comes last, after
    everything that could have changed the level.
    """
    spec = PRESETS[preset]
    if not spec.needs:
        # `dry` is the explicit "leave it alone" answer. It never renders a
        # file, so it never carries a makeup gain either.
        return []
    values = spec.at(intensity)
    parts: list[str] = []
    if preset == "distant":
        parts.append(f"lowpass=f={values['cutoff']:.0f}")
    for position, delays in enumerate(spec.echoes):
        # aecho's out_gain scales the whole output, so it stays at unity and
        # the wet amount lives in the decays. A later stage is quieter than the
        # one before it: those are reflections of reflections.
        level = values["wet"] * (values["decay"] ** position)
        decays = "|".join(f"{level * (values['decay'] ** i):.3f}"
                          for i in range(len(delays)))
        taps = "|".join(f"{d:.1f}" for d in delays)
        parts.append(f"aecho=1:1:{taps}:{decays}")
    if preset == "phone":
        parts.append(f"highpass=f={values['low']:.0f}")
        parts.append(f"lowpass=f={values['high']:.0f}")
        parts.append(f"acompressor=threshold={values['threshold']:.3f}"
                     f":ratio={values['ratio']:.2f}:attack=5:release=90")
    elif preset == "radio":
        parts.append(f"highpass=f={values['low']:.0f}")
        parts.append(f"lowpass=f={values['high']:.0f}")
        parts.append(f"acompressor=threshold={values['threshold']:.3f}"
                     f":ratio={values['ratio']:.2f}:attack=8:release=140")
        parts.append(f"equalizer=f=2400:t=q:w=1.4:g={values['presence']:.2f}")
    if abs(makeup_db) >= 1e-3:
        parts.append(f"volume={makeup_db:.3f}dB")
    return parts


def tail_seconds(preset: str, intensity: float = 1.0) -> float:
    """Ring-out this preset adds past the dry audio, in seconds.

    Independent of intensity on purpose: intensity changes how *loud* the
    reflections are, not how big the room is. A tail that moved with a level
    dial would make the mix reserve a different amount of time for the same
    space, which is a difference nobody asked for and nobody could hear.
    """
    return PRESETS[preset].tail


def makeup_for(dry_db: float | None, treated_db: float | None,
               offset_db: float) -> tuple[float, str]:
    """The bounded gain that puts a treated line where the preset meant it to be.

    Returns the gain and a reason. An unmeasurable line gets no makeup at all:
    guessing a correction from a number that does not exist is how a treatment
    silently becomes a level decision.
    """
    if dry_db is None or treated_db is None:
        return 0.0, "the treated line could not be measured, so no makeup was applied"
    wanted = dry_db + offset_db
    error = wanted - treated_db
    if abs(error) < LEVEL_DEADBAND_DB:
        return 0.0, "the effect left the level where the preset intended it"
    gain = max(-MAX_MAKEUP_DB, min(MAX_MAKEUP_DB, error))
    if abs(gain) < abs(error) - 1e-6:
        return round(gain, 3), (
            f"the effect moved the line {error:+.1f} dB; makeup was clamped to "
            f"{gain:+.1f} dB so it could not become a level decision")
    return round(gain, 3), f"{gain:+.1f} dB of makeup kept the level where the preset intended"


def decide(seg, config: dict, edits: dict | None = None) -> Treatment:
    """The treatment this line asked for, before anything is rendered.

    Always returns a record. A preset this build cannot render comes back as
    `unsupported` with the missing filters named — asked-for, never applied.
    """
    picked = choose(seg, config, edits)
    state, missing = capability(picked["preset"])
    treatment = Treatment(
        preset=picked["preset"],
        intensity=picked["intensity"],
        origin=picked["origin"],
        version=CATALOGUE,
        capability=state,
        missing=missing,
        scene=picked["scene"],
        reason=picked["reason"],
        offset=PRESETS[picked["preset"]].offset * picked["intensity"],
        tail=tail_seconds(picked["preset"], picked["intensity"]),
    )
    if picked["preset"] == "dry":
        treatment.outcome = "bypassed"
        treatment.tail = 0.0
        treatment.offset = 0.0
    elif state == "supported":
        treatment.outcome = "unknown"   # decided; the render stage settles it
    else:
        treatment.outcome = "unsupported"
        treatment.tail = 0.0
        treatment.reason = (
            f"{picked['reason']}; this FFmpeg build has no "
            f"{', '.join(missing)} filter, so the treatment was not applied")
    return treatment


def summary(job) -> dict:
    """Per-run counts for the report and the review payload."""
    rows = [s.treatment for s in job.segments]
    used: dict[str, int] = {}
    for row in rows:
        if row.applied:
            used[row.preset] = used.get(row.preset, 0) + 1
    return {
        "applied": sum(1 for r in rows if r.applied),
        "bypassed": sum(1 for r in rows if r.outcome == "bypassed"),
        "unsupported": sum(1 for r in rows if r.outcome == "unsupported"),
        "unavailable": sum(1 for r in rows if r.outcome in ("unavailable", "failed")),
        "presets": dict(sorted(used.items())),
        "longest_tail": round(max((r.tail for r in rows), default=0.0), 4),
        "makeup_clamped": sum(1 for r in rows
                              if abs(r.makeup) >= MAX_MAKEUP_DB - 1e-6),
        "catalogue": CATALOGUE,
    }


def describe(preset: str) -> dict:
    """One preset as the settings and review UI should show it."""
    spec = PRESETS[preset]
    state, missing = capability(preset)
    return {
        "preset": preset,
        "summary": spec.summary,
        "needs": list(spec.needs),
        "capability": state,
        "missing": missing,
        "tail": spec.tail,
        "offset": spec.offset,
    }


def catalogue() -> list[dict]:
    """Every preset, in the order a reviewer should meet them."""
    return [describe(name) for name in TREATMENT_PRESETS]


def spoken_render(seg):
    """The artifact whose *speech* timing is this line's, treatment or not.

    A treated file is the dry line plus a ring-out, and its speech-active
    region runs to the end of the tail. Anything asking "when does this line
    stop talking" — turn-taking above all — measures the dry render instead,
    because a reverb tail over the next speaker is not an interruption and
    reporting it as one would be a lie with a timecode on it.
    """
    from .cues import TREATED

    if seg.treatment.applied and seg.audio.render(TREATED) is not None:
        return seg.audio.upstream_of(TREATED)
    return seg.audio.current()

"""A voice study on synthetic sidecars: profiles from film 1, held out on film 2."""

import json
from types import SimpleNamespace

import numpy as np
import pytest

from doblarr import voice_study
from doblarr.commands import base
from doblarr.commands import voice_study as command
from doblarr.config import Config

CENTRES = {"Kaito": [1.0, 0, 0, 0], "Mina": [0, 1.0, 0, 0], "Ren": [0, 0, 1.0, 0]}


def sidecar(groups, seed, model="m1"):
    """Lines of 2 s each; `groups` maps a voice group to its character's centre."""
    rng = np.random.default_rng(seed)
    lines = []
    for group, (character, count) in groups.items():
        for _ in range(count):
            v = np.asarray(CENTRES[character]) + rng.normal(0, 0.05, 4)
            n = len(lines)
            lines.append({"cue": f"c{n}", "start": n * 3.0, "end": n * 3.0 + 2.0,
                          "speaker": group, "vector": [round(float(x), 5) for x in v]})
    return {"model": model, "lines": lines}


FILM1 = sidecar({"SPEAKER_00": ("Kaito", 6), "SPEAKER_01": ("Mina", 6)}, 1)
FILM2 = sidecar({"SPEAKER_00": ("Mina", 4), "SPEAKER_02": ("Kaito", 4),
                 "SPEAKER_03": ("Ren", 2)}, 2)
NAMES1 = {"SPEAKER_00": "Kaito", "SPEAKER_01": "Mina"}
NAMES2 = {"SPEAKER_00": "Mina", "SPEAKER_02": "Kaito", "SPEAKER_03": "Ren"}


def test_profiles_come_from_film_1_and_film_2_is_scored_held_out():
    out = voice_study.run(FILM1, NAMES1, FILM2, NAMES2, floor=0.5)
    assert set(out["profiles"]) == {"Kaito", "Mina"}      # Ren is only in film 2
    assert out["profiles"]["Kaito"]["lines"] == 6 and out["profiles"]["Kaito"]["seconds"] == 12
    assert out["unseen"] == ["Ren"]
    assert out["lines"] == 10 and len(out["per_line"]) == 10
    assert out["per_line"][0]["cue"] == "c0" and out["per_line"][0]["true"] == "Mina"
    counts = dict(zip(out["matrix"]["labels"], out["matrix"]["counts"], strict=True))
    assert counts["Mina"][1] == 4 and counts["Kaito"][0] == 4


def test_unnamed_film_2_lines_are_skipped_and_models_must_match():
    out = voice_study.run(FILM1, NAMES1, FILM2, {"SPEAKER_00": "Mina"})
    assert out["lines"] == 4
    with pytest.raises(voice_study.StudyError, match="different voice models"):
        voice_study.run(FILM1, NAMES1, {**FILM2, "model": "m2"}, NAMES2)
    with pytest.raises(voice_study.StudyError, match="no named lines"):
        voice_study.run(FILM1, {}, FILM2, NAMES2)


def test_command_prints_a_table_and_json(tmp_path, capsys):
    one, two = tmp_path / "one.speakers.json", tmp_path / "two.speakers.json"
    one.write_text(json.dumps(FILM1))
    two.write_text(json.dumps(FILM2))
    names = tmp_path / "names.json"
    names.write_text(json.dumps({"film1": NAMES1, "film2": NAMES2}))
    config = Config.load(str(tmp_path / "none.yaml"))
    args = SimpleNamespace(film1=str(one), film2=str(two), names=str(names), floor=0.5,
                           margin=0.05, prototypes=False, json=False)
    assert command.cmd_voice_study(base.Ctx(args, config)) == 0
    text = capsys.readouterr().out
    assert "top-1" in text and "abstained" in text and "Ren" in text
    args.json = True
    assert command.cmd_voice_study(base.Ctx(args, config)) == 0
    assert json.loads(capsys.readouterr().out)["matrix"]["labels"] == ["Kaito", "Mina", "Ren"]
    args.film2 = str(tmp_path / "missing.speakers.json")
    with pytest.raises(base.ApiError, match="no voice analysis"):
        command.cmd_voice_study(base.Ctx(args, config))

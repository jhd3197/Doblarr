"""Voice study records: profiles, held-out results, scope and deletion."""

import json

import pytest

from doblarr.store import Database
from doblarr.studio import records, voice_studies
from tests.test_voice_study import FILM1, FILM2, NAMES1, NAMES2


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "doblarr.db")
    yield database
    database.close()


@pytest.fixture
def films(tmp_path):
    one, two = tmp_path / "one.speakers.json", tmp_path / "two.speakers.json"
    one.write_text(json.dumps(FILM1))
    two.write_text(json.dumps(FILM2))
    return str(one), str(two)


def test_a_study_stores_profiles_and_results_without_vectors(db, tmp_path, films):
    one, two = films
    study = voice_studies.create(db, tmp_path, "Harbor Lights pair",
                                 {"path": one, "names": NAMES1},
                                 [{"path": two, "names": NAMES2}], floor=0.5, margin=0.05)
    result = voice_studies.probe(db, tmp_path, study["id"], two)
    assert result["scope"] == study["id"] and result["lines"] == 10
    assert all(set(r) == {"cue", "true", "pred", "score", "margin", "abstained"}
               for r in result["per_line"])
    found = voice_studies.detail(db, study["id"])
    assert found["status"] == "probed" and found["model"] == "m1"
    assert {p["character"] for p in found["profiles"]} == {"Kaito", "Mina"}
    assert len(found["results"]) == 1
    stored = json.dumps([records.list_latest(db, k) for k in voice_studies.KINDS])
    assert '"vector"' not in stored       # prototype sums are the only vectors kept


def test_deleting_a_study_removes_only_its_records(db, tmp_path, films):
    one, two = films
    args = ("Pair", {"path": one, "names": NAMES1}, [{"path": two, "names": NAMES2}])
    keep = voice_studies.create(db, tmp_path, "Other", *args[1:], floor=0.3, margin=0.05)
    gone = voice_studies.create(db, tmp_path, *args, floor=0.3, margin=0.05)
    voice_studies.probe(db, tmp_path, gone["id"], two)
    assert voice_studies.delete(db, gone["id"]) == 5          # study (2 revisions), 2 profiles, 1 result
    assert voice_studies.detail(db, gone["id"]) is None
    assert voice_studies.detail(db, keep["id"])["profiles"]
    with pytest.raises(KeyError):
        voice_studies.probe(db, tmp_path, keep["id"], "elsewhere.speakers.json")

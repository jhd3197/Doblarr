"""The /api/voice-studies routes on synthetic sidecars."""

import json

from tests.test_voice_study import FILM1, FILM2, NAMES1, NAMES2


def _films(tmp_path):
    work = tmp_path / "app-0" / "work"
    work.mkdir(parents=True, exist_ok=True)
    one, two = work / "one.speakers.json", work / "two.speakers.json"
    one.write_text(json.dumps(FILM1))
    two.write_text(json.dumps(FILM2))
    return str(one), str(two)


def test_a_study_runs_from_create_to_delete(client_factory, tmp_path):
    client = client_factory()
    one, two = _films(tmp_path)
    body = {"name": "Harbor Lights pair", "source": {"path": one, "names": NAMES1},
            "probes": [{"path": two, "names": NAMES2}], "floor": 0.5}
    made = client.post("/api/voice-studies", json=body).json()
    sid = made["id"]
    assert {p["character"] for p in made["profiles"]} == {"Kaito", "Mina"}
    assert client.get("/api/voice-studies/" + sid + "/matrix").status_code == 404
    probed = client.post(f"/api/voice-studies/{sid}/probe", json={"path": two})
    assert probed.status_code == 200 and probed.json()["lines"] == 10
    matrix = client.get(f"/api/voice-studies/{sid}/matrix", params={"probe": two}).json()
    assert matrix["matrix"]["labels"] == ["Kaito", "Mina", "Ren"] and matrix["unseen"] == ["Ren"]
    listed = client.get("/api/voice-studies").json()["studies"]
    assert listed[0]["results"][0]["coverage"] is not None
    assert len(client.get("/api/voice-studies/" + sid).json()["results"]) == 1
    assert client.post(f"/api/voice-studies/{sid}/probe", json={"path": one}).status_code == 404
    assert client.delete("/api/voice-studies/" + sid).json()["deleted"] > 0
    assert client.get("/api/voice-studies/" + sid).status_code == 404
    assert client.get("/api/voice-studies").json() == {"studies": []}


def test_bad_input_is_refused(client_factory, tmp_path):
    client = client_factory()
    one, _ = _films(tmp_path)
    outside = tmp_path / "elsewhere.speakers.json"
    outside.write_text(json.dumps(FILM1))
    post = client.post
    assert post("/api/voice-studies", json={
        "name": "x", "source": {"path": str(outside), "names": NAMES1}}).status_code == 422
    assert post("/api/voice-studies", json={
        "name": "x", "source": {"path": one, "names": {}}}).status_code == 422
    assert post("/api/voice-studies", json={
        "name": "x", "source": {"path": one + ".missing.speakers.json"}}).status_code == 422


def test_every_voice_study_route_needs_the_key(client_factory, tmp_path):
    client = client_factory({"web": {"api_key": "k"}})
    for method, path in (("POST", "/api/voice-studies"), ("GET", "/api/voice-studies"),
                         ("GET", "/api/voice-studies/x"), ("POST", "/api/voice-studies/x/probe"),
                         ("GET", "/api/voice-studies/x/matrix"),
                         ("DELETE", "/api/voice-studies/x")):
        assert client.request(method, path).status_code == 401, (method, path)
    assert client.get("/api/voice-studies", headers={"X-Api-Key": "k"}).status_code == 200

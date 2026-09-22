"""Knowledge API tests — CRUD, pagination, filters, coverage honesty, preview."""

import pytest

from doblarr.knowledge import save_entry, save_realization
from doblarr.knowledge.models import Entry, Realization


@pytest.fixture
def client_factory(client_factory):
    """Starter auto-install stays off here so counts and overlays stay exact."""

    def make(data=None):
        return client_factory({"knowledge.auto_install_starter": False, **(data or {})})

    return make


def add(
    client,
    phrase="Ginko",
    kind="pronunciation",
    locale="es-MX",
    scope="personal",
    status="proposed",
    **kw,
):
    response = client.post(
        "/api/knowledge/entries",
        json={
            "phrase": phrase,
            "kind": kind,
            "locale": locale,
            "scope": scope,
            "status": status,
            **kw,
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()["entry"]


def test_entry_crud_revisions_and_validation(client_factory):
    client = client_factory()
    entry = add(client, locale="es-mx")
    assert entry["locale"] == "es-MX"  # canonicalized
    assert entry["revision"] == 1 and entry["origin"] == "local"

    updated = client.put(
        f"/api/knowledge/entries/{entry['id']}",
        json={"phrase": "Ginko", "kind": "pronunciation", "locale": "es-MX", "usage": "anime only"},
    )
    assert updated.json()["entry"]["revision"] == 2
    detail = client.get(f"/api/knowledge/entries/{entry['id']}").json()
    assert detail["entry"]["usage"] == "anime only"

    retired = client.post(f"/api/knowledge/entries/{entry['id']}/retire").json()
    assert retired["entry"]["status"] == "retired" and retired["entry"]["revision"] == 3

    assert (
        client.post(
            "/api/knowledge/entries",
            json={"phrase": "x", "kind": "pronunciation", "locale": "es-MXX"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/knowledge/entries",
            json={"phrase": "x", "kind": "pronunciation", "locale": "es", "scope": "pack"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/knowledge/entries", json={"phrase": "", "kind": "pronunciation", "locale": "es"}
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/knowledge/entries/nope", json={"phrase": "x", "kind": "term", "locale": "es"}
        ).status_code
        == 404
    )


def test_realization_crud(client_factory):
    client = client_factory()
    entry = add(client)
    assert (
        client.post(
            "/api/knowledge/realizations",
            json={"entry_id": "missing", "engine": "chatterbox", "replacement": "x"},
        ).status_code
        == 422
    )
    realization = client.post(
        "/api/knowledge/realizations",
        json={
            "entry_id": entry["id"],
            "engine": "chatterbox",
            "replacement": "Guin-ko",
            "evidence": "listened 2026-09",
        },
    ).json()["realization"]
    assert realization["revision"] == 1 and realization["origin"] == "local"
    updated = client.put(
        f"/api/knowledge/realizations/{realization['id']}",
        json={
            "entry_id": entry["id"],
            "engine": "chatterbox",
            "replacement": "Ghin-ko",
            "status": "reviewed",
        },
    ).json()["realization"]
    assert updated["revision"] == 2 and updated["replacement"] == "Ghin-ko"
    detail = client.get(f"/api/knowledge/entries/{entry['id']}").json()
    assert [r["replacement"] for r in detail["realizations"]] == ["Ghin-ko"]


def test_list_pagination_and_filters(client_factory):
    client = client_factory()
    for i in range(30):
        add(
            client,
            phrase=f"term-{i:02d}",
            kind="term" if i % 2 else "pronunciation",
            locale="es-MX" if i % 3 else "es-VE",
            status="reviewed" if i % 4 else "proposed",
        )
    page1 = client.get("/api/knowledge/entries?page=1&page_size=25").json()
    assert page1["total"] == 30 and len(page1["entries"]) == 25
    page2 = client.get("/api/knowledge/entries?page=2&page_size=25").json()
    assert len(page2["entries"]) == 5
    by_locale = client.get("/api/knowledge/entries?locale=es-VE").json()
    assert by_locale["total"] == 10
    by_kind = client.get("/api/knowledge/entries?kind=term&page_size=100").json()
    assert all(e["kind"] == "term" for e in by_kind["entries"])
    by_status = client.get("/api/knowledge/entries?status=reviewed&page_size=100").json()
    assert all(e["status"] == "reviewed" for e in by_status["entries"])
    searched = client.get("/api/knowledge/entries?q=term-07").json()
    assert searched["total"] == 1
    # an edited entry appears once, at its latest revision
    first = page1["entries"][0]
    client.put(
        f"/api/knowledge/entries/{first['id']}",
        json={"phrase": first["phrase"], "kind": first["kind"], "locale": first["locale"]},
    )
    assert client.get("/api/knowledge/entries?page_size=100").json()["total"] == 30


def test_coverage_counts_are_real(client_factory):
    client = client_factory()
    add(client, phrase="a", locale="es-MX", status="reviewed")
    add(client, phrase="b", locale="es-MX", status="proposed")
    add(client, phrase="c", locale="es-VE", status="proposed")
    coverage = {c["locale"]: c for c in client.get("/api/knowledge/coverage").json()["locales"]}
    assert {"en", "es-MX", "es-VE"} <= set(coverage)  # validation targets always listed
    assert coverage["es-MX"]["reviewed"] == 1 and coverage["es-MX"]["proposed"] == 1
    assert coverage["es-VE"]["reviewed"] == 0 and coverage["es-VE"]["proposed"] == 1
    assert coverage["en"]["entries"] == 0  # zero content shown honestly


def test_detail_shows_suppression_info(client_factory):
    client = client_factory()
    rule = add(client)
    add(client, phrase="", scope="show", scope_ref="grp", suppresses=rule["id"])
    detail = client.get(f"/api/knowledge/entries/{rule['id']}").json()
    assert [e["suppresses"] for e in detail["suppressed_by"]] == [rule["id"]]


def test_preview_resolves_draft_against_sample(client_factory):
    client = client_factory()
    response = client.post(
        "/api/knowledge/preview",
        json={
            "entry": {"phrase": "Ginko", "kind": "pronunciation", "locale": "es-MX"},
            "realization": {"entry_id": "x", "engine": "chatterbox", "replacement": "Guin-ko"},
            "text": "Ginko camina con Mushi.",
            "engine": "chatterbox",
        },
    )
    assert response.json()["after"] == "Guin-ko camina con Mushi."
    none = client.post(
        "/api/knowledge/preview",
        json={
            "entry": {"phrase": "Ausente", "kind": "pronunciation", "locale": "es-MX"},
            "text": "Ginko camina.",
            "engine": "chatterbox",
        },
    )
    assert none.json()["after"] == "Ginko camina."


def test_preview_surfaces_conflicts_with_existing_rules(client_factory):
    client = client_factory()
    existing = add(client, phrase="Ginko", status="reviewed")
    client.post(
        "/api/knowledge/realizations",
        json={"entry_id": existing["id"], "engine": "chatterbox", "replacement": "OLD"},
    )
    response = client.post(
        "/api/knowledge/preview",
        json={
            "entry": {"phrase": "Ginko", "kind": "pronunciation", "locale": "es-MX"},
            "realization": {"entry_id": "x", "engine": "chatterbox", "replacement": "NEW"},
            "text": "Ginko camina.",
            "engine": "chatterbox",
        },
    )
    assert response.json()["conflicts"]  # same phrase/scope: surfaced, never silently won


def test_review_rerender_inherits_or_updates_knowledge(client_factory, tmp_path):
    from tests.test_review import review_job

    client = client_factory()
    queued, _, _ = review_job(client, tmp_path)
    store = client.app.state.jobs
    original_snapshot = {"version": 1, "entries": {}, "realizations": {}}
    store.update(queued.id, knowledge_snapshot=original_snapshot)
    db = store.db
    entry = save_entry(db, Entry(phrase="Ginko", locale="es"))
    save_realization(db, Realization(entry_id=entry.id, replacement="Guin-ko", engine="chatterbox"))
    edit = {"edits": [{"index": 0, "text": "buenas", "regenerate": True}]}
    inherited = client.post(f"/api/jobs/{queued.id}/review", json=edit).json()["job"]
    assert inherited["knowledge_snapshot"] == original_snapshot
    updated = client.post(
        f"/api/jobs/{queued.id}/review", json={**edit, "use_updated_knowledge": True}
    ).json()["job"]
    assert updated["knowledge_snapshot"]["entries"] == {entry.id: 1}


def test_review_exposes_knowledge_scope_refs(client_factory, tmp_path):
    from tests.test_review import review_job

    client = client_factory()
    queued, job, _ = review_job(client, tmp_path)
    data = client.get(f"/api/jobs/{queued.id}/review").json()
    assert data["title_ref"]
    assert data["locale"] == "es"
    assert "show_ref" in data

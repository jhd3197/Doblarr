import copy

import pytest

from doblarr.knowledge import packs
from doblarr.knowledge import store as ks
from doblarr.store import Database


def release(version="1", revision=1):
    entries = [{"id": "test-term", "revision": revision, "kind": "term", "locale": "en",
                "phrase": "hello", "scope": "episode", "status": "proposed"}]
    return dict(format=packs.PACK_FORMAT, schema_version=1, id="test", name="Test",
                release=version, entries=entries, content_sha256=packs.content_hash(entries))


def install(db, document):
    return packs.activate(db, packs.validate_pack(db, document),
                          source="third-party", distribution="test")


def test_pack_update_rollback_and_frozen_pins(tmp_path):
    db = Database(tmp_path / "test.db")
    install(db, release())
    frozen = ks.snapshot(db)
    install(db, release("2", 2))
    assert ks.snapshot(db)["entries"]["test-term"] == 2
    packs.rollback_pack(db, "test")
    assert ks.snapshot(db)["entries"] == frozen["entries"]
    assert len(ks.pack_releases(db)) == 2
    assert ks.entries_at(db, {"test-term": 2})[0].revision == 2


def test_corruption_conflict_and_dependency_are_atomic(tmp_path):
    db = Database(tmp_path / "test.db")
    install(db, release())
    corrupt = release("2", 2)
    corrupt["entries"][0]["phrase"] = "different"
    with pytest.raises(packs.PackError, match="hash"):
        install(db, corrupt)
    conflict = release("2")
    conflict["entries"][0]["phrase"] = "different"
    conflict["content_sha256"] = packs.content_hash(conflict["entries"])
    with pytest.raises(packs.PackError, match="conflict"):
        install(db, conflict)
    dependent = copy.deepcopy(release())
    dependent.update(id="dependent", entries=[], dependencies={"test": "1"},
                     content_sha256=packs.content_hash([]))
    install(db, dependent)
    with pytest.raises(packs.PackError, match="requires"):
        install(db, release("2", 2))
    assert ks.active_installed_pins(db)["entries"]["test-term"] == 1


def test_offline_starter_and_coverage(tmp_path):
    db = Database(tmp_path / "test.db")
    result = packs.ensure_starter_pack(db, {})
    assert result["entries"] > 0
    assert packs.ensure_starter_pack(db, {}) is None
    assert all(e.status == "proposed" for e in ks.latest_entries(db))
    assert all(c["reviewed"] == 0 for c in ks.coverage_counts(db).values())


def test_download_rejects_path_traversal(tmp_path):
    db = Database(tmp_path / "test.db")
    with pytest.raises(packs.PackError, match="invalid pack id"):
        packs.download_pack(db, {}, "../config", tmp_path)


def test_recipe_pack_pin_survives_new_active_release(tmp_path):
    from doblarr.jobs import JobStore

    db = Database(tmp_path / "test.db")
    install(db, release())
    install(db, release("2", 2))
    queue = JobStore(db)
    job = queue.add(title="test", source="manual", source_lang="en", target_lang="es",
                    overrides={"knowledge.pack_releases": {"test": "1"}})
    assert job.knowledge_snapshot["entries"]["test-term"] == 1
    assert ks.active_installed_pins(db)["entries"]["test-term"] == 2

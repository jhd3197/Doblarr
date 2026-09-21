import json

from doblarr.knowledge.packs import load_pack_file, validate_pack
from doblarr.store import Database
from scripts.import_knowledge_candidates import convert


def test_additional_locale_needs_only_candidate_data(tmp_path):
    source = tmp_path / "authored.jsonl"
    row = dict(source_lang="en", locale="fr-CA", source_form="Hello", phrase="Bonjour")
    source.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    paths = convert(source, tmp_path / "packs", license="CC0-1.0", provenance="authored test")
    db = Database(tmp_path / "test.db")
    prepared = validate_pack(db, load_pack_file(paths[0]))
    assert len(prepared.entries) == 1
    assert prepared.entries[0].locale == "fr-CA"
    assert prepared.entries[0].status == "proposed"

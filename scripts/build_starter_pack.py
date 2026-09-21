"""Build the bundled starter pack from starter-entries.json (run after editing it).

Computes real coverage counts and the content hash, then writes
doblarr/knowledge/data/starter-pack.json — the file shipped as package data.
"""

import json
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "doblarr" / "knowledge" / "data"
RELEASE = "0.1.0"


def main():
    source = json.loads((DATA / "starter-entries.json").read_text(encoding="utf-8"))
    realizations_by_entry = {}
    for realization in source["realizations"]:
        realizations_by_entry.setdefault(realization["entry_id"], []).append(realization)
    entries = []
    for entry in source["entries"]:
        entries.append(
            {
                "id": entry["id"],
                "revision": 1,
                "kind": entry["kind"],
                "locale": entry["locale"],
                "coverage": [],
                "source_lang": None,
                "source_form": entry.get("source_form", ""),
                "phrase": entry["phrase"],
                "sense": "",
                "usage": entry["usage"],
                "examples": entry["examples"],
                "pronunciation": "",
                "ipa": None,
                "scope": "episode",  # placeholder; pack installation forces pack scope
                "status": "proposed",  # unverified: inactive until Juan's review
                "license": "CC0-1.0",
                "contributor": "Doblarr contributors",
                "realizations": [
                    {
                        "id": r["id"],
                        "revision": 1,
                        "engine": r["engine"],
                        "model": None,
                        "voice": None,
                        "replacement": r["replacement"],
                        "evidence": r["evidence"],
                        "status": "proposed",  # no listening evidence yet
                    }
                    for r in realizations_by_entry.get(entry["id"], [])
                ],
            }
        )
    coverage = {}
    for entry in entries:
        bucket = coverage.setdefault(
            entry["locale"], {"entries": 0, "reviewed": 0, "proposed": 0}
        )
        bucket["entries"] += 1
        bucket[entry["status"]] += 1
    canonical = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    import hashlib

    pack = {
        "format": "doblarr-knowledge-pack",
        "schema_version": 1,
        "id": "doblarr-starter",
        "name": "Doblarr starter scenarios (unverified)",
        "release": RELEASE,
        "app_versions": ">=0.1",
        "coverage": coverage,
        "dependencies": {},
        "attribution": {
            "author": "Doblarr contributors",
            "license": "CC0-1.0",
            "note": "Authored scenario scaffolding; every entry is proposed pending review.",
        },
        "content_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "entries": entries,
    }
    out = DATA / "starter-pack.json"
    out.write_text(
        json.dumps(pack, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {out} ({len(entries)} entries, {len(coverage)} locales)")


if __name__ == "__main__":
    main()

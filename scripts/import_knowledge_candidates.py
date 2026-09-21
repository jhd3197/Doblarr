"""Convert licensed JSONL candidates into inactive, reviewable pack shards.

Input rows: source_lang, locale, source_form, phrase, and optional usage.
This command never installs packs, downloads corpora or marks content reviewed.
"""

import argparse
import json
from pathlib import Path

from doblarr.knowledge.packs import MAX_PACK_BYTES, content_hash
from doblarr.languages import parse


def convert(source: Path, output: Path, *, license: str, provenance: str) -> list[Path]:
    if not license.strip() or not provenance.strip():
        raise ValueError("license and provenance are required")
    output.mkdir(parents=True, exist_ok=True)
    paths = []
    entries = []
    seen = set()

    def flush():
        if not entries:
            return
        identifier = "candidates-" + content_hash(entries)[:16]
        document = dict(
            format="doblarr-knowledge-pack", schema_version=1, id=identifier,
            name="Unreviewed corpus candidates", release="1", app_versions=">=0.1",
            attribution={"license": license, "provenance": provenance},
            content_sha256=content_hash(entries), entries=list(entries),
        )
        raw = json.dumps(document, ensure_ascii=False, indent=2)
        if len(raw.encode()) > MAX_PACK_BYTES:
            raise ValueError("candidate shard exceeds pack size limit")
        path = output / f"{identifier}.json"
        path.write_text(raw + "\n", encoding="utf-8")
        paths.append(path)
        entries.clear()

    with source.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if len(line.encode()) > 16384:
                raise ValueError(f"line {number} exceeds 16 KB")
            row = json.loads(line)
            locale, source_lang = parse(row["locale"]), parse(row["source_lang"])
            if not locale or not source_lang:
                raise ValueError(f"line {number}: invalid language")
            phrase, source_form = row["phrase"].strip(), row["source_form"].strip()
            if not phrase or not source_form or max(len(phrase), len(source_form)) > 300:
                raise ValueError(f"line {number}: phrases must have 1–300 characters")
            key = (source_lang, locale, source_form, phrase)
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > 100000:
                raise ValueError("candidate import exceeds 100,000 unique rows")
            entries.append(dict(
                id="candidate-" + content_hash([{"key": key}])[:24], revision=1,
                kind="term", locale=locale, source_lang=source_lang, source_form=source_form,
                phrase=phrase, usage=str(row.get("usage", ""))[:2000], scope="episode",
                status="proposed", license=license, contributor="Corpus candidate import",
            ))
            if len(entries) == 50:
                flush()
    flush()
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--license", required=True)
    parser.add_argument("--provenance", required=True)
    args = parser.parse_args()
    paths = convert(args.source, args.output, license=args.license, provenance=args.provenance)
    print(f"Wrote {len(paths)} proposed pack shards. Review provenance and wording before release.")


if __name__ == "__main__":
    main()

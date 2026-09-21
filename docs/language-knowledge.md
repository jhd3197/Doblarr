# Language knowledge release and review

English, Mexican Spanish and Venezuelan Spanish have 32 authored validation
scenes each in `doblarr/knowledge/data/validation-scenes.json`. The same scenes
are used across targets. Eight per target are held out from development.
Every candidate is unverified. Installing the bundled pack does not activate
its proposed entries or certify an accent.

Use Language knowledge to install a local JSON pack, download from the configured
`knowledge.pack_distribution_url`, or roll back a release. Downloads happen only
on an explicit request. Installed releases remain available for frozen jobs.
Personal edits live separately from pack content. No official remote distribution
is configured or published by this implementation.

For each validation scene, record the wording assessment, meaning assessment,
engine/model/voice, pronunciation listening result, timing and any correction.
Use the same generation settings for comparisons. Juan reviews target wording
and listening for the three initial targets. Source meaning in languages Juan
does not understand requires a competent source-language reviewer.

Contributions are explicitly selected through `POST /api/packs/contribution`
with entry IDs, author, license and notes. The result is a local JSON bundle;
the application does not upload it. Review every exported field before sharing:
short text can still be copyrighted or private. Use authored examples, do not
include movie dialogue, source audio, or private voice profiles. Submit the
bundle through a repository pull request. Maintainers verify provenance,
distribution rights, meaning, regional naturalness and model/voice evidence
separately. Record the reviewer and each verified dimension in review history.
Changing wording or engine realization requires a new revision; mark untested
realizations `needs-retest`. Regenerate the pack's SHA-256 after editing its
entries and increment the release. Installation preserves review history.

Pack manifests declare dependencies by exact release. Missing dependencies,
conflicting immutable record revisions, malformed content and invalid hashes
prevent activation. An update or rollback cannot break another active pack's
declared dependency. Content hashes detect corruption; they are not signatures.

Release gates still requiring human evidence: target-language listening and
wording review, held-out meaning/timing comparisons, and an additional language
maintainer. No coverage, accent or savings claims follow from automated tests.

## Private reuse pilot

In dialogue review, choose **Save translation for reuse**. Record the reviewer
and only the meaning, regional wording and timing assessments actually performed.
Unreviewed entries remain suggestions. Translation settings include an opt-in
**Reuse reviewed translations** switch, off by default. Set character dialogue
notes to capture speaker register: missing register never permits automatic reuse.
Complete source text, directed language pair, exact target locale, surrounding
three lines on either side, speaker, translation settings, relevant glossary and
duration (within 50 ms) must match. The target must also fit the character budget.
Conflicting reviewed translations fall back to translation. No fuzzy matching or
automatic pivot through another language is implemented.

The memory list supports pagination and retirement. Each save creates an immutable
revision. Jobs freeze a sequence watermark, so edits and retirement cannot alter
an already queued run. Recipes omit private memory and pin exact pack releases.
CLI runs persist their knowledge selection in the target work directory.

**Adapt wording to this region** explicitly translates already-target-language
subtitles; leaving it off preserves the existing shortcut. Original text remains
in review and version provenance. Timing shortening retains regional direction
and terminology and records that the original translation was rewritten.

Run reports include checked/reused lines, retrieval time, translation latency,
instrumented provider calls and raw provider usage for translation and timing
repair. Missing usage is unknown; Voicebox does not supply tokens. These metrics
do not establish cost savings. A resumed job may reuse script checkpoints without
making any translation calls, so compare cold new-title runs separately.

For the held-out scenes, compare baseline (no knowledge), guidance only and
guidance plus reuse with identical source, models, voices, settings and seed
where supported. Record source-meaning accuracy, naturalness, pronunciation,
timing and human corrections alongside report counters. Do not promote a reuse
condition to the default until these comparisons pass human review.

## Expansion tooling

`python scripts/import_knowledge_candidates.py INPUT.jsonl OUTPUT --license LICENSE
--provenance SOURCE` writes bounded proposed pack shards. Each JSONL row contains
`source_lang`, `locale`, `source_form`, `phrase` and optional `usage`. Duplicate
pairs are removed. The tool never downloads, activates or publishes a corpus.
The supplied license is a declaration that maintainers must verify, not a legal
assessment. A French Canadian fixture demonstrates adding another locale through
data without a new translation branch.

Appoint a maintainer for each new locale and directed source pair through the
repository review process. Record their identity, language competence and reviewed
dimensions in entry review history. No maintainers have been appointed or external
corpora selected by this implementation. Pack releases can contain any catalog
locale; source-pair terminology stays isolated.

Reproduce synthetic benchmarks with `scripts/benchmark_translation_memory.py
--output docs/benchmarks/translation-memory.json` and
`scripts/benchmark_knowledge_matcher.py`. Reports identify hardware, Python,
10,000/100,000 mixed-language records, cold time, Python allocation peak and warm
p50/p95. Matcher construction excludes database loading; Python allocation peaks
exclude SQLite native allocations. These are engineering baselines, not held-out
dubbing quality results. Knowledge browsing filters and paginates in SQLite, and coverage is aggregated
against active pack releases without loading every entry into the UI.

Implementation status: phases 1–3 are present; phase 4 pack/review tooling is
implemented with unverified starter content; phase 5 is an opt-in local pilot;
phase 6 has candidate import and synthetic benchmark tooling. Reviewed starter
publication, real held-out quality/savings validation, additional maintainers and
reviewed language-pair releases remain open human release gates.

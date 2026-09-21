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

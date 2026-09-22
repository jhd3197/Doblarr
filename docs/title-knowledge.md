# Subtitle title knowledge: foundation contract

The foundation is a local Python storage API in
`doblarr/knowledge/title_drafts.py`, backed by SQLite migration 8. It stores
validated, versioned drafts and separate human review overlays. It does not yet
analyze subtitles, call providers, display a review UI, activate guidance, change
dubbing, or export a title file. Fixtures use an authored synthetic scene.

## Isolation and compatibility

`title_drafts` and `title_draft_reviews` are separate from `knowledge_entries`,
realizations, translation memory, title recipes, and job snapshots. There is no
draft-to-Entry adapter. The resolver reads none of these draft tables, even when
a draft is complete or a review says `accept`. Review decisions are preparation
for a later activation workflow, not active knowledge revisions.

This boundary is deliberate: existing proposed personal/local entries can apply
immediately. Changing that behavior would break human rules. Existing resolver
precedence, suppressions, pack revisions, and recipe v1/v2 behavior remain intact.
Narrative summaries and relationships have their own candidate kinds; they are
not stored as pronunciation entries.

## Schema and identity

All records reject unknown fields. JSON round trips use schema version 1.

| Record | Contract |
| --- | --- |
| SourceIdentity | Local title and optional series reference, edition, track identity, source language, SHA-256 of original subtitle bytes, parser revision. No path is required. |
| Cue | Contiguous zero-based ordinal, original subtitle label, original text including whitespace/markup, start/end milliseconds. Repeated labels, overlapping times, and empty cues are preserved. |
| AnalysisIdentity | Chunking revision, instruction hash, provider/model and nullable model revision, settings hash, inherited knowledge revision hash. |
| Candidate | Stable reconciliation key, typed kind, statement, entity keys, explicit applicability, cue references, checkpoint provenance, nullable confidence, explicit uncertainty notes, optional conflict group. |
| Locale proposal | A locale-specific candidate kind additionally requires target locale and an exact accepted shared revision reference (ID, revision, content hash). Pronunciation remains intent, never a tested realization. |
| Review | Exact candidate ID and proposal hash, accept/edit/reject/defer, reviewer, note, and a separate correction only for edit. |

Source IDs hash the entire source identity. Cue IDs hash source ID plus ordinal;
they never depend on a cleaned or split translation segment. Preserve these IDs
as many-to-many evidence links through future split/merge adapters. Original cues
cannot be edited under an existing source ID: fix parsing with a new parser
revision or changed source bytes. This module expects already parsed cues; the
caller must calculate the content hash from the actual bytes before parsing.

Candidate IDs hash source ID, reconciliation key, and locale, excluding generated
wording, confidence, evidence order, and output order. The future reconciler must
retain keys for the same proposition and allocate distinct keys for alternatives;
the foundation does not infer semantic identity. Reuse a conflict group to show
competing spellings or identities without choosing a winner. Subjects are entity
keys, not an assumption that subtitles identify a speaker. Do not reassign keys
to different entities. Candidate disappearance does not delete review history.

Applicability lists explicit title references and optional scene references. An
empty scene list means the listed titles only. A series ID alone grants no
inheritance. The evidence title must be included. Later shared revisions need
explicit episode/scene eligibility and revelation boundaries; each new episode
still requires analysis. Cross-edition identity mapping and carrying corrections
to changed source IDs require a visible reconciliation step, not fuzzy auto-merge.

The analysis fingerprint covers schema, source, original cues, and analysis
identity. The full document digest also includes candidates, locale, accepted
shared pins, progress, and provenance. Hash canonical settings and inherited
revision selections, not secrets, endpoint credentials, or local paths. Locale
analysis settings must include applicable engine/model/voice choices; unknown
model revisions stay null. A shared-revision reference is structurally validated
here; a later locale service must verify it exists and is accepted before calling
a provider. No accepted shared-revision repository exists in this milestone.

## Local operations and review

`save_draft(db, draft, expected_revision=0)` creates revision 1. Subsequent calls
require the last read revision. Successful calls append an entire checkpoint
document atomically. `load_draft` returns `(revision, draft)` and can read exact
older revisions. SQLite write transactions serialize competing connections;
stale writers fail instead of overwriting work. Sources, cues, proposals, and
prior checkpoints remain available in earlier revisions. Draft state is partial,
complete, cancelled, or failed; none is active.

`save_review` appends a separate overlay using an exact proposal hash and expected
review revision (0 for the first). It rejects stale or missing proposals and
competing review writes. `review_history` preserves every human decision;
`review_view` pairs current proposals with the latest overlay, keeping stale
corrections visible and marking changed proposals for review. Removed candidates'
reviews remain in history. Generated saves never write the overlay table and
cannot replace a correction. There is intentionally no merged effective-text
API that could accidentally feed a stale correction to production.

## Next integrations

1. **Bounded analysis:** parse original bytes, assign source identity and cue IDs,
   partition windows with limited overlap, and retain completed checkpoint IDs.
   Each candidate must cite existing cues and completed checkpoints. Validate
   extraction, merge, and refinement output through `Draft` before saving.
   Provider orchestration must bound every request (including merges), retries,
   context size, and output; record actual usage; handle cancellation; persist
   successful checkpoints. The current store records checkpoint results, not a
   scheduler, retry policy, or raw provider-response archive. Subtitle text and
   imported notes are untrusted data, even when they resemble instructions.
2. **Review and activation:** expose raw proposal/overlay diffs, conflicts, stale
   decisions, and orphaned review history. An explicit acceptance transaction
   must create immutable, scoped accepted revisions, validate source and locale
   links, and preview replacements against personal corrections, suppressions,
   recipes, and approved packs. Never translate a draft status into a personal
   Entry. Rejected, deferred, incomplete, or unresolved conflicting findings
   cannot be implicitly promoted.
3. **Locale and jobs:** adapt accepted shared context for each locale, with exact
   revision pins. Keep register, honorifics, terminology, and pronunciation intent
   separate from tested engine/model/voice realizations. Testing pronunciation
   needs listening evidence. A context adapter must filter summaries and
   relationships by episode/scene before translation. Delivery suggestions cannot
   establish acoustic measurements or replace approved character voices; compose
   acting and locale instructions. Freeze accepted selections in jobs and saved
   versions before execution, and retain them on resume.
4. **Portability:** extend the existing recipe v2/knowledge mechanisms for accepted
   narrative context after an import/activation design is agreed. Do not introduce
   a competing extension or export these local draft documents. Export approved
   guidance and minimal evidence references through an allowlist; exclude full
   subtitles, raw media, local paths, secrets, and private overlays by default.
   Imports need size/schema limits, explicit portable-to-local identity mapping,
   pinned dependencies, and a merge preview before activation.

Remaining product decisions include request budgets, the review UI/API,
accepted shared-revision storage, cross-source reconciliation, and the portable
narrative extension. Automated tests establish isolation and data integrity;
they do not establish narrative accuracy, language quality, or listening quality.

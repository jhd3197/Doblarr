"""Language knowledge: typed records, SQLite storage, resolver, frozen selections.

Phase 2 of docs/plan/language-knowledge-plan.md. Pronunciation and terminology
only — no translation memory (phase 5), no pack install/update (phase 4).
"""

from .matching import PhraseMatcher, boundary_policy
from .models import Entry, Realization
from .resolver import KnowledgeSelection, line_ref
from .store import (
    latest_entries,
    latest_realizations,
    save_entry,
    save_realization,
    snapshot,
)

__all__ = [
    "Entry",
    "KnowledgeSelection",
    "PhraseMatcher",
    "Realization",
    "boundary_policy",
    "latest_entries",
    "latest_realizations",
    "line_ref",
    "save_entry",
    "save_realization",
    "snapshot",
]

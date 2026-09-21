"""Phrase matching: longest-first at each position, applied once, no cascading.

Text and patterns are NFC-normalized (diacritics preserved). Matching is
case-sensitive by explicit policy — captions keep their spelling either way.
Word boundaries use Unicode ``\\w`` lookarounds, but languages without
whitespace-separated words (Japanese, Chinese) must not rely on them: their
policy is a plain substring match.
"""

from __future__ import annotations

import re
import unicodedata

CASE_POLICY = "sensitive"

_NO_BOUNDARY_LANGS = {"ja", "zh"}


def boundary_policy(base_language: str) -> str:
    return "none" if base_language in _NO_BOUNDARY_LANGS else "word"


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


class PhraseMatcher:
    """A compiled rule set, built once per frozen selection and reused per line."""

    def __init__(self, mapping: dict[str, str], boundary: str = "word"):
        self.mapping = {nfc(k): v for k, v in mapping.items() if k}
        terms = sorted(self.mapping, key=len, reverse=True)
        if not terms:
            self.regex = None
            return
        body = "|".join(re.escape(t) for t in terms)
        pattern = rf"(?<!\w)(?:{body})(?!\w)" if boundary == "word" else rf"(?:{body})"
        flags = 0 if CASE_POLICY == "sensitive" else re.IGNORECASE
        self.regex = re.compile(pattern, flags)

    def apply(self, text: str) -> tuple[str, list[str]]:
        """Return (spoken text, matched terms). Each position is substituted once."""
        if self.regex is None:
            return text, []
        matched: list[str] = []

        def sub(match: re.Match) -> str:
            matched.append(match.group())
            return self.mapping[match.group()]

        return self.regex.sub(sub, nfc(text)), matched

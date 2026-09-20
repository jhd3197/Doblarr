"""Translation providers.

The real quality lever is here: translating each line *for dubbing*, which means
staying faithful AND fitting the original line's spoken duration. An LLM (Claude)
can shorten/rephrase to hit a target length in a way plain MT cannot.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Protocol

from ..errors import ArrClientError, ConfigError

log = logging.getLogger("doblarr.clients.translator")


class Translator(Protocol):
    def translate(self, text: str, source_lang: str, target_lang: str,
                  target_chars: int | None = None) -> str:
        ...


class PassthroughTranslator:
    """Stub: returns the source text unchanged. Lets the pipeline run dry."""

    def translate(self, text: str, source_lang: str, target_lang: str,
                  target_chars: int | None = None) -> str:
        return text


class _LineCountMismatch(Exception):
    """Claude's reply could not be mapped 1:1 onto the input lines."""


_NUMBERED = re.compile(r"^\s*(\d+)[.)]\s*(.*)$")
_BATCH_ATTEMPTS = 2


def _anthropic():
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "translate.provider is 'claude' but the 'anthropic' package is not "
            "installed. Run: pip install anthropic — or set translate.provider "
            "to passthrough/voicebox.") from exc
    return anthropic


class ClaudeTranslator:
    """Claude-backed, dubbing-aware translation.

    Lines go to the Messages API as a numbered list and must come back 1:1 and
    in order; a count mismatch retries the batch, then falls back to one call
    per line. The `anthropic` package and ANTHROPIC_API_KEY are only needed when
    a real (non-dry-run) job reaches this translator.
    """

    def __init__(self, model: str = "claude-sonnet-5",
                 api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = None

    def _get_client(self):
        if self._client is None:
            anthropic = _anthropic()
            if not self.api_key:
                raise ConfigError(
                    "translate.provider is 'claude' but no API key was found: "
                    "set the ANTHROPIC_API_KEY environment variable")
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    @staticmethod
    def _system_prompt(source_lang: str, target_lang: str,
                       target_chars: int | None) -> str:
        budget = (f" Keep each translated line under about {target_chars} "
                  "characters so it fits the original time slot."
                  if target_chars else "")
        return (
            f"You are a dubbing translator. Translate movie/TV dialogue from "
            f"{source_lang} to {target_lang} for voice-over: spoken, natural, "
            "concise phrasing rather than a literal written rendering. Preserve "
            "meaning, tone and speaker register; keep names and proper nouns."
            f"{budget} Reply with ONLY the translations, numbered exactly like "
            "the input, one translation per line."
        )

    @staticmethod
    def _parse_numbered(raw: str, count: int) -> list[str]:
        found: dict[int, str] = {}
        for line in raw.splitlines():
            m = _NUMBERED.match(line)
            if m and 1 <= int(m.group(1)) <= count:
                found[int(m.group(1))] = m.group(2).strip()
        out = [found.get(i, "") for i in range(1, count + 1)]
        if all(out):
            return out
        if count == 1 and raw.strip():
            return [re.sub(r"^\s*\d+[.)]\s*", "", raw.strip())]
        raise _LineCountMismatch(
            f"expected {count} numbered lines, got {sum(bool(t) for t in out)}")

    def _translate_lines(self, lines: list[str], source_lang: str,
                         target_lang: str, target_chars: int | None) -> list[str]:
        anthropic = _anthropic()
        client = self._get_client()
        numbered = "\n".join(f"{i}. {line}" for i, line in enumerate(lines, 1))
        for attempt in range(_BATCH_ATTEMPTS):
            try:
                resp = client.messages.create(
                    model=self.model,
                    max_tokens=max(1024, sum(len(line) for line in lines) * 4),
                    system=self._system_prompt(source_lang, target_lang, target_chars),
                    messages=[{"role": "user", "content": numbered}],
                )
            except anthropic.APIError as exc:
                raise ArrClientError(
                    f"Claude API error: {exc}",
                    status=getattr(exc, "status_code", None)) from exc
            raw = "".join(b.text for b in resp.content
                          if getattr(b, "type", None) == "text")
            try:
                return self._parse_numbered(raw, len(lines))
            except _LineCountMismatch as exc:
                if attempt == _BATCH_ATTEMPTS - 1:
                    raise
                log.warning("claude reply off (%s) — retrying batch", exc)
        raise AssertionError("unreachable")

    def translate(self, text: str, source_lang: str, target_lang: str,
                  target_chars: int | None = None) -> str:
        lines = text.splitlines()
        if not lines:
            return text
        try:
            return "\n".join(
                self._translate_lines(lines, source_lang, target_lang, target_chars))
        except _LineCountMismatch:
            if len(lines) == 1:
                raise
            log.warning("falling back to per-line translation (%d lines)", len(lines))
            return "\n".join(
                self._translate_lines([line], source_lang, target_lang, target_chars)[0]
                for line in lines)


class VoiceboxTranslator:
    """Dubbing-aware translation via voicebox's bundled local LLM (no API key)."""

    def __init__(self, client):
        self.client = client

    def translate(self, text: str, source_lang: str, target_lang: str,
                  target_chars: int | None = None) -> str:
        budget = f" Keep it under about {target_chars} characters so it fits the timing." \
            if target_chars else ""
        prompt = (
            f"Translate this movie subtitle line from {source_lang} to {target_lang}. "
            f"Reply with ONLY the translation, no quotes or notes.{budget}\n\n{text}"
        )
        out = self.client.llm_generate(prompt)
        return out or text


def build_translator(provider: str, model: str, voicebox_client=None) -> Translator:
    provider = (provider or "passthrough").lower()
    if provider == "claude":
        return ClaudeTranslator(model=model)
    if provider == "voicebox":
        if voicebox_client is None:
            raise ValueError("voicebox translator needs a voicebox client")
        return VoiceboxTranslator(voicebox_client)
    if provider == "passthrough":
        return PassthroughTranslator()
    raise ValueError(f"unknown translate provider: {provider}")

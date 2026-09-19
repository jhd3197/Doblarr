"""Translation providers.

The real quality lever is here: translating each line *for dubbing*, which means
staying faithful AND fitting the original line's spoken duration. An LLM (Claude)
can shorten/rephrase to hit a target length in a way plain MT cannot.
"""

from __future__ import annotations

import os
from typing import Protocol


class Translator(Protocol):
    def translate(self, text: str, source_lang: str, target_lang: str,
                  target_chars: int | None = None) -> str:
        ...


class PassthroughTranslator:
    """Stub: returns the source text unchanged. Lets the pipeline run dry."""

    def translate(self, text: str, source_lang: str, target_lang: str,
                  target_chars: int | None = None) -> str:
        return text


class ClaudeTranslator:
    """Claude-backed, dubbing-aware translation.

    NOTE: implementation stubbed. When wiring this up, load the `claude-api`
    skill first for the current model ids and SDK usage, then call the Messages
    API with a system prompt that enforces:
      - faithful meaning + natural target-language phrasing
      - a length budget (~target_chars) so the dub fits the time slot
      - preserve speaker tone; keep names/proper nouns
    """

    def __init__(self, model: str = "claude-sonnet-5",
                 api_key: str | None = None):
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")

    def translate(self, text: str, source_lang: str, target_lang: str,
                  target_chars: int | None = None) -> str:
        raise NotImplementedError(
            "ClaudeTranslator.translate not implemented yet. "
            "Use provider: passthrough for dry runs, or wire the Anthropic SDK here."
        )


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

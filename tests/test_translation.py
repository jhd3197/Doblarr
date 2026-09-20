"""Exercise real Prompture JSON handling with offline drivers."""

import json
import sys
from types import SimpleNamespace

import prompture
import pytest
from prompture.drivers.base import Driver

from doblarr.clients.translator import (
    ClaudeTranslator,
    PassthroughTranslator,
    PromptureTranslator,
    TranslationError,
    VoiceboxTranslator,
    build_translator,
)
from doblarr.errors import ConfigError, JobCancelled


def reply(*texts):
    return json.dumps({"translations": [
        {"segment_id": i, "text": text} for i, text in enumerate(texts, 1)
    ]})


class OfflineDriver(Driver):
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def generate(self, prompt, options):
        self.calls.append((prompt, options))
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        return {"text": value, "meta": {"total_tokens": 20, "cost": 0.01}}


@pytest.fixture
def setup_driver(monkeypatch):
    def setup(replies):
        driver = OfflineDriver(replies)
        init = []

        def factory(model, **kwargs):
            init.append((model, kwargs))
            return driver

        monkeypatch.setattr(prompture, "get_driver_for_model", factory)
        return driver, init
    return setup


@pytest.mark.parametrize("provider", ["prompture", "claude", "voicebox"])
def test_all_providers_share_structured_translation(provider, setup_driver):
    driver, init = setup_driver([reply('Translation: "Hola"')])
    client = SimpleNamespace(
        llm_generate=lambda prompt, system=None: driver.generate(prompt, {})["text"])
    translator = build_translator(provider, "claude-sonnet-5", voicebox_client=client)
    assert translator.translate("Hello", "en", "es", 40) == 'Translation: "Hola"'
    assert len(driver.calls) == 1
    assert "segment_id" in driver.calls[0][0]
    if provider != "voicebox":
        assert "40" in driver.calls[0][0]
        assert translator.last_usage[0]["total_tokens"] == 20
    if provider == "claude":
        assert init[0][0] == "claude/claude-sonnet-5"


def test_reordered_ids_map_to_source_and_blank_lines_survive(setup_driver):
    raw = json.dumps({"translations": [
        {"segment_id": 2, "text": "dos"}, {"segment_id": 1, "text": "uno"}
    ]})
    setup_driver([raw])
    assert PromptureTranslator("local/test").translate("one\n\ntwo", "en", "es") == "uno\n\ndos"


def test_translation_direction_survives_batch_and_timing_rewrite(setup_driver):
    driver, _ = setup_driver([reply("Hola"), reply("Hola")])
    translator = build_translator("prompture", "local/test", direction={
        "locale": "es-419", "adaptation": "faithful", "direction": "Quiet anime dialogue",
        "character_notes": {"GINKO": "Calm and concise"}})
    translator.translate_batch([{"text": "Hello", "speaker": "GINKO"}], "en", "es")
    translator.shorten("Hola", "es", 10)
    for prompt, _ in driver.calls:
        assert "Neutral Latin American Spanish" in prompt
        assert "Stay close to the source" in prompt
        assert "Quiet anime dialogue" in prompt
        assert "Calm and concise" in prompt
    assert "SAME language" in driver.calls[-1][0]


def test_spanish_region_does_not_leak_into_other_languages(setup_driver):
    driver, _ = setup_driver([reply("Bonjour")])
    translator = build_translator("prompture", "local/test", direction={"locale": "es-MX"})
    translator.translate("Hello", "en", "fr")
    assert "Mexican Spanish" not in driver.calls[0][0]


@pytest.mark.parametrize("invalid", [
    "", "not JSON", reply(""), reply("   "), reply("a\nb"),
    '{"translations": [{"segment_id": "1", "text": "hola"}]}',
    '{"translations": [{"segment_id": 1, "text": 5}]}',
    '{"translations": [{"segment_id": 2, "text": "hola"}]}',
    '{"translations": [{"segment_id": 1, "text": "hola", "notes": "x"}]}',
    '{"translations": []}',
])
def test_invalid_output_retries_then_fails_without_source_fallback(setup_driver, invalid):
    driver, _ = setup_driver([invalid, invalid])
    with pytest.raises(TranslationError, match="source dialogue was not substituted"):
        PromptureTranslator("local/test").translate("hello", "en", "es")
    assert len(driver.calls) == 2
    assert "previous response was invalid" in driver.calls[1][0]


def test_json_fences_handled_by_prompture(setup_driver):
    setup_driver(["```json\n" + reply("hola") + "\n```"])
    assert PromptureTranslator("local/test").translate("hello", "en", "es") == "hola"


@pytest.mark.parametrize("invalid", [reply("one"), reply("one", "two", "three"),
    '{"translations":[{"segment_id":1,"text":"uno"},{"segment_id":1,"text":"dos"}]}'])
def test_bad_batch_falls_back_to_validated_individual_calls(setup_driver, invalid):
    driver, _ = setup_driver([invalid, invalid, reply("uno"), reply("dos")])
    assert PromptureTranslator("local/test").translate("one\ntwo", "en", "es") == "uno\ndos"
    assert len(driver.calls) == 4


def test_retry_can_recover(setup_driver):
    driver, _ = setup_driver(["bad", reply("hola")])
    assert PromptureTranslator("local/test").translate("hello", "en", "es") == "hola"
    assert len(driver.calls) == 2


def test_endpoint_key_and_driver_reuse(setup_driver, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    _, init = setup_driver([reply("hola"), reply("hola")])
    translator = PromptureTranslator("claude/test", endpoint="http://localhost:1234")
    translator.translate("hello", "en", "es")
    translator.translate("hello", "en", "es")
    assert init == [("claude/test", {"endpoint": "http://localhost:1234", "api_key": "test-key"})]
    assert len(translator.last_usage) == 1


def test_explicit_claude_key(setup_driver):
    _, init = setup_driver([reply("hola")])
    ClaudeTranslator(api_key="explicit").translate("hello", "en", "es")
    assert init[0][1]["api_key"] == "explicit"


def test_voicebox_receives_system_and_json_schema():
    calls = []

    def generate(prompt, system=None):
        calls.append((prompt, system))
        return reply("hola")

    translator = VoiceboxTranslator(SimpleNamespace(llm_generate=generate))
    assert translator.translate("hello", "en", "es", 40) == "hola"
    assert "translations" in calls[0][0]
    assert "40" in calls[0][1] and "en" in calls[0][1] and "es" in calls[0][1]


def test_cancellation_is_not_retried_or_wrapped(setup_driver):
    driver, _ = setup_driver([JobCancelled("cancelled")])
    with pytest.raises(JobCancelled):
        PromptureTranslator("local/test").translate("hello", "en", "es")
    assert len(driver.calls) == 1


def test_provider_error_is_not_treated_as_bad_json(setup_driver):
    error = RuntimeError("overloaded")
    error.status_code = 529
    driver, _ = setup_driver([error])
    with pytest.raises(TranslationError) as caught:
        PromptureTranslator("local/test").translate("hello", "en", "es")
    assert caught.value.status == 529
    assert len(driver.calls) == 1


def test_missing_package_is_actionable(monkeypatch):
    monkeypatch.setitem(sys.modules, "prompture", None)
    with pytest.raises(ConfigError, match="pip install"):
        PromptureTranslator("local/test").translate("hello", "en", "es")


def test_driver_init_failure(monkeypatch):
    def fail(*args, **kwargs):
        raise ConnectionError("server unavailable")
    monkeypatch.setattr(prompture, "get_driver_for_model", fail)
    with pytest.raises(ConfigError, match="server unavailable"):
        PromptureTranslator("local/test").translate("hello", "en", "es")


def test_blank_input_and_passthrough_need_no_prompture(monkeypatch):
    monkeypatch.setitem(sys.modules, "prompture", None)
    assert PromptureTranslator("local/test").translate(" \n", "en", "es") == " \n"
    assert PassthroughTranslator().translate("hello", "en", "es") == "hello"

"""The shared model client: driver setup, retries, the budget and the guard."""

import json

import prompture
import pytest
from prompture.drivers.base import Driver
from pydantic import BaseModel

from doblarr import llm


class Mood(BaseModel):
    mood: str


class OfflineDriver(Driver):
    def __init__(self, replies):
        self.replies = iter(replies)
        self.prompts = []

    def generate(self, prompt, options):
        self.prompts.append(prompt)
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        return {"text": value, "meta": {"total_tokens": 5, "cost": 0.0}}


@pytest.fixture
def driver(monkeypatch):
    made = {}

    def install(replies):
        fake = OfflineDriver(replies)

        def factory(model, **overrides):
            made.update(model=model, overrides=overrides)
            return fake
        monkeypatch.setattr(prompture, "get_driver_for_model", factory)
        return fake, made
    return install


def test_driver_for_passes_the_endpoint_and_a_claude_key(driver, monkeypatch):
    _, made = driver([])
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    llm.driver_for("claude/sonnet", endpoint="http://127.0.0.1:9")
    assert made["overrides"] == {"endpoint": "http://127.0.0.1:9", "api_key": "sk-test"}
    llm.driver_for("ollama/qwen3:8b")
    assert made["overrides"] == {}


def test_a_driver_that_cannot_start_is_unavailable(monkeypatch):
    def broken(model, **overrides):
        raise RuntimeError("no such provider")
    monkeypatch.setattr(prompture, "get_driver_for_model", broken)
    with pytest.raises(llm.ModelUnavailable, match="no such provider"):
        llm.driver_for("nope/model")


def test_an_invalid_reply_is_retried_with_the_error_then_given_up(driver):
    fake, _ = driver(["not json", json.dumps({"mood": "calm"})])
    client = llm.Client("ollama/test")
    assert client.ask(Mood, "Read the mood.", {"line": "Kaito waits."}).mood == "calm"
    assert "previous response was invalid" in fake.prompts[1]

    driver(["nope", "still not json"])
    with pytest.raises(llm.InvalidReply):
        llm.Client("ollama/test").ask(Mood, "Read the mood.", {"line": "Mina waits."})


def test_a_spent_budget_sends_nothing(driver):
    fake, _ = driver([json.dumps({"mood": "calm"})])

    class Spent:
        def charge(self, kind):
            return False

    with pytest.raises(llm.BudgetExhausted):
        llm.Client("ollama/test", budget=Spent()).ask(Mood, "Read the mood.", {"line": "Ren"})
    assert fake.prompts == []


def test_a_transport_failure_is_unavailability(driver):
    driver([ConnectionError("refused")])
    with pytest.raises(llm.ModelUnavailable, match="refused"):
        llm.Client("ollama/test").ask(Mood, "Read the mood.", {"line": "Sora"})

"""One validated JSON request to a language model, through Prompture.

Narrative extraction and the recommendation judge both ask a model for a typed
answer. They share this: Prompture picks native structured output or prompted
JSON, the reply is validated against a Pydantic schema, an invalid reply is
retried once with feedback and then reported as invalid (never repaired into a
guess), every request is charged to the run's request budget, and an optional
holdout guard scans the request before it leaves the process.

The model string is a Prompture one (``ollama/qwen3:8b``, ``claude/...``). The
caller decides which; nothing here picks a remote provider on its own.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from .errors import DoblarrError

log = logging.getLogger("doblarr.llm")


class ModelUnavailable(DoblarrError):
    """The model could not be reached or initialised."""


class InvalidReply(DoblarrError):
    """The model answered, but not in the shape asked for (after a retry)."""


class BudgetExhausted(DoblarrError):
    """The run's allowance for model requests is spent."""


class Client:
    """A lazily initialised Prompture driver for one model string."""

    def __init__(self, model: str, *, endpoint: str | None = None, guard=None,
                 budget=None, budget_kind: str = "llm", timeout: int = 300,
                 think: bool | None = None):
        self.model = model
        # A reasoning model's thinking counts against the reply length; a quick
        # reading (a still's expression) asks it not to think at all.
        self.think = think
        self.endpoint = endpoint
        self.guard = guard
        self.budget = budget
        self.budget_kind = budget_kind
        self.timeout = timeout
        self.calls = 0
        self.seconds = 0.0
        self.usage: list[dict] = []
        self._driver: Any = None

    def _get_driver(self):
        if self._driver is not None:
            return self._driver
        try:
            import prompture
        except ImportError as exc:
            raise ModelUnavailable("Prompture is not installed") from exc
        overrides: dict[str, Any] = {}
        if self.endpoint:
            overrides["endpoint"] = self.endpoint
        if self.model.startswith("claude/") and os.environ.get("ANTHROPIC_API_KEY"):
            overrides["api_key"] = os.environ["ANTHROPIC_API_KEY"]
        try:
            driver = prompture.get_driver_for_model(self.model, **overrides)
        except Exception as exc:  # noqa: BLE001 - any init failure: unavailable
            raise ModelUnavailable(f"could not initialise {self.model}: {exc}") from exc
        if self.guard is not None:
            from .studio.holdout import GuardedDriver

            driver = GuardedDriver(driver, self.guard, "model request")
        self._driver = driver
        return driver

    def ask(self, schema: type[BaseModel], system: str, payload: dict, *,
            max_tokens: int = 4096, retries: int = 1, images: list | None = None) -> BaseModel:
        """A validated instance of `schema`, or InvalidReply/ModelUnavailable.

        `images` (bytes, paths or data URLs) go with the request to a model
        that can see; Prompture converts them for the provider."""
        import prompture
        from prompture.exceptions import ExtractionError

        if self.budget is not None and not self.budget.charge(self.budget_kind):
            raise BudgetExhausted(f"the request budget for {self.budget_kind} is spent")
        driver = self._get_driver()
        content = json.dumps(payload, ensure_ascii=False, default=str)
        feedback = ""
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            started = time.perf_counter()
            try:
                self.calls += 1
                result = prompture.ask_for_json(
                    driver=driver, content_prompt=content + feedback,
                    json_schema=schema.model_json_schema(), system_prompt=system,
                    model_name=self.model.split("/", 1)[-1],
                    options={"timeout": self.timeout, "max_tokens": max_tokens,
                             **({"think": self.think} if self.think is not None else {})},
                    ai_cleanup=False, cache=False, images=images or None)
                self.usage.append(result.get("usage", {}))
                return schema.model_validate(result["json_object"])
            except (ExtractionError, ValidationError, KeyError, TypeError) as exc:
                last_error = exc
                feedback = ("\nThe previous response was invalid: "
                            f"{str(exc)[:300]}. Return JSON matching the schema exactly.")
                log.warning("llm: invalid reply from %s (attempt %d)", self.model, attempt + 1)
            except DoblarrError:
                raise       # a holdout guard or the budget refused it; not unavailability
            except Exception as exc:  # noqa: BLE001 - transport failures are unavailability
                raise ModelUnavailable(f"{self.model} failed: {exc}") from exc
            finally:
                self.seconds += time.perf_counter() - started
        raise InvalidReply(f"{self.model} returned invalid JSON after {retries + 1} attempts: "
                           f"{last_error}")

    def describe(self) -> dict:
        return {"model": self.model, "calls": self.calls, "seconds": round(self.seconds, 2),
                "endpoint": bool(self.endpoint)}

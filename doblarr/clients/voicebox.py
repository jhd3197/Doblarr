"""HTTP client for the voicebox service (TTS + voice cloning).

Endpoints wired against voicebox's OpenAPI spec:
    GET  /health
    POST /transcribe                       (multipart: file, language)
    POST /profiles                         (json: name, description, language)
    POST /profiles/{profile_id}/samples    (multipart: file, reference_text)
    POST /generate                         (json: profile_id, text, language, seed, model_size)
    GET  /generate/{generation_id}/status
    GET  /audio/{generation_id}            (returns the rendered audio)

Doblarr never imports a TTS model directly — it drives voicebox over HTTP so the
two projects stay decoupled and voicebox upgrades come for free.
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

from ..artifacts import read_json
from ..errors import ArrClientError, JobCancelled
from ..telemetry import write_json
from .base import ArrClient


class VoiceboxError(ArrClientError, RuntimeError):
    pass


class GenerationFailed(VoiceboxError):
    """A confirmed terminal failure; safe to submit a new generation."""


# Engines whose /generate accepts an `instruct` delivery direction. This is
# the adapter's answer to "can this engine be directed", and it is the only
# place that answer is written down: `generate` refuses instructions for
# anything else, and `doblarr.performance` asks here before claiming an
# instruction was applied.
DIRECTABLE_ENGINES = frozenset({"qwen", "qwen_custom_voice"})
# Aliases the service accepts for the same engine.
ENGINE_ALIASES = {"chatterbox-multilingual": "chatterbox", "qwen3-tts": "qwen"}


class VoiceboxClient(ArrClient):
    service = "voicebox"
    error_cls = VoiceboxError

    @staticmethod
    def canonical_engine(engine: str) -> str:
        return ENGINE_ALIASES.get(str(engine or ""), str(engine or ""))

    @classmethod
    def supports_direction(cls, engine: str) -> bool:
        """Whether `engine` can be given a delivery instruction at all."""
        return cls.canonical_engine(engine) in DIRECTABLE_ENGINES

    def __init__(self, base_url: str, timeout: int = 600):
        super().__init__(base_url, timeout=timeout)
        self.observer = None

    def observe(self, name, value=1):
        if self.observer:
            self.observer(name, value)

    # -- health -----------------------------------------------------------
    def health(self, timeout: int = 15) -> dict:
        """Return service health, or raise if unreachable."""
        return self._get("/health", timeout=timeout)

    # -- local LLM (translation, refinement) ------------------------------
    def llm_generate(self, prompt: str, system: str | None = None) -> str:
        payload: dict = {"prompt": prompt}
        if system:
            payload["system"] = system
        data = self._post("/llm/generate", json=payload)
        # Accept a few common shapes.
        return (data.get("text") or data.get("response") or data.get("output") or "").strip()

    # -- transcription ----------------------------------------------------
    def transcribe(self, audio: Path, language: str | None = None) -> dict:
        with open(audio, "rb") as fh:
            files = {"file": (audio.name, fh)}
            data = {"language": language} if language else {}
            return self._post("/transcribe", files=files, data=data)

    # -- voice profiles (cloning) -----------------------------------------
    def list_voices(self) -> list[dict]:
        """Available voices (voicebox profiles) as [{id, name}]."""
        data = self._get("/profiles")
        profiles = data if isinstance(data, list) else data.get("profiles", [])
        return [
            {"id": p.get("id") or p.get("profile_id"), "name": p.get("name", "?")} for p in profiles
        ]

    def create_profile(self, name: str, language: str, description: str = "") -> str:
        payload = {"name": name, "language": language, "description": description}
        data = self._post("/profiles", json=payload)
        profile_id = data.get("id") or data.get("profile_id")
        if not profile_id:
            raise VoiceboxError(f"no profile id in response: {data}")
        return profile_id

    def voice_profiles(self):
        data = self._get("/profiles")
        return data if isinstance(data, list) else data.get("profiles", [])

    def preset_voices(self, engine):
        return self._get(f"/profiles/presets/{engine}").get("voices", [])

    def register_preset(self, voice, engine):
        for profile in self.voice_profiles():
            if (
                profile.get("preset_engine") == engine
                and profile.get("preset_voice_id") == voice["voice_id"]
            ):
                return profile["id"]
        data = self._post(
            "/profiles",
            json={
                "name": voice["name"],
                "language": voice["language"],
                "voice_type": "preset",
                "preset_engine": engine,
                "preset_voice_id": voice["voice_id"],
                "default_engine": engine,
            },
        )
        return data["id"]

    def add_sample(self, profile_id: str, sample: Path, reference_text: str) -> dict:
        with open(sample, "rb") as fh:
            files = {"file": (sample.name, fh)}
            data = {"reference_text": reference_text}
            return self._post(f"/profiles/{profile_id}/samples", files=files, data=data)

    # -- speech generation ------------------------------------------------
    def generate(
        self,
        profile_id: str,
        text: str,
        language: str,
        seed: int | None = None,
        model_size: str | None = None,
        engine: str | None = None,
        instruct: str | None = None,
    ) -> str:
        payload: dict = {"profile_id": profile_id, "text": text, "language": language}
        if seed is not None:
            payload["seed"] = seed
        if model_size is not None:
            payload["model_size"] = model_size
        if engine is not None:
            payload["engine"] = self.canonical_engine(engine)
        if instruct:
            if not self.supports_direction(payload.get("engine") or ""):
                raise VoiceboxError("delivery instructions require a Qwen engine")
            payload["instruct"] = instruct
        # Creation is not idempotent. A lost response must not silently submit twice.
        data = self._attempt("POST", "/generate", json=payload).json()
        gen_id = data.get("id") or data.get("generation_id")
        if not gen_id:
            raise VoiceboxError(f"no generation id in response: {data}")
        return gen_id

    def wait_for(self, generation_id: str, poll: float = 0.15, cancel_event=None) -> None:
        """Block until a generation reports a terminal status.

        Status lives on the generation record at /history/{id} (the
        /generate/{id}/status route returns nothing useful in practice).
        `cancel_event` aborts the wait each poll iteration and requests remote
        cancellation. The remote request is best-effort; a disconnected server
        may continue generating.
        """
        deadline = time.monotonic() + self.timeout
        delay = max(0.01, poll)
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                self._cancel_quietly(generation_id)
                raise JobCancelled(f"cancelled while waiting for {generation_id}")
            data = self._get(f"/history/{generation_id}", timeout=30)
            self.observe("tts_polls")
            status = (data.get("status") or "").lower()
            if status in {"done", "completed", "success", "ready"}:
                return
            if status in {"failed", "error", "cancelled", "canceled"}:
                raise GenerationFailed(
                    f"generation {generation_id} failed: {data.get('error') or data}"
                )
            if cancel_event is not None:
                cancel_event.wait(delay)
            else:
                time.sleep(delay)
            delay = min(1.0, delay * 1.5)
        raise VoiceboxError(f"generation {generation_id} timed out")

    def _cancel_quietly(self, generation_id: str) -> None:
        """Best-effort server-side cancel so a wedged generation frees the queue."""
        with contextlib.suppress(Exception):
            self._post(f"/generate/{generation_id}/cancel", json={})

    def download_audio(self, generation_id: str, dest: Path) -> Path:
        resp = self._request("GET", f"/audio/{generation_id}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix(dest.suffix + ".partial")
        temp.write_bytes(resp.content)
        temp.replace(dest)
        return dest

    # -- convenience ------------------------------------------------------
    def synthesize_to_file(
        self, profile_id: str, text: str, language: str, dest: Path, cancel_event=None, **kwargs
    ) -> Path:
        """Full round-trip: generate -> wait -> download."""
        receipt = dest.with_suffix(".request.json")
        request = {
            "server": self.base_url,
            "profile": profile_id,
            "text": text,
            "language": language,
            "options": kwargs,
        }
        saved = read_json(receipt)
        gen_id = saved.get("generation_id") if saved.get("request") == request else None
        if not gen_id:
            gen_id = self.generate(profile_id, text, language, **kwargs)
            write_json(receipt, {"request": request, "generation_id": gen_id})
        else:
            self.observe("remote_resumes")
        try:
            started = time.perf_counter()
            try:
                self.wait_for(gen_id, cancel_event=cancel_event)
            finally:
                self.observe("tts_wait_seconds", time.perf_counter() - started)
            started = time.perf_counter()
            result = self.download_audio(gen_id, dest)
            self.observe("tts_download_seconds", time.perf_counter() - started)
        except GenerationFailed:
            receipt.unlink(missing_ok=True)
            raise
        except VoiceboxError as exc:
            if exc.status == 404:
                receipt.unlink(missing_ok=True)
                raise GenerationFailed("remote generation no longer exists") from exc
            raise
        receipt.unlink(missing_ok=True)
        return result

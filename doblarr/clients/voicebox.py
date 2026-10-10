"""voicebox adapter for the speech contract (TTS + voice cloning + local LLM).

Endpoints wired against voicebox's OpenAPI spec:
    GET  /health
    POST /llm/generate                     (json: prompt, system, ...)
    POST /transcribe                       (multipart: file, language)
    GET  /profiles, /profiles/presets/{engine}
    POST /profiles                         (json: name, description, language)
    POST /profiles/{profile_id}/samples    (multipart: file, reference_text)
    POST /generate                         (json: profile_id, text, language, seed, model_size)
    GET  /history/{generation_id}          (the generation's status)
    GET  /audio/{generation_id}            (returns the rendered audio)
"""

from __future__ import annotations

import contextlib
import time
from pathlib import Path

from ..artifacts import read_json
from ..errors import JobCancelled
from ..fileio import write_json
from .speech import GenerationFailed, SpeechClient, SpeechError

__all__ = ["GenerationFailed", "VoiceboxClient", "VoiceboxError"]


class VoiceboxError(SpeechError):
    pass


# Engines whose /generate accepts an `instruct` delivery direction. This is
# the adapter's answer to "can this engine be directed", and it is the only
# place that answer is written down: `generate` refuses instructions for
# anything else, and `doblarr.performance` asks here before claiming an
# instruction was applied.
DIRECTABLE_ENGINES = frozenset({"qwen", "qwen_custom_voice"})
# Engines that clone from a reference sample.
CLONE_ENGINES = frozenset({"chatterbox", "chatterbox_turbo", "qwen"})
# Engines whose /generate takes exaggeration / cfg_weight / temperature. Only
# the jhd3197/voicebox fork accepts them; upstream ignores unknown fields.
SAMPLING_ENGINES = frozenset({"chatterbox"})
SAMPLING_KEYS = ("exaggeration", "cfg_weight", "temperature")
# Aliases the service accepts for the same engine.
ENGINE_ALIASES = {"chatterbox-multilingual": "chatterbox", "qwen3-tts": "qwen"}


class VoiceboxClient(SpeechClient):
    service = "voicebox"
    error_cls = VoiceboxError
    preset_engines = ("kokoro", "qwen_custom_voice")

    @staticmethod
    def canonical_engine(engine: str) -> str:
        return ENGINE_ALIASES.get(str(engine or ""), str(engine or ""))

    @classmethod
    def supports_direction(cls, engine: str) -> bool:
        """Whether `engine` can be given a delivery instruction at all."""
        return cls.canonical_engine(engine) in DIRECTABLE_ENGINES

    @classmethod
    def supports_sampling(cls, engine: str) -> bool:
        return cls.canonical_engine(engine) in SAMPLING_ENGINES

    @classmethod
    def supports_cloning(cls, engine: str) -> bool:
        canonical = cls.canonical_engine(engine)
        return canonical in CLONE_ENGINES or not canonical

    def fork(self) -> VoiceboxClient:
        return VoiceboxClient(self.base_url, timeout=self.timeout)

    # -- health -----------------------------------------------------------
    def health(self, timeout: int = 15) -> dict:
        """Return service health, or raise if unreachable."""
        return self._get("/health", timeout=timeout)

    # -- local LLM (translation, refinement) ------------------------------
    def llm_generate(self, prompt: str, system: str | None = None, **options) -> str:
        payload: dict = {"prompt": prompt}
        if system:
            payload["system"] = system
        # model_size / max_tokens / temperature, only when a caller pins them.
        payload.update({k: v for k, v in options.items() if v is not None})
        data = self._post("/llm/generate", json=payload)
        # Accept a few common shapes.
        return (data.get("text") or data.get("response") or data.get("output") or "").strip()

    # -- transcription ----------------------------------------------------
    def transcribe(self, audio: Path, language: str | None = None) -> dict:
        # Bytes, not an open file: a retry must resend the whole clip, and a
        # file handle is already at its end after the first attempt.
        files = {"file": (audio.name, audio.read_bytes())}
        data = {"language": language} if language else {}
        return self._post("/transcribe", files=files, data=data)

    # -- voice profiles (cloning) -----------------------------------------
    def create_profile(self, name: str, language: str, description: str = "") -> str:
        payload = {"name": name, "language": language, "description": description}
        # Creation is not idempotent: a retry after a lost response would make a twin.
        data = self._attempt("POST", "/profiles", json=payload).json()
        profile_id = data.get("id") or data.get("profile_id")
        if not profile_id:
            raise VoiceboxError(f"no profile id in response: {data}")
        return profile_id

    def voice_profiles(self) -> list[dict]:
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
        data = self._attempt(
            "POST", "/profiles",
            json={
                "name": voice["name"],
                "language": voice["language"],
                "voice_type": "preset",
                "preset_engine": engine,
                "preset_voice_id": voice["voice_id"],
                "default_engine": engine,
            },
        ).json()
        return data["id"]

    def add_sample(self, profile_id: str, sample: Path, reference_text: str) -> dict:
        files = {"file": (sample.name, sample.read_bytes())}
        data = {"reference_text": reference_text}
        # Not retried: each POST adds another sample to the profile.
        return self._attempt("POST", f"/profiles/{profile_id}/samples",
                             files=files, data=data).json()

    def clone_voice(self, name: str, language: str, sample: Path, reference_text: str,
                    description: str = "") -> str:
        profile_id = self.create_profile(name, language, description=description)
        try:
            self.add_sample(profile_id, sample, reference_text)
        except SpeechError:
            # A rejected sample would leave an empty profile behind.
            with contextlib.suppress(SpeechError):
                self.delete_profile(profile_id)
            raise
        return profile_id

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
        sampling: dict | None = None,
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
        if sampling:
            if not self.supports_sampling(payload.get("engine") or ""):
                raise VoiceboxError("sampling overrides require the chatterbox engine")
            payload.update({k: sampling[k] for k in SAMPLING_KEYS if sampling.get(k) is not None})
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

    def generation_status(self, generation_id: str) -> dict:
        data = self._get(f"/history/{generation_id}", timeout=30)
        return {"status": data.get("status"), "error": data.get("error")}

    def fetch_audio(self, generation_id: str) -> bytes:
        return self._request("GET", f"/audio/{generation_id}").content

    def download_audio(self, generation_id: str, dest: Path) -> Path:
        audio = self.fetch_audio(generation_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix(dest.suffix + ".partial")
        temp.write_bytes(audio)
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

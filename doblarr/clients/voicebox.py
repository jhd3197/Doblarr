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

import time
from pathlib import Path

from ..errors import ArrClientError
from .base import ArrClient


class VoiceboxError(ArrClientError, RuntimeError):
    pass


class VoiceboxClient(ArrClient):
    service = "voicebox"
    error_cls = VoiceboxError

    def __init__(self, base_url: str, timeout: int = 600):
        super().__init__(base_url, timeout=timeout)

    # -- health -----------------------------------------------------------
    def health(self) -> dict:
        """Return service health, or raise if unreachable."""
        return self._get("/health", timeout=15)

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
    def create_profile(self, name: str, language: str,
                       description: str = "") -> str:
        payload = {"name": name, "language": language, "description": description}
        data = self._post("/profiles", json=payload)
        profile_id = data.get("id") or data.get("profile_id")
        if not profile_id:
            raise VoiceboxError(f"no profile id in response: {data}")
        return profile_id

    def add_sample(self, profile_id: str, sample: Path, reference_text: str) -> dict:
        with open(sample, "rb") as fh:
            files = {"file": (sample.name, fh)}
            data = {"reference_text": reference_text}
            return self._post(f"/profiles/{profile_id}/samples",
                              files=files, data=data)

    # -- speech generation ------------------------------------------------
    def generate(self, profile_id: str, text: str, language: str,
                 seed: int | None = None, model_size: str | None = None) -> str:
        payload: dict = {"profile_id": profile_id, "text": text, "language": language}
        if seed is not None:
            payload["seed"] = seed
        if model_size is not None:
            payload["model_size"] = model_size
        data = self._post("/generate", json=payload)
        gen_id = data.get("id") or data.get("generation_id")
        if not gen_id:
            raise VoiceboxError(f"no generation id in response: {data}")
        return gen_id

    def wait_for(self, generation_id: str, poll: float = 1.0) -> None:
        """Block until a generation reports a terminal status.

        Status lives on the generation record at /history/{id} (the
        /generate/{id}/status route returns nothing useful in practice).
        """
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            data = self._get(f"/history/{generation_id}", timeout=30)
            status = (data.get("status") or "").lower()
            if status in {"done", "completed", "success", "ready"}:
                return
            if status in {"failed", "error"}:
                raise VoiceboxError(
                    f"generation {generation_id} failed: {data.get('error') or data}")
            time.sleep(poll)
        raise VoiceboxError(f"generation {generation_id} timed out")

    def download_audio(self, generation_id: str, dest: Path) -> Path:
        resp = self._request("GET", f"/audio/{generation_id}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        return dest

    # -- convenience ------------------------------------------------------
    def synthesize_to_file(self, profile_id: str, text: str, language: str,
                           dest: Path, **kwargs) -> Path:
        """Full round-trip: generate -> wait -> download."""
        gen_id = self.generate(profile_id, text, language, **kwargs)
        self.wait_for(gen_id)
        return self.download_audio(gen_id, dest)

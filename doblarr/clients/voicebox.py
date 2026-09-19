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

import requests


class VoiceboxError(RuntimeError):
    pass


class VoiceboxClient:
    def __init__(self, base_url: str, timeout: int = 600):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- internals --------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _json(self, resp: requests.Response) -> dict:
        if not resp.ok:
            raise VoiceboxError(f"{resp.status_code} {resp.request.method} "
                                f"{resp.url}: {resp.text[:300]}")
        return resp.json()

    # -- health -----------------------------------------------------------
    def health(self) -> dict:
        """Return service health, or raise if unreachable."""
        try:
            resp = requests.get(self._url("/health"), timeout=15)
        except requests.RequestException as exc:
            raise VoiceboxError(f"voicebox unreachable at {self.base_url}: {exc}")
        return self._json(resp)

    # -- transcription ----------------------------------------------------
    def transcribe(self, audio: Path, language: str | None = None) -> dict:
        with open(audio, "rb") as fh:
            files = {"file": (audio.name, fh)}
            data = {"language": language} if language else {}
            resp = requests.post(self._url("/transcribe"), files=files,
                                 data=data, timeout=self.timeout)
        return self._json(resp)

    # -- voice profiles (cloning) -----------------------------------------
    def create_profile(self, name: str, language: str,
                       description: str = "") -> str:
        payload = {"name": name, "language": language, "description": description}
        data = self._json(requests.post(self._url("/profiles"), json=payload,
                                        timeout=self.timeout))
        profile_id = data.get("id") or data.get("profile_id")
        if not profile_id:
            raise VoiceboxError(f"no profile id in response: {data}")
        return profile_id

    def add_sample(self, profile_id: str, sample: Path, reference_text: str) -> dict:
        with open(sample, "rb") as fh:
            files = {"file": (sample.name, fh)}
            data = {"reference_text": reference_text}
            resp = requests.post(self._url(f"/profiles/{profile_id}/samples"),
                                 files=files, data=data, timeout=self.timeout)
        return self._json(resp)

    # -- speech generation ------------------------------------------------
    def generate(self, profile_id: str, text: str, language: str,
                 seed: int | None = None, model_size: str | None = None) -> str:
        payload: dict = {"profile_id": profile_id, "text": text, "language": language}
        if seed is not None:
            payload["seed"] = seed
        if model_size is not None:
            payload["model_size"] = model_size
        data = self._json(requests.post(self._url("/generate"), json=payload,
                                        timeout=self.timeout))
        gen_id = data.get("id") or data.get("generation_id")
        if not gen_id:
            raise VoiceboxError(f"no generation id in response: {data}")
        return gen_id

    def wait_for(self, generation_id: str, poll: float = 1.0) -> None:
        """Block until a generation reports a terminal status."""
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            data = self._json(requests.get(
                self._url(f"/generate/{generation_id}/status"), timeout=30))
            status = (data.get("status") or "").lower()
            if status in {"done", "completed", "success", "ready"}:
                return
            if status in {"failed", "error"}:
                raise VoiceboxError(f"generation {generation_id} failed: {data}")
            time.sleep(poll)
        raise VoiceboxError(f"generation {generation_id} timed out")

    def download_audio(self, generation_id: str, dest: Path) -> Path:
        resp = requests.get(self._url(f"/audio/{generation_id}"),
                            timeout=self.timeout)
        if not resp.ok:
            raise VoiceboxError(f"audio fetch {generation_id}: {resp.status_code}")
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

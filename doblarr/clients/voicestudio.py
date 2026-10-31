"""VoiceStudio adapter for the speech contract (TTS + voice cloning + ASR).

Endpoints used (VoiceStudio's local API, default port 3900):
    GET    /health                      (503 with a startup step while loading)
    GET    /engines/tts                 (engine inventory: supports_cloning, ...)
    GET    /profiles
    POST   /profiles                    (multipart: name, ref_audio, ref_text, language, kind)
    DELETE /profiles/{profile_id}
    POST   /v1/audio/speech             (OpenAI-compatible; voice = profile id)
    POST   /v1/audio/transcriptions     (OpenAI-compatible)

VoiceStudio renders a line in the request that asks for it, so there is no
server-side generation to poll or resume: `synthesize_to_file` blocks, and
`generate` runs the same request on a local thread for callers that poll.
VoiceStudio has no language-model endpoint, so the `voicebox` translation
provider is not available with it.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from ..cache import TTLCache
from ..errors import JobCancelled
from .speech import SpeechClient, SpeechError

# Asks the server for its active engine (any OpenAI TTS model id does).
ACTIVE_ENGINE = "tts-1"


class VoiceStudioError(SpeechError):
    pass


class VoiceStudioClient(SpeechClient):
    service = "VoiceStudio"
    error_cls = VoiceStudioError

    def __init__(self, base_url: str, timeout: int = 600, api_key: str | None = None,
                 directable_engines=()):
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
        super().__init__(base_url, timeout=timeout, headers=headers)
        self.api_key = api_key
        # VoiceStudio does not say which engines follow a free-text direction
        # (OmniVoice keeps only its voice-design tags), so only the engines a
        # person has listed after listening are directable.
        self.directable_engines = frozenset(directable_engines)
        self._engines = TTLCache(ttl=300)
        self._pending: TTLCache = TTLCache(ttl=3600, max_size=200)
        self._pool: ThreadPoolExecutor | None = None
        self._worker: VoiceStudioClient | None = None
        self._pool_lock = threading.Lock()

    def fork(self) -> VoiceStudioClient:
        return VoiceStudioClient(self.base_url, timeout=self.timeout, api_key=self.api_key,
                                 directable_engines=self.directable_engines)

    # -- engines ----------------------------------------------------------
    def supports_direction(self, engine: str) -> bool:
        return self.canonical_engine(engine) in self.directable_engines

    def supports_cloning(self, engine: str) -> bool | None:
        engines = self._engines.get("tts")
        if engines is None:
            try:
                data = self._get("/engines/tts", timeout=15)
            except SpeechError:
                return None
            rows = data if isinstance(data, list) else data.get("backends") or []
            engines = {row.get("id"): row for row in rows if isinstance(row, dict)}
            self._engines.set("tts", engines)
        row = engines.get(self.canonical_engine(engine))
        return row.get("supports_cloning") if row else None

    # -- service ----------------------------------------------------------
    def health(self, timeout: int = 15) -> dict:
        return self._get("/health", timeout=timeout)

    def transcribe(self, audio: Path, language: str | None = None) -> dict:
        # Bytes, not an open file, so a retry resends the whole clip.
        files = {"file": (audio.name, audio.read_bytes())}
        data = {"response_format": "json"}
        if language:
            data["language"] = language
        return self._post("/v1/audio/transcriptions", files=files, data=data)

    # -- voices -----------------------------------------------------------
    def voice_profiles(self) -> list[dict]:
        kinds = {"clone": "cloned", "design": "designed"}
        return [
            {**p, "voice_type": kinds.get(p.get("kind"), p.get("kind") or "cloned"),
             "description": p.get("description") or p.get("instruct") or ""}
            for p in self._get("/profiles")
        ]

    def clone_voice(self, name: str, language: str, sample: Path, reference_text: str,
                    description: str = "") -> str:
        # VoiceStudio profiles have no free-text description field.
        # Not retried: a retry after a lost response would create a second profile.
        data = self._attempt(
            "POST", "/profiles",
            files={"ref_audio": (sample.name, sample.read_bytes(), "audio/wav")},
            data={"name": name, "ref_text": reference_text, "language": language,
                  "kind": "clone"},
        ).json()
        profile_id = data.get("id")
        if not profile_id:
            raise self._error(f"no profile id in response: {data}")
        return profile_id

    # -- generation -------------------------------------------------------
    def speech(self, profile_id: str, text: str, language: str, seed: int | None = None,
               model_size: str | None = None, engine: str | None = None,
               instruct: str | None = None) -> bytes:
        """Render one line and return WAV bytes. `model_size` has no VoiceStudio meaning."""
        self._check_direction(engine, instruct)
        payload: dict = {
            "model": self.canonical_engine(engine or "") or ACTIVE_ENGINE,
            "input": text,
            "voice": profile_id,
            "language": language,
            "response_format": "wav",
        }
        if seed is not None:
            payload["seed"] = seed
        if instruct:
            payload["instruct"] = instruct
        return self._request("POST", "/v1/audio/speech", json=payload).content

    def generate(self, profile_id: str, text: str, language: str, seed: int | None = None,
                 model_size: str | None = None, engine: str | None = None,
                 instruct: str | None = None, sampling: dict | None = None) -> str:
        # VoiceStudio takes no Chatterbox sampling (supports_sampling is off).
        self._check_direction(engine, instruct)
        with self._pool_lock:
            if self._pool is None or self._worker is None:
                # One worker with its own session: previews queue, never pile up.
                self._pool = ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix="voicestudio")
                self._worker = self.fork()
            pool, worker = self._pool, self._worker
        generation_id = uuid.uuid4().hex
        self._pending.set(generation_id, pool.submit(
            worker.speech, profile_id, text, language, seed=seed, engine=engine,
            instruct=instruct))
        return generation_id

    def _future(self, generation_id: str) -> Future:
        future = self._pending.get(generation_id)
        if future is None:
            raise self._error(f"generation {generation_id} is unknown or expired", status=404)
        return future

    def generation_status(self, generation_id: str) -> dict:
        future = self._future(generation_id)
        if not future.done():
            return {"status": "generating", "error": None}
        error = future.exception()
        return {"status": "failed", "error": str(error)} if error else \
            {"status": "completed", "error": None}

    def fetch_audio(self, generation_id: str) -> bytes:
        future = self._future(generation_id)
        if not future.done():
            raise self._error(f"generation {generation_id} is not finished", status=409)
        return future.result()

    def synthesize_to_file(self, profile_id: str, text: str, language: str, dest: Path,
                           cancel_event=None, **kwargs) -> Path:
        if cancel_event is not None and cancel_event.is_set():
            raise JobCancelled("cancelled before speech generation")
        started = time.perf_counter()
        if cancel_event is None:
            audio = self.speech(profile_id, text, language, **kwargs)
        else:
            # The request cannot be interrupted; stop waiting for it instead.
            # Like voicebox's remote cancel, the server may finish the line.
            outcome: dict = {}

            def run():
                try:
                    outcome["audio"] = self.speech(profile_id, text, language, **kwargs)
                except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
                    outcome["error"] = exc

            worker = threading.Thread(target=run, daemon=True, name="voicestudio-line")
            worker.start()
            while worker.is_alive():
                if cancel_event.wait(0.25):
                    raise JobCancelled("cancelled while waiting for VoiceStudio")
            if "error" in outcome:
                raise outcome["error"]
            audio = outcome["audio"]
        self.observe("tts_wait_seconds", time.perf_counter() - started)
        dest.parent.mkdir(parents=True, exist_ok=True)
        temp = dest.with_suffix(dest.suffix + ".partial")
        temp.write_bytes(audio)
        temp.replace(dest)
        return dest

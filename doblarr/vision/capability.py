"""Which visual backends this machine can actually run, and the files they need.

Every model is a file with a published source and a checksum. Nothing is
downloaded on read: `status` only looks, and `download` is an explicit action.
A missing file makes its stage unsupported, with the reason, rather than
letting an analysis report "no faces found" when nothing looked.

Backends, chosen after checking what is installed here (2026-10):

- **live action**: OpenCV's YuNet face detector and SFace face embedding
  (opencv_zoo, Apache-2.0). They are trained on photographs of people and are
  not claimed to work on drawn characters.
- **anime**: nagadomi's ``lbpcascade_animeface`` detector (MIT) through OpenCV,
  and DINOv2-small image embeddings (Apache-2.0, via transformers) for matching
  a face crop against approved references. DINOv2 is a general image model, not
  a character recogniser; its matches are proposals.
- **shots**: FFmpeg's scene-change score (no extra dependency); PySceneDetect is
  used instead when it is installed.
- **active speaker**: mouth-region motion inside a face track, correlated with
  the line's speech energy (doblarr.vision.active). TalkNet-ASD was evaluated
  as the research reference; its weights are distributed through a file host
  and it is not installed here, so it is not offered.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path

from ..fileio import file_sha256


@dataclass(frozen=True)
class ModelFile:
    id: str
    name: str
    url: str
    sha256: str
    size: int
    license: str
    domain: str          # live_action | anime | any
    role: str            # detector | embedding


MODELS: dict[str, ModelFile] = {m.id: m for m in (
    ModelFile("yunet", "YuNet face detector (2023mar)",
              "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
              "face_detection_yunet_2023mar.onnx",
              "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4", 232589,
              "Apache-2.0", "live_action", "detector"),
    ModelFile("sface", "SFace face embedding (2021dec)",
              "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/"
              "face_recognition_sface_2021dec.onnx",
              "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79", 38696353,
              "Apache-2.0", "live_action", "embedding"),
    ModelFile("animeface", "lbpcascade_animeface",
              "https://raw.githubusercontent.com/nagadomi/lbpcascade_animeface/master/"
              "lbpcascade_animeface.xml",
              "9376d30ac38db6bda2a68b88b3b76bbd7e6aa33af47f7f5c76bc88ca75f1ce30", 246945,
              "MIT", "anime", "detector"),
)}
DINO = "facebook/dinov2-small"
FILENAMES = {"yunet": "face_detection_yunet_2023mar.onnx",
             "sface": "face_recognition_sface_2021dec.onnx",
             "animeface": "lbpcascade_animeface.xml"}


def folder(config=None) -> Path:
    section = (config.get("vision") if config is not None else None) or {}
    if section.get("models_dir"):
        return Path(section["models_dir"])
    base = getattr(config, "work_dir", None) or Path("work")
    return Path(base) / "models" / "vision"


def path_of(model_id: str, config=None) -> Path:
    return folder(config) / FILENAMES[model_id]


def has_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def dino_cached() -> bool:
    """Whether the DINOv2 weights are already on disk (never downloads)."""
    try:
        from huggingface_hub import try_to_load_from_cache

        found = try_to_load_from_cache(DINO, "model.safetensors")
        return isinstance(found, str) and Path(found).is_file()
    except Exception:  # noqa: BLE001 - no hub library or no cache: not cached
        return False


def status(config=None) -> dict:
    """What each domain can do here, and why not when it cannot."""
    section = (config.get("vision") if config is not None else None) or {}
    files = {mid: path_of(mid, config).is_file() for mid in MODELS}
    cv2 = has_module("cv2")
    transformers = has_module("transformers") and has_module("torch")
    live = {"detector": cv2 and files["yunet"], "embedding": cv2 and files["sface"]}
    anime = {"detector": cv2 and files["animeface"],
             "embedding": transformers and dino_cached()}
    reasons = []
    if not cv2:
        reasons.append("OpenCV is not installed (pip install opencv-python-headless<5)")
    for mid, present in files.items():
        if not present:
            reasons.append(f"{MODELS[mid].name} is not downloaded")
    if not transformers:
        reasons.append("transformers/torch are not installed (anime matching)")
    elif not dino_cached():
        reasons.append(f"{DINO} is not in the local model cache (anime matching)")
    enabled = section.get("backend", "auto") != "off"
    return {
        "enabled": enabled,
        "shots": {"ffmpeg": True, "pyscenedetect": has_module("scenedetect")},
        "live_action": live, "anime": anime,
        "active_speaker": {"mouth_motion": cv2, "talknet": False,
                           "note": "mouth-motion correlation; TalkNet is not installed"},
        "files": {mid: {"present": files[mid], "path": str(path_of(mid, config)),
                        "url": MODELS[mid].url, "license": MODELS[mid].license,
                        "size": MODELS[mid].size} for mid in MODELS},
        "reasons": reasons,
    }


def unsupported_stages(config=None) -> dict[str, str]:
    """Visual stages that cannot run here, with the reason (for coverage pages)."""
    found = status(config)
    if not found["enabled"]:
        return {s: "visual analysis is off (vision.backend)" for s in
                ("faces", "tracks", "active_speaker", "association")}
    out: dict[str, str] = {}
    can_detect = any(found[d]["detector"] for d in ("live_action", "anime"))
    if not can_detect:
        why = "; ".join(found["reasons"]) or "no face detector available"
        for stage in ("faces", "tracks", "active_speaker", "association"):
            out[stage] = why
    return out


def verify(path: Path, model: ModelFile) -> bool:
    return file_sha256(path) == model.sha256


def download(model_id: str, config=None) -> Path:
    """Fetch one model file and check it against its published checksum."""
    import requests

    model = MODELS[model_id]
    dest = path_of(model_id, config)
    if dest.is_file() and verify(dest, model):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    with requests.get(model.url, stream=True, timeout=60) as response:
        response.raise_for_status()
        with part.open("wb") as out:
            for chunk in response.iter_content(1 << 20):
                out.write(chunk)
    if not verify(part, model):
        part.unlink(missing_ok=True)
        raise OSError(f"{model.name}: the downloaded file does not match its checksum")
    part.replace(dest)
    return dest

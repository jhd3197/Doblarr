"""Explicit generation presets; never mutate the user's saved settings."""


def effective_config(config):
    name = config["dub"].get("preset", "custom")
    if name == "preview":
        return config.with_overrides(
            {
                "separate.model": "htdemucs",
                "dub.voice_mode": "preset",
                "dub.max_fit_attempts": 0,
                "voicebox.default_engine": config["voicebox"].get("preview_engine", "kokoro"),
            }
        )
    if name == "final":
        return config.with_overrides({"dub.duration_match": True})
    if name != "custom":
        raise ValueError(f"unknown generation preset: {name}")
    return config

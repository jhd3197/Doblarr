"""requirements.txt (Docker, run from source) must match pyproject's dependencies."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _names(specs) -> set[str]:
    return {re.split(r"[\[<>=!~ ]", spec.strip(), maxsplit=1)[0].lower() for spec in specs}


def test_requirements_list_every_core_dependency():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    required = [line.split("#")[0] for line in lines if line.strip() and not line.startswith("#")]
    assert _names(required) == _names(pyproject["project"]["dependencies"])


# Values the app sets on a job or a run, never a person in config.yaml, and
# translate.locale, kept only so old files still load (it migrates).
NOT_USER_SETTINGS = {
    "translate.reference_file", "translate.holdout_files", "translate.locale", "dub.version_name",
    "dub.line_edits", "dub.candidates", "levels.gains", "analysis.stages", "adaptive.lines",
    "knowledge.pack_releases",
}


def test_every_setting_is_documented_in_the_example():
    import yaml

    from doblarr.config_schema import ConfigModel

    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    example = yaml.safe_load(text) or {}
    missing = []
    for section, field in ConfigModel.model_fields.items():
        for key in field.annotation.model_fields:
            dotted = f"{section}.{key}"
            listed = key in (example.get(section) or {}) or f"# {key}:" in text
            if not listed and dotted not in NOT_USER_SETTINGS:
                missing.append(dotted)
    assert missing == [], "document these in config.example.yaml: " + ", ".join(missing)


def test_the_example_configs_are_valid():
    import yaml

    from doblarr.config import DEFAULTS, _deep_merge
    from doblarr.config_schema import config_problems

    for name in ("config.example.yaml", "config.docker.example.yaml"):
        data = yaml.safe_load((ROOT / name).read_text(encoding="utf-8"))
        assert config_problems(_deep_merge(DEFAULTS, data)) == [], name

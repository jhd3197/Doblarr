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

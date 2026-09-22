"""Allocate automatic container versions without committing bot changes to dev."""

import os
import re
import subprocess
from pathlib import Path

STABLE_TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


def stable_version(tag: str) -> tuple[int, ...] | None:
    match = STABLE_TAG.fullmatch(tag)
    return tuple(map(int, match.groups())) if match else None


def resolve_version(
    tags: list[str], head_tags: list[str], initial: str, branch: str, run_number: str
) -> str:
    """Reuse this commit's stable tag on retries; otherwise advance the patch."""
    if branch not in {"dev", "main", "master"}:
        raise ValueError(f"Unsupported release branch: {branch}")
    released = [value for tag in tags if (value := stable_version(tag)) is not None]
    current = [value for tag in head_tags if (value := stable_version(tag)) is not None]
    if branch != "dev" and current:
        if released and max(current) < max(released):
            raise ValueError("Refusing to move latest back to an older stable release")
        return ".".join(map(str, max(current)))
    if released:
        major, minor, patch = max(released)
        base = f"{major}.{minor}.{patch + 1}"
    else:
        if stable_version(f"v{initial}") is None:
            raise ValueError(f"Invalid initial version: {initial}")
        base = initial
    if branch == "dev":
        if not run_number.isdigit() or int(run_number) < 1:
            raise ValueError("Dev versions require a positive workflow run number")
        return f"{base}.dev{run_number}"
    return base


def git_tags(*args: str) -> list[str]:
    return subprocess.check_output(["git", "tag", *args], text=True).splitlines()


def main() -> None:
    source = Path("doblarr/__init__.py")
    content = source.read_text(encoding="utf-8")
    match = re.search(r'^__version__ = "([^"]+)"$', content, re.MULTILINE)
    if match is None:
        raise ValueError("Cannot find the initial package version")
    branch = os.environ["GITHUB_REF_NAME"]
    version = resolve_version(
        git_tags("--list"), git_tags("--points-at", "HEAD"), match[1],
        branch, os.environ["GITHUB_RUN_NUMBER"],
    )
    # This modifies only the runner's build context, not either Git branch.
    source.write_text(
        content[:match.start(1)] + version + content[match.end(1):], encoding="utf-8"
    )
    outputs = {
        "version": version,
        "tag": f"v{version}",
        "channel": "dev" if branch == "dev" else "latest",
        "image": f"ghcr.io/{os.environ['GITHUB_REPOSITORY'].lower()}",
    }
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.writelines(f"{key}={value}\n" for key, value in outputs.items())
    print(f"Publishing {outputs['image']}:{version} ({outputs['channel']})")


if __name__ == "__main__":
    main()

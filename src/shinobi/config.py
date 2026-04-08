from __future__ import annotations

import json
import socket
import subprocess
import uuid
from pathlib import Path

from .models import Config


def discover_workspace_root(cwd: Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    root = result.stdout.strip()
    if not root:
        return cwd
    return Path(root)


def discover_repo_slug(cwd: Path) -> str:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    remote = result.stdout.strip()
    if not remote:
        return "unknown/unknown"
    if remote.startswith("git@github.com:"):
        return remote.removeprefix("git@github.com:").removesuffix(".git")
    if remote.startswith("https://github.com/"):
        return remote.removeprefix("https://github.com/").removesuffix(".git")
    return remote.removesuffix(".git")


def build_agent_identity(repo: str) -> str:
    host = socket.gethostname()
    suffix = uuid.uuid4().hex[:8]
    return f"{repo}#default@{host}-{suffix}"


def default_config(cwd: Path) -> Config:
    repo = discover_repo_slug(cwd)
    return Config(repo=repo, agent_identity=build_agent_identity(repo))


def save_config(path: Path, config: Config) -> None:
    path.write_text(json.dumps(config.to_dict(), indent=2) + "\n", encoding="utf-8")


def load_config(path: Path) -> Config:
    return Config.from_dict(json.loads(path.read_text(encoding="utf-8")))

from __future__ import annotations

import json
import subprocess
from json import JSONDecodeError
from pathlib import Path


def select_ready_issue(root: Path, ready_label: str) -> int | None:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "list",
            "--label",
            ready_label,
            "--state",
            "open",
            "--limit",
            "1",
            "--json",
            "number",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(stderr or "failed to list ready issues with gh")

    try:
        issues = json.loads(result.stdout or "[]")
    except JSONDecodeError as error:
        raise RuntimeError("failed to parse ready issue list from gh") from error

    if not issues:
        return None
    return int(issues[0]["number"])

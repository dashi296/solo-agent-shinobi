from __future__ import annotations

import json
import subprocess
from json import JSONDecodeError
from pathlib import Path


PRIORITY_ORDER = {
    "priority:high": 0,
    "priority:medium": 1,
    "priority:low": 2,
}


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
            "100",
            "--json",
            "number,labels",
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

    ranked_issues = sorted(issues, key=issue_priority_key)
    return int(ranked_issues[0]["number"])


def ensure_open_issue(root: Path, issue_number: int) -> int:
    result = subprocess.run(
        [
            "gh",
            "issue",
            "view",
            str(issue_number),
            "--json",
            "number,state",
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(stderr or f"failed to load issue #{issue_number} with gh")

    try:
        issue = json.loads(result.stdout or "{}")
    except JSONDecodeError as error:
        raise RuntimeError(f"failed to parse issue #{issue_number} from gh") from error

    if issue.get("state") != "OPEN":
        raise RuntimeError(f"issue #{issue_number} is not open")

    return int(issue["number"])


def issue_priority_key(issue: dict) -> tuple[int, int]:
    labels = issue.get("labels", [])
    label_names = {label.get("name", "") for label in labels if isinstance(label, dict)}
    priority_rank = min(
        (PRIORITY_ORDER[label] for label in label_names if label in PRIORITY_ORDER),
        default=len(PRIORITY_ORDER),
    )
    return priority_rank, int(issue["number"])

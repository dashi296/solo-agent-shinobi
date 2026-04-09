from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Iterable

from .github_client import GitHubClient, GitHubClientError


PRIORITY_ORDER = {
    "priority:high": 0,
    "priority:medium": 1,
    "priority:low": 2,
}
ISSUES_PER_PAGE = 100


def select_ready_issue(root: Path, ready_label: str, *, repo: str | None = None) -> int | None:
    issues = list_open_issues(root, ready_label, repo=repo)
    if not issues:
        return None

    ranked_issues = sorted(issues, key=issue_priority_key)
    return int(ranked_issues[0]["number"])


def ensure_open_issue(
    root: Path,
    issue_number: int,
    *,
    active_labels: Iterable[str] = (),
    allow_active_labels: bool = False,
    repo: str | None = None,
) -> int:
    issue = load_issue(root, issue_number, repo=repo)

    if "pull_request" in issue:
        raise RuntimeError(f"issue #{issue_number} is a pull request, not an issue")

    if str(issue.get("state", "")).upper() != "OPEN":
        raise RuntimeError(f"issue #{issue_number} is not open")

    label_names = {
        label.get("name", "")
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }
    conflicting_labels = sorted(label for label in active_labels if label in label_names)
    if conflicting_labels and not allow_active_labels:
        joined = ", ".join(conflicting_labels)
        raise RuntimeError(
            f"issue #{issue_number} already has active mission label(s): {joined}"
        )

    return int(issue["number"])


def load_issue(root: Path, issue_number: int, *, repo: str | None = None) -> dict:
    try:
        return GitHubClient(root, repo=repo).get_issue(issue_number)
    except GitHubClientError as error:
        raise RuntimeError(str(error)) from error


def list_open_issues_with_any_label(
    root: Path, labels: Sequence[str], *, repo: str | None = None
) -> list[int]:
    issue_numbers: set[int] = set()
    for label in labels:
        for issue in list_open_issues(root, label, repo=repo):
            issue_numbers.add(int(issue["number"]))

    return sorted(issue_numbers)


def list_open_issues(root: Path, label: str, *, repo: str | None = None) -> list[dict]:
    try:
        return GitHubClient(root, repo=repo).list_open_issues(label, per_page=ISSUES_PER_PAGE)
    except GitHubClientError as error:
        raise RuntimeError(str(error)) from error


def issue_priority_key(issue: dict) -> tuple[int, int]:
    labels = issue.get("labels", [])
    label_names = {label.get("name", "") for label in labels if isinstance(label, dict)}
    priority_rank = min(
        (PRIORITY_ORDER[label] for label in label_names if label in PRIORITY_ORDER),
        default=len(PRIORITY_ORDER),
    )
    return priority_rank, int(issue["number"])

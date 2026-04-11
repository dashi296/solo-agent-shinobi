from __future__ import annotations

import time
from collections.abc import Callable
import subprocess
from pathlib import Path
from typing import Any

from .github_client import GitHubClient, GitHubClientError
from .models import CIStatus, Config, DiffStats, PullRequestCheck, ReviewDecision, State


class ReviewerError(RuntimeError):
    """Raised when review inputs cannot be gathered safely."""


PENDING_CHECK_STATES = {
    "action_required",
    "expected",
    "in_progress",
    "pending",
    "queued",
    "requested",
    "startup_failure",
    "waiting",
}
SUCCESS_CHECK_STATES = {"neutral", "success", "skipped"}
FAILURE_CHECK_STATES = {"cancelled", "failure", "error", "timed_out"}


def collect_diff_stats(root: Path, *, base_ref: str) -> DiffStats:
    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", f"{base_ref}...HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ReviewerError(f"failed to collect diff stats against {base_ref}: {error}") from error

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"git exited with {result.returncode}"
        raise ReviewerError(f"failed to collect diff stats against {base_ref}: {message}")

    return parse_numstat(result.stdout)


def parse_numstat(output: str) -> DiffStats:
    changed_files = 0
    added_lines = 0
    deleted_lines = 0

    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        added, deleted, _path = raw_line.split("\t", 2)
        changed_files += 1
        if added.isdigit():
            added_lines += int(added)
        if deleted.isdigit():
            deleted_lines += int(deleted)

    return DiffStats(
        changed_files=changed_files,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
    )


def evaluate_review(
    *,
    config: Config,
    state: State,
    issue: dict[str, Any],
    diff_stats: DiffStats,
) -> ReviewDecision:
    reasons: list[str] = []
    label_names = issue_label_names(issue)
    risky_label = config.labels["risky"]

    if risky_label in label_names:
        reasons.append(
            f"issue #{issue['number']} has label {risky_label}, requiring human review"
        )

    if diff_stats.changed_files > config.max_changed_files:
        reasons.append(
            "changed files "
            f"{diff_stats.changed_files} exceed max_changed_files {config.max_changed_files}"
        )

    if diff_stats.total_changed_lines > config.max_lines_changed:
        reasons.append(
            "total changed lines "
            f"{diff_stats.total_changed_lines} exceed max_lines_changed {config.max_lines_changed}"
        )

    if state.review_loop_count >= config.max_review_loops:
        reasons.append(
            "review loop count "
            f"{state.review_loop_count} reached max_review_loops {config.max_review_loops}"
        )

    return ReviewDecision(should_stop=bool(reasons), reasons=reasons)


def collect_ci_status(client: GitHubClient, pr_number: int) -> CIStatus:
    try:
        payload = client.get_ci_status(pr_number)
    except GitHubClientError as error:
        raise ReviewerError(f"failed to load CI status for PR #{pr_number}: {error}") from error

    checks = [parse_pull_request_check(item) for item in payload if isinstance(item, dict)]
    return CIStatus(checks=checks, status=resolve_ci_status(checks))


def wait_for_ci(
    client: GitHubClient,
    pr_number: int,
    *,
    timeout_seconds: float = 900,
    poll_interval_seconds: float = 10,
    heartbeat: Callable[[], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> CIStatus:
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be positive")

    deadline = monotonic() + timeout_seconds

    while True:
        if heartbeat is not None:
            heartbeat()

        status = collect_ci_status(client, pr_number)
        if not status.is_pending:
            return status

        now = monotonic()
        if now >= deadline:
            return CIStatus(checks=status.checks, status=status.status, timed_out=True)

        sleep(min(poll_interval_seconds, deadline - now))


def parse_pull_request_check(payload: dict[str, Any]) -> PullRequestCheck:
    state = str(payload.get("state", "pending"))
    bucket = normalize_check_bucket(
        str(payload.get("bucket", "")),
        state,
    )
    link = payload.get("link")
    return PullRequestCheck(
        name=str(payload.get("name", "(unnamed check)")),
        state=state,
        bucket=bucket,
        link=str(link) if isinstance(link, str) else None,
    )


def resolve_ci_status(checks: list[PullRequestCheck]) -> str:
    if not checks:
        return "pending"
    if any(check.is_failed for check in checks):
        return "failure"
    if any(check.is_pending for check in checks):
        return "pending"
    return "success"


def normalize_check_bucket(bucket: str, state: str) -> str:
    normalized_bucket = bucket.strip().lower()
    if normalized_bucket in {"pass", "pending", "fail", "cancel", "skipping"}:
        return normalized_bucket

    normalized_state = state.strip().lower()
    if normalized_state in FAILURE_CHECK_STATES:
        return "fail"
    if normalized_state in SUCCESS_CHECK_STATES:
        return "pass"
    if normalized_state in PENDING_CHECK_STATES:
        return "pending"
    return "pending"


def issue_label_names(issue: dict[str, Any]) -> set[str]:
    return {
        label.get("name", "")
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }

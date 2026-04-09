from __future__ import annotations

import re
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .github_client import GitHubClient, GitHubClientError
from .models import Config, State
from .state_store import StateStore


class MissionStartError(RuntimeError):
    """Raised when the start phase cannot complete safely."""


@dataclass(frozen=True)
class StartedMission:
    issue_number: int
    branch: str
    lease_expires_at: str


def start_mission(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    run_id: str,
    issue: dict,
    now: datetime | None = None,
) -> StartedMission:
    started_at = now or datetime.now(timezone.utc)
    issue_number = require_startable_issue(issue, config)
    branch = build_branch_name(issue_number=issue_number, issue_title=str(issue.get("title", "")))

    store.require_lock_owner(run_id, config.agent_identity)
    create_branch(root, branch)

    provisional_state = State(
        issue_number=issue_number,
        pr_number=None,
        branch=branch,
        agent_identity=config.agent_identity,
        run_id=run_id,
        phase="start",
        review_loop_count=0,
        retryable_local_only=True,
        lease_expires_at=None,
        last_result="start_pending",
        last_error=None,
    )
    try:
        store.save_state(provisional_state)
    except OSError as error:
        raise MissionStartError(
            f"failed to persist local mission state after creating branch {branch}: {error}"
        ) from error

    try:
        sync_start_labels(root, issue_number, config)
    except MissionStartError as error:
        provisional_state.last_error = str(error)
        store.save_state(provisional_state)
        raise

    lease_expires_at = store.format_timestamp(
        started_at + timedelta(minutes=config.mission_lease_minutes)
    )
    active_state = State(
        issue_number=issue_number,
        pr_number=None,
        branch=branch,
        agent_identity=config.agent_identity,
        run_id=run_id,
        phase="start",
        review_loop_count=0,
        retryable_local_only=False,
        lease_expires_at=lease_expires_at,
        last_result="started",
        last_error=None,
    )
    try:
        store.save_state(active_state)
    except OSError as error:
        rollback_error = rollback_started_labels(root, issue_number, config)
        provisional_state.last_error = (
            "GitHub labels were updated but final local state persistence failed: "
            f"{error}"
        )
        if rollback_error is not None:
            provisional_state.last_error += f"; rollback also failed: {rollback_error}"
        store.save_state(provisional_state)
        raise MissionStartError(
            format_final_state_persistence_failure(issue_number, error, rollback_error)
        ) from error

    return StartedMission(
        issue_number=issue_number,
        branch=branch,
        lease_expires_at=lease_expires_at,
    )


def build_branch_name(*, issue_number: int, issue_title: str) -> str:
    slug = slugify_issue_title(issue_title)
    return f"feature/issue-{issue_number}-{slug}"


def require_startable_issue(issue: dict, config: Config) -> int:
    issue_number = int(issue["number"])
    if "pull_request" in issue:
        raise MissionStartError(f"issue #{issue_number} is a pull request, not an issue")

    if str(issue.get("state", "")).upper() != "OPEN":
        raise MissionStartError(f"issue #{issue_number} is not open")

    label_names = {
        label.get("name", "")
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }
    ready_label = config.labels["ready"]
    if ready_label not in label_names:
        raise MissionStartError(f"issue #{issue_number} is not labeled {ready_label}")

    blocked_label = config.labels["blocked"]
    needs_human_label = config.labels["needs_human"]
    conflicting_labels = sorted(
        label for label in (blocked_label, needs_human_label) if label in label_names
    )
    if conflicting_labels:
        joined = ", ".join(conflicting_labels)
        raise MissionStartError(
            f"issue #{issue_number} has non-startable label(s): {joined}"
        )

    return issue_number


def slugify_issue_title(issue_title: str) -> str:
    normalized = unicodedata.normalize("NFKD", issue_title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug or "mission"


def create_branch(root: Path, branch: str) -> None:
    try:
        result = subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise MissionStartError(f"failed to create branch {branch}: {error}") from error

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"git exited with {result.returncode}"
        raise MissionStartError(f"failed to create branch {branch}: {message}")


def sync_start_labels(root: Path, issue_number: int, config: Config) -> None:
    client = GitHubClient(root, repo=config.repo)
    working_label = config.labels["working"]
    ready_label = config.labels["ready"]

    try:
        client.update_issue_labels(issue_number, add=[working_label])
    except GitHubClientError as error:
        raise MissionStartError(f"failed to add {working_label} to issue #{issue_number}: {error}") from error

    try:
        client.update_issue_labels(issue_number, remove=[ready_label])
    except GitHubClientError as error:
        rollback_error = rollback_working_label(client, issue_number, working_label)
        message = f"failed to remove {ready_label} from issue #{issue_number}: {error}"
        if rollback_error is not None:
            message += f"; rollback also failed: {rollback_error}"
        raise MissionStartError(message) from error


def rollback_started_labels(root: Path, issue_number: int, config: Config) -> str | None:
    client = GitHubClient(root, repo=config.repo)
    ready_label = config.labels["ready"]
    working_label = config.labels["working"]

    try:
        client.update_issue_labels(issue_number, add=[ready_label])
        client.update_issue_labels(issue_number, remove=[working_label])
    except GitHubClientError as error:
        return str(error)
    return None


def format_final_state_persistence_failure(
    issue_number: int,
    error: OSError,
    rollback_error: str | None,
) -> str:
    message = (
        "GitHub labels were updated but final local state persistence failed for "
        f"issue #{issue_number}: {error}"
    )
    if rollback_error is not None:
        message += f"; rollback also failed: {rollback_error}"
    return message


def rollback_working_label(
    client: GitHubClient,
    issue_number: int,
    working_label: str,
) -> str | None:
    try:
        client.update_issue_labels(issue_number, remove=[working_label])
    except GitHubClientError as error:
        return str(error)
    return None

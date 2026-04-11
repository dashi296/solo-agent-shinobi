from __future__ import annotations

import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .github_client import GitHubClient, GitHubClientError
from .models import Config, MissionSummary, State
from .state_store import StateStore

STATUS_LABEL_KEYS = ("ready", "working", "reviewing", "blocked", "needs_human", "merged")


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
    issue_label_names = get_issue_label_names(issue)
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
        persist_retryable_start_failure(
            store=store,
            state=provisional_state,
            error_message=(
                f"failed to persist local mission state after creating branch {branch}: {error}"
            ),
            started_at=started_at,
        )
        raise MissionStartError(
            f"failed to persist local mission state after creating branch {branch}: {error}"
        ) from error

    try:
        sync_start_labels(root, issue_number, config, issue_label_names=issue_label_names)
    except MissionStartError as error:
        provisional_state.last_error = str(error)
        save_retryable_state_or_raise(
            store=store,
            state=provisional_state,
            base_message=str(error),
        )
        raise error

    lease_expires_at = store.format_timestamp(
        started_at + timedelta(minutes=config.mission_lease_minutes)
    )
    try:
        post_start_comment(
            root=root,
            issue_number=issue_number,
            branch=branch,
            lease_expires_at=lease_expires_at,
            config=config,
            run_id=run_id,
        )
    except MissionStartError as error:
        provisional_state.last_error = str(error)
        save_retryable_state_or_raise(
            store=store,
            state=provisional_state,
            base_message=str(error),
        )
        raise error

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
        rollback_error = transition_issue_to_needs_human(
            root=root,
            issue_number=issue_number,
            config=config,
            reason=(
                "Shinobi failed to persist final local state during start phase after "
                f"updating active labels: {error}"
            ),
            known_label_names={config.labels["working"]},
        )
        provisional_state.last_error = (
            "GitHub labels were updated but final local state persistence failed: "
            f"{error}"
        )
        if rollback_error is not None:
            provisional_state.last_error += f"; rollback also failed: {rollback_error}"
        failure_message = format_final_state_persistence_failure(
            issue_number, error, rollback_error
        )
        save_retryable_state_or_raise(
            store=store,
            state=provisional_state,
            base_message=failure_message,
        )
        raise MissionStartError(failure_message) from error

    return StartedMission(
        issue_number=issue_number,
        branch=branch,
        lease_expires_at=lease_expires_at,
    )


def handoff_started_mission(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    run_id: str,
    started_mission: StartedMission,
    reason: str,
) -> None:
    store.require_lock_owner(run_id, config.agent_identity)
    rollback_error = transition_issue_to_needs_human(
        root=root,
        issue_number=started_mission.issue_number,
        config=config,
        reason=reason,
        known_label_names={config.labels["working"]},
    )
    if rollback_error is not None:
        raise MissionStartError(
            "failed to hand off started mission for "
            f"issue #{started_mission.issue_number}: {rollback_error}"
        )

    store.save_state(
        State(
            issue_number=None,
            pr_number=None,
            branch=None,
            agent_identity=config.agent_identity,
            run_id=None,
            phase="idle",
            review_loop_count=0,
            retryable_local_only=False,
            lease_expires_at=None,
            last_result="needs-human",
            last_error=reason,
            last_mission=MissionSummary(
                issue_number=started_mission.issue_number,
                pr_number=None,
                branch=started_mission.branch,
                phase="start",
                conclusion="needs-human",
            ),
        )
    )


def resume_local_only_mission(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    run_id: str,
    issue: dict,
    state: State,
    now: datetime | None = None,
) -> StartedMission:
    started_at = now or datetime.now(timezone.utc)
    issue_number = require_startable_issue(issue, config)
    if state.issue_number != issue_number:
        raise MissionStartError(
            "retryable local-only state issue does not match requested issue "
            f"(state: {state.issue_number}, issue: {issue_number})"
        )
    if not state.branch:
        raise MissionStartError(
            f"retryable local-only mission for issue #{issue_number} is missing branch"
        )

    store.require_lock_owner(run_id, config.agent_identity)
    lease_expires_at = store.format_timestamp(
        started_at + timedelta(minutes=config.mission_lease_minutes)
    )
    issue_label_names = get_issue_label_names(issue)

    try:
        sync_start_labels(root, issue_number, config, issue_label_names=issue_label_names)
    except MissionStartError as error:
        state.last_error = str(error)
        save_retryable_state_or_raise(
            store=store,
            state=state,
            base_message=str(error),
        )
        raise error

    try:
        post_start_comment(
            root=root,
            issue_number=issue_number,
            branch=state.branch,
            lease_expires_at=lease_expires_at,
            config=config,
            run_id=run_id,
        )
    except MissionStartError as error:
        state.last_error = str(error)
        save_retryable_state_or_raise(
            store=store,
            state=state,
            base_message=str(error),
        )
        raise error

    active_state = State(
        issue_number=issue_number,
        pr_number=None,
        branch=state.branch,
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
        rollback_error = transition_issue_to_needs_human(
            root=root,
            issue_number=issue_number,
            config=config,
            reason=(
                "Shinobi failed to persist final local state while resuming a local-only "
                f"mission after updating active labels: {error}"
            ),
            known_label_names={config.labels["working"]},
        )
        state.last_error = (
            "GitHub labels were updated but final local state persistence failed while "
            f"resuming local-only mission: {error}"
        )
        if rollback_error is not None:
            state.last_error += f"; rollback also failed: {rollback_error}"
        failure_message = format_final_state_persistence_failure(
            issue_number, error, rollback_error
        )
        save_retryable_state_or_raise(
            store=store,
            state=state,
            base_message=failure_message,
        )
        raise MissionStartError(failure_message) from error

    return StartedMission(
        issue_number=issue_number,
        branch=state.branch,
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


def get_issue_label_names(issue: dict) -> set[str]:
    return {
        label.get("name", "")
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }


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


def sync_start_labels(
    root: Path,
    issue_number: int,
    config: Config,
    *,
    issue_label_names: set[str],
) -> None:
    client = GitHubClient(root, repo=config.repo)
    working_label = config.labels["working"]
    removable_labels = labels_to_remove_for_transition(
        config=config,
        current_label_names=issue_label_names | {working_label},
        target_label=working_label,
    )

    try:
        client.update_issue_labels(issue_number, add=[working_label])
    except GitHubClientError as error:
        raise MissionStartError(f"failed to add {working_label} to issue #{issue_number}: {error}") from error

    try:
        if removable_labels:
            client.update_issue_labels(issue_number, remove=removable_labels)
    except GitHubClientError as error:
        rollback_error = transition_issue_to_needs_human(
            root=root,
            issue_number=issue_number,
            config=config,
            reason=(
                "Shinobi failed to complete start label transition after adding "
                f"{working_label}: {error}"
            ),
            known_label_names=issue_label_names | {working_label},
        )
        message = (
            f"failed to normalize start labels for issue #{issue_number}: {error}"
        )
        if rollback_error is not None:
            message += f"; rollback also failed: {rollback_error}"
        raise MissionStartError(message) from error


def post_start_comment(
    *,
    root: Path,
    issue_number: int,
    branch: str,
    lease_expires_at: str,
    config: Config,
    run_id: str,
) -> None:
    client = GitHubClient(root, repo=config.repo)
    comment = render_start_comment(
        issue_number=issue_number,
        branch=branch,
        lease_expires_at=lease_expires_at,
        agent_identity=config.agent_identity,
        run_id=run_id,
    )
    try:
        client.create_issue_comment(issue_number, comment)
    except GitHubClientError as error:
        rollback_error = transition_issue_to_needs_human(
            root=root,
            issue_number=issue_number,
            config=config,
            reason=(
                "Shinobi failed to create the mission-state comment during start phase "
                f"after updating active labels: {error}"
            ),
            known_label_names={config.labels["working"]},
        )
        message = (
            f"failed to create mission-state comment on issue #{issue_number}: {error}"
        )
        if rollback_error is not None:
            message += f"; rollback also failed: {rollback_error}"
        raise MissionStartError(message) from error


def render_start_comment(
    *,
    issue_number: int,
    branch: str,
    lease_expires_at: str,
    agent_identity: str,
    run_id: str,
) -> str:
    return (
        "<!-- shinobi:mission-state\n"
        f"issue: {issue_number}\n"
        f"branch: {branch}\n"
        "phase: start\n"
        "pr: null\n"
        f"lease_expires_at: {lease_expires_at}\n"
        f"agent_identity: {agent_identity}\n"
        f"run_id: {run_id}\n"
        "-->\n"
        "Shinobi Start\n\n"
        f"任務 #{issue_number} に着手します。\n"
        "- scope: issue body の要件内に限定\n"
    )


def transition_issue_to_needs_human(
    *,
    root: Path,
    issue_number: int,
    config: Config,
    reason: str,
    known_label_names: set[str] | None = None,
) -> str | None:
    client = GitHubClient(root, repo=config.repo)
    needs_human_label = config.labels["needs_human"]
    removable_labels = labels_to_remove_for_transition(
        config=config,
        current_label_names=known_label_names or set(),
        target_label=needs_human_label,
    )

    try:
        client.update_issue_labels(issue_number, add=[needs_human_label])
        if removable_labels:
            client.update_issue_labels(issue_number, remove=removable_labels)
        client.create_issue_comment(issue_number, reason)
    except GitHubClientError as error:
        return str(error)
    return None


def labels_to_remove_for_transition(
    *,
    config: Config,
    current_label_names: set[str],
    target_label: str,
) -> list[str]:
    state_labels = {
        config.labels[key]
        for key in STATUS_LABEL_KEYS
        if key in config.labels
    }
    return sorted(
        label
        for label in current_label_names
        if label in state_labels and label != target_label
    )


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


def save_retryable_state_or_raise(
    *,
    store: StateStore,
    state: State,
    base_message: str,
) -> None:
    try:
        store.save_state(state)
    except OSError as error:
        raise MissionStartError(
            f"{base_message}; additionally failed to persist retryable local state: {error}"
        ) from error


def persist_retryable_start_failure(
    *,
    store: StateStore,
    state: State,
    error_message: str,
    started_at: datetime,
) -> None:
    state.last_error = error_message
    payload = {
        "started_at": store.format_timestamp(started_at),
        "issue_number": state.issue_number,
        "branch": state.branch,
        "phase": state.phase,
        "agent_identity": state.agent_identity,
        "run_id": state.run_id,
        "retryable_local_only": state.retryable_local_only,
        "last_result": state.last_result,
        "last_error": state.last_error,
    }
    log_path = store.paths.logs_dir / "retryable-start-failures.jsonl"
    try:
        store.paths.logs_dir.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError:
        return

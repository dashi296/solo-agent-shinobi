from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .github_client import GitHubClient, GitHubClientError
from .mission_start import get_issue_label_names, labels_to_remove_for_transition
from .models import Config, ExecutionResult, State, VerificationCommandResult
from .state_store import StateStore

MISSION_STATE_MARKER = "<!-- shinobi:mission-state"


class MissionPublishError(RuntimeError):
    """Raised when the publish phase cannot complete safely."""


@dataclass(frozen=True)
class PublishedMission:
    issue_number: int
    pr_number: int
    branch: str
    lease_expires_at: str
    pr_url: str | None = None


def publish_mission(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    run_id: str,
    state: State,
    execution_result: ExecutionResult,
    now: datetime | None = None,
) -> PublishedMission:
    published_at = now or datetime.now(timezone.utc)
    issue_number, branch = require_publishable_state(
        state,
        run_id=run_id,
        agent_identity=config.agent_identity,
    )
    require_publishable_execution_result(execution_result)
    store.require_lock_owner(run_id, config.agent_identity)
    store.refresh_lock_heartbeat(
        run_id=run_id,
        agent_identity=config.agent_identity,
        now=published_at,
    )

    push_branch(root, branch)

    client = GitHubClient(root, repo=config.repo)
    pr = create_or_update_pull_request(
        client=client,
        config=config,
        issue_number=issue_number,
        branch=branch,
        execution_result=execution_result,
    )
    pr_number = int(pr["number"])
    pr_url = str(pr.get("url") or "") or None
    lease_expires_at = store.format_timestamp(
        published_at + timedelta(minutes=config.mission_lease_minutes)
    )

    sync_publish_labels(client=client, issue_number=issue_number, config=config)
    upsert_publish_comment(
        client=client,
        issue_number=issue_number,
        branch=branch,
        pr_number=pr_number,
        lease_expires_at=lease_expires_at,
        agent_identity=config.agent_identity,
        run_id=run_id,
    )

    published_state = State(
        issue_number=issue_number,
        pr_number=pr_number,
        branch=branch,
        agent_identity=config.agent_identity,
        run_id=run_id,
        phase="publish",
        review_loop_count=state.review_loop_count,
        retryable_local_only=False,
        lease_expires_at=lease_expires_at,
        last_result="published",
        last_error=None,
    )
    try:
        store.save_state(published_state)
    except OSError as error:
        raise MissionPublishError(
            "GitHub PR, labels, and mission-state comment were updated but final "
            f"local state persistence failed for issue #{issue_number}: {error}"
        ) from error

    return PublishedMission(
        issue_number=issue_number,
        pr_number=pr_number,
        branch=branch,
        lease_expires_at=lease_expires_at,
        pr_url=pr_url,
    )


def require_publishable_state(
    state: State,
    *,
    run_id: str,
    agent_identity: str,
) -> tuple[int, str]:
    if state.phase != "start":
        raise MissionPublishError(
            f"publish phase requires local state phase start, got {state.phase}"
        )
    if state.run_id != run_id:
        raise MissionPublishError(
            f"publish phase requires local state run_id {run_id}, got {state.run_id}"
        )
    if state.agent_identity != agent_identity:
        raise MissionPublishError(
            "publish phase requires local state agent_identity "
            f"{agent_identity}, got {state.agent_identity}"
        )
    if state.issue_number is None:
        raise MissionPublishError("publish phase requires issue_number in local state")
    if not state.branch:
        raise MissionPublishError("publish phase requires branch in local state")
    if state.retryable_local_only:
        raise MissionPublishError("publish phase cannot run on retryable local-only state")
    return state.issue_number, state.branch


def require_publishable_execution_result(execution_result: ExecutionResult) -> None:
    blocking_results = blocking_verification_results(execution_result)
    if not blocking_results:
        return

    rendered_results = ", ".join(
        f"{command.name}: {command.status}" for command in blocking_results
    )
    raise MissionPublishError(
        "publish phase requires verification commands without failed/error results "
        f"({rendered_results})"
    )


def blocking_verification_results(
    execution_result: ExecutionResult,
) -> list[VerificationCommandResult]:
    return [
        command
        for command in execution_result.commands
        if command.status in {"failed", "error"}
    ]


def push_branch(root: Path, branch: str) -> None:
    try:
        result = subprocess.run(
            ["git", "push", "-u", "origin", branch],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise MissionPublishError(f"failed to push branch {branch}: {error}") from error

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"git exited with {result.returncode}"
        raise MissionPublishError(f"failed to push branch {branch}: {message}")


def create_or_update_pull_request(
    *,
    client: GitHubClient,
    config: Config,
    issue_number: int,
    branch: str,
    execution_result: ExecutionResult,
) -> dict[str, Any]:
    title = f"#{issue_number} Publish mission changes"
    body = render_pr_body(issue_number=issue_number, execution_result=execution_result)
    try:
        existing_pr = client.get_pull_request(branch)
    except GitHubClientError:
        try:
            return client.create_pull_request(
                title=title,
                body=body,
                base=config.main_branch,
                head=branch,
                draft=config.use_draft_pr,
            )
        except GitHubClientError as error:
            raise MissionPublishError(
                f"failed to create PR for issue #{issue_number}: {error}"
            ) from error

    try:
        return client.update_pull_request(
            int(existing_pr["number"]),
            title=title,
            body=body,
            base=config.main_branch,
        )
    except GitHubClientError as error:
        raise MissionPublishError(
            f"failed to update PR #{existing_pr.get('number')} for issue #{issue_number}: {error}"
        ) from error


def render_pr_body(*, issue_number: int, execution_result: ExecutionResult) -> str:
    lines = [
        "## Summary",
        f"- Refs #{issue_number}",
        f"- {execution_result.change_summary}",
        "",
        "## Verification",
    ]
    for command in execution_result.commands:
        rendered_command = " ".join(command.command) if command.command else "not configured"
        lines.append(f"- {command.name}: {command.status} (`{rendered_command}`)")
    return "\n".join(lines) + "\n"


def sync_publish_labels(
    *,
    client: GitHubClient,
    issue_number: int,
    config: Config,
) -> None:
    reviewing_label = config.labels["reviewing"]
    try:
        issue = client.get_issue(issue_number)
        current_label_names = get_issue_label_names(issue)
        removable_labels = labels_to_remove_for_transition(
            config=config,
            current_label_names=current_label_names | {reviewing_label},
            target_label=reviewing_label,
        )
        client.update_issue_labels(issue_number, add=[reviewing_label])
        if removable_labels:
            client.update_issue_labels(issue_number, remove=removable_labels)
    except GitHubClientError as error:
        raise MissionPublishError(
            f"failed to normalize publish labels for issue #{issue_number}: {error}"
        ) from error


def upsert_publish_comment(
    *,
    client: GitHubClient,
    issue_number: int,
    branch: str,
    pr_number: int,
    lease_expires_at: str,
    agent_identity: str,
    run_id: str,
) -> None:
    body = render_publish_comment(
        issue_number=issue_number,
        branch=branch,
        pr_number=pr_number,
        lease_expires_at=lease_expires_at,
        agent_identity=agent_identity,
        run_id=run_id,
    )
    try:
        comment = find_mission_state_comment(
            client.list_issue_comments(issue_number),
            issue_number=issue_number,
            branch=branch,
        )
        if comment is None:
            client.create_issue_comment(issue_number, body)
            return
        client.update_issue_comment(int(comment["id"]), body)
    except (GitHubClientError, KeyError, TypeError, ValueError) as error:
        raise MissionPublishError(
            f"failed to upsert mission-state comment for issue #{issue_number}: {error}"
        ) from error


def find_mission_state_comment(
    comments: list[dict[str, Any]],
    *,
    issue_number: int,
    branch: str,
) -> dict[str, Any] | None:
    for comment in comments:
        marker_fields = parse_mission_state_fields(str(comment.get("body") or ""))
        if marker_fields.get("issue") == str(issue_number) and marker_fields.get("branch") == branch:
            return comment
    return None


def parse_mission_state_fields(body: str) -> dict[str, str]:
    if MISSION_STATE_MARKER not in body:
        return {}

    fields: dict[str, str] = {}
    in_marker = False
    for line in body.splitlines():
        stripped = line.strip()
        if stripped == MISSION_STATE_MARKER:
            in_marker = True
            continue
        if not in_marker:
            continue
        if stripped == "-->":
            break
        key, separator, value = stripped.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    return fields


def render_publish_comment(
    *,
    issue_number: int,
    branch: str,
    pr_number: int,
    lease_expires_at: str,
    agent_identity: str,
    run_id: str,
) -> str:
    return (
        "<!-- shinobi:mission-state\n"
        f"issue: {issue_number}\n"
        f"branch: {branch}\n"
        "phase: publish\n"
        f"pr: {pr_number}\n"
        f"lease_expires_at: {lease_expires_at}\n"
        f"agent_identity: {agent_identity}\n"
        f"run_id: {run_id}\n"
        "-->\n"
        "Shinobi Publish\n\n"
        f"任務 #{issue_number} の draft PR を公開しました。\n"
        f"- pr: #{pr_number}\n"
    )

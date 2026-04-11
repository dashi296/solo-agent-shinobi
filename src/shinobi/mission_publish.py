from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .github_client import GitHubClient, GitHubClientError
from .mission_start import get_issue_label_names, labels_to_remove_for_transition
from .models import Config, ExecutionResult, MissionSummary, State, VerificationCommandResult
from .state_store import StateStore

MISSION_STATE_MARKER = "<!-- shinobi:mission-state"
BLOCKING_PUBLISH_LABEL_KEYS = ("blocked", "needs_human")


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
    lease_expires_at = store.format_timestamp(
        published_at + timedelta(minutes=config.mission_lease_minutes)
    )
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

    client = GitHubClient(root, repo=config.repo)
    issue_label_names = load_publishable_issue_label_names(
        client=client,
        issue_number=issue_number,
        config=config,
    )

    push_branch(root, branch)

    try:
        pr = create_or_update_pull_request(
            client=client,
            config=config,
            issue_number=issue_number,
            branch=branch,
            execution_result=execution_result,
        )
    except MissionPublishError as error:
        rollback_error = transition_publish_failure_to_needs_human(
            client=client,
            issue_number=issue_number,
            config=config,
            reason=(
                "Shinobi failed to complete publish phase after pushing branch "
                f"{branch}: {error}"
            ),
            known_label_names=load_handoff_label_names(
                client=client,
                issue_number=issue_number,
                fallback_label_names=issue_label_names,
            ),
            lease_expires_at=lease_expires_at,
            agent_identity=config.agent_identity,
            run_id=run_id,
            branch=branch,
        )
        state_error = None
        if rollback_error is None:
            state_error = save_publish_handoff_state(
                store=store,
                issue_number=issue_number,
                branch=branch,
                agent_identity=config.agent_identity,
                reason=str(error),
            )
        message = str(error)
        if rollback_error is not None:
            message += f"; failed to hand off publish failure: {rollback_error}"
        elif state_error is not None:
            message += f"; failed to persist local publish handoff state: {state_error}"
        raise MissionPublishError(message) from error
    pr_number = int(pr["number"])
    pr_url = str(pr.get("url") or "") or None

    try:
        sync_publish_labels(
            client=client,
            issue_number=issue_number,
            config=config,
            current_label_names=issue_label_names,
        )
        upsert_publish_comment(
            client=client,
            issue_number=issue_number,
            branch=branch,
            pr_number=pr_number,
            lease_expires_at=lease_expires_at,
            agent_identity=config.agent_identity,
            run_id=run_id,
        )
    except MissionPublishError as error:
        rollback_error = transition_publish_failure_to_needs_human(
            client=client,
            issue_number=issue_number,
            config=config,
            reason=(
                "Shinobi failed to complete publish phase after creating or updating "
                f"PR #{pr_number}: {error}"
            ),
            known_label_names=load_handoff_label_names(
                client=client,
                issue_number=issue_number,
                fallback_label_names=issue_label_names,
            ),
            lease_expires_at=lease_expires_at,
            agent_identity=config.agent_identity,
            run_id=run_id,
            pr_number=pr_number,
            branch=branch,
        )
        state_error = None
        if rollback_error is None:
            state_error = save_publish_handoff_state(
                store=store,
                issue_number=issue_number,
                pr_number=pr_number,
                branch=branch,
                agent_identity=config.agent_identity,
                reason=str(error),
            )
        message = str(error)
        if rollback_error is not None:
            message += f"; failed to hand off publish failure: {rollback_error}"
        elif state_error is not None:
            message += f"; failed to persist local publish handoff state: {state_error}"
        raise MissionPublishError(message) from error

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
        rollback_error = transition_publish_failure_to_needs_human(
            client=client,
            issue_number=issue_number,
            config=config,
            reason=(
                "Shinobi failed to persist final local state during publish phase "
                f"after updating PR #{pr_number}: {error}"
            ),
            known_label_names=load_handoff_label_names(
                client=client,
                issue_number=issue_number,
                fallback_label_names={config.labels["reviewing"]},
            ),
            lease_expires_at=lease_expires_at,
            agent_identity=config.agent_identity,
            run_id=run_id,
            pr_number=pr_number,
            branch=branch,
        )
        state_error = None
        if rollback_error is None:
            state_error = save_publish_handoff_state(
                store=store,
                issue_number=issue_number,
                pr_number=pr_number,
                branch=branch,
                agent_identity=config.agent_identity,
                reason=(
                    "GitHub PR, labels, and mission-state comment were updated but final "
                    f"local state persistence failed for issue #{issue_number}: {error}"
                ),
            )
        message = (
            "GitHub PR, labels, and mission-state comment were updated but final "
            f"local state persistence failed for issue #{issue_number}: {error}"
        )
        if rollback_error is not None:
            message += f"; failed to hand off publish failure: {rollback_error}"
        elif state_error is not None:
            message += f"; failed to persist local publish handoff state: {state_error}"
        raise MissionPublishError(message) from error

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


def load_publishable_issue_label_names(
    *,
    client: GitHubClient,
    issue_number: int,
    config: Config,
) -> set[str]:
    try:
        issue = client.get_issue(issue_number)
    except GitHubClientError as error:
        raise MissionPublishError(
            f"failed to load issue #{issue_number} before publish: {error}"
        ) from error

    label_names = get_issue_label_names(issue)
    blocking_labels = sorted(
        config.labels[key]
        for key in BLOCKING_PUBLISH_LABEL_KEYS
        if key in config.labels and config.labels[key] in label_names
    )
    if blocking_labels:
        joined = ", ".join(blocking_labels)
        raise MissionPublishError(
            f"publish phase cannot proceed because issue #{issue_number} "
            f"has blocking label(s): {joined}"
        )

    return label_names


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
    head_selector = build_same_repo_head_selector(config.repo, branch)
    try:
        existing_prs = client.list_pull_requests_by_head(head_selector)
    except GitHubClientError as error:
        raise MissionPublishError(
            f"failed to look up existing PR for issue #{issue_number}: {error}"
        ) from error

    if not existing_prs:
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

    existing_pr = existing_prs[0]
    try:
        updated_pr = client.update_pull_request(
            int(existing_pr["number"]),
            title=title,
            body=body,
            base=config.main_branch,
        )
    except GitHubClientError as error:
        raise MissionPublishError(
            f"failed to update PR #{existing_pr.get('number')} for issue #{issue_number}: {error}"
        ) from error

    if config.use_draft_pr and not bool(updated_pr.get("isDraft")):
        pr_number = int(updated_pr["number"])
        try:
            return client.convert_pull_request_to_draft(pr_number)
        except GitHubClientError as error:
            raise MissionPublishError(
                f"failed to convert PR #{pr_number} to draft for issue #{issue_number}: {error}"
            ) from error

    return updated_pr


def build_same_repo_head_selector(repo: str, branch: str) -> str:
    owner, separator, _ = repo.partition("/")
    if not separator or not owner:
        return branch
    return f"{owner}:{branch}"


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
    current_label_names: set[str],
) -> None:
    reviewing_label = config.labels["reviewing"]
    try:
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


def load_handoff_label_names(
    *,
    client: GitHubClient,
    issue_number: int,
    fallback_label_names: set[str],
) -> set[str]:
    try:
        return get_issue_label_names(client.get_issue(issue_number))
    except GitHubClientError:
        return set(fallback_label_names)


def save_publish_handoff_state(
    *,
    store: StateStore,
    issue_number: int,
    branch: str,
    agent_identity: str,
    reason: str,
    pr_number: int | None = None,
) -> str | None:
    try:
        store.save_state(
            State(
                issue_number=None,
                pr_number=None,
                branch=None,
                agent_identity=agent_identity,
                run_id=None,
                phase="idle",
                review_loop_count=0,
                retryable_local_only=False,
                lease_expires_at=None,
                last_result="needs-human",
                last_error=reason,
                last_mission=MissionSummary(
                    issue_number=issue_number,
                    pr_number=pr_number,
                    branch=branch,
                    phase="publish",
                    conclusion="needs-human",
                ),
            )
        )
    except OSError as error:
        return str(error)
    return None


def transition_publish_failure_to_needs_human(
    *,
    client: GitHubClient,
    issue_number: int,
    config: Config,
    reason: str,
    known_label_names: set[str],
    lease_expires_at: str,
    agent_identity: str,
    run_id: str,
    pr_number: int | None = None,
    branch: str | None = None,
) -> str | None:
    needs_human_label = config.labels["needs_human"]
    removable_labels = labels_to_remove_for_transition(
        config=config,
        current_label_names=known_label_names | {needs_human_label},
        target_label=needs_human_label,
    )

    errors: list[str] = []
    try:
        client.update_issue_labels(issue_number, add=[needs_human_label])
    except GitHubClientError as error:
        errors.append(str(error))

    if removable_labels:
        try:
            client.update_issue_labels(issue_number, remove=removable_labels)
        except GitHubClientError as error:
            errors.append(str(error))

    try:
        if pr_number is not None and branch:
            upsert_publish_failure_comment(
                client=client,
                issue_number=issue_number,
                branch=branch,
                pr_number=pr_number,
                lease_expires_at=lease_expires_at,
                agent_identity=agent_identity,
                run_id=run_id,
                reason=reason,
            )
        else:
            client.create_issue_comment(
                issue_number,
                render_publish_failure_comment(
                    reason=reason,
                    pr_number=pr_number,
                    branch=branch,
                ),
            )
    except (GitHubClientError, MissionPublishError) as error:
        errors.append(str(error))

    return "; ".join(errors) or None


def render_publish_failure_comment(
    *,
    reason: str,
    pr_number: int | None,
    branch: str | None,
) -> str:
    lines = [reason, ""]
    if branch:
        lines.append(f"Branch: `{branch}`")
    if pr_number is not None:
        lines.append(f"PR: #{pr_number}")
    return "\n".join(lines) + "\n"


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


def upsert_publish_failure_comment(
    *,
    client: GitHubClient,
    issue_number: int,
    branch: str,
    pr_number: int,
    lease_expires_at: str,
    agent_identity: str,
    run_id: str,
    reason: str,
) -> None:
    body = render_publish_failure_state_comment(
        issue_number=issue_number,
        branch=branch,
        pr_number=pr_number,
        lease_expires_at=lease_expires_at,
        agent_identity=agent_identity,
        run_id=run_id,
        reason=reason,
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
            f"failed to upsert publish failure comment for issue #{issue_number}: {error}"
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


def render_publish_failure_state_comment(
    *,
    issue_number: int,
    branch: str,
    pr_number: int,
    lease_expires_at: str,
    agent_identity: str,
    run_id: str,
    reason: str,
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
        "Shinobi Publish Handoff\n\n"
        f"任務 #{issue_number} の publish 中に人手対応が必要になりました。\n"
        f"- pr: #{pr_number}\n"
        f"- reason: {reason}\n"
    )

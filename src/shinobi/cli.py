from __future__ import annotations

import argparse
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional

from .config import discover_workspace_root
from .executor import detect_high_risk_stop, execute_verification
from .github_client import GitHubClient, GitHubClientError
from .issue_selector import (
    ensure_open_issue,
    load_issue,
    list_open_issues,
    list_open_issues_with_any_label,
    select_ready_issue,
)
from .mission_finalize import MissionFinalizeError, finalize_mission
from .mission_publish import (
    MissionPublishError,
    blocking_verification_results,
    find_blocking_publish_labels,
    load_publishable_issue_label_names,
    publish_mission,
    push_branch,
    stop_publish_for_blocking_labels,
    upsert_review_comment,
)
from .mission_start import (
    MissionStartError,
    StartedMission,
    handoff_started_mission,
    start_mission,
)
from .models import Config, ExecutionResult, State, StopDecision
from .reviewer import ReviewerError, wait_for_ci
from .state_store import StateStore


@dataclass(frozen=True)
class StatusMissionRef:
    source: str
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    phase: str | None
    conclusion: str | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shinobi")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Initialize local Shinobi state.")
    subparsers.add_parser("status", help="Show local Shinobi state.")
    run_parser = subparsers.add_parser("run", help="Start a mission lifecycle.")
    run_parser.add_argument("--issue", type=positive_issue_number, help="Issue number to run.")
    review_parser = subparsers.add_parser("review", help="Wait for PR CI and persist review state.")
    review_parser.add_argument(
        "--timeout-seconds",
        type=non_negative_seconds,
        default=900,
        help="Maximum time to wait for CI completion.",
    )
    review_parser.add_argument(
        "--poll-interval-seconds",
        type=positive_seconds,
        default=10,
        help="Polling interval for CI status checks.",
    )
    return parser


def positive_issue_number(value: str) -> int:
    issue_number = int(value)
    if issue_number <= 0:
        raise argparse.ArgumentTypeError("issue number must be a positive integer")
    return issue_number


def non_negative_seconds(value: str) -> float:
    seconds = float(value)
    if seconds < 0:
        raise argparse.ArgumentTypeError("seconds must be non-negative")
    return seconds


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if seconds <= 0:
        raise argparse.ArgumentTypeError("seconds must be positive")
    return seconds


def command_init(root: Path) -> int:
    store = StateStore(root)
    config, state = store.initialize()
    print(f"Initialized Shinobi in {store.paths.shinobi_dir}")
    print(f"agent_identity: {config.agent_identity}")
    print(f"phase: {state.phase}")
    print("GitHub labels to configure: " + ", ".join(config.labels.values()))
    return 0


def command_status(root: Path) -> int:
    store = StateStore(root)
    if not store.has_state():
        print("Shinobi is not initialized in this workspace.")
        print("Run `shinobi init` first.")
        return 1

    state, state_error = store.try_load_state()
    config, config_error = store.try_load_config()

    print("Shinobi status")

    if config is None and state is None:
        print(f"warning: failed to load config: {config_error}")
        print(f"warning: failed to load local state: {state_error}")
        return 1

    if config is not None:
        print(f"repo: {config.repo}")
        print(f"agent_identity: {config.agent_identity}")
        if state is not None and state.agent_identity != config.agent_identity:
            print(
                "warning: local state agent_identity does not match config; "
                "run `shinobi init` to repair it"
            )
    else:
        print("repo: unavailable")
        print(f"agent_identity: {state.agent_identity if state is not None else 'unavailable'}")
        print(f"warning: failed to load config: {config_error}")

    if state is None:
        print(f"warning: failed to load local state: {state_error}")
        return 1

    render_local_status(state)
    mission_ref = resolve_status_mission_ref(state)
    render_github_status(root, config, mission_ref)
    return 0


def render_local_status(state: State) -> None:
    print(f"phase: {state.phase}")
    print(f"issue_number: {state.issue_number}")
    print(f"pr_number: {state.pr_number}")
    print(f"branch: {state.branch}")

    if state.last_mission is None:
        return

    print(f"last_mission_issue_number: {state.last_mission.issue_number}")
    print(f"last_mission_pr_number: {state.last_mission.pr_number}")
    print(f"last_mission_branch: {state.last_mission.branch}")
    print(f"last_mission_phase: {state.last_mission.phase}")
    print(f"last_mission_conclusion: {state.last_mission.conclusion}")


def resolve_status_mission_ref(state: State) -> StatusMissionRef | None:
    if state.issue_number is not None or state.pr_number is not None or state.branch is not None:
        return StatusMissionRef(
            source="active",
            issue_number=state.issue_number,
            pr_number=state.pr_number,
            branch=state.branch,
            phase=state.phase,
        )

    if state.last_mission is None:
        return None

    return StatusMissionRef(
        source="last_mission",
        issue_number=state.last_mission.issue_number,
        pr_number=state.last_mission.pr_number,
        branch=state.last_mission.branch,
        phase=state.last_mission.phase,
        conclusion=state.last_mission.conclusion,
    )


def render_github_status(
    root: Path,
    config: Config | None,
    mission_ref: StatusMissionRef | None,
) -> None:
    if config is None:
        print("github_status: unavailable (config missing)")
        return

    if mission_ref is None or (
        mission_ref.issue_number is None
        and mission_ref.pr_number is None
        and mission_ref.branch is None
    ):
        print("github_status: no active or recent mission to reconcile")
        return

    print(f"github_status_target: {mission_ref.source}")
    client = GitHubClient(root, repo=config.repo)
    issue = load_status_issue(client, mission_ref.issue_number)
    pull_request = load_status_pull_request(client, mission_ref.pr_number)

    if issue is None and pull_request is None:
        return

    warnings = build_status_warnings(
        mission_ref=mission_ref,
        config=config,
        issue=issue,
        pull_request=pull_request,
    )
    for warning in warnings:
        print(f"warning: {warning}")


def load_status_issue(
    client: GitHubClient, issue_number: int | None
) -> dict[str, Any] | None:
    if issue_number is None:
        print("github_issue: unavailable (no tracked issue)")
        return None

    try:
        issue = client.get_issue(issue_number)
    except GitHubClientError as error:
        print(f"warning: failed to load issue #{issue_number}: {error}")
        return None

    labels = ", ".join(sorted(get_status_label_names(issue))) or "(none)"
    print(f"github_issue_number: {issue_number}")
    print(f"github_issue_state: {issue.get('state', 'unknown')}")
    print(f"github_issue_labels: {labels}")
    return issue


def load_status_pull_request(
    client: GitHubClient, pr_number: int | None
) -> dict[str, Any] | None:
    if pr_number is None:
        print("github_pr: unavailable (no tracked PR)")
        return None

    try:
        pull_request = client.get_pull_request(str(pr_number))
    except GitHubClientError as error:
        print(f"warning: failed to load PR #{pr_number}: {error}")
        return None

    readiness = "draft" if pull_request.get("isDraft") else "ready"
    print(f"github_pr_number: {pull_request.get('number', pr_number)}")
    print(f"github_pr_state: {readiness}")
    print(f"github_pr_url: {pull_request.get('url', 'unavailable')}")
    print(f"github_pr_head: {pull_request.get('headRefName', 'unknown')}")
    print(f"github_pr_base: {pull_request.get('baseRefName', 'unknown')}")
    return pull_request


def get_status_label_names(issue: dict[str, Any]) -> set[str]:
    return {
        label.get("name", "")
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }


def build_status_warnings(
    *,
    mission_ref: StatusMissionRef,
    config: Config,
    issue: dict[str, Any] | None,
    pull_request: dict[str, Any] | None,
) -> list[str]:
    warnings: list[str] = []
    if issue is not None:
        warnings.extend(
            build_issue_status_warnings(
                mission_ref=mission_ref,
                config=config,
                issue=issue,
            )
        )
    if pull_request is not None:
        warnings.extend(
            build_pr_status_warnings(
                mission_ref=mission_ref,
                config=config,
                pull_request=pull_request,
            )
        )
    return warnings


def build_issue_status_warnings(
    *, mission_ref: StatusMissionRef, config: Config, issue: dict[str, Any]
) -> list[str]:
    warnings: list[str] = []
    issue_state = str(issue.get("state", "")).upper()
    if (
        mission_ref.source == "active"
        and mission_ref.phase != "idle"
        and issue_state != "OPEN"
    ):
        warnings.append(
            f"local mission is active in phase {mission_ref.phase} but issue "
            f"#{mission_ref.issue_number} is {str(issue.get('state', 'unknown')).lower()} on GitHub"
        )

    expected_label = expected_status_label(mission_ref, config)
    if expected_label is not None:
        label_names = get_status_label_names(issue)
        if expected_label not in label_names:
            warnings.append(
                f"issue #{mission_ref.issue_number} is missing expected label {expected_label} "
                f"for phase {mission_ref.phase}"
            )

    return warnings


def build_pr_status_warnings(
    *,
    mission_ref: StatusMissionRef,
    config: Config,
    pull_request: dict[str, Any],
) -> list[str]:
    warnings: list[str] = []
    head_ref = pull_request.get("headRefName")
    if mission_ref.branch is not None and head_ref != mission_ref.branch:
        warnings.append(
            f"local branch {mission_ref.branch} does not match GitHub PR head {head_ref}"
        )

    base_ref = pull_request.get("baseRefName")
    if base_ref is not None and base_ref != config.main_branch:
        warnings.append(
            f"GitHub PR base branch {base_ref} does not match configured main branch "
            f"{config.main_branch}"
        )

    return warnings


def expected_status_label(mission_ref: StatusMissionRef, config: Config) -> str | None:
    if mission_ref.source != "active":
        return None
    if mission_ref.phase == "start":
        return config.labels["working"]
    if mission_ref.phase in {"publish", "review"}:
        return config.labels["reviewing"]
    return None


def command_review(
    root: Path,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> int:
    if timeout_seconds < 0:
        print("review aborted: timeout_seconds must be non-negative")
        return 1
    if poll_interval_seconds <= 0:
        print("review aborted: poll_interval_seconds must be positive")
        return 1

    store = StateStore(root)
    if not store.has_state():
        print("Shinobi is not initialized in this workspace.")
        print("Run `shinobi init` first.")
        return 1

    config, config_error = store.try_load_config()
    if config is None:
        print(f"failed to load config: {config_error}")
        return 1

    state, state_error = store.try_load_state()
    if state is None:
        print(f"failed to load local state: {state_error}")
        return 1

    if state.phase not in {"publish", "review"}:
        print(
            "review aborted: local mission state must be in publish or review phase, "
            f"got {state.phase}"
        )
        return 1
    if state.issue_number is None or state.pr_number is None or not state.branch or not state.run_id:
        print(
            "review aborted: local mission state requires issue_number, pr_number, branch, "
            "and run_id"
        )
        return 1
    if state.agent_identity and state.agent_identity != config.agent_identity:
        print(
            "review aborted: local mission state belongs to a different agent "
            f"({state.agent_identity})"
        )
        return 1

    run_id = state.run_id
    now = datetime.now(timezone.utc)
    lease_expires_at = store.format_timestamp(now + timedelta(minutes=config.mission_lease_minutes))
    try:
        store.acquire_lock(
            config=config,
            run_id=run_id,
            pid=os.getpid(),
            now=now,
        )
    except (RuntimeError, ValueError) as error:
        print(f"review aborted: {error}")
        return 1

    try:
        try:
            store.save_state(
                State(
                    issue_number=state.issue_number,
                    pr_number=state.pr_number,
                    branch=state.branch,
                    agent_identity=config.agent_identity,
                    run_id=run_id,
                    phase="review",
                    review_loop_count=state.review_loop_count,
                    retryable_local_only=False,
                    lease_expires_at=lease_expires_at,
                    last_result=state.last_result,
                    last_error=None,
                    last_mission=state.last_mission,
                    extra=state.extra,
                )
            )
        except OSError as error:
            print(f"review aborted: failed to persist review state: {error}")
            return 1

        client = GitHubClient(root, repo=config.repo)

        def update_review_comment(*, comment_lease_expires_at: str) -> None:
            try:
                upsert_review_comment(
                    client=client,
                    issue_number=state.issue_number,
                    branch=state.branch,
                    pr_number=state.pr_number,
                    lease_expires_at=comment_lease_expires_at,
                    agent_identity=config.agent_identity,
                    run_id=run_id,
                )
            except MissionPublishError as error:
                raise ReviewerError(str(error)) from error

        def persist_review_error(error_message: str) -> None:
            try:
                store.save_state(
                    State(
                        issue_number=state.issue_number,
                        pr_number=state.pr_number,
                        branch=state.branch,
                        agent_identity=config.agent_identity,
                        run_id=run_id,
                        phase="review",
                        review_loop_count=state.review_loop_count,
                        retryable_local_only=False,
                        lease_expires_at=store.format_timestamp(
                            datetime.now(timezone.utc)
                            + timedelta(minutes=config.mission_lease_minutes)
                        ),
                        last_result="review-error",
                        last_error=error_message,
                        last_mission=state.last_mission,
                        extra=state.extra,
                    )
                )
            except OSError:
                pass

        def heartbeat() -> None:
            heartbeat_now = datetime.now(timezone.utc)
            heartbeat_lease_expires_at = store.format_timestamp(
                heartbeat_now + timedelta(minutes=config.mission_lease_minutes)
            )
            store.refresh_lock_heartbeat(
                run_id=run_id,
                agent_identity=config.agent_identity,
                now=heartbeat_now,
            )
            update_review_comment(comment_lease_expires_at=heartbeat_lease_expires_at)

        try:
            update_review_comment(comment_lease_expires_at=lease_expires_at)
            ci_status = wait_for_ci(
                client,
                state.pr_number,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                heartbeat=heartbeat,
            )
        except (ReviewerError, RuntimeError, ValueError) as error:
            persist_review_error(str(error))
            print(f"review aborted: {error}")
            return 1

        completed_at = datetime.now(timezone.utc)
        completed_lease_expires_at = store.format_timestamp(
            completed_at + timedelta(minutes=config.mission_lease_minutes)
        )
        if ci_status.is_failed:
            try:
                return handle_failed_ci_review(
                    root=root,
                    store=store,
                    config=config,
                    run_id=run_id,
                    state=state,
                    ci_status=ci_status,
                    lease_expires_at=completed_lease_expires_at,
                    update_review_comment=update_review_comment,
                )
            except (
                MissionFinalizeError,
                MissionPublishError,
                ReviewerError,
                RuntimeError,
                ValueError,
            ) as error:
                persist_review_error(str(error))
                print(f"review aborted: {error}")
                return 1
            except OSError as error:
                persist_review_error(f"failed to persist CI review retry result: {error}")
                print(f"review aborted: failed to persist CI review retry result: {error}")
                return 1

        review_state = State(
            issue_number=state.issue_number,
            pr_number=state.pr_number,
            branch=state.branch,
            agent_identity=config.agent_identity,
            run_id=run_id,
            phase="review",
            review_loop_count=state.review_loop_count,
            retryable_local_only=False,
            lease_expires_at=completed_lease_expires_at,
            last_result=render_review_result(ci_status),
            last_error="CI polling timed out before checks completed" if ci_status.timed_out else None,
            last_mission=state.last_mission,
            extra={
                **state.extra,
                "ci_status": {
                    "status": ci_status.status,
                    "timed_out": ci_status.timed_out,
                    "checks": [
                        {
                            "name": check.name,
                            "state": check.state,
                            "bucket": check.bucket,
                            "link": check.link,
                        }
                        for check in ci_status.checks
                    ],
                },
            },
        )
        try:
            update_review_comment(comment_lease_expires_at=completed_lease_expires_at)
            store.save_state(review_state)
        except (ReviewerError, RuntimeError, ValueError) as error:
            persist_review_error(str(error))
            print(f"review aborted: {error}")
            return 1
        except OSError as error:
            persist_review_error(f"failed to persist CI review result: {error}")
            print(f"review aborted: failed to persist CI review result: {error}")
            return 1

        print(f"review_issue: #{state.issue_number}")
        print(f"review_pr: #{state.pr_number}")
        print(f"ci_status: {ci_status.status}")
        print(f"ci_timed_out: {ci_status.timed_out}")
        if ci_status.checks:
            print(
                "ci_checks: "
                + ", ".join(f"{check.name}={check.bucket}" for check in ci_status.checks)
            )
        else:
            print("ci_checks: none")
        return 1 if ci_status.timed_out or ci_status.is_failed else 0
    finally:
        store.clear_lock(run_id)


def handle_failed_ci_review(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    run_id: str,
    state: State,
    ci_status: Any,
    lease_expires_at: str,
    update_review_comment: Callable[..., None],
) -> int:
    issue_number = require_review_issue_number(state)
    pr_number = require_review_pr_number(state)
    branch = require_review_branch(state)

    if state.review_loop_count >= config.max_review_loops:
        reason = (
            "Shinobi stopped review because CI failed and review loop count "
            f"{state.review_loop_count} reached max_review_loops {config.max_review_loops}."
        )
        finalize_mission(
            root=root,
            store=store,
            config=config,
            run_id=run_id,
            state=build_review_state(
                state=state,
                config=config,
                run_id=run_id,
                phase="review",
                review_loop_count=state.review_loop_count,
                lease_expires_at=lease_expires_at,
                last_result="ci-failure",
                last_error=reason,
                ci_status=ci_status,
            ),
            conclusion="needs-human",
            reason=reason,
        )
        print(f"review_issue: #{issue_number}")
        print(f"review_pr: #{pr_number}")
        print("ci_status: failure")
        print("review_result: needs-human")
        print(reason)
        return 1

    execution_result = execute_verification(root, config)
    blocking_results = blocking_verification_results(execution_result)
    if blocking_results:
        stop_lease_expires_at = store.format_timestamp(
            datetime.now(timezone.utc) + timedelta(minutes=config.mission_lease_minutes)
        )
        rendered_results = ", ".join(
            f"{command.name}: {command.status}" for command in blocking_results
        )
        reason = (
            "Shinobi stopped review retry because local verification failed "
            f"after CI failure ({rendered_results})."
        )
        finalize_mission(
            root=root,
            store=store,
            config=config,
            run_id=run_id,
            state=build_review_state(
                state=state,
                config=config,
                run_id=run_id,
                phase="review",
                review_loop_count=state.review_loop_count,
                lease_expires_at=stop_lease_expires_at,
                last_result="ci-failure",
                last_error=reason,
                ci_status=ci_status,
                execution_result=execution_result,
            ),
            conclusion="needs-human",
            reason=reason,
        )
        print(f"review_issue: #{issue_number}")
        print(f"review_pr: #{pr_number}")
        print("ci_status: failure")
        print("review_result: needs-human")
        print(reason)
        return 1

    push_branch(root, branch)
    retry_count = state.review_loop_count + 1
    retry_reason = (
        "CI failed; local verification passed and the branch was pushed for another review pass."
    )
    retry_now = datetime.now(timezone.utc)
    retry_lease_expires_at = store.format_timestamp(
        retry_now + timedelta(minutes=config.mission_lease_minutes)
    )
    store.refresh_lock_heartbeat(
        run_id=run_id,
        agent_identity=config.agent_identity,
        now=retry_now,
    )
    update_review_comment(comment_lease_expires_at=retry_lease_expires_at)
    store.save_state(
        build_review_state(
            state=state,
            config=config,
            run_id=run_id,
            phase="review",
            review_loop_count=retry_count,
            lease_expires_at=retry_lease_expires_at,
            last_result="review-retry",
            last_error=retry_reason,
            ci_status=ci_status,
            execution_result=execution_result,
        )
    )

    print(f"review_issue: #{issue_number}")
    print(f"review_pr: #{pr_number}")
    print("ci_status: failure")
    print(f"review_loop_count: {retry_count}")
    print("review_result: retry")
    print("next_phase: review")
    return 1


def require_review_issue_number(state: State) -> int:
    if state.issue_number is None:
        raise ReviewerError("review retry requires issue_number")
    return state.issue_number


def require_review_pr_number(state: State) -> int:
    if state.pr_number is None:
        raise ReviewerError("review retry requires pr_number")
    return state.pr_number


def require_review_branch(state: State) -> str:
    if not state.branch:
        raise ReviewerError("review retry requires branch")
    return state.branch


def build_review_state(
    *,
    state: State,
    config: Config,
    run_id: str,
    phase: str,
    review_loop_count: int,
    lease_expires_at: str,
    last_result: str,
    last_error: str | None,
    ci_status: Any,
    execution_result: ExecutionResult | None = None,
) -> State:
    extra: dict[str, Any] = {
        **state.extra,
        "ci_status": serialize_ci_status(ci_status),
    }
    if execution_result is not None:
        extra["retry_verification"] = serialize_execution_result(execution_result)

    return State(
        issue_number=state.issue_number,
        pr_number=state.pr_number,
        branch=state.branch,
        agent_identity=config.agent_identity,
        run_id=run_id,
        phase=phase,
        review_loop_count=review_loop_count,
        retryable_local_only=False,
        lease_expires_at=lease_expires_at,
        last_result=last_result,
        last_error=last_error,
        last_mission=state.last_mission,
        extra=extra,
    )


def serialize_ci_status(ci_status: Any) -> dict[str, Any]:
    return {
        "status": ci_status.status,
        "timed_out": ci_status.timed_out,
        "checks": [
            {
                "name": check.name,
                "state": check.state,
                "bucket": check.bucket,
                "link": check.link,
            }
            for check in ci_status.checks
        ],
    }


def serialize_execution_result(execution_result: ExecutionResult) -> dict[str, Any]:
    return {
        "commands": [
            {
                "name": command.name,
                "status": command.status,
                "returncode": command.returncode,
                "message": command.message,
            }
            for command in execution_result.commands
        ],
        "change_summary": execution_result.change_summary,
    }


def render_review_result(ci_status: Any) -> str:
    if ci_status.timed_out:
        return "ci-timeout"
    if ci_status.is_failed:
        return "ci-failure"
    if ci_status.is_successful:
        return "ci-success"
    return "ci-pending"


def command_run(root: Path, issue_number: Optional[int]) -> int:
    store = StateStore(root)
    if not store.has_state():
        print("Shinobi is not initialized in this workspace.")
        print("Run `shinobi init` first.")
        return 1

    config, config_error = store.try_load_config()

    if config is None:
        print(f"failed to load config: {config_error}")
        return 1

    run_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    try:
        _, took_over_stale_lock = store.acquire_lock(
            config=config,
            run_id=run_id,
            pid=os.getpid(),
            now=now,
        )
    except (RuntimeError, ValueError) as error:
        print(f"run aborted: {error}")
        return 1

    try:
        state, state_error = store.try_load_state()
        if state is None:
            print(f"run aborted: failed to load local state: {state_error}")
            return 1

        conflict = detect_local_mission_conflict(state=state, requested_issue=issue_number)
        if conflict is not None:
            print(f"run aborted: {conflict}")
            return 1

        try:
            active_issue_numbers = list_open_issues_with_any_label(
                root,
                (
                    config.labels["working"],
                    config.labels["reviewing"],
                ),
                repo=config.repo,
            )
        except RuntimeError as error:
            print(f"run aborted: {error}")
            return 1

        selected_issue = issue_number
        if selected_issue is None:
            if active_issue_numbers:
                rendered = ", ".join(f"#{number}" for number in active_issue_numbers)
                print(
                    "run aborted: active GitHub mission exists for "
                    f"{rendered}; recovery/resume is not implemented yet"
                )
                return 1
            try:
                selected_issue = select_ready_issue(root, config.labels["ready"], repo=config.repo)
            except RuntimeError as error:
                print(f"run aborted: {error}")
                return 1
            if selected_issue is None:
                print(f"run aborted: no open issues labeled {config.labels['ready']}")
                return 1
        else:
            if active_issue_numbers:
                rendered = ", ".join(f"#{number}" for number in active_issue_numbers)
                print(
                    "run aborted: active GitHub mission exists for "
                    f"{rendered}; targeted resume/cancel is not implemented yet"
                )
                return 1
            try:
                selected_issue = ensure_open_issue(
                    root,
                    selected_issue,
                    active_labels=(
                        config.labels["working"],
                        config.labels["reviewing"],
                    ),
                    repo=config.repo,
                )
            except RuntimeError as error:
                print(f"run aborted: {error}")
                return 1

        try:
            issue = load_issue(root, selected_issue, repo=config.repo)
            started_mission = start_mission(
                root=root,
                store=store,
                config=config,
                run_id=run_id,
                issue=issue,
                now=now,
            )
            execution_result = execute_verification(root, config)
            handoff_failed_verification(
                root=root,
                store=store,
                config=config,
                run_id=run_id,
                started_mission=started_mission,
                execution_result=execution_result,
            )
            stop_decision = detect_pre_publish_stop(
                root=root,
                store=store,
                config=config,
                run_id=run_id,
                started_mission=started_mission,
            )
            handoff_pre_publish_stop(
                root=root,
                store=store,
                config=config,
                run_id=run_id,
                started_mission=started_mission,
                stop_decision=stop_decision,
            )
            published_mission = publish_mission(
                root=root,
                store=store,
                config=config,
                run_id=run_id,
                state=store.load_state(),
                execution_result=execution_result,
            )
        except (MissionStartError, MissionPublishError, RuntimeError, ValueError) as error:
            print(f"run aborted: {error}")
            return 1

        print(f"run_id: {run_id}")
        if took_over_stale_lock:
            print("run lock: took over stale lock during select phase")
        else:
            print("run lock: acquired for select phase")
        print(f"selected_issue: {selected_issue}")
        print(f"started_branch: {started_mission.branch}")
        print(f"published_pr: #{published_mission.pr_number}")
        if published_mission.pr_url:
            print(f"published_pr_url: {published_mission.pr_url}")
        print(f"lease_expires_at: {published_mission.lease_expires_at}")
        print("next_phase: review")
        return 0
    finally:
        store.clear_lock(run_id)


def detect_local_mission_conflict(*, state: State, requested_issue: Optional[int]) -> str | None:
    if state.retryable_local_only and state.issue_number is not None:
        return (
            "retryable local-only mission exists for "
            f"issue #{state.issue_number}; resume/cancel logic is not implemented yet"
        )
    if state.retryable_local_only:
        return (
            "retryable local-only mission exists but local state is missing issue_number; "
            "repair/cancel logic is not implemented yet"
        )

    if state.phase != "idle" and state.issue_number is not None:
        return (
            f"local mission state is active for issue #{state.issue_number} "
            f"(phase: {state.phase}); resume logic is not implemented yet"
        )
    if state.phase != "idle":
        return (
            f"local mission state is active in phase {state.phase} but issue_number is missing; "
            "repair/resume logic is not implemented yet"
        )

    return None


def handoff_failed_verification(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    run_id: str,
    started_mission: StartedMission,
    execution_result: ExecutionResult,
) -> None:
    blocking_results = blocking_verification_results(execution_result)
    if not blocking_results:
        return

    rendered_results = ", ".join(
        f"{command.name}: {command.status}" for command in blocking_results
    )
    reason = (
        "Shinobi stopped before publish because verification failed or errored "
        f"({rendered_results})."
    )
    handoff_started_mission(
        root=root,
        store=store,
        config=config,
        run_id=run_id,
        started_mission=started_mission,
        reason=reason,
    )
    raise MissionPublishError(reason)


def detect_pre_publish_stop(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    run_id: str,
    started_mission: StartedMission,
) -> StopDecision | None:
    client = GitHubClient(root, repo=config.repo)
    try:
        issue_label_names = load_publishable_issue_label_names(
            client=client,
            issue_number=started_mission.issue_number,
        )
    except MissionPublishError as error:
        reason = f"Shinobi failed to evaluate pre-publish stop conditions: {error}"
        handoff_started_mission(
            root=root,
            store=store,
            config=config,
            run_id=run_id,
            started_mission=started_mission,
            reason=reason,
        )
        raise MissionPublishError(reason) from error

    blocking_labels = find_blocking_publish_labels(
        label_names=issue_label_names,
        config=config,
    )
    if blocking_labels:
        stop_publish_for_blocking_labels(
            store=store,
            issue_number=started_mission.issue_number,
            branch=started_mission.branch,
            agent_identity=config.agent_identity,
            blocking_labels=blocking_labels,
            blocked_label=config.labels["blocked"],
        )

    try:
        return detect_high_risk_stop(root, config)
    except RuntimeError as error:
        reason = f"Shinobi failed to evaluate pre-publish stop conditions: {error}"
        handoff_started_mission(
            root=root,
            store=store,
            config=config,
            run_id=run_id,
            started_mission=started_mission,
            reason=reason,
        )
        raise MissionPublishError(reason) from error


def handoff_pre_publish_stop(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    run_id: str,
    started_mission: StartedMission,
    stop_decision: StopDecision | None,
) -> None:
    if stop_decision is None:
        return

    if stop_decision.conclusion != "needs-human":
        reason = (
            "pre-publish stop requested unsupported conclusion "
            f"{stop_decision.conclusion}; handing off to needs-human instead. "
            f"Original reason: {stop_decision.reason}"
        )
        handoff_started_mission(
            root=root,
            store=store,
            config=config,
            run_id=run_id,
            started_mission=started_mission,
            reason=reason,
        )
        raise MissionPublishError(reason)

    handoff_started_mission(
        root=root,
        store=store,
        config=config,
        run_id=run_id,
        started_mission=started_mission,
        reason=stop_decision.reason,
    )
    raise MissionPublishError(stop_decision.reason)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = discover_workspace_root(Path.cwd())

    if args.command == "init":
        return command_init(root)
    if args.command == "status":
        return command_status(root)
    if args.command == "review":
        return command_review(
            root,
            timeout_seconds=args.timeout_seconds,
            poll_interval_seconds=args.poll_interval_seconds,
        )
    if args.command == "run":
        return command_run(root, args.issue)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

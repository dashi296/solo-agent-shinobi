from __future__ import annotations

import argparse
import os
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, List, Optional
from urllib.parse import urlparse

from .config import discover_workspace_root
from .executor import (
    collect_paths_against_base_ref,
    detect_high_risk_stop,
    execute_verification,
    find_high_risk_paths,
    path_matches_high_risk,
)
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
    stop_publish_for_blocking_labels,
    upsert_review_comment,
)
from .mission_start import (
    MissionStartError,
    StartedMission,
    handoff_started_mission,
    resume_local_only_mission,
    start_mission,
)
from .merger import MergerError, evaluate_merge, merge_pull_request
from .models import Config, ExecutionResult, MissionSummary, State, StopDecision
from .reviewer import ReviewerError, collect_diff_stats, wait_for_ci
from .state_store import StateStore


@dataclass(frozen=True)
class StatusMissionRef:
    source: str
    issue_number: int | None
    pr_number: int | None
    branch: str | None
    phase: str | None
    conclusion: str | None = None


@dataclass(frozen=True)
class ActionsRunRetry:
    run_id: str
    failed_only: bool


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
    try:
        current_branch = load_current_branch(root)
    except RuntimeError as error:
        print(f"review aborted: {error}")
        return 1
    if current_branch != state.branch:
        print(
            "review aborted: current git branch "
            f"{current_branch} does not match mission branch {state.branch}"
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
        rendered_checks = (
            ", ".join(f"{check.name}={check.bucket}" for check in ci_status.checks)
            if ci_status.checks
            else "none"
        )
        if ci_status.is_failed:
            try:
                return handle_failed_ci_review(
                    root=root,
                    store=store,
                    config=config,
                    client=client,
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

        if not ci_status.is_successful:
            review_state = build_review_state(
                state=state,
                config=config,
                run_id=run_id,
                phase="review",
                review_loop_count=state.review_loop_count,
                lease_expires_at=completed_lease_expires_at,
                last_result=render_review_result(ci_status),
                last_error="CI polling timed out before checks completed"
                if ci_status.timed_out
                else None,
                ci_status=ci_status,
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
            print(f"ci_checks: {rendered_checks}")
            return 1

        try:
            update_review_comment(comment_lease_expires_at=completed_lease_expires_at)
            return handle_successful_ci_review(
                root=root,
                store=store,
                config=config,
                client=client,
                run_id=run_id,
                state=state,
                ci_status=ci_status,
                lease_expires_at=completed_lease_expires_at,
                rendered_checks=rendered_checks,
            )
        except (
            MissionFinalizeError,
            MergerError,
            ReviewerError,
            RuntimeError,
            ValueError,
        ) as error:
            persist_review_error(str(error))
            print(f"review aborted: {error}")
            return 1
        except OSError as error:
            persist_review_error(f"failed to finalize successful CI review: {error}")
            print(f"review aborted: failed to finalize successful CI review: {error}")
            return 1
    finally:
        store.clear_lock(run_id)


def handle_successful_ci_review(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    client: GitHubClient,
    run_id: str,
    state: State,
    ci_status: Any,
    lease_expires_at: str,
    rendered_checks: str,
) -> int:
    issue_number = require_review_issue_number(state)
    pr_number = require_review_pr_number(state)

    try:
        issue = client.get_issue(issue_number)
    except GitHubClientError as error:
        raise ReviewerError(f"failed to load issue #{issue_number} before merge: {error}") from error

    pull_request = load_review_pull_request(client=client, pr_number=pr_number)
    validate_review_pull_request_branch(
        pull_request=pull_request,
        state=state,
        pr_number=pr_number,
    )
    base_ref = resolve_review_base_ref(pull_request=pull_request, config=config)
    diff_stats = collect_diff_stats(root, base_ref=base_ref)
    changed_paths = collect_paths_against_base_ref(root, base_ref=base_ref)
    high_risk_paths = find_high_risk_paths(
        changed_paths=changed_paths,
        high_risk_paths=config.high_risk_paths,
    )
    high_risk_changed_paths = sorted(
        path
        for path in changed_paths
        if any(path_matches_high_risk(path, high_risk_path) for high_risk_path in high_risk_paths)
    )
    decision = evaluate_merge(
        config=config,
        state=state,
        issue=issue,
        ci_status=ci_status,
        diff_stats=diff_stats,
        high_risk_paths=high_risk_paths,
        high_risk_changed_paths=high_risk_changed_paths,
    )
    review_state = build_review_state(
        state=state,
        config=config,
        run_id=run_id,
        phase="review",
        review_loop_count=state.review_loop_count,
        lease_expires_at=lease_expires_at,
        last_result=render_review_result(ci_status),
        last_error=None,
        ci_status=ci_status,
        extra={
            "merge_decision": {
                "should_merge": decision.should_merge,
                "reasons": list(decision.reasons),
                "diff_stats": {
                    "changed_files": diff_stats.changed_files,
                    "added_lines": diff_stats.added_lines,
                    "deleted_lines": diff_stats.deleted_lines,
                },
            }
        },
    )

    print(f"review_issue: #{issue_number}")
    print(f"review_pr: #{pr_number}")
    print(f"ci_status: {ci_status.status}")
    print(f"ci_timed_out: {ci_status.timed_out}")
    print(f"ci_checks: {rendered_checks}")

    if not decision.can_merge:
        reason = "Shinobi stopped auto-merge because " + "; ".join(decision.reasons) + "."
        finalize_mission(
            root=root,
            store=store,
            config=config,
            run_id=run_id,
            state=build_review_state(
                state=review_state,
                config=config,
                run_id=run_id,
                phase="review",
                review_loop_count=state.review_loop_count,
                lease_expires_at=lease_expires_at,
                last_result="needs-human",
                last_error=reason,
                ci_status=ci_status,
            ),
            conclusion=decision.conclusion,
            reason=reason,
        )
        print(f"merge_result: {decision.conclusion}")
        print(reason)
        return 1

    try:
        merge_pull_request(
            client=client,
            pr_number=pr_number,
            config=config,
            pull_request=pull_request,
        )
    except MergerError as error:
        reason = f"Shinobi stopped auto-merge because {error}."
        finalize_mission(
            root=root,
            store=store,
            config=config,
            run_id=run_id,
            state=build_review_state(
                state=review_state,
                config=config,
                run_id=run_id,
                phase="review",
                review_loop_count=state.review_loop_count,
                lease_expires_at=lease_expires_at,
                last_result="needs-human",
                last_error=reason,
                ci_status=ci_status,
            ),
            conclusion="needs-human",
            reason=reason,
        )
        print("merge_result: needs-human")
        print(reason)
        return 1

    try:
        finalize_mission(
            root=root,
            store=store,
            config=config,
            run_id=run_id,
            state=review_state,
            conclusion="merged",
        )
    except MissionFinalizeError as error:
        warning = f"merged PR but finalize follow-up failed: {error}"
        persist_merged_review_state(
            store=store,
            config=config,
            state=review_state,
            warning=warning,
        )
        print(f"merge_result: merged ({config.merge_method})")
        print(f"merge_warning: {warning}")
        return 1

    print(f"merge_result: merged ({config.merge_method})")
    return 0


def load_review_pull_request(
    *,
    client: GitHubClient,
    pr_number: int,
) -> dict[str, Any]:
    try:
        return client.get_pull_request(str(pr_number))
    except GitHubClientError as error:
        raise ReviewerError(f"failed to load PR #{pr_number} before merge: {error}") from error


def resolve_review_base_ref(
    *,
    pull_request: dict[str, Any],
    config: Config,
) -> str:
    base_ref = pull_request.get("baseRefName")
    return base_ref if isinstance(base_ref, str) and base_ref.strip() else config.main_branch


def validate_review_pull_request_branch(
    *,
    pull_request: dict[str, Any],
    state: State,
    pr_number: int,
) -> None:
    branch = require_review_branch(state)
    head_ref = pull_request.get("headRefName")
    if not isinstance(head_ref, str) or not head_ref.strip():
        raise ReviewerError(f"failed to validate PR #{pr_number} head branch before merge")
    if head_ref != branch:
        raise ReviewerError(
            f"PR #{pr_number} head branch {head_ref} does not match mission branch {branch}"
        )


def handle_failed_ci_review(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    client: GitHubClient,
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

    retry_runs = actions_run_retries(ci_status, repo=config.repo)
    if not retry_runs:
        stop_lease_expires_at = store.format_timestamp(
            datetime.now(timezone.utc) + timedelta(minutes=config.mission_lease_minutes)
        )
        reason = (
            "Shinobi stopped review retry because local verification passed after CI failure, "
            "but no rerunnable GitHub Actions workflow run was found."
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

    for retry_run in retry_runs:
        client.rerun_workflow_run(retry_run.run_id, failed_only=retry_run.failed_only)

    retry_run_ids = [retry_run.run_id for retry_run in retry_runs]
    retry_count = state.review_loop_count + 1
    retry_reason = (
        "CI failed; local verification passed and failed GitHub Actions workflow "
        f"run(s) were rerun: {', '.join(retry_run_ids)}."
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
            retry_run_ids=retry_run_ids,
        )
    )

    print(f"review_issue: #{issue_number}")
    print(f"review_pr: #{pr_number}")
    print("ci_status: failure")
    print(f"review_loop_count: {retry_count}")
    print("review_result: retry")
    print("next_phase: review")
    return 1


def failed_actions_run_ids(ci_status: Any, *, repo: str) -> list[str]:
    return [retry.run_id for retry in actions_run_retries(ci_status, repo=repo)]


def actions_run_retries(ci_status: Any, *, repo: str) -> list[ActionsRunRetry]:
    retries: dict[str, ActionsRunRetry] = {}
    for check in ci_status.checks:
        if check.bucket not in {"fail", "cancel"} or not check.link:
            continue
        run_id = parse_actions_run_id(check.link, repo=repo)
        if run_id is None:
            continue
        failed_only = check.bucket != "cancel"
        if run_id in retries:
            failed_only = retries[run_id].failed_only and failed_only
        retries[run_id] = ActionsRunRetry(run_id=run_id, failed_only=failed_only)
    return list(retries.values())


def parse_actions_run_id(link: str, *, repo: str) -> str | None:
    parsed = urlparse(link)
    if parsed.scheme != "https" or parsed.hostname != "github.com":
        return None
    repo_parts = [part for part in repo.strip("/").split("/") if part]
    path_parts = [part for part in parsed.path.split("/") if part]
    expected_prefix = [*repo_parts, "actions", "runs"]
    if path_parts[: len(expected_prefix)] != expected_prefix:
        return None
    if len(path_parts) <= len(expected_prefix):
        return None
    run_id = path_parts[len(expected_prefix)]
    return run_id if run_id.isdigit() else None


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


def load_current_branch(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise RuntimeError(f"failed to resolve current git branch: {error}") from error

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"git exited with {result.returncode}"
        raise RuntimeError(f"failed to resolve current git branch: {message}")

    branch = result.stdout.strip()
    if not branch:
        raise RuntimeError("failed to resolve current git branch: git returned an empty branch name")
    if branch == "HEAD":
        raise RuntimeError("current git checkout is detached; review requires the mission branch")
    return branch


def persist_merged_review_state(
    *,
    store: StateStore,
    config: Config,
    state: State,
    warning: str,
) -> None:
    mission = state.last_mission or MissionSummary(
        issue_number=state.issue_number,
        pr_number=state.pr_number,
        branch=state.branch,
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
            last_result="merged",
            last_error=warning,
            last_mission=MissionSummary(
                issue_number=mission.issue_number,
                pr_number=mission.pr_number,
                branch=mission.branch,
                phase="finalize",
                conclusion="merged",
            ),
            extra=state.extra,
        )
    )


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
    retry_run_ids: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> State:
    merged_extra: dict[str, Any] = {
        **state.extra,
        "ci_status": serialize_ci_status(ci_status),
    }
    if execution_result is not None:
        merged_extra["retry_verification"] = serialize_execution_result(execution_result)
    if retry_run_ids is not None:
        merged_extra["retry_workflow_run_ids"] = retry_run_ids
    if extra is not None:
        merged_extra.update(extra)

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
        extra=merged_extra,
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

        local_only_issue, local_only_error = recover_local_only_mission_candidate(
            root=root,
            store=store,
            config=config,
            state=state,
            requested_issue=issue_number,
        )
        if local_only_error is not None:
            print(f"run aborted: {local_only_error}")
            return 1

        state = store.load_state()
        if local_only_issue is None:
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

        selected_issue = local_only_issue if local_only_issue is not None else issue_number
        blocking_active_issue_numbers = active_issue_numbers
        if local_only_issue is not None:
            blocking_active_issue_numbers = [
                number for number in active_issue_numbers if number != local_only_issue
            ]
        if selected_issue is None:
            if blocking_active_issue_numbers:
                rendered = ", ".join(f"#{number}" for number in blocking_active_issue_numbers)
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
            if blocking_active_issue_numbers:
                rendered = ", ".join(f"#{number}" for number in blocking_active_issue_numbers)
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
                    allow_active_labels=local_only_issue is not None,
                    repo=config.repo,
                )
            except RuntimeError as error:
                print(f"run aborted: {error}")
                return 1

        try:
            issue = load_issue(root, selected_issue, repo=config.repo)
            if local_only_issue is not None:
                started_mission = resume_local_only_mission(
                    root=root,
                    store=store,
                    config=config,
                    run_id=run_id,
                    issue=issue,
                    state=store.load_state(),
                    now=now,
                )
            else:
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


def recover_local_only_mission_candidate(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    state: State,
    requested_issue: Optional[int],
) -> tuple[int | None, str | None]:
    if not state.retryable_local_only:
        return None, None

    issue_number = state.issue_number
    if issue_number is None:
        cleanup_error = cleanup_retryable_local_only_state(
            root=root,
            store=store,
            config=config,
            state=state,
            conclusion="aborted",
            error="retryable local-only mission is missing issue_number",
        )
        message = "retryable local-only mission exists but local state is missing issue_number"
        if cleanup_error is not None:
            message += f"; cleanup also failed: {cleanup_error}"
        return None, message

    recovery_error = validate_retryable_local_only_state(
        root=root,
        store=store,
        config=config,
        state=state,
    )
    if recovery_error is not None:
        cleanup_error = cleanup_retryable_local_only_state(
            root=root,
            store=store,
            config=config,
            state=state,
            conclusion="aborted",
            error=recovery_error,
        )
        message = (
            f"retryable local-only mission for issue #{issue_number} could not be resumed: "
            f"{recovery_error}"
        )
        if cleanup_error is not None:
            message += f"; cleanup also failed: {cleanup_error}"
        return None, message

    if requested_issue is not None and requested_issue != issue_number:
        return None, f"retryable local-only mission exists for issue #{issue_number}"

    return issue_number, None


def validate_retryable_local_only_state(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    state: State,
) -> str | None:
    if state.phase != "start":
        return f"phase must be start, got {state.phase}"
    if not state.branch:
        return "branch is missing"
    if not state.run_id:
        return "run_id is missing"
    if not state.agent_identity:
        return "agent_identity is missing"
    if state.agent_identity != config.agent_identity:
        return (
            "agent_identity does not match current workspace "
            f"({state.agent_identity} != {config.agent_identity})"
        )
    if not git_local_branch_exists(root, state.branch):
        return f"branch {state.branch} does not exist locally"
    current_branch = git_current_branch(root)
    if current_branch != state.branch:
        current_branch_label = current_branch if current_branch is not None else "detached HEAD"
        return (
            "current branch does not match retryable local-only branch "
            f"({current_branch_label} != {state.branch})"
        )
    if not store.has_retryable_start_failure(
        issue_number=state.issue_number,
        branch=state.branch,
        phase=state.phase,
        agent_identity=state.agent_identity,
        run_id=state.run_id,
    ):
        return "retryable start failure record is missing"
    return None


def cleanup_retryable_local_only_state(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    state: State,
    conclusion: str,
    error: str,
) -> str | None:
    current_workspace_identity = config.agent_identity
    state_belongs_to_current_workspace = state.agent_identity == current_workspace_identity
    try:
        store.save_state(
            State(
                issue_number=None,
                pr_number=None,
                branch=None,
                agent_identity=current_workspace_identity,
                run_id=None,
                phase="idle",
                review_loop_count=0,
                retryable_local_only=False,
                lease_expires_at=None,
                last_result=conclusion,
                last_error=error,
                last_mission=MissionSummary(
                    issue_number=state.issue_number,
                    pr_number=state.pr_number,
                    branch=state.branch,
                    phase=state.phase,
                    conclusion=conclusion,
                ),
            )
        )
    except OSError as save_error:
        return f"failed to clear retryable local-only state: {save_error}"

    if state.issue_number is None:
        return None

    if not state_belongs_to_current_workspace:
        return None

    try:
        GitHubClient(root, repo=config.repo).create_issue_comment(
            state.issue_number,
            render_retryable_local_only_cleanup_comment(
                issue_number=state.issue_number,
                branch=state.branch,
                phase=state.phase,
                error=error,
            ),
        )
    except GitHubClientError as comment_error:
        return (
            "failed to comment on cleared retryable local-only mission for "
            f"issue #{state.issue_number}: {comment_error}"
        )
    return None


def render_retryable_local_only_cleanup_comment(
    *,
    issue_number: int,
    branch: str | None,
    phase: str,
    error: str,
) -> str:
    branch_line = branch if branch is not None else "unknown"
    return (
        "Shinobi cleared an invalid retryable local-only mission record and will not auto-resume it.\n\n"
        f"- issue: #{issue_number}\n"
        f"- branch: {branch_line}\n"
        f"- phase: {phase}\n"
        f"- reason: {error}\n"
    )


def git_local_branch_exists(root: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def git_current_branch(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    branch = result.stdout.strip()
    return branch or None


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

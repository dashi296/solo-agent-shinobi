from __future__ import annotations

import argparse
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from .config import discover_workspace_root
from .executor import execute_verification
from .github_client import GitHubClient, GitHubClientError
from .issue_selector import (
    ensure_open_issue,
    load_issue,
    list_open_issues,
    list_open_issues_with_any_label,
    select_ready_issue,
)
from .mission_publish import (
    MissionPublishError,
    blocking_verification_results,
    publish_mission,
)
from .mission_start import (
    MissionStartError,
    StartedMission,
    handoff_started_mission,
    start_mission,
)
from .models import Config, ExecutionResult, State
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
    return parser


def positive_issue_number(value: str) -> int:
    issue_number = int(value)
    if issue_number <= 0:
        raise argparse.ArgumentTypeError("issue number must be a positive integer")
    return issue_number


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
    if mission_ref.phase == "publish":
        return config.labels["reviewing"]
    return None


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


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = discover_workspace_root(Path.cwd())

    if args.command == "init":
        return command_init(root)
    if args.command == "status":
        return command_status(root)
    if args.command == "run":
        return command_run(root, args.issue)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

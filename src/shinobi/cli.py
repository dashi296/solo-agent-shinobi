from __future__ import annotations

import argparse
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import discover_workspace_root
from .issue_selector import (
    ensure_open_issue,
    list_open_issues,
    list_open_issues_with_any_label,
    select_ready_issue,
)
from .models import State
from .state_store import StateStore


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
        print("github_status: unavailable in foundations MVP")
        return 1

    print(f"phase: {state.phase}")
    print(f"issue_number: {state.issue_number}")
    print(f"pr_number: {state.pr_number}")
    print(f"branch: {state.branch}")
    print("github_status: unavailable in foundations MVP")
    return 0


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
                selected_issue = select_ready_issue(root, config.labels["ready"])
            except RuntimeError as error:
                print(f"run aborted: {error}")
                return 1
            if selected_issue is None:
                print(f"run aborted: no open issues labeled {config.labels['ready']}")
                return 1
        else:
            conflicting_active_issue_numbers = [
                number for number in active_issue_numbers if number != selected_issue
            ]
            if conflicting_active_issue_numbers:
                rendered = ", ".join(f"#{number}" for number in conflicting_active_issue_numbers)
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
                    allow_active_labels=selected_issue in active_issue_numbers,
                )
            except RuntimeError as error:
                print(f"run aborted: {error}")
                return 1

        print(f"run_id: {run_id}")
        if took_over_stale_lock:
            print("run lock: took over stale lock during select phase")
        else:
            print("run lock: acquired for select phase")
        print(f"selected_issue: {selected_issue}")
        print("next_phase: start (not implemented in this milestone)")
        return 0
    finally:
        store.clear_lock(run_id)


def detect_local_mission_conflict(*, state: State, requested_issue: Optional[int]) -> str | None:
    same_requested_issue = (
        requested_issue is not None
        and state.issue_number is not None
        and state.issue_number == requested_issue
    )

    if state.retryable_local_only and state.issue_number is not None:
        if same_requested_issue:
            return None
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
        if same_requested_issue:
            return None
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

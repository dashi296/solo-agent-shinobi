from __future__ import annotations

import argparse
from pathlib import Path
from typing import List, Optional

from .config import discover_workspace_root, load_config
from .state_store import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shinobi")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="Initialize local Shinobi state.")
    subparsers.add_parser("status", help="Show local Shinobi state.")
    return parser


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
    config_error = None
    try:
        config = load_config(store.paths.config_path)
    except (OSError, ValueError, TypeError) as error:
        config = None
        config_error = str(error)

    print("Shinobi status")

    if config is None and state is None:
        print(f"warning: failed to load config: {config_error}")
        print(f"warning: failed to load local state: {state_error}")
        return 1

    if config is not None:
        print(f"repo: {config.repo}")
        print(f"agent_identity: {config.agent_identity}")
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


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = discover_workspace_root(Path.cwd())

    if args.command == "init":
        return command_init(root)
    if args.command == "status":
        return command_status(root)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

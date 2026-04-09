from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from json import JSONDecodeError
from pathlib import Path
from typing import Tuple

from .config import default_config, load_config, save_config
from .models import Config, RunLock, State


SUMMARY_TEMPLATE = """# Shinobi Summary

- Project summary:
- Recent decisions:
- Open risks:
"""


DECISIONS_TEMPLATE = """# Shinobi Decisions

- Agreed constraints:
- Things Shinobi must not do:
- Follow-up notes:
"""


@dataclass
class WorkspacePaths:
    root: Path

    @property
    def shinobi_dir(self) -> Path:
        return self.root / ".shinobi"

    @property
    def config_path(self) -> Path:
        return self.shinobi_dir / "config.json"

    @property
    def state_path(self) -> Path:
        return self.shinobi_dir / "state.json"

    @property
    def summary_path(self) -> Path:
        return self.shinobi_dir / "summary.md"

    @property
    def decisions_path(self) -> Path:
        return self.shinobi_dir / "decisions.md"

    @property
    def lock_path(self) -> Path:
        return self.shinobi_dir / "run.lock"

    @property
    def logs_dir(self) -> Path:
        return self.shinobi_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.shinobi_dir / "cache"


class StateStore:
    def __init__(self, root: Path) -> None:
        self.paths = WorkspacePaths(root=root)

    def initialize(self) -> Tuple[Config, State]:
        self.paths.shinobi_dir.mkdir(exist_ok=True)
        self.paths.logs_dir.mkdir(exist_ok=True)
        self.paths.cache_dir.mkdir(exist_ok=True)

        if self.paths.state_path.exists():
            state, _ = self.try_load_state()
        else:
            state = None

        if self.paths.config_path.exists():
            config, _ = self.try_load_config()
        else:
            config = None

        if config is None:
            config = default_config(self.paths.root)
            if state is not None and state.agent_identity:
                config.agent_identity = state.agent_identity
            save_config(self.paths.config_path, config)

        if state is None:
            state = State(agent_identity=config.agent_identity)
            self.save_state(state)
        elif not state.agent_identity and config.agent_identity:
            state.agent_identity = config.agent_identity
            self.save_state(state)

        if not config.agent_identity:
            config.agent_identity = state.agent_identity or default_config(
                self.paths.root
            ).agent_identity
            save_config(self.paths.config_path, config)
            if not state.agent_identity:
                state.agent_identity = config.agent_identity
                self.save_state(state)

        if state.agent_identity != config.agent_identity:
            state.agent_identity = config.agent_identity
            self.save_state(state)

        if not self.paths.summary_path.exists():
            self.paths.summary_path.write_text(SUMMARY_TEMPLATE, encoding="utf-8")
        if not self.paths.decisions_path.exists():
            self.paths.decisions_path.write_text(DECISIONS_TEMPLATE, encoding="utf-8")
        if not self.paths.lock_path.exists():
            self.paths.lock_path.write_text("", encoding="utf-8")

        self.ensure_shinobi_ignored()

        return config, state

    def ensure_shinobi_ignored(self) -> None:
        exclude_path = self.resolve_git_info_exclude_path()
        if exclude_path is None:
            return

        lines = (
            exclude_path.read_text(encoding="utf-8").splitlines()
            if exclude_path.exists()
            else []
        )

        if any(line.strip() in {".shinobi", ".shinobi/"} for line in lines):
            return

        content = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        if content and not content.endswith("\n"):
            content += "\n"
        content += ".shinobi/\n"
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        exclude_path.write_text(content, encoding="utf-8")

    def resolve_git_info_exclude_path(self) -> Path | None:
        result = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=self.paths.root,
            check=False,
            capture_output=True,
            text=True,
        )
        git_path = result.stdout.strip()
        if not git_path:
            return None
        resolved = Path(git_path)
        if not resolved.is_absolute():
            resolved = self.paths.root / resolved
        return resolved

    def load_state(self) -> State:
        return State.from_dict(json.loads(self.paths.state_path.read_text(encoding="utf-8")))

    def try_load_config(self) -> Tuple[Config | None, str | None]:
        try:
            return load_config(self.paths.config_path), None
        except (OSError, JSONDecodeError, TypeError, ValueError) as error:
            return None, str(error)

    def try_load_state(self) -> Tuple[State | None, str | None]:
        try:
            return self.load_state(), None
        except (OSError, JSONDecodeError, TypeError, ValueError) as error:
            return None, str(error)

    def save_state(self, state: State) -> None:
        self.paths.state_path.write_text(
            json.dumps(state.to_dict(), indent=2) + "\n", encoding="utf-8"
        )

    def has_state(self) -> bool:
        return self.paths.config_path.exists() or self.paths.state_path.exists()

    def try_load_lock(self) -> Tuple[RunLock | None, str | None]:
        try:
            return self.load_lock(), None
        except (OSError, JSONDecodeError, TypeError, ValueError) as error:
            return None, str(error)

    def load_lock(self) -> RunLock | None:
        if not self.paths.lock_path.exists():
            return None
        content = self.paths.lock_path.read_text(encoding="utf-8").strip()
        if not content:
            return None
        return RunLock.from_dict(json.loads(content))

    def save_lock(self, lock: RunLock) -> None:
        self.paths.lock_path.write_text(
            json.dumps(lock.to_dict(), indent=2) + "\n", encoding="utf-8"
        )

    def clear_lock(self, run_id: str) -> None:
        lock, _ = self.try_load_lock()
        if lock is None or lock.run_id != run_id:
            return
        self.paths.lock_path.write_text("", encoding="utf-8")

    def acquire_lock(
        self,
        *,
        config: Config,
        run_id: str,
        pid: int,
        now: datetime,
    ) -> Tuple[RunLock, bool]:
        existing, error = self.try_load_lock()
        if error is not None:
            raise ValueError(f"failed to load run lock: {error}")

        if existing is not None and not self.is_lock_stale(existing, config, now):
            raise RuntimeError(
                f"run lock is held by live run {existing.run_id} ({existing.agent_identity})"
            )

        timestamp = self.format_timestamp(now)
        lock = RunLock(
            agent_identity=config.agent_identity,
            run_id=run_id,
            pid=pid,
            started_at=timestamp,
            heartbeat_at=timestamp,
        )
        took_over_stale_lock = existing is not None
        self.save_lock(lock)
        return lock, took_over_stale_lock

    def is_lock_stale(self, lock: RunLock, config: Config, now: datetime) -> bool:
        heartbeat_at = self.parse_timestamp(lock.heartbeat_at)
        lease_deadline = heartbeat_at + timedelta(minutes=config.mission_lease_minutes)
        return now > lease_deadline

    @staticmethod
    def parse_timestamp(value: str) -> datetime:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized)

    @staticmethod
    def format_timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"
        )

from __future__ import annotations

import json
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from typing import Tuple

from .config import default_config, load_config, save_config
from .models import Config, State


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

        if self.paths.config_path.exists():
            config, _ = self.try_load_config()
        else:
            config = None

        if config is None:
            config = default_config(self.paths.root)
            save_config(self.paths.config_path, config)

        if self.paths.state_path.exists():
            state, _ = self.try_load_state()
        else:
            state = None

        if state is None:
            state = State(agent_identity=config.agent_identity)
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

        return config, state

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
        return self.paths.config_path.exists() and self.paths.state_path.exists()

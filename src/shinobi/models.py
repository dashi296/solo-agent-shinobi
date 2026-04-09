from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


DEFAULT_LABELS = {
    "ready": "shinobi:ready",
    "working": "shinobi:working",
    "reviewing": "shinobi:reviewing",
    "blocked": "shinobi:blocked",
    "needs_human": "shinobi:needs-human",
    "merged": "shinobi:merged",
    "risky": "shinobi:risky",
}


@dataclass
class Config:
    repo: str
    main_branch: str = "main"
    agent_identity: str = ""
    mission_lease_minutes: int = 30
    mission_heartbeat_interval_minutes: int = 5
    max_review_loops: int = 3
    max_commits_per_issue: int = 8
    max_changed_files: int = 20
    max_lines_changed: int = 800
    max_runtime_minutes: int = 30
    max_token_budget: int = 40000
    auto_merge: bool = True
    use_draft_pr: bool = True
    merge_method: str = "squash"
    labels: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LABELS))
    high_risk_paths: List[str] = field(
        default_factory=lambda: ["migrations/", "infra/", "auth/", "billing/"]
    )
    extra: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        extra = payload.pop("extra")
        payload.update(extra)
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        merged = dict(data)
        labels = dict(DEFAULT_LABELS)
        labels.update(merged.get("labels", {}))
        merged["labels"] = labels
        known_fields = {field.name for field in cls.__dataclass_fields__.values()}
        known_fields.discard("extra")
        extra = {key: merged.pop(key) for key in list(merged) if key not in known_fields}
        return cls(**merged, extra=extra)


@dataclass
class MissionSummary:
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None
    branch: Optional[str] = None
    phase: Optional[str] = None
    conclusion: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["MissionSummary"]:
        if data is None:
            return None
        return cls(**data)


@dataclass
class State:
    issue_number: Optional[int] = None
    pr_number: Optional[int] = None
    branch: Optional[str] = None
    agent_identity: str = ""
    run_id: Optional[str] = None
    phase: str = "idle"
    review_loop_count: int = 0
    retryable_local_only: bool = False
    lease_expires_at: Optional[str] = None
    last_result: Optional[str] = "initialized"
    last_error: Optional[str] = None
    last_mission: Optional[MissionSummary] = None
    extra: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        extra = payload.pop("extra")
        if self.last_mission is not None:
            payload["last_mission"] = self.last_mission.to_dict()
        payload.update(extra)
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "State":
        merged = dict(data)
        merged["last_mission"] = MissionSummary.from_dict(merged.get("last_mission"))
        known_fields = {field.name for field in cls.__dataclass_fields__.values()}
        known_fields.discard("extra")
        extra = {key: merged.pop(key) for key in list(merged) if key not in known_fields}
        return cls(**merged, extra=extra)


@dataclass
class RunLock:
    agent_identity: str
    run_id: str
    pid: int
    started_at: str
    heartbeat_at: str
    extra: Dict[str, Any] = field(default_factory=dict, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        extra = payload.pop("extra")
        payload.update(extra)
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunLock":
        merged = dict(data)
        known_fields = {field.name for field in cls.__dataclass_fields__.values()}
        known_fields.discard("extra")
        extra = {key: merged.pop(key) for key in list(merged) if key not in known_fields}
        return cls(**merged, extra=extra)

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .github_client import GitHubClient, GitHubClientError
from .mission_start import labels_to_remove_for_transition
from .models import Config, MissionSummary, State
from .state_store import StateStore

FINALIZE_PHASE = "finalize"
TERMINAL_CONCLUSION_LABEL_KEYS = ("merged", "blocked", "needs_human")


class MissionFinalizeError(RuntimeError):
    """Raised when the finalize phase cannot complete safely."""


@dataclass(frozen=True)
class FinalizableMission:
    issue_number: int
    pr_number: int | None
    branch: str | None


@dataclass(frozen=True)
class FinalizedMission:
    issue_number: int
    pr_number: int | None
    branch: str | None
    conclusion: str


def finalize_mission(
    *,
    root: Path,
    store: StateStore,
    config: Config,
    run_id: str,
    state: State,
    conclusion: str,
    reason: str | None = None,
) -> FinalizedMission:
    normalized_conclusion = normalize_conclusion_key(conclusion)
    require_supported_conclusion(normalized_conclusion, config)
    rendered_conclusion = normalize_conclusion(normalized_conclusion)
    mission = resolve_finalizable_mission(state)
    store.require_lock_owner(run_id, config.agent_identity)

    client = GitHubClient(root, repo=config.repo)
    issue_label_names = load_finalize_issue_label_names(client, mission.issue_number)
    sync_finalize_labels(
        client=client,
        issue_number=mission.issue_number,
        config=config,
        current_label_names=issue_label_names,
        conclusion=normalized_conclusion,
    )
    post_finalize_comment(
        client=client,
        issue_number=mission.issue_number,
        pr_number=mission.pr_number,
        branch=mission.branch,
        conclusion=normalized_conclusion,
        reason=reason,
    )
    if normalized_conclusion == "merged":
        close_finalized_issue(client, mission.issue_number)

    finalized_state = build_finalized_state(
        prior_state=state,
        config=config,
        mission=mission,
        conclusion=rendered_conclusion,
        reason=reason,
    )
    try:
        store.save_state(finalized_state)
    except OSError as error:
        raise MissionFinalizeError(
            f"failed to persist finalized local state for issue #{mission.issue_number}: {error}"
        ) from error

    try:
        store.clear_lock(run_id)
    except OSError as error:
        raise MissionFinalizeError(
            f"failed to clear run lock after finalizing issue #{mission.issue_number}: {error}"
        ) from error

    return FinalizedMission(
        issue_number=mission.issue_number,
        pr_number=mission.pr_number,
        branch=mission.branch,
        conclusion=rendered_conclusion,
    )


def require_supported_conclusion(conclusion: str, config: Config) -> None:
    supported = {
        config.labels[key]: key for key in TERMINAL_CONCLUSION_LABEL_KEYS if key in config.labels
    }
    if conclusion in TERMINAL_CONCLUSION_LABEL_KEYS:
        return
    if conclusion in supported:
        return
    allowed = ", ".join(TERMINAL_CONCLUSION_LABEL_KEYS)
    raise MissionFinalizeError(
        f"unsupported finalize conclusion {conclusion!r}; expected one of {allowed}"
    )


def resolve_finalizable_mission(state: State) -> FinalizableMission:
    if state.issue_number is not None:
        return FinalizableMission(
            issue_number=state.issue_number,
            pr_number=state.pr_number,
            branch=state.branch,
        )

    if state.last_mission is not None and state.last_mission.issue_number is not None:
        return FinalizableMission(
            issue_number=state.last_mission.issue_number,
            pr_number=state.last_mission.pr_number,
            branch=state.last_mission.branch,
        )

    raise MissionFinalizeError(
        "finalize phase requires an active or last mission with issue_number"
    )


def load_finalize_issue_label_names(
    client: GitHubClient,
    issue_number: int,
) -> set[str]:
    try:
        issue = client.get_issue(issue_number)
    except GitHubClientError as error:
        raise MissionFinalizeError(
            f"failed to load issue #{issue_number} before finalize: {error}"
        ) from error
    labels = issue.get("labels", [])
    return {
        label.get("name", "")
        for label in labels
        if isinstance(label, dict)
    }


def sync_finalize_labels(
    *,
    client: GitHubClient,
    issue_number: int,
    config: Config,
    current_label_names: set[str],
    conclusion: str,
) -> None:
    target_label = config.labels.get(conclusion, conclusion)
    removable_labels = labels_to_remove_for_transition(
        config=config,
        current_label_names=current_label_names | {target_label},
        target_label=target_label,
    )
    try:
        client.update_issue_labels(issue_number, add=[target_label])
        if removable_labels:
            client.update_issue_labels(issue_number, remove=removable_labels)
    except GitHubClientError as error:
        raise MissionFinalizeError(
            f"failed to normalize finalize labels for issue #{issue_number}: {error}"
        ) from error


def post_finalize_comment(
    *,
    client: GitHubClient,
    issue_number: int,
    pr_number: int | None,
    branch: str | None,
    conclusion: str,
    reason: str | None,
) -> None:
    body = render_finalize_comment(
        issue_number=issue_number,
        pr_number=pr_number,
        branch=branch,
        conclusion=conclusion,
        reason=reason,
    )
    try:
        client.create_issue_comment(issue_number, body)
    except GitHubClientError as error:
        raise MissionFinalizeError(
            f"failed to create finalize comment on issue #{issue_number}: {error}"
        ) from error


def close_finalized_issue(client: GitHubClient, issue_number: int) -> None:
    try:
        client.close_issue(issue_number)
    except GitHubClientError as error:
        raise MissionFinalizeError(f"failed to close issue #{issue_number}: {error}") from error


def build_finalized_state(
    *,
    prior_state: State,
    config: Config,
    mission: FinalizableMission,
    conclusion: str,
    reason: str | None,
) -> State:
    return State(
        issue_number=None,
        pr_number=None,
        branch=None,
        agent_identity=config.agent_identity or prior_state.agent_identity,
        run_id=None,
        phase="idle",
        review_loop_count=0,
        retryable_local_only=False,
        lease_expires_at=None,
        last_result=conclusion,
        last_error=reason,
        last_mission=MissionSummary(
            issue_number=mission.issue_number,
            pr_number=mission.pr_number,
            branch=mission.branch,
            phase=FINALIZE_PHASE,
            conclusion=conclusion,
        ),
    )


def render_finalize_comment(
    *,
    issue_number: int,
    pr_number: int | None,
    branch: str | None,
    conclusion: str,
    reason: str | None,
) -> str:
    headline = {
        "merged": "Shinobi Finalize: merged",
        "blocked": "Shinobi Finalize: blocked",
        "needs_human": "Shinobi Finalize: needs-human",
        "needs-human": "Shinobi Finalize: needs-human",
    }.get(conclusion, f"Shinobi Finalize: {conclusion}")
    lines = [
        headline,
        "",
        f"任務 #{issue_number} を `{normalize_conclusion(conclusion)}` として終了します。",
    ]
    if branch:
        lines.append(f"- branch: `{branch}`")
    if pr_number is not None:
        lines.append(f"- pr: #{pr_number}")
    if reason:
        lines.append(f"- reason: {reason}")
    return "\n".join(lines) + "\n"


def normalize_conclusion(conclusion: str) -> str:
    return "needs-human" if conclusion == "needs_human" else conclusion


def normalize_conclusion_key(conclusion: str) -> str:
    return "needs_human" if conclusion == "needs-human" else conclusion

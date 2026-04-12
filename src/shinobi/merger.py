from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .github_client import GitHubClient, GitHubClientError
from .models import CIStatus, Config, DiffStats, State
from .reviewer import issue_label_names


class MergerError(RuntimeError):
    """Raised when merge inputs or operations are unsafe."""


@dataclass(frozen=True)
class MergeDecision:
    should_merge: bool
    reasons: list[str]

    @property
    def can_merge(self) -> bool:
        return self.should_merge


def evaluate_merge(
    *,
    config: Config,
    state: State,
    issue: dict[str, Any],
    ci_status: CIStatus,
    diff_stats: DiffStats,
) -> MergeDecision:
    reasons: list[str] = []
    label_names = issue_label_names(issue)
    risky_label = config.labels["risky"]

    if not config.auto_merge:
        reasons.append("auto_merge is disabled in config")

    if ci_status.timed_out:
        reasons.append("CI polling timed out before checks completed")
    elif not ci_status.is_successful:
        reasons.append(f"CI status is {ci_status.status}, not success")

    if risky_label in label_names:
        reasons.append(
            f"issue #{issue['number']} has label {risky_label}, requiring human merge"
        )

    if state.review_loop_count >= config.max_review_loops:
        reasons.append(
            "review loop count "
            f"{state.review_loop_count} reached max_review_loops {config.max_review_loops}"
        )

    if diff_stats.changed_files > config.max_changed_files:
        reasons.append(
            "changed files "
            f"{diff_stats.changed_files} exceed max_changed_files {config.max_changed_files}"
        )

    if diff_stats.total_changed_lines > config.max_lines_changed:
        reasons.append(
            "total changed lines "
            f"{diff_stats.total_changed_lines} exceed max_lines_changed {config.max_lines_changed}"
        )

    return MergeDecision(should_merge=not reasons, reasons=reasons)


def merge_pull_request(
    *,
    client: GitHubClient,
    pr_number: int,
    config: Config,
) -> None:
    try:
        pull_request = client.get_pull_request(str(pr_number))
        if pull_request.get("isDraft"):
            client.convert_pull_request_to_ready(pr_number)
        client.merge_pull_request(
            pr_number,
            merge_method=config.merge_method,
            delete_branch=True,
        )
    except GitHubClientError as error:
        raise MergerError(f"failed to merge PR #{pr_number}: {error}") from error

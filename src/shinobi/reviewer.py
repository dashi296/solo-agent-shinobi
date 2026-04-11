from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from .models import Config, DiffStats, ReviewDecision, State


class ReviewerError(RuntimeError):
    """Raised when review inputs cannot be gathered safely."""


def collect_diff_stats(root: Path, *, base_ref: str) -> DiffStats:
    try:
        result = subprocess.run(
            ["git", "diff", "--numstat", f"{base_ref}...HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise ReviewerError(f"failed to collect diff stats against {base_ref}: {error}") from error

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"git exited with {result.returncode}"
        raise ReviewerError(f"failed to collect diff stats against {base_ref}: {message}")

    return parse_numstat(result.stdout)


def parse_numstat(output: str) -> DiffStats:
    changed_files = 0
    added_lines = 0
    deleted_lines = 0

    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        added, deleted, _path = raw_line.split("\t", 2)
        changed_files += 1
        if added.isdigit():
            added_lines += int(added)
        if deleted.isdigit():
            deleted_lines += int(deleted)

    return DiffStats(
        changed_files=changed_files,
        added_lines=added_lines,
        deleted_lines=deleted_lines,
    )


def evaluate_review(
    *,
    config: Config,
    state: State,
    issue: dict[str, Any],
    diff_stats: DiffStats,
) -> ReviewDecision:
    reasons: list[str] = []
    label_names = issue_label_names(issue)
    risky_label = config.labels["risky"]

    if risky_label in label_names:
        reasons.append(
            f"issue #{issue['number']} has label {risky_label}, requiring human review"
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

    if state.review_loop_count >= config.max_review_loops:
        reasons.append(
            "review loop count "
            f"{state.review_loop_count} reached max_review_loops {config.max_review_loops}"
        )

    return ReviewDecision(should_stop=bool(reasons), reasons=reasons)


def issue_label_names(issue: dict[str, Any]) -> set[str]:
    return {
        label.get("name", "")
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }

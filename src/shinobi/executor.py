from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Iterable

from .models import Config, ExecutionResult, StopDecision, VerificationCommandResult


VERIFICATION_ORDER = ("lint", "typecheck", "test")


def execute_verification(root: Path, config: Config) -> ExecutionResult:
    results = [
        run_verification_command(root, name, config.verification_commands.get(name, []))
        for name in VERIFICATION_ORDER
    ]
    return ExecutionResult(
        commands=results,
        change_summary="No automated code changes are performed by the minimal executor.",
    )


def detect_high_risk_stop(root: Path, config: Config) -> StopDecision | None:
    changed_paths = collect_changed_paths(root, base_ref=config.main_branch)
    matched_paths = find_high_risk_paths(
        changed_paths=changed_paths,
        high_risk_paths=config.high_risk_paths,
    )
    if not matched_paths:
        return None

    risky_files = sorted(
        path
        for path in changed_paths
        if any(path_matches_high_risk(path, matched_path) for matched_path in matched_paths)
    )
    joined_paths = ", ".join(matched_paths)
    joined_files = ", ".join(risky_files)
    return StopDecision(
        reason=(
            "Shinobi stopped before publish because changed files match "
            f"high-risk path(s): {joined_paths} (files: {joined_files})"
        ),
        conclusion="needs-human",
        retryable=False,
        changed_paths=risky_files,
        matched_paths=matched_paths,
    )


def collect_changed_paths(root: Path, *, base_ref: str) -> list[str]:
    changed_paths = set(
        collect_paths_against_base_ref(
            root,
            base_ref=base_ref,
        )
    )
    changed_paths.update(
        run_diff_paths_command(
            root,
            ["git", "diff", "--cached", "--name-status", "--diff-filter=ACMRD"],
            error_context="failed to collect staged changed paths",
        )
    )
    changed_paths.update(
        run_diff_paths_command(
            root,
            ["git", "diff", "--name-status", "--diff-filter=ACMRD"],
            error_context="failed to collect unstaged changed paths",
        )
    )
    changed_paths.update(
        run_line_paths_command(
            root,
            ["git", "ls-files", "--others", "--exclude-standard"],
            error_context="failed to collect untracked changed paths",
        )
    )
    return sorted(changed_paths)


def collect_paths_against_base_ref(root: Path, *, base_ref: str) -> list[str]:
    candidate_refs = [base_ref]
    remote_candidate = f"origin/{base_ref}"
    if remote_candidate != base_ref:
        candidate_refs.append(remote_candidate)

    errors: list[str] = []
    for candidate_ref in candidate_refs:
        try:
            return run_diff_paths_command(
                root,
                ["git", "diff", "--name-status", "--diff-filter=ACMRD", f"{candidate_ref}...HEAD"],
                error_context=f"failed to collect changed paths against {candidate_ref}",
            )
        except RuntimeError as error:
            error_message = str(error)
            if not is_missing_revision_error(error_message) or candidate_ref == candidate_refs[-1]:
                errors.append(error_message)
                break
            errors.append(error_message)

    joined_errors = "; ".join(errors)
    raise RuntimeError(joined_errors)


def is_missing_revision_error(message: str) -> bool:
    lowered = message.lower()
    return "unknown revision" in lowered or "bad revision" in lowered


def run_diff_paths_command(
    root: Path,
    command: list[str],
    *,
    error_context: str,
) -> list[str]:
    result = run_git_command(root, command, error_context=error_context)
    return parse_name_status_paths(result.stdout)


def run_line_paths_command(
    root: Path,
    command: list[str],
    *,
    error_context: str,
) -> list[str]:
    result = run_git_command(root, command, error_context=error_context)
    return [normalize_repo_path(line) for line in result.stdout.splitlines() if line.strip()]


def run_git_command(
    root: Path,
    command: list[str],
    *,
    error_context: str,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise RuntimeError(f"{error_context}: {error}") from error

    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or f"git exited with {result.returncode}"
        raise RuntimeError(f"{error_context}: {message}")

    return result


def parse_name_status_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0]
        changed_fields = fields[1:]
        if not status or not changed_fields:
            continue
        if status[0] in {"R", "C"}:
            paths.extend(normalize_repo_path(path) for path in changed_fields if path.strip())
            continue
        paths.append(normalize_repo_path(changed_fields[0]))
    return paths


def find_high_risk_paths(*, changed_paths: Iterable[str], high_risk_paths: Iterable[str]) -> list[str]:
    normalized_high_risk_paths = [normalize_repo_path(path) for path in high_risk_paths if path.strip()]
    matched = {
        high_risk_path
        for high_risk_path in normalized_high_risk_paths
        if any(path_matches_high_risk(changed_path, high_risk_path) for changed_path in changed_paths)
    }
    return sorted(matched)


def path_matches_high_risk(changed_path: str, high_risk_path: str) -> bool:
    if high_risk_path.endswith("/"):
        return changed_path.startswith(high_risk_path)
    return changed_path == high_risk_path or changed_path.startswith(f"{high_risk_path}/")


def normalize_repo_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    while normalized.startswith("/"):
        normalized = normalized[1:]
    return normalized if not path.endswith("/") else normalized.rstrip("/") + "/"


def run_verification_command(
    root: Path,
    name: str,
    command: list[str],
) -> VerificationCommandResult:
    if not command:
        return VerificationCommandResult(
            name=name,
            command=[],
            status="not_configured",
            message=f"verification command `{name}` is not configured",
        )

    try:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        return VerificationCommandResult(
            name=name,
            command=list(command),
            status="error",
            message=f"failed to run verification command `{name}`: {error}",
        )

    status = "passed" if result.returncode == 0 else "failed"
    return VerificationCommandResult(
        name=name,
        command=list(command),
        status=status,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )

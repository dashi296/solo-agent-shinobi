from __future__ import annotations

import subprocess
from pathlib import Path

from .models import Config, ExecutionResult, VerificationCommandResult


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

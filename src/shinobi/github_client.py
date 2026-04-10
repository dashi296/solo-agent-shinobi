from __future__ import annotations

import json
import subprocess
from json import JSONDecodeError
from pathlib import Path
from typing import Any

from .config import discover_repo_slug


class GitHubClientError(RuntimeError):
    """Raised when a GitHub CLI operation fails."""


class GitHubClient:
    def __init__(self, root: Path, *, repo: str | None = None) -> None:
        self.root = root
        self.repo = repo or discover_repo_slug(root)

    def get_issue(self, issue_number: int) -> dict[str, Any]:
        payload = self._api_json(
            ["repos/{repo}/issues/{issue_number}"],
            action=f"load issue #{issue_number}",
            issue_number=issue_number,
        )
        if not isinstance(payload, dict):
            raise GitHubClientError(f"failed to parse issue #{issue_number}: expected object payload")
        return payload

    def list_open_issues(self, label: str, *, per_page: int = 100) -> list[dict[str, Any]]:
        page = 1
        issues: list[dict[str, Any]] = []

        while True:
            payload = self._api_json(
                ["repos/{repo}/issues", "--method", "GET"],
                action=f"list open issues for label {label}",
                fields={
                    "state": "open",
                    "labels": label,
                    "per_page": str(per_page),
                    "page": str(page),
                },
            )
            if not isinstance(payload, list):
                raise GitHubClientError(
                    f"failed to parse open issue list for label {label}: expected list payload"
                )

            page_issues = [
                issue
                for issue in payload
                if isinstance(issue, dict) and "number" in issue and "pull_request" not in issue
            ]
            issues.extend(page_issues)

            if len(payload) < per_page:
                return issues
            page += 1

    def update_issue_labels(
        self,
        issue_number: int,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        if add:
            self._issue_edit(issue_number, ["--add-label", ",".join(add)], "update labels")
        if remove:
            self._issue_edit(
                issue_number,
                ["--remove-label", ",".join(remove)],
                "update labels",
            )

    def create_issue_comment(self, issue_number: int, body: str) -> None:
        self._run_gh(
            ["issue", "comment", str(issue_number), "--body", body],
            action=f"create comment on issue #{issue_number}",
        )

    def list_issue_comments(self, issue_number: int, *, per_page: int = 100) -> list[dict[str, Any]]:
        page = 1
        comments: list[dict[str, Any]] = []

        while True:
            payload = self._api_json(
                ["repos/{repo}/issues/{issue_number}/comments", "--method", "GET"],
                action=f"list comments on issue #{issue_number}",
                fields={
                    "per_page": str(per_page),
                    "page": str(page),
                },
                issue_number=issue_number,
            )
            if not isinstance(payload, list):
                raise GitHubClientError(
                    f"failed to parse comments for issue #{issue_number}: expected list payload"
                )

            comments.extend(comment for comment in payload if isinstance(comment, dict))
            if len(payload) < per_page:
                return comments
            page += 1

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        self._api(
            ["repos/{repo}/issues/comments/{comment_id}", "--method", "PATCH"],
            action=f"update comment {comment_id}",
            fields={"body": body},
            comment_id=comment_id,
        )

    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        base: str,
        head: str,
        draft: bool = False,
    ) -> dict[str, Any]:
        args = [
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            base,
            "--head",
            head,
        ]
        if draft:
            args.append("--draft")
        self._run_gh(args, action=f"create PR from {head} into {base}")
        return self.get_pull_request(head)

    def update_pull_request(
        self,
        pr_number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        base: str | None = None,
    ) -> dict[str, Any]:
        args = ["pr", "edit", str(pr_number)]
        if title is not None:
            args.extend(["--title", title])
        if body is not None:
            args.extend(["--body", body])
        if base is not None:
            args.extend(["--base", base])
        self._run_gh(args, action=f"update PR #{pr_number}")
        return self.get_pull_request(str(pr_number))

    def get_pull_request(self, identifier: str) -> dict[str, Any]:
        payload = self._run_gh_json(
            ["pr", "view", identifier, "--json", "number,url,isDraft,headRefName,baseRefName"],
            action=f"load PR {identifier}",
        )
        if not isinstance(payload, dict):
            raise GitHubClientError(f"failed to parse PR {identifier}: expected object payload")
        return payload

    def list_pull_requests_by_head(self, head: str) -> list[dict[str, Any]]:
        payload = self._run_gh_json(
            [
                "pr",
                "list",
                "--head",
                head,
                "--state",
                "open",
                "--json",
                "number,url,isDraft,headRefName,baseRefName",
            ],
            action=f"list PRs for head {head}",
        )
        if not isinstance(payload, list):
            raise GitHubClientError(
                f"failed to parse PR list for head {head}: expected list payload"
            )
        return [pr for pr in payload if isinstance(pr, dict)]

    def get_ci_status(self, pr_number: int) -> list[dict[str, Any]]:
        payload = self._run_gh_json(
            [
                "pr",
                "checks",
                str(pr_number),
                "--json",
                "name,state,link,bucket",
            ],
            action=f"load CI status for PR #{pr_number}",
        )
        if not isinstance(payload, list):
            raise GitHubClientError(
                f"failed to parse CI status for PR #{pr_number}: expected list payload"
            )
        return payload

    def merge_pull_request(
        self,
        pr_number: int,
        *,
        merge_method: str = "squash",
        delete_branch: bool = False,
    ) -> None:
        args = ["pr", "merge", str(pr_number), f"--{merge_method}"]
        if delete_branch:
            args.append("--delete-branch")
        self._run_gh(args, action=f"merge PR #{pr_number}")

    def _issue_edit(self, issue_number: int, args: list[str], action: str) -> None:
        self._run_gh(
            ["issue", "edit", str(issue_number), *args],
            action=f"{action} for issue #{issue_number}",
        )

    def _api_json(
        self,
        args: list[str],
        *,
        action: str,
        fields: dict[str, str] | None = None,
        **template_values: str | int,
    ) -> Any:
        rendered = [part.format(repo=self.repo, **template_values) for part in args]
        return self._run_gh_json(["api", *rendered], action=action, fields=fields)

    def _api(
        self,
        args: list[str],
        *,
        action: str,
        fields: dict[str, str] | None = None,
        **template_values: str | int,
    ) -> subprocess.CompletedProcess[str]:
        rendered = [part.format(repo=self.repo, **template_values) for part in args]
        return self._run_gh(["api", *rendered], action=action, fields=fields)

    def _run_gh_json(
        self,
        args: list[str],
        *,
        action: str,
        fields: dict[str, str] | None = None,
    ) -> Any:
        result = self._run_gh(args, action=action, fields=fields)
        try:
            return json.loads(result.stdout or "null")
        except JSONDecodeError as error:
            raise GitHubClientError(f"failed to parse GitHub response while trying to {action}") from error

    def _run_gh(
        self,
        args: list[str],
        *,
        action: str,
        fields: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = ["gh", *args]
        if fields:
            for key, value in fields.items():
                command.extend(["-f", f"{key}={value}"])

        try:
            result = subprocess.run(
                command,
                cwd=self.root,
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError as error:
            raise GitHubClientError(f"failed to {action} with gh: {error}") from error

        if result.returncode != 0:
            stderr = result.stderr.strip()
            message = stderr or (result.stdout.strip() if result.stdout else "")
            if not message:
                message = f"gh exited with status {result.returncode}"
            raise GitHubClientError(f"failed to {action} with gh: {message}")

        return result

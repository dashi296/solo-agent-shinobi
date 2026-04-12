from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shinobi import cli
from shinobi.config import discover_repo_slug
from shinobi.context_builder import build_mission_context
from shinobi.executor import (
    collect_changed_paths,
    detect_high_risk_stop,
    execute_verification,
    find_high_risk_paths,
    run_verification_command,
)
from shinobi.github_client import GitHubClient, GitHubClientError
from shinobi.merger import MergeDecision, MergerError, evaluate_merge, merge_pull_request
from shinobi.mission_finalize import (
    MissionFinalizeError,
    finalize_mission,
    render_finalize_comment,
)
from shinobi.mission_publish import (
    MissionPublishError,
    build_same_repo_head_selector,
    find_mission_state_comment,
    parse_mission_state_fields,
    publish_mission,
)
from shinobi.mission_start import (
    MissionStartError,
    StartedMission,
    handoff_started_mission,
    start_mission,
)
from shinobi.models import (
    CIStatus,
    Config,
    DiffStats,
    ExecutionResult,
    MissionSummary,
    PullRequestCheck,
    ReviewDecision,
    StopDecision,
    State,
    VerificationCommandResult,
)
from shinobi.reviewer import (
    ReviewerError,
    collect_ci_status,
    collect_diff_stats,
    evaluate_review,
    parse_numstat,
    wait_for_ci,
)
from shinobi.state_store import StateStore


class FakeGitHubClient:
    def __init__(
        self,
        *,
        issue_number: int,
        title: str,
        labels: list[str],
        state: str = "OPEN",
    ) -> None:
        self.issue_number = issue_number
        self.issue_title = title
        self.issue_state = state
        self.issue_labels = set(labels)
        self.comments: list[dict[str, object]] = []
        self.comment_id = 100
        self.pull_requests: list[dict[str, object]] = []
        self.next_pr_number = 40
        self.closed_issues: list[int] = []
        self.label_operations: list[tuple[str, int, tuple[str, ...]]] = []
        self.raise_on_list_pull_requests: Exception | None = None

    def get_issue(self, issue_number: int) -> dict[str, object]:
        self._require_issue(issue_number)
        return {
            "number": self.issue_number,
            "title": self.issue_title,
            "state": self.issue_state,
            "labels": [{"name": label} for label in sorted(self.issue_labels)],
        }

    def update_issue_labels(
        self,
        issue_number: int,
        *,
        add: list[str] | None = None,
        remove: list[str] | None = None,
    ) -> None:
        self._require_issue(issue_number)
        if add:
            self.issue_labels.update(add)
            self.label_operations.append(("add", issue_number, tuple(add)))
        if remove:
            self.issue_labels.difference_update(remove)
            self.label_operations.append(("remove", issue_number, tuple(remove)))

    def create_issue_comment(self, issue_number: int, body: str) -> None:
        self._require_issue(issue_number)
        self.comment_id += 1
        self.comments.append({"id": self.comment_id, "body": body})

    def list_issue_comments(
        self,
        issue_number: int,
        *,
        per_page: int = 100,
    ) -> list[dict[str, object]]:
        self._require_issue(issue_number)
        return list(self.comments[:per_page])

    def update_issue_comment(self, comment_id: int, body: str) -> None:
        for comment in self.comments:
            if comment["id"] == comment_id:
                comment["body"] = body
                return
        raise GitHubClientError(f"comment {comment_id} not found")

    def list_pull_requests_by_head(self, head: str) -> list[dict[str, object]]:
        if self.raise_on_list_pull_requests is not None:
            raise self.raise_on_list_pull_requests
        return [pr for pr in self.pull_requests if pr["headRefName"] == head]

    def create_pull_request(
        self,
        *,
        title: str,
        body: str,
        base: str,
        head: str,
        draft: bool = False,
    ) -> dict[str, object]:
        pr = {
            "number": self.next_pr_number,
            "url": f"https://github.com/owner/repo/pull/{self.next_pr_number}",
            "isDraft": draft,
            "headRefName": head,
            "baseRefName": base,
            "title": title,
            "body": body,
        }
        self.next_pr_number += 1
        self.pull_requests.append(pr)
        return dict(pr)

    def update_pull_request(
        self,
        pr_number: int,
        *,
        title: str | None = None,
        body: str | None = None,
        base: str | None = None,
    ) -> dict[str, object]:
        pr = self._require_pull_request(pr_number)
        if title is not None:
            pr["title"] = title
        if body is not None:
            pr["body"] = body
        if base is not None:
            pr["baseRefName"] = base
        return dict(pr)

    def convert_pull_request_to_draft(self, pr_number: int) -> dict[str, object]:
        pr = self._require_pull_request(pr_number)
        pr["isDraft"] = True
        return dict(pr)

    def convert_pull_request_to_ready(self, pr_number: int) -> dict[str, object]:
        pr = self._require_pull_request(pr_number)
        pr["isDraft"] = False
        return dict(pr)

    def get_pull_request(self, identifier: str) -> dict[str, object]:
        pr_number = int(identifier)
        return dict(self._require_pull_request(pr_number))

    def close_issue(self, issue_number: int) -> None:
        self._require_issue(issue_number)
        self.issue_state = "CLOSED"
        self.closed_issues.append(issue_number)

    def _require_issue(self, issue_number: int) -> None:
        if issue_number != self.issue_number:
            raise GitHubClientError(f"issue #{issue_number} not found")

    def _require_pull_request(self, pr_number: int) -> dict[str, object]:
        for pr in self.pull_requests:
            if int(pr["number"]) == pr_number:
                return pr
        raise GitHubClientError(f"PR #{pr_number} not found")


class CliTest(unittest.TestCase):
    def test_init_creates_workspace_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output = io.StringIO()
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(output):
                        exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            store = StateStore(root)
            self.assertTrue(store.paths.config_path.exists())
            self.assertTrue(store.paths.state_path.exists())
            self.assertTrue(store.paths.summary_path.exists())
            self.assertTrue(store.paths.decisions_path.exists())
            self.assertTrue(store.paths.review_notes_path.exists())
            self.assertTrue(store.paths.self_review_template_path.exists())
            self.assertTrue(store.paths.review_note_rule_template_path.exists())
            self.assertTrue(store.paths.lock_path.exists())
            config = json.loads(store.paths.config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["verification_commands"]["lint"], [])
            self.assertEqual(
                config["verification_commands"]["typecheck"],
                [
                    "env",
                    "PYTHONPYCACHEPREFIX=/tmp/pycache",
                    "python3",
                    "-m",
                    "compileall",
                    "src",
                    "tests",
                ],
            )
            self.assertEqual(
                config["verification_commands"]["test"],
                ["python3", "-m", "unittest", "tests.test_cli"],
            )
            self.assertIn("Initialized Shinobi", output.getvalue())

    def test_init_does_not_overwrite_existing_workspace_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.templates_dir.mkdir()
            store.paths.review_notes_path.write_text("# custom notes\n", encoding="utf-8")
            store.paths.self_review_template_path.write_text("# custom self review\n", encoding="utf-8")
            store.paths.review_note_rule_template_path.write_text(
                "# custom review rule\n",
                encoding="utf-8",
            )

            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(
                store.paths.review_notes_path.read_text(encoding="utf-8"),
                "# custom notes\n",
            )
            self.assertEqual(
                store.paths.self_review_template_path.read_text(encoding="utf-8"),
                "# custom self review\n",
            )
            self.assertEqual(
                store.paths.review_note_rule_template_path.read_text(encoding="utf-8"),
                "# custom review rule\n",
            )

    def test_status_requires_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output = io.StringIO()
            with patch("pathlib.Path.cwd", return_value=root):
                with redirect_stdout(output):
                    exit_code = cli.main(["status"])

            self.assertEqual(exit_code, 1)
            self.assertIn("Run `shinobi init` first.", output.getvalue())

    def test_status_prints_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["status"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("repo: owner/repo", rendered)
            self.assertIn("github_status: no active or recent mission to reconcile", rendered)

    def test_status_reconciles_active_mission_with_github(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-status-github-reconciliation",
                            agent_identity=config.agent_identity,
                            run_id="run-123",
                            phase="publish",
                        )
                    )

                    client = Mock()
                    client.get_issue.return_value = {
                        "number": 33,
                        "state": "OPEN",
                        "labels": [
                            {"name": "priority:medium"},
                            {"name": "shinobi:reviewing"},
                        ],
                    }
                    client.get_pull_request.return_value = {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                        "isDraft": True,
                        "headRefName": "feature/issue-33-status-github-reconciliation",
                        "baseRefName": "main",
                    }

                    output = io.StringIO()
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with redirect_stdout(output):
                            exit_code = cli.main(["status"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("github_status_target: active", rendered)
            self.assertIn("github_issue_state: OPEN", rendered)
            self.assertIn("github_issue_labels: priority:medium, shinobi:reviewing", rendered)
            self.assertIn("github_pr_state: draft", rendered)
            self.assertIn("github_pr_head: feature/issue-33-status-github-reconciliation", rendered)

    def test_status_warns_on_github_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-status-github-reconciliation",
                            agent_identity=config.agent_identity,
                            run_id="run-123",
                            phase="publish",
                        )
                    )

                    client = Mock()
                    client.get_issue.return_value = {
                        "number": 33,
                        "state": "OPEN",
                        "labels": [{"name": "priority:medium"}],
                    }
                    client.get_pull_request.return_value = {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                        "isDraft": False,
                        "headRefName": "feature/unexpected-head",
                        "baseRefName": "develop",
                    }

                    output = io.StringIO()
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with redirect_stdout(output):
                            exit_code = cli.main(["status"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn(
                "warning: issue #33 is missing expected label shinobi:reviewing for phase publish",
                rendered,
            )
            self.assertIn(
                "warning: local branch feature/issue-33-status-github-reconciliation "
                "does not match GitHub PR head feature/unexpected-head",
                rendered,
            )
            self.assertIn(
                "warning: GitHub PR base branch develop does not match configured main branch main",
                rendered,
            )

    def test_status_uses_last_mission_when_idle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            agent_identity=config.agent_identity,
                            phase="idle",
                            last_mission=MissionSummary(
                                issue_number=31,
                                pr_number=44,
                                branch="feature/issue-31-publish-phase",
                                phase="publish",
                                conclusion="needs-human",
                            ),
                        )
                    )

                    client = Mock()
                    client.get_issue.return_value = {
                        "number": 31,
                        "state": "OPEN",
                        "labels": [{"name": "shinobi:needs-human"}],
                    }
                    client.get_pull_request.return_value = {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                        "isDraft": False,
                        "headRefName": "feature/issue-31-publish-phase",
                        "baseRefName": "main",
                    }

                    output = io.StringIO()
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with redirect_stdout(output):
                            exit_code = cli.main(["status"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("last_mission_issue_number: 31", rendered)
            self.assertIn("last_mission_conclusion: needs-human", rendered)
            self.assertIn("github_status_target: last_mission", rendered)
            self.assertIn("github_pr_number: 44", rendered)

    def test_status_warns_when_github_reconciliation_fails_but_keeps_local_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-status-github-reconciliation",
                            agent_identity=config.agent_identity,
                            run_id="run-123",
                            phase="publish",
                        )
                    )

                    client = Mock()
                    client.get_issue.side_effect = GitHubClientError("boom issue")
                    client.get_pull_request.side_effect = GitHubClientError("boom pr")

                    output = io.StringIO()
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with redirect_stdout(output):
                            exit_code = cli.main(["status"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("phase: publish", rendered)
            self.assertIn("warning: failed to load issue #33: boom issue", rendered)
            self.assertIn("warning: failed to load PR #44: boom pr", rendered)

    def test_status_does_not_recreate_missing_support_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    store.paths.summary_path.unlink()
                    store.paths.decisions_path.unlink()
                    store.paths.lock_path.unlink()

                    with redirect_stdout(io.StringIO()):
                        exit_code = cli.main(["status"])

            self.assertEqual(exit_code, 0)
            self.assertFalse(store.paths.summary_path.exists())
            self.assertFalse(store.paths.decisions_path.exists())
            self.assertFalse(store.paths.lock_path.exists())

    def test_review_waits_for_ci_merges_and_finalizes_when_auto_merge_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="publish",
                            last_result="published",
                        )
                    )

                    client = Mock()
                    output = io.StringIO()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: publish\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    client.get_issue.return_value = {
                        "number": 33,
                        "state": "OPEN",
                        "labels": [{"name": config.labels["reviewing"]}],
                    }
                    client.get_pull_request.return_value = {
                        "number": 44,
                        "isDraft": True,
                    }
                    client.convert_pull_request_to_ready.return_value = {
                        "number": 44,
                        "isDraft": False,
                    }
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch("shinobi.mission_finalize.GitHubClient", return_value=client):
                            with patch(
                                "shinobi.cli.collect_diff_stats",
                                return_value=DiffStats(
                                    changed_files=2,
                                    added_lines=10,
                                    deleted_lines=3,
                                ),
                            ):
                                with patch(
                                    "shinobi.cli.wait_for_ci",
                                    return_value=CIStatus(
                                        checks=[
                                            PullRequestCheck(
                                                name="test",
                                                state="SUCCESS",
                                                bucket="pass",
                                            )
                                        ],
                                        status="success",
                                    ),
                                ) as wait_for_ci_mock:
                                    with patch(
                                        "shinobi.cli.load_current_branch",
                                        return_value="feature/issue-33-review-ci",
                                    ):
                                        with redirect_stdout(output):
                                            exit_code = cli.main(
                                                [
                                                    "review",
                                                    "--timeout-seconds",
                                                    "30",
                                                    "--poll-interval-seconds",
                                                    "5",
                                                ]
                                            )

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("review_issue: #33", rendered)
            self.assertIn("review_pr: #44", rendered)
            self.assertIn("ci_status: success", rendered)
            self.assertIn("ci_checks: test=pass", rendered)
            self.assertIn("merge_result: merged (squash)", rendered)
            wait_for_ci_mock.assert_called_once()
            self.assertIs(wait_for_ci_mock.call_args.args[0], client)
            self.assertEqual(wait_for_ci_mock.call_args.args[1], 44)
            self.assertEqual(wait_for_ci_mock.call_args.kwargs["timeout_seconds"], 30.0)
            self.assertEqual(wait_for_ci_mock.call_args.kwargs["poll_interval_seconds"], 5.0)
            self.assertIsNotNone(wait_for_ci_mock.call_args.kwargs["heartbeat"])
            self.assertEqual(client.update_issue_comment.call_count, 2)
            self.assertIn("phase: review", client.update_issue_comment.call_args_list[0].args[1])
            self.assertIn("pr: 44", client.update_issue_comment.call_args_list[0].args[1])
            client.get_pull_request.assert_called_once_with("44")
            client.convert_pull_request_to_ready.assert_called_once_with(44)
            client.merge_pull_request.assert_called_once_with(
                44,
                merge_method=config.merge_method,
                delete_branch=True,
            )
            client.update_issue_labels.assert_any_call(33, add=[config.labels["merged"]])
            client.update_issue_labels.assert_any_call(33, remove=[config.labels["reviewing"]])
            client.close_issue.assert_called_once_with(33)

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "merged")
            self.assertEqual(saved_state.last_mission.issue_number, 33)
            self.assertEqual(saved_state.last_mission.pr_number, 44)
            self.assertEqual(saved_state.last_mission.conclusion, "merged")
            self.assertIsNone(store.load_lock())

    def test_review_successful_ci_finalizes_needs_human_when_merge_is_ineligible(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="review",
                            last_result="review-retry",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: review\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    client.get_issue.return_value = {
                        "number": 33,
                        "state": "OPEN",
                        "labels": [
                            {"name": config.labels["reviewing"]},
                            {"name": config.labels["risky"]},
                        ],
                    }
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch("shinobi.mission_finalize.GitHubClient", return_value=client):
                            with patch(
                                "shinobi.cli.collect_diff_stats",
                                return_value=DiffStats(
                                    changed_files=2,
                                    added_lines=10,
                                    deleted_lines=3,
                                ),
                            ):
                                with patch(
                                    "shinobi.cli.wait_for_ci",
                                    return_value=CIStatus(
                                        checks=[
                                            PullRequestCheck(
                                                name="test",
                                                state="SUCCESS",
                                                bucket="pass",
                                            )
                                        ],
                                        status="success",
                                    ),
                                ):
                                    with patch(
                                        "shinobi.cli.load_current_branch",
                                        return_value="feature/issue-33-review-ci",
                                    ):
                                        with redirect_stdout(output):
                                            exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            rendered = output.getvalue()
            self.assertIn("merge_result: needs-human", rendered)
            self.assertIn("has label shinobi:risky", rendered)
            client.merge_pull_request.assert_not_called()
            client.update_issue_labels.assert_any_call(33, add=[config.labels["needs_human"]])
            client.update_issue_labels.assert_any_call(33, remove=[config.labels["reviewing"]])

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "needs-human")
            self.assertEqual(saved_state.last_mission.issue_number, 33)
            self.assertEqual(saved_state.last_mission.conclusion, "needs-human")
            self.assertIsNone(store.load_lock())

    def test_review_successful_ci_preserves_blocked_handoff_when_issue_is_human_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="review",
                            last_result="review-retry",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: review\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    client.get_issue.return_value = {
                        "number": 33,
                        "state": "OPEN",
                        "labels": [
                            {"name": config.labels["reviewing"]},
                            {"name": config.labels["blocked"]},
                        ],
                    }
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch("shinobi.mission_finalize.GitHubClient", return_value=client):
                            with patch(
                                "shinobi.cli.collect_diff_stats",
                                return_value=DiffStats(
                                    changed_files=2,
                                    added_lines=10,
                                    deleted_lines=3,
                                ),
                            ):
                                with patch(
                                    "shinobi.cli.wait_for_ci",
                                    return_value=CIStatus(
                                        checks=[
                                            PullRequestCheck(
                                                name="test",
                                                state="SUCCESS",
                                                bucket="pass",
                                            )
                                        ],
                                        status="success",
                                    ),
                                ):
                                    with patch(
                                        "shinobi.cli.load_current_branch",
                                        return_value="feature/issue-33-review-ci",
                                    ):
                                        with redirect_stdout(output):
                                            exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            rendered = output.getvalue()
            self.assertIn("merge_result: blocked", rendered)
            self.assertIn(f"has blocking label(s): {config.labels['blocked']}", rendered)
            client.merge_pull_request.assert_not_called()
            client.update_issue_labels.assert_any_call(33, add=[config.labels["blocked"]])
            client.update_issue_labels.assert_any_call(33, remove=[config.labels["reviewing"]])

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "blocked")
            self.assertEqual(saved_state.last_mission.issue_number, 33)
            self.assertEqual(saved_state.last_mission.conclusion, "blocked")
            self.assertIsNone(store.load_lock())

    def test_review_successful_ci_finalizes_needs_human_when_merge_command_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="review",
                            last_result="review-retry",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: review\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    client.get_issue.return_value = {
                        "number": 33,
                        "state": "OPEN",
                        "labels": [{"name": config.labels["reviewing"]}],
                    }
                    client.merge_pull_request.side_effect = GitHubClientError(
                        "merge blocked by branch protection"
                    )
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch("shinobi.mission_finalize.GitHubClient", return_value=client):
                            with patch(
                                "shinobi.cli.collect_diff_stats",
                                return_value=DiffStats(
                                    changed_files=2,
                                    added_lines=10,
                                    deleted_lines=3,
                                ),
                            ):
                                with patch(
                                    "shinobi.cli.wait_for_ci",
                                    return_value=CIStatus(
                                        checks=[
                                            PullRequestCheck(
                                                name="test",
                                                state="SUCCESS",
                                                bucket="pass",
                                            )
                                        ],
                                        status="success",
                                    ),
                                ):
                                    with patch(
                                        "shinobi.cli.load_current_branch",
                                        return_value="feature/issue-33-review-ci",
                                    ):
                                        with redirect_stdout(output):
                                            exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            rendered = output.getvalue()
            self.assertIn("merge_result: needs-human", rendered)
            self.assertIn("failed to merge PR #44", rendered)
            self.assertIn("branch protection", rendered)
            client.update_issue_labels.assert_any_call(33, add=[config.labels["needs_human"]])
            client.update_issue_labels.assert_any_call(33, remove=[config.labels["reviewing"]])
            client.close_issue.assert_not_called()

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "needs-human")
            self.assertEqual(saved_state.last_mission.issue_number, 33)
            self.assertEqual(saved_state.last_mission.pr_number, 44)
            self.assertEqual(saved_state.last_mission.conclusion, "needs-human")
            self.assertIsNone(store.load_lock())

    def test_review_persists_merged_state_when_finalize_fails_after_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="review",
                            last_result="review-retry",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: review\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    client.get_issue.return_value = {
                        "number": 33,
                        "state": "OPEN",
                        "labels": [{"name": config.labels["reviewing"]}],
                    }
                    client.get_pull_request.return_value = {
                        "number": 44,
                        "isDraft": False,
                    }
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch(
                            "shinobi.cli.collect_diff_stats",
                            return_value=DiffStats(
                                changed_files=2,
                                added_lines=10,
                                deleted_lines=3,
                            ),
                        ):
                            with patch(
                                "shinobi.cli.wait_for_ci",
                                return_value=CIStatus(
                                    checks=[
                                        PullRequestCheck(
                                            name="test",
                                            state="SUCCESS",
                                            bucket="pass",
                                        )
                                    ],
                                    status="success",
                                ),
                            ):
                                with patch(
                                    "shinobi.cli.load_current_branch",
                                    return_value="feature/issue-33-review-ci",
                                ):
                                    with patch(
                                        "shinobi.cli.finalize_mission",
                                        side_effect=MissionFinalizeError(
                                            "failed to create finalize comment on issue #33: api unavailable"
                                        ),
                                    ):
                                        with redirect_stdout(output):
                                            exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            rendered = output.getvalue()
            self.assertIn("merge_result: merged (squash)", rendered)
            self.assertIn("merge_warning: merged PR but finalize follow-up failed", rendered)
            client.merge_pull_request.assert_called_once_with(
                44,
                merge_method=config.merge_method,
                delete_branch=True,
            )

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "merged")
            self.assertIn("merged PR but finalize follow-up failed", saved_state.last_error)
            self.assertEqual(saved_state.last_mission.issue_number, 33)
            self.assertEqual(saved_state.last_mission.pr_number, 44)
            self.assertEqual(saved_state.last_mission.conclusion, "merged")
            self.assertIsNone(store.load_lock())

    def test_review_timeout_returns_nonzero_and_persists_pending_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="publish",
                            last_result="published",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: publish\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch(
                            "shinobi.cli.wait_for_ci",
                            return_value=CIStatus(
                                checks=[
                                    PullRequestCheck(
                                        name="test",
                                        state="QUEUED",
                                        bucket="pending",
                                    )
                                ],
                                status="pending",
                                timed_out=True,
                            ),
                        ):
                            with patch(
                                "shinobi.cli.load_current_branch",
                                return_value="feature/issue-33-review-ci",
                            ):
                                with redirect_stdout(output):
                                    exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            rendered = output.getvalue()
            self.assertIn("ci_status: pending", rendered)
            self.assertIn("ci_timed_out: True", rendered)

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "review")
            self.assertEqual(saved_state.run_id, "publish-run")
            self.assertEqual(saved_state.last_result, "ci-timeout")
            self.assertEqual(saved_state.last_error, "CI polling timed out before checks completed")
            self.assertTrue(saved_state.extra["ci_status"]["timed_out"])
            self.assertIsNone(store.load_lock())

    def test_review_heartbeat_updates_mission_state_comment_during_polling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="publish",
                            last_result="published",
                        )
                    )

                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: publish\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    client.get_issue.return_value = {
                        "number": 33,
                        "state": "OPEN",
                        "labels": [{"name": config.labels["reviewing"]}],
                    }

                    def wait_for_ci_side_effect(*_args, **kwargs):
                        kwargs["heartbeat"]()
                        return CIStatus(
                            checks=[
                                PullRequestCheck(
                                    name="test",
                                    state="SUCCESS",
                                    bucket="pass",
                                )
                            ],
                            status="success",
                        )

                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch(
                            "shinobi.mission_finalize.GitHubClient",
                            return_value=client,
                        ):
                            with patch(
                                "shinobi.cli.collect_diff_stats",
                                return_value=DiffStats(
                                    changed_files=2,
                                    added_lines=10,
                                    deleted_lines=3,
                                ),
                            ):
                                with patch(
                                    "shinobi.cli.wait_for_ci",
                                    side_effect=wait_for_ci_side_effect,
                                ):
                                    with patch(
                                        "shinobi.cli.load_current_branch",
                                        return_value="feature/issue-33-review-ci",
                                    ):
                                        with redirect_stdout(io.StringIO()):
                                            exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 0)
            self.assertEqual(client.update_issue_comment.call_count, 3)
            for call in client.update_issue_comment.call_args_list:
                self.assertIn("phase: review", call.args[1])
                self.assertIn("pr: 44", call.args[1])
            self.assertIsNone(store.load_lock())

    def test_review_failed_ci_retries_once_and_persists_retry_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="publish",
                            last_result="published",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: publish\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    execution_result = ExecutionResult(
                        commands=[
                            VerificationCommandResult(
                                name="test",
                                command=["python3", "-m", "unittest"],
                                status="passed",
                                returncode=0,
                            )
                        ],
                        change_summary="retry verification passed",
                    )
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch(
                            "shinobi.cli.wait_for_ci",
                            return_value=CIStatus(
                                checks=[
                                    PullRequestCheck(
                                        name="test",
                                        state="FAILURE",
                                        bucket="fail",
                                        link="https://github.com/owner/repo/actions/runs/123456789/job/987",
                                    )
                                ],
                                status="failure",
                            ),
                        ):
                            with patch(
                                "shinobi.cli.execute_verification",
                                return_value=execution_result,
                            ) as execute_verification_mock:
                                with patch(
                                    "shinobi.cli.load_current_branch",
                                    return_value="feature/issue-33-review-ci",
                                ):
                                    with redirect_stdout(output):
                                        exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            rendered = output.getvalue()
            self.assertIn("ci_status: failure", rendered)
            self.assertIn("review_loop_count: 1", rendered)
            self.assertIn("review_result: retry", rendered)
            self.assertIn("next_phase: review", rendered)
            execute_verification_mock.assert_called_once()
            client.rerun_workflow_run.assert_called_once_with("123456789", failed_only=True)

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "review")
            self.assertEqual(saved_state.run_id, "publish-run")
            self.assertEqual(saved_state.review_loop_count, 1)
            self.assertEqual(saved_state.last_result, "review-retry")
            self.assertEqual(
                saved_state.last_error,
                "CI failed; local verification passed and failed GitHub Actions workflow "
                "run(s) were rerun: 123456789.",
            )
            self.assertEqual(saved_state.extra["ci_status"]["status"], "failure")
            self.assertFalse(saved_state.extra["ci_status"]["timed_out"])
            self.assertEqual(saved_state.extra["retry_verification"]["commands"][0]["status"], "passed")
            self.assertEqual(saved_state.extra["retry_workflow_run_ids"], ["123456789"])
            self.assertIsNone(store.load_lock())

    def test_review_failed_ci_finalizes_needs_human_when_loop_limit_reached(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="review",
                            review_loop_count=3,
                            last_result="review-retry",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: review\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    client.get_issue.return_value = {
                        "number": 33,
                        "state": "OPEN",
                        "labels": [{"name": config.labels["reviewing"]}],
                    }
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch("shinobi.mission_finalize.GitHubClient", return_value=client):
                            with patch(
                                "shinobi.cli.wait_for_ci",
                                return_value=CIStatus(
                                    checks=[
                                        PullRequestCheck(
                                            name="test",
                                            state="FAILURE",
                                            bucket="fail",
                                        )
                                    ],
                                    status="failure",
                                ),
                            ):
                                with patch(
                                    "shinobi.cli.load_current_branch",
                                    return_value="feature/issue-33-review-ci",
                                ):
                                    with redirect_stdout(output):
                                        exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            rendered = output.getvalue()
            self.assertIn("review_result: needs-human", rendered)
            self.assertIn("max_review_loops 3", rendered)
            client.update_issue_labels.assert_any_call(
                33,
                add=[config.labels["needs_human"]],
            )
            client.update_issue_labels.assert_any_call(
                33,
                remove=[config.labels["reviewing"]],
            )
            self.assertEqual(client.create_issue_comment.call_count, 1)

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "needs-human")
            self.assertEqual(saved_state.last_mission.issue_number, 33)
            self.assertEqual(saved_state.last_mission.conclusion, "needs-human")
            self.assertIsNone(store.load_lock())

    def test_failed_actions_run_ids_extracts_unique_failed_action_runs(self) -> None:
        status = CIStatus(
            checks=[
                PullRequestCheck(
                    name="test",
                    state="FAILURE",
                    bucket="fail",
                    link="https://github.com/owner/repo/actions/runs/123456789/job/987",
                ),
                PullRequestCheck(
                    name="lint",
                    state="FAILURE",
                    bucket="fail",
                    link="https://github.com/owner/repo/actions/runs/123456789",
                ),
                PullRequestCheck(
                    name="docs",
                    state="SUCCESS",
                    bucket="pass",
                    link="https://github.com/owner/repo/actions/runs/222",
                ),
                PullRequestCheck(
                    name="external",
                    state="FAILURE",
                    bucket="fail",
                    link="https://ci.example.test/check/333",
                ),
                PullRequestCheck(
                    name="same-path-external-host",
                    state="FAILURE",
                    bucket="fail",
                    link="https://ci.example.test/owner/repo/actions/runs/333",
                ),
                PullRequestCheck(
                    name="other-repo",
                    state="FAILURE",
                    bucket="fail",
                    link="https://github.com/other/repo/actions/runs/444",
                ),
                PullRequestCheck(
                    name="malformed-run-id",
                    state="FAILURE",
                    bucket="fail",
                    link="https://github.com/owner/repo/actions/runs/555abc",
                ),
            ],
            status="failure",
        )

        self.assertEqual(
            cli.failed_actions_run_ids(status, repo="owner/repo"),
            ["123456789"],
        )

    def test_actions_run_retries_reruns_canceled_actions_runs_entirely(self) -> None:
        status = CIStatus(
            checks=[
                PullRequestCheck(
                    name="cancelled",
                    state="CANCELLED",
                    bucket="cancel",
                    link="https://github.com/owner/repo/actions/runs/123456789",
                ),
                PullRequestCheck(
                    name="failed",
                    state="FAILURE",
                    bucket="fail",
                    link="https://github.com/owner/repo/actions/runs/123456789",
                ),
                PullRequestCheck(
                    name="other-failed",
                    state="FAILURE",
                    bucket="fail",
                    link="https://github.com/owner/repo/actions/runs/222",
                ),
            ],
            status="failure",
        )

        retries = cli.actions_run_retries(status, repo="owner/repo")

        self.assertEqual(
            retries,
            [
                cli.ActionsRunRetry(run_id="123456789", failed_only=False),
                cli.ActionsRunRetry(run_id="222", failed_only=True),
            ],
        )

    def test_review_failed_ci_finalizes_needs_human_when_retry_verification_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="review",
                            review_loop_count=1,
                            last_result="review-retry",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: review\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    client.get_issue.return_value = {
                        "number": 33,
                        "state": "OPEN",
                        "labels": [{"name": config.labels["reviewing"]}],
                    }
                    execution_result = ExecutionResult(
                        commands=[
                            VerificationCommandResult(
                                name="test",
                                command=["python3", "-m", "unittest"],
                                status="failed",
                                returncode=1,
                            )
                        ],
                        change_summary="retry verification failed",
                    )
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch(
                            "shinobi.mission_finalize.GitHubClient",
                            return_value=client,
                        ):
                            with patch(
                                "shinobi.cli.wait_for_ci",
                                return_value=CIStatus(
                                    checks=[
                                        PullRequestCheck(
                                            name="test",
                                            state="FAILURE",
                                            bucket="fail",
                                        )
                                    ],
                                    status="failure",
                                ),
                            ):
                                with patch(
                                    "shinobi.cli.execute_verification",
                                    return_value=execution_result,
                                ) as execute_verification_mock:
                                    with patch(
                                        "shinobi.cli.load_current_branch",
                                        return_value="feature/issue-33-review-ci",
                                    ):
                                        with redirect_stdout(output):
                                            exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            rendered = output.getvalue()
            self.assertIn("review_result: needs-human", rendered)
            self.assertIn("local verification failed", rendered)
            execute_verification_mock.assert_called_once()
            client.rerun_workflow_run.assert_not_called()
            client.update_issue_labels.assert_any_call(
                33,
                add=[config.labels["needs_human"]],
            )
            client.update_issue_labels.assert_any_call(
                33,
                remove=[config.labels["reviewing"]],
            )
            self.assertEqual(client.create_issue_comment.call_count, 1)

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "needs-human")
            self.assertEqual(saved_state.last_mission.issue_number, 33)
            self.assertEqual(saved_state.last_mission.conclusion, "needs-human")
            self.assertIsNone(store.load_lock())

    def test_review_persists_last_error_when_ci_polling_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="publish",
                            last_result="published",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: publish\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch(
                            "shinobi.cli.wait_for_ci",
                            side_effect=ReviewerError("api unavailable"),
                        ):
                            with patch(
                                "shinobi.cli.load_current_branch",
                                return_value="feature/issue-33-review-ci",
                            ):
                                with redirect_stdout(output):
                                    exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            self.assertIn("review aborted: api unavailable", output.getvalue())

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "review")
            self.assertEqual(saved_state.run_id, "publish-run")
            self.assertEqual(saved_state.last_result, "review-error")
            self.assertEqual(saved_state.last_error, "api unavailable")
            self.assertIsNone(store.load_lock())

    def test_review_persists_last_error_when_completion_comment_update_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="publish",
                            last_result="published",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 33\n"
                                "branch: feature/issue-33-review-ci\n"
                                "phase: publish\n"
                                "pr: 44\n"
                                "-->\n"
                            ),
                        }
                    ]
                    client.update_issue_comment.side_effect = [
                        None,
                        GitHubClientError("failed to write review heartbeat comment"),
                    ]
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch(
                            "shinobi.cli.wait_for_ci",
                            return_value=CIStatus(
                                checks=[
                                    PullRequestCheck(
                                        name="test",
                                        state="SUCCESS",
                                        bucket="pass",
                                    )
                                ],
                                status="success",
                            ),
                        ):
                            with patch(
                                "shinobi.cli.load_current_branch",
                                return_value="feature/issue-33-review-ci",
                            ):
                                with redirect_stdout(output):
                                    exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "review aborted: failed to upsert review mission-state comment for issue #33: "
                "failed to write review heartbeat comment",
                output.getvalue(),
            )

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "review")
            self.assertEqual(saved_state.run_id, "publish-run")
            self.assertEqual(saved_state.last_result, "review-error")
            self.assertEqual(
                saved_state.last_error,
                "failed to upsert review mission-state comment for issue #33: "
                "failed to write review heartbeat comment",
            )
            self.assertIsNone(store.load_lock())

    def test_review_aborts_when_active_mission_lacks_run_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id=None,
                            phase="publish",
                        )
                    )

                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "review aborted: local mission state requires issue_number, pr_number, branch, "
                "and run_id",
                output.getvalue(),
            )

    def test_review_aborts_when_active_mission_belongs_to_different_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity="other-agent",
                            run_id="publish-run",
                            phase="publish",
                        )
                    )

                    client = Mock()
                    output = io.StringIO()
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with redirect_stdout(output):
                            exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "review aborted: local mission state belongs to a different agent (other-agent)",
                output.getvalue(),
            )
            client.assert_not_called()
            self.assertIsNone(store.load_lock())

    def test_review_aborts_when_current_branch_does_not_match_mission_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    store.save_state(
                        State(
                            issue_number=33,
                            pr_number=44,
                            branch="feature/issue-33-review-ci",
                            agent_identity=config.agent_identity,
                            run_id="publish-run",
                            phase="publish",
                            last_result="published",
                        )
                    )

                    output = io.StringIO()
                    client = Mock()
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with patch("shinobi.cli.load_current_branch", return_value="main"):
                            with redirect_stdout(output):
                                exit_code = cli.main(["review"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "review aborted: current git branch main does not match mission branch "
                "feature/issue-33-review-ci",
                output.getvalue(),
            )
            client.assert_not_called()
            self.assertEqual(store.load_state().phase, "publish")
            self.assertEqual(store.load_state().last_result, "published")
            self.assertIsNone(store.load_lock())

    def test_review_rejects_invalid_poll_interval_before_side_effects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config, config_error = store.try_load_config()
                    self.assertIsNotNone(config, config_error)
                    original_state = State(
                        issue_number=33,
                        pr_number=44,
                        branch="feature/issue-33-review-ci",
                        agent_identity=config.agent_identity,
                        run_id="publish-run",
                        phase="publish",
                        last_result="published",
                    )
                    store.save_state(original_state)

                    output = io.StringIO()
                    client = Mock()
                    with patch("shinobi.cli.GitHubClient", return_value=client):
                        with redirect_stdout(output):
                            exit_code = cli.command_review(
                                root,
                                timeout_seconds=30,
                                poll_interval_seconds=0,
                            )

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "review aborted: poll_interval_seconds must be positive",
                output.getvalue(),
            )
            self.assertEqual(store.load_state(), original_state)
            client.assert_not_called()
            self.assertIsNone(store.load_lock())

    def test_init_preserves_state_agent_identity_when_config_is_recreated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    original_state = store.load_state()
                    store.paths.config_path.unlink()

                    with redirect_stdout(io.StringIO()):
                        exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            repaired_config = json.loads(store.paths.config_path.read_text(encoding="utf-8"))
            repaired_state = store.load_state()
            self.assertEqual(original_state.agent_identity, repaired_config["agent_identity"])
            self.assertEqual(repaired_state.agent_identity, repaired_config["agent_identity"])

    def test_init_repairs_invalid_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    config = json.loads(store.paths.config_path.read_text(encoding="utf-8"))
                    store.paths.state_path.write_text("{broken", encoding="utf-8")

                    with redirect_stdout(io.StringIO()):
                        exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            repaired_state = store.load_state()
            self.assertEqual(repaired_state.agent_identity, config["agent_identity"])
            self.assertEqual(repaired_state.phase, "idle")

    def test_init_repairs_invalid_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    original_config = json.loads(store.paths.config_path.read_text(encoding="utf-8"))
                    store.paths.config_path.write_text("{broken", encoding="utf-8")

                    with redirect_stdout(io.StringIO()):
                        exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            repaired_config = json.loads(store.paths.config_path.read_text(encoding="utf-8"))
            repaired_state = store.load_state()
            self.assertEqual(repaired_config["agent_identity"], original_config["agent_identity"])
            self.assertEqual(repaired_state.agent_identity, repaired_config["agent_identity"])

    def test_init_repairs_blank_config_agent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.config_path.write_text(
                json.dumps({"repo": "owner/repo", "agent_identity": ""}), encoding="utf-8"
            )

            with patch("pathlib.Path.cwd", return_value=root):
                with redirect_stdout(io.StringIO()):
                    exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            repaired_config = json.loads(store.paths.config_path.read_text(encoding="utf-8"))
            repaired_state = store.load_state()
            self.assertTrue(repaired_config["agent_identity"])
            self.assertEqual(repaired_state.agent_identity, repaired_config["agent_identity"])

    def test_init_repairs_blank_state_agent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.state_path.write_text(
                json.dumps({"agent_identity": "", "phase": "idle"}), encoding="utf-8"
            )

            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            repaired_config = json.loads(store.paths.config_path.read_text(encoding="utf-8"))
            repaired_state = store.load_state()
            self.assertTrue(repaired_state.agent_identity)
            self.assertEqual(repaired_state.agent_identity, repaired_config["agent_identity"])

    def test_init_repairs_conflicting_state_agent_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.config_path.write_text(
                json.dumps({"repo": "owner/repo", "agent_identity": "config-id"}),
                encoding="utf-8",
            )
            store.paths.state_path.write_text(
                json.dumps({"agent_identity": "state-id", "phase": "idle"}),
                encoding="utf-8",
            )

            with patch("pathlib.Path.cwd", return_value=root):
                with redirect_stdout(io.StringIO()):
                    exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            repaired_state = store.load_state()
            self.assertEqual(repaired_state.agent_identity, "config-id")

    def test_status_warns_when_state_file_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    store.paths.state_path.write_text("{broken", encoding="utf-8")

                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["status"])

            self.assertEqual(exit_code, 1)
            rendered = output.getvalue()
            self.assertIn("Shinobi status", rendered)
            self.assertIn("warning: failed to load local state:", rendered)
            self.assertIn("repo: owner/repo", rendered)

    def test_status_warns_when_config_file_is_missing_but_state_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    store.paths.config_path.unlink()

                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["status"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("Shinobi status", rendered)
            self.assertIn("repo: unavailable", rendered)
            self.assertIn("warning: failed to load config:", rendered)
            self.assertIn("phase: idle", rendered)

    def test_status_warns_when_agent_identity_files_diverge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.config_path.write_text(
                json.dumps({"repo": "owner/repo", "agent_identity": "config-id"}),
                encoding="utf-8",
            )
            store.paths.state_path.write_text(
                json.dumps({"agent_identity": "state-id", "phase": "idle"}),
                encoding="utf-8",
            )

            output = io.StringIO()
            with patch("pathlib.Path.cwd", return_value=root):
                with redirect_stdout(output):
                    exit_code = cli.main(["status"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn(
                "warning: local state agent_identity does not match config;",
                rendered,
            )
            self.assertIn("run `shinobi init` to repair it", rendered)

    def test_run_requires_init(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            output = io.StringIO()

            with patch("pathlib.Path.cwd", return_value=root):
                with redirect_stdout(output):
                    exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn("Run `shinobi init` first.", output.getvalue())

    def test_run_reports_platform_without_run_lock_support(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    output = io.StringIO()
                    with patch("shinobi.state_store.fcntl", None):
                        with redirect_stdout(output):
                            exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: run locking is not supported on this platform",
                output.getvalue(),
            )

    def test_run_refuses_live_lock_owned_by_another_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    now = datetime.now(timezone.utc).replace(microsecond=0)
                    store.paths.lock_path.write_text(
                        json.dumps(
                            {
                                "agent_identity": "owner/repo#default@otherhost-11111111",
                                "run_id": "live-run",
                                "pid": 123,
                                "started_at": now.isoformat().replace("+00:00", "Z"),
                                "heartbeat_at": now.isoformat().replace("+00:00", "Z"),
                            }
                        ),
                        encoding="utf-8",
                    )

                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn("run aborted: run lock is held by live run live-run", output.getvalue())
            self.assertIn("live-run", store.paths.lock_path.read_text(encoding="utf-8"))

    def test_run_takes_over_stale_lock_and_selects_ready_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    stale_time = (
                        datetime.now(timezone.utc) - timedelta(days=1)
                    ).replace(microsecond=0)
                    store.paths.lock_path.write_text(
                        json.dumps(
                            {
                                "agent_identity": "owner/repo#default@otherhost-11111111",
                                "run_id": "stale-run",
                                "pid": 123,
                                "started_at": stale_time.isoformat().replace("+00:00", "Z"),
                                "heartbeat_at": stale_time.isoformat().replace("+00:00", "Z"),
                            }
                        ),
                        encoding="utf-8",
                    )

                    output = io.StringIO()
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[]):
                        with patch("shinobi.cli.select_ready_issue", return_value=6):
                            with patch(
                                "shinobi.cli.load_issue",
                                return_value={"number": 6, "title": "Run start phase"},
                            ):
                                with patch(
                                    "shinobi.cli.start_mission",
                                    return_value=Mock(
                                        branch="feature/issue-6-run-start-phase",
                                        issue_number=6,
                                        lease_expires_at="2026-04-09T00:30:00Z",
                                    ),
                                ):
                                    execution_result = Mock()
                                    execution_result.succeeded = True
                                    execution_result.commands = []
                                    with patch(
                                        "shinobi.cli.execute_verification",
                                        return_value=execution_result,
                                    ):
                                        with patch(
                                            "shinobi.cli.load_publishable_issue_label_names",
                                            return_value={"shinobi:working"},
                                        ):
                                            with patch(
                                                "shinobi.cli.detect_high_risk_stop",
                                                return_value=None,
                                            ):
                                                with patch(
                                                    "shinobi.cli.publish_mission",
                                                    return_value=Mock(
                                                        pr_number=31,
                                                        pr_url="https://github.com/owner/repo/pull/31",
                                                        lease_expires_at="2026-04-09T00:30:00Z",
                                                    ),
                                                ) as publish_mock:
                                                    with redirect_stdout(output):
                                                        exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("run lock: took over stale lock during select phase", rendered)
            self.assertIn("selected_issue: 6", rendered)
            self.assertIn("started_branch: feature/issue-6-run-start-phase", rendered)
            self.assertIn("published_pr: #31", rendered)
            self.assertIn("next_phase: review", rendered)
            self.assertNotIn("now", publish_mock.call_args.kwargs)
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_hands_off_when_verification_fails_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    output = io.StringIO()
                    started_mission = Mock(
                        branch="feature/issue-6-run-start-phase",
                        issue_number=6,
                        lease_expires_at="2026-04-09T00:30:00Z",
                    )
                    execution_result = ExecutionResult(
                        commands=[
                            VerificationCommandResult(
                                name="test",
                                command=["python3", "-m", "unittest"],
                                status="failed",
                                returncode=1,
                            )
                        ],
                        change_summary="No automated code changes are performed.",
                    )
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[]):
                        with patch("shinobi.cli.select_ready_issue", return_value=6):
                            with patch(
                                "shinobi.cli.load_issue",
                                return_value={"number": 6, "title": "Run start phase"},
                            ):
                                with patch(
                                    "shinobi.cli.start_mission",
                                    return_value=started_mission,
                                ):
                                    with patch(
                                        "shinobi.cli.execute_verification",
                                        return_value=execution_result,
                                    ):
                                        with patch(
                                            "shinobi.cli.handoff_started_mission"
                                        ) as handoff_mock:
                                            with patch(
                                                "shinobi.cli.publish_mission"
                                            ) as publish_mock:
                                                with redirect_stdout(output):
                                                    exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: Shinobi stopped before publish because verification failed",
                output.getvalue(),
            )
            handoff_mock.assert_called_once()
            self.assertIn("test: failed", handoff_mock.call_args.kwargs["reason"])
            publish_mock.assert_not_called()
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_hands_off_when_high_risk_paths_are_detected_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    output = io.StringIO()
                    started_mission = Mock(
                        branch="feature/issue-6-run-start-phase",
                        issue_number=6,
                        lease_expires_at="2026-04-09T00:30:00Z",
                    )
                    published_mission = Mock(
                        branch="feature/issue-6-run-start-phase",
                        issue_number=6,
                        pr_number=31,
                        pr_url="https://github.com/owner/repo/pull/31",
                        lease_expires_at="2026-04-09T00:30:00Z",
                    )
                    execution_result = ExecutionResult(
                        commands=[
                            VerificationCommandResult(
                                name="test",
                                command=["python3", "-m", "unittest"],
                                status="passed",
                                returncode=0,
                            )
                        ],
                        change_summary="Changed auth flow.",
                    )
                    stop_decision = StopDecision(
                        reason=(
                            "Shinobi stopped before publish because changed files match "
                            "high-risk path(s): auth/ (files: auth/login.py)"
                        ),
                        conclusion="needs-human",
                        changed_paths=["auth/login.py"],
                        matched_paths=["auth/"],
                    )
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[]):
                        with patch("shinobi.cli.select_ready_issue", return_value=6):
                            with patch(
                                "shinobi.cli.load_issue",
                                return_value={"number": 6, "title": "Run start phase"},
                            ):
                                with patch(
                                    "shinobi.cli.start_mission",
                                    return_value=started_mission,
                                ):
                                    with patch(
                                        "shinobi.cli.execute_verification",
                                        return_value=execution_result,
                                    ):
                                        with patch(
                                            "shinobi.cli.load_publishable_issue_label_names",
                                            return_value={"shinobi:working"},
                                        ):
                                            with patch(
                                                "shinobi.cli.detect_high_risk_stop",
                                                return_value=stop_decision,
                                            ):
                                                with patch(
                                                    "shinobi.cli.handoff_started_mission"
                                                ) as handoff_mock:
                                                    with patch(
                                                        "shinobi.cli.publish_mission",
                                                    ) as publish_mock:
                                                        with redirect_stdout(output):
                                                            exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: Shinobi stopped before publish because changed files match "
                "high-risk path(s): auth/ (files: auth/login.py)",
                output.getvalue(),
            )
            handoff_mock.assert_called_once()
            self.assertEqual(
                handoff_mock.call_args.kwargs["reason"],
                stop_decision.reason,
            )
            publish_mock.assert_not_called()
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_prefers_existing_blocking_label_before_high_risk_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    output = io.StringIO()
                    started_mission = Mock(
                        branch="feature/issue-6-run-start-phase",
                        issue_number=6,
                        lease_expires_at="2026-04-09T00:30:00Z",
                    )
                    execution_result = ExecutionResult(
                        commands=[
                            VerificationCommandResult(
                                name="test",
                                command=["python3", "-m", "unittest"],
                                status="passed",
                                returncode=0,
                            )
                        ],
                        change_summary="Changed auth flow.",
                    )
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[]):
                        with patch("shinobi.cli.select_ready_issue", return_value=6):
                            with patch(
                                "shinobi.cli.load_issue",
                                return_value={"number": 6, "title": "Run start phase"},
                            ):
                                with patch(
                                    "shinobi.cli.start_mission",
                                    return_value=started_mission,
                                ):
                                    with patch(
                                        "shinobi.cli.execute_verification",
                                        return_value=execution_result,
                                    ):
                                        with patch(
                                            "shinobi.cli.load_publishable_issue_label_names",
                                            return_value={"shinobi:blocked", "shinobi:working"},
                                        ):
                                            with patch(
                                                "shinobi.cli.detect_high_risk_stop"
                                            ) as detect_mock:
                                                with patch(
                                                    "shinobi.cli.handoff_started_mission"
                                                ) as handoff_mock:
                                                    with patch(
                                                        "shinobi.cli.publish_mission",
                                                    ) as publish_mock:
                                                        with redirect_stdout(output):
                                                            exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: publish phase cannot proceed because issue #6 "
                "has blocking label(s): shinobi:blocked",
                output.getvalue(),
            )
            detect_mock.assert_not_called()
            handoff_mock.assert_not_called()
            publish_mock.assert_not_called()
            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "blocked")
            self.assertEqual(saved_state.last_error, "publish phase cannot proceed because issue #6 has blocking label(s): shinobi:blocked")
            self.assertIsNotNone(saved_state.last_mission)
            self.assertEqual(saved_state.last_mission.phase, "publish")
            self.assertEqual(saved_state.last_mission.conclusion, "blocked")
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_hands_off_when_pre_publish_stop_requests_unsupported_conclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    output = io.StringIO()
                    started_mission = Mock(
                        branch="feature/issue-6-run-start-phase",
                        issue_number=6,
                        lease_expires_at="2026-04-09T00:30:00Z",
                    )
                    execution_result = ExecutionResult(
                        commands=[
                            VerificationCommandResult(
                                name="test",
                                command=["python3", "-m", "unittest"],
                                status="passed",
                                returncode=0,
                            )
                        ],
                        change_summary="Changed auth flow.",
                    )
                    stop_decision = StopDecision(
                        reason="Shinobi stopped before publish because auth changes require block.",
                        conclusion="blocked",
                        changed_paths=["auth/login.py"],
                        matched_paths=["auth/"],
                    )
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[]):
                        with patch("shinobi.cli.select_ready_issue", return_value=6):
                            with patch(
                                "shinobi.cli.load_issue",
                                return_value={"number": 6, "title": "Run start phase"},
                            ):
                                with patch(
                                    "shinobi.cli.start_mission",
                                    return_value=started_mission,
                                ):
                                    with patch(
                                        "shinobi.cli.execute_verification",
                                        return_value=execution_result,
                                    ):
                                        with patch(
                                            "shinobi.cli.load_publishable_issue_label_names",
                                            return_value={"shinobi:working"},
                                        ):
                                            with patch(
                                                "shinobi.cli.detect_high_risk_stop",
                                                return_value=stop_decision,
                                            ):
                                                with patch(
                                                    "shinobi.cli.handoff_started_mission"
                                                ) as handoff_mock:
                                                    with patch(
                                                        "shinobi.cli.publish_mission",
                                                    ) as publish_mock:
                                                        with redirect_stdout(output):
                                                            exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: pre-publish stop requested unsupported conclusion blocked; "
                "handing off to needs-human instead. Original reason: "
                "Shinobi stopped before publish because auth changes require block.",
                output.getvalue(),
            )
            handoff_mock.assert_called_once()
            self.assertEqual(
                handoff_mock.call_args.kwargs["reason"],
                "pre-publish stop requested unsupported conclusion blocked; "
                "handing off to needs-human instead. Original reason: "
                "Shinobi stopped before publish because auth changes require block.",
            )
            publish_mock.assert_not_called()
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_hands_off_when_high_risk_detection_fails_before_publish(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    output = io.StringIO()
                    started_mission = Mock(
                        branch="feature/issue-6-run-start-phase",
                        issue_number=6,
                        lease_expires_at="2026-04-09T00:30:00Z",
                    )
                    execution_result = ExecutionResult(
                        commands=[
                            VerificationCommandResult(
                                name="test",
                                command=["python3", "-m", "unittest"],
                                status="passed",
                                returncode=0,
                            )
                        ],
                        change_summary="Changed auth flow.",
                    )
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[]):
                        with patch("shinobi.cli.select_ready_issue", return_value=6):
                            with patch(
                                "shinobi.cli.load_issue",
                                return_value={"number": 6, "title": "Run start phase"},
                            ):
                                with patch(
                                    "shinobi.cli.start_mission",
                                    return_value=started_mission,
                                ):
                                    with patch(
                                        "shinobi.cli.execute_verification",
                                        return_value=execution_result,
                                    ):
                                        with patch(
                                            "shinobi.cli.load_publishable_issue_label_names",
                                            return_value={"shinobi:working"},
                                        ):
                                            with patch(
                                                "shinobi.cli.detect_high_risk_stop",
                                                side_effect=RuntimeError("unknown revision"),
                                            ):
                                                with patch(
                                                    "shinobi.cli.handoff_started_mission"
                                                ) as handoff_mock:
                                                    with patch(
                                                        "shinobi.cli.publish_mission"
                                                    ) as publish_mock:
                                                        with redirect_stdout(output):
                                                            exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: Shinobi failed to evaluate pre-publish stop conditions: "
                "unknown revision",
                output.getvalue(),
            )
            handoff_mock.assert_called_once()
            self.assertEqual(
                handoff_mock.call_args.kwargs["reason"],
                "Shinobi failed to evaluate pre-publish stop conditions: unknown revision",
            )
            publish_mock.assert_not_called()
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_hands_off_when_issue_reload_fails_before_pre_publish_stop_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    output = io.StringIO()
                    started_mission = Mock(
                        branch="feature/issue-6-run-start-phase",
                        issue_number=6,
                        lease_expires_at="2026-04-09T00:30:00Z",
                    )
                    execution_result = ExecutionResult(
                        commands=[
                            VerificationCommandResult(
                                name="test",
                                command=["python3", "-m", "unittest"],
                                status="passed",
                                returncode=0,
                            )
                        ],
                        change_summary="Changed auth flow.",
                    )
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[]):
                        with patch("shinobi.cli.select_ready_issue", return_value=6):
                            with patch(
                                "shinobi.cli.load_issue",
                                return_value={"number": 6, "title": "Run start phase"},
                            ):
                                with patch(
                                    "shinobi.cli.start_mission",
                                    return_value=started_mission,
                                ):
                                    with patch(
                                        "shinobi.cli.execute_verification",
                                        return_value=execution_result,
                                    ):
                                        with patch(
                                            "shinobi.cli.load_publishable_issue_label_names",
                                            side_effect=MissionPublishError(
                                                "failed to load issue #6 before publish: api unavailable"
                                            ),
                                        ):
                                            with patch(
                                                "shinobi.cli.handoff_started_mission"
                                            ) as handoff_mock:
                                                with patch(
                                                    "shinobi.cli.detect_high_risk_stop"
                                                ) as detect_mock:
                                                    with patch(
                                                        "shinobi.cli.publish_mission"
                                                    ) as publish_mock:
                                                        with redirect_stdout(output):
                                                            exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: Shinobi failed to evaluate pre-publish stop conditions: "
                "failed to load issue #6 before publish: api unavailable",
                output.getvalue(),
            )
            handoff_mock.assert_called_once()
            self.assertEqual(
                handoff_mock.call_args.kwargs["reason"],
                "Shinobi failed to evaluate pre-publish stop conditions: "
                "failed to load issue #6 before publish: api unavailable",
            )
            detect_mock.assert_not_called()
            publish_mock.assert_not_called()
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_refuses_active_github_mission_before_selecting_ready_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    output = io.StringIO()
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[9]):
                        with patch("shinobi.cli.select_ready_issue") as select_mock:
                            with redirect_stdout(output):
                                exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: active GitHub mission exists for #9",
                output.getvalue(),
            )
            select_mock.assert_not_called()
            self.assertEqual(StateStore(root).paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_aborts_cleanly_when_listing_active_github_missions_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    output = io.StringIO()
                    with patch(
                        "shinobi.cli.list_open_issues_with_any_label",
                        side_effect=RuntimeError("gh failed"),
                    ):
                        with redirect_stdout(output):
                            exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn("run aborted: gh failed", output.getvalue())
            self.assertEqual(StateStore(root).paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_aborts_cleanly_when_lock_timestamp_is_timezone_naive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    store.paths.lock_path.write_text(
                        json.dumps(
                            {
                                "agent_identity": "other-agent",
                                "run_id": "live-run",
                                "pid": 321,
                                "started_at": "2026-04-09T00:00:00Z",
                                "heartbeat_at": "2026-04-09T00:00:00",
                            }
                        ),
                        encoding="utf-8",
                    )

                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: failed to load run lock: timestamp must include timezone offset",
                output.getvalue(),
            )

    def test_run_with_issue_refuses_other_active_github_mission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    output = io.StringIO()
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[5]):
                        with patch("shinobi.cli.ensure_open_issue") as issue_mock:
                            with redirect_stdout(output):
                                exit_code = cli.main(["run", "--issue", "6"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: active GitHub mission exists for #5",
                output.getvalue(),
            )
            issue_mock.assert_not_called()
            self.assertEqual(StateStore(root).paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_with_issue_refuses_same_issue_when_local_mission_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    state = store.load_state()
                    state.issue_number = 6
                    state.phase = "start"
                    store.save_state(state)

                    output = io.StringIO()
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[]):
                        with patch("shinobi.cli.ensure_open_issue", return_value=6) as issue_mock:
                            with redirect_stdout(output):
                                exit_code = cli.main(["run", "--issue", "6"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: local mission state is active for issue #6",
                output.getvalue(),
            )
            issue_mock.assert_not_called()
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_with_issue_refuses_same_issue_when_retryable_local_only_mission_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    state = store.load_state()
                    state.issue_number = 6
                    state.phase = "start"
                    state.retryable_local_only = True
                    store.save_state(state)

                    output = io.StringIO()
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[]):
                        with patch("shinobi.cli.ensure_open_issue", return_value=6) as issue_mock:
                            with redirect_stdout(output):
                                exit_code = cli.main(["run", "--issue", "6"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: retryable local-only mission exists for issue #6",
                output.getvalue(),
            )
            issue_mock.assert_not_called()
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_with_issue_refuses_same_issue_when_github_mission_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    output = io.StringIO()
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[6]):
                        with patch("shinobi.cli.ensure_open_issue", return_value=6) as issue_mock:
                            with redirect_stdout(output):
                                exit_code = cli.main(["run", "--issue", "6"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: active GitHub mission exists for #6",
                output.getvalue(),
            )
            issue_mock.assert_not_called()
            self.assertEqual(StateStore(root).paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_with_issue_refuses_other_active_github_mission_when_target_issue_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    output = io.StringIO()
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[6, 9]):
                        with patch("shinobi.cli.ensure_open_issue") as issue_mock:
                            with redirect_stdout(output):
                                exit_code = cli.main(["run", "--issue", "6"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: active GitHub mission exists for #6, #9",
                output.getvalue(),
            )
            issue_mock.assert_not_called()
            self.assertEqual(StateStore(root).paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_with_issue_refuses_closed_or_missing_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    output = io.StringIO()
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[]):
                        with patch(
                            "shinobi.cli.ensure_open_issue",
                            side_effect=RuntimeError("issue #6 is not open"),
                        ):
                            with redirect_stdout(output):
                                exit_code = cli.main(["run", "--issue", "6"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: issue #6 is not open",
                output.getvalue(),
            )

    def test_run_with_issue_refuses_conflicting_active_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    state = store.load_state()
                    state.issue_number = 5
                    state.phase = "start"
                    store.save_state(state)

                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["run", "--issue", "6"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: local mission state is active for issue #5",
                output.getvalue(),
            )
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_with_issue_refuses_conflicting_retryable_local_only_mission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    state = store.load_state()
                    state.issue_number = 5
                    state.phase = "start"
                    state.retryable_local_only = True
                    store.save_state(state)

                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["run", "--issue", "6"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: retryable local-only mission exists for issue #5",
                output.getvalue(),
            )
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_aborts_cleanly_when_gh_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("shinobi.cli.discover_workspace_root", return_value=root):
                    with patch("pathlib.Path.cwd", return_value=root):
                        with redirect_stdout(io.StringIO()):
                            cli.main(["init"])

                        output = io.StringIO()
                        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
                            with patch(
                                "shinobi.github_client.subprocess.run",
                                side_effect=FileNotFoundError("No such file or directory: 'gh'"),
                            ):
                                with redirect_stdout(output):
                                    exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: failed to list open issues for label shinobi:working with gh:",
                output.getvalue(),
            )

    def test_run_refuses_active_local_state_when_issue_number_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    state = store.load_state()
                    state.phase = "start"
                    state.issue_number = None
                    store.save_state(state)

                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: local mission state is active in phase start but issue_number is missing",
                output.getvalue(),
            )
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_refuses_retryable_local_only_state_when_issue_number_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

                    store = StateStore(root)
                    state = store.load_state()
                    state.phase = "start"
                    state.issue_number = None
                    state.retryable_local_only = True
                    store.save_state(state)

                    output = io.StringIO()
                    with redirect_stdout(output):
                        exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: retryable local-only mission exists but local state is missing issue_number",
                output.getvalue(),
            )
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_select_ready_issue_prefers_high_priority_labels(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["gh", "api", "repos/owner/repo/issues"],
                returncode=0,
                stdout=json.dumps(
                    [{"number": 1, "labels": [{"name": "shinobi:ready"}]}]
                    + [
                        {
                            "number": number,
                            "labels": [{"name": "shinobi:ready"}, {"name": "priority:medium"}],
                        }
                        for number in range(2, 101)
                    ]
                ),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["gh", "api", "repos/owner/repo/issues"],
                returncode=0,
                stdout=json.dumps(
                    [{"number": 15, "labels": [{"name": "shinobi:ready"}, {"name": "priority:high"}]}]
                ),
                stderr="",
            ),
        ]

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", side_effect=responses) as run_mock:
                selected_issue = cli.select_ready_issue(Path("/tmp/repo"), "shinobi:ready")

        self.assertEqual(selected_issue, 15)
        self.assertEqual(run_mock.call_count, 2)

    def test_select_ready_issue_uses_explicit_repo_override(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "api", "repos/configured/repo/issues"],
            returncode=0,
            stdout=json.dumps([{"number": 15, "labels": [{"name": "shinobi:ready"}]}]),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                selected_issue = cli.select_ready_issue(
                    Path("/tmp/repo"), "shinobi:ready", repo="configured/repo"
                )

        self.assertEqual(selected_issue, 15)
        self.assertIn("repos/configured/repo/issues", run_mock.call_args.args[0])

    def test_ensure_open_issue_rejects_closed_issue(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "api", "repos/owner/repo/issues/6"],
            returncode=0,
            stdout=json.dumps({"number": 6, "state": "closed", "labels": []}),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=result):
                with self.assertRaisesRegex(RuntimeError, "issue #6 is not open"):
                    cli.ensure_open_issue(Path("/tmp/repo"), 6)

    def test_ensure_open_issue_rejects_issue_with_active_label(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "api", "repos/owner/repo/issues/6"],
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 6,
                    "state": "open",
                    "labels": [{"name": "shinobi:working"}],
                }
            ),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=result):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "issue #6 already has active mission label\\(s\\): shinobi:working",
                ):
                    cli.ensure_open_issue(
                        Path("/tmp/repo"),
                        6,
                        active_labels=("shinobi:working", "shinobi:reviewing"),
                    )

    def test_ensure_open_issue_allows_active_label_when_requested(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "api", "repos/owner/repo/issues/6"],
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 6,
                    "state": "open",
                    "labels": [{"name": "shinobi:working"}],
                }
            ),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=result):
                issue_number = cli.ensure_open_issue(
                    Path("/tmp/repo"),
                    6,
                    active_labels=("shinobi:working", "shinobi:reviewing"),
                    allow_active_labels=True,
                )

        self.assertEqual(issue_number, 6)

    def test_ensure_open_issue_rejects_pull_request(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "api", "repos/owner/repo/issues/6"],
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 6,
                    "state": "open",
                    "labels": [],
                    "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/6"},
                }
            ),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=result):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "issue #6 is a pull request, not an issue",
                ):
                    cli.ensure_open_issue(Path("/tmp/repo"), 6)

    def test_list_open_issues_with_any_label_merges_issue_numbers(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["gh", "api", "repos/owner/repo/issues"],
                returncode=0,
                stdout=json.dumps([{"number": 6}, {"number": 8}]),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["gh", "api", "repos/owner/repo/issues"],
                returncode=0,
                stdout=json.dumps([{"number": 8}, {"number": 10}]),
                stderr="",
            ),
        ]

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", side_effect=responses):
                issue_numbers = cli.list_open_issues_with_any_label(
                    Path("/tmp/repo"),
                    ("shinobi:working", "shinobi:reviewing"),
                )

        self.assertEqual(issue_numbers, [6, 8, 10])

    def test_list_open_issues_paginates_past_first_page(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["gh", "api", "repos/owner/repo/issues"],
                returncode=0,
                stdout=json.dumps(
                    [
                        {"number": 1, "labels": [{"name": "shinobi:ready"}]},
                        {"number": 2, "labels": [{"name": "shinobi:ready"}], "pull_request": {}},
                    ]
                    + [
                        {"number": number, "labels": [{"name": "shinobi:ready"}]}
                        for number in range(3, 101)
                    ]
                ),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["gh", "api", "repos/owner/repo/issues"],
                returncode=0,
                stdout=json.dumps(
                    [{"number": 150, "labels": [{"name": "shinobi:ready"}, {"name": "priority:high"}]}]
                ),
                stderr="",
            ),
        ]

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", side_effect=responses):
                issues = cli.list_open_issues(Path("/tmp/repo"), "shinobi:ready")

        self.assertEqual(issues[0]["number"], 1)
        self.assertNotIn("pull_request", issues[1])
        self.assertEqual(issues[-1]["number"], 150)
        self.assertEqual(len(issues), 100)

    def test_load_issue_uses_explicit_repo_override(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "api", "repos/configured/repo/issues/6"],
            returncode=0,
            stdout=json.dumps({"number": 6, "state": "open", "labels": []}),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                issue = cli.load_issue(Path("/tmp/repo"), 6, repo="configured/repo")

        self.assertEqual(issue["number"], 6)
        self.assertEqual(
            run_mock.call_args.args[0],
            ["gh", "api", "repos/configured/repo/issues/6"],
        )

    def test_acquire_lock_is_atomic_across_threads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()

            config_payload = {"repo": "owner/repo", "agent_identity": "config-id"}
            store.paths.config_path.write_text(json.dumps(config_payload), encoding="utf-8")
            store.paths.state_path.write_text(
                json.dumps({"agent_identity": "config-id", "phase": "idle"}),
                encoding="utf-8",
            )

            barrier = Barrier(2)
            now = datetime.now(timezone.utc)
            results: list[tuple[str, str]] = []

            def acquire(run_id: str) -> None:
                barrier.wait()
                try:
                    store.acquire_lock(
                        config=store.try_load_config()[0],
                        run_id=run_id,
                        pid=123,
                        now=now,
                    )
                except RuntimeError as error:
                    results.append(("error", str(error)))
                else:
                    results.append(("ok", run_id))

            first = Thread(target=acquire, args=("run-a",))
            second = Thread(target=acquire, args=("run-b",))
            first.start()
            second.start()
            first.join()
            second.join()

            self.assertEqual(len(results), 2)
            self.assertEqual(sum(1 for status, _ in results if status == "ok"), 1)
            self.assertEqual(sum(1 for status, _ in results if status == "error"), 1)
            self.assertIn(
                "run lock is held by live run",
                next(message for status, message in results if status == "error"),
            )

    def test_init_uses_git_workspace_root_from_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            nested = root / "src"
            nested.mkdir()
            output = io.StringIO()

            with patch("shinobi.cli.discover_workspace_root", return_value=root):
                with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                    with patch("pathlib.Path.cwd", return_value=nested):
                        with redirect_stdout(output):
                            exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            self.assertTrue((root / ".shinobi").exists())
            self.assertFalse((nested / ".shinobi").exists())
            self.assertIn(str(root / ".shinobi"), output.getvalue())


class MissionStartTest(unittest.TestCase):
    def test_start_mission_creates_branch_updates_state_and_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(
                config=config,
                run_id=run_id,
                pid=123,
                now=now,
            )

            with patch(
                "shinobi.mission_start.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["git", "checkout", "-b", "feature/issue-26-task-run-start-phase"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ):
                with patch("shinobi.mission_start.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    started = start_mission(
                        root=root,
                        store=store,
                        config=config,
                        run_id=run_id,
                        issue={
                            "number": 26,
                            "title": "[TASK] run start phase を実装する",
                            "labels": [{"name": "shinobi:ready"}],
                            "state": "open",
                        },
                        now=now,
                    )

            self.assertEqual(started.branch, "feature/issue-26-task-run-start-phase")
            self.assertEqual(started.lease_expires_at, "2026-04-09T00:30:00Z")
            state = store.load_state()
            self.assertEqual(state.issue_number, 26)
            self.assertEqual(state.branch, started.branch)
            self.assertEqual(state.phase, "start")
            self.assertEqual(state.run_id, run_id)
            self.assertFalse(state.retryable_local_only)
            self.assertEqual(state.lease_expires_at, started.lease_expires_at)
            self.assertEqual(state.last_result, "started")
            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(26, add=["shinobi:working"]),
                    unittest.mock.call(26, remove=["shinobi:ready"]),
                ],
            )
            client.create_issue_comment.assert_called_once()
            self.assertIn("<!-- shinobi:mission-state", client.create_issue_comment.call_args.args[1])
            self.assertIn("phase: start", client.create_issue_comment.call_args.args[1])
            self.assertIn("branch: feature/issue-26-task-run-start-phase", client.create_issue_comment.call_args.args[1])

    def test_start_mission_leaves_retryable_local_only_state_when_label_update_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(
                config=config,
                run_id=run_id,
                pid=123,
                now=now,
            )

            with patch(
                "shinobi.mission_start.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["git", "checkout", "-b", "feature/issue-26-task-run-start-phase"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ):
                with patch("shinobi.mission_start.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    client.update_issue_labels.side_effect = [
                        None,
                        GitHubClientError("remove failed"),
                        None,
                        None,
                        None,
                    ]
                    with self.assertRaisesRegex(
                        MissionStartError,
                        "failed to normalize start labels for issue #26",
                    ):
                        start_mission(
                            root=root,
                            store=store,
                            config=config,
                            run_id=run_id,
                            issue={
                                "number": 26,
                                "title": "[TASK] run start phase を実装する",
                                "labels": [{"name": "shinobi:ready"}],
                                "state": "open",
                            },
                            now=now,
                        )

            state = store.load_state()
            self.assertEqual(state.issue_number, 26)
            self.assertEqual(state.phase, "start")
            self.assertTrue(state.retryable_local_only)
            self.assertEqual(state.branch, "feature/issue-26-task-run-start-phase")
            self.assertIn("failed to normalize start labels", state.last_error or "")
            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(26, add=["shinobi:working"]),
                    unittest.mock.call(26, remove=["shinobi:ready"]),
                    unittest.mock.call(26, add=["shinobi:needs-human"]),
                    unittest.mock.call(26, remove=["shinobi:ready", "shinobi:working"]),
                ],
            )
            client.create_issue_comment.assert_called_once()
            self.assertIn(
                "failed to complete start label transition",
                client.create_issue_comment.call_args.args[1],
            )

    def test_start_mission_rolls_back_labels_when_final_state_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(
                config=config,
                run_id=run_id,
                pid=123,
                now=now,
            )

            real_save_state = store.save_state

            def failing_save_state(state):
                real_save_state(state)
                if state.last_result == "started":
                    raise OSError("disk full")

            with patch(
                "shinobi.mission_start.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["git", "checkout", "-b", "feature/issue-26-task-run-start-phase"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ):
                with patch("shinobi.mission_start.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    with patch.object(store, "save_state", side_effect=failing_save_state):
                        with self.assertRaisesRegex(
                            MissionStartError,
                            "final local state persistence failed for issue #26: disk full",
                        ):
                            start_mission(
                                root=root,
                                store=store,
                                config=config,
                                run_id=run_id,
                                issue={
                                    "number": 26,
                                    "title": "[TASK] run start phase を実装する",
                                    "labels": [{"name": "shinobi:ready"}],
                                    "state": "open",
                                },
                                now=now,
                            )

            state = store.load_state()
            self.assertEqual(state.issue_number, 26)
            self.assertTrue(state.retryable_local_only)
            self.assertIn("disk full", state.last_error or "")
            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(26, add=["shinobi:working"]),
                    unittest.mock.call(26, remove=["shinobi:ready"]),
                    unittest.mock.call(26, add=["shinobi:needs-human"]),
                    unittest.mock.call(26, remove=["shinobi:working"]),
                ],
            )
            self.assertEqual(client.create_issue_comment.call_count, 2)
            self.assertIn(
                "failed to persist final local state during start phase",
                client.create_issue_comment.call_args_list[-1].args[1],
            )

    def test_start_mission_rolls_back_labels_when_start_comment_creation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(
                config=config,
                run_id=run_id,
                pid=123,
                now=now,
            )

            with patch(
                "shinobi.mission_start.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["git", "checkout", "-b", "feature/issue-26-task-run-start-phase"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ):
                with patch("shinobi.mission_start.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    client.create_issue_comment.side_effect = GitHubClientError("comment failed")
                    with self.assertRaisesRegex(
                        MissionStartError,
                        "failed to create mission-state comment on issue #26",
                    ):
                        start_mission(
                            root=root,
                            store=store,
                            config=config,
                            run_id=run_id,
                            issue={
                                "number": 26,
                                "title": "[TASK] run start phase を実装する",
                                "labels": [{"name": "shinobi:ready"}],
                                "state": "open",
                            },
                            now=now,
                        )

            state = store.load_state()
            self.assertEqual(state.issue_number, 26)
            self.assertTrue(state.retryable_local_only)
            self.assertIn("failed to create mission-state comment", state.last_error or "")
            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(26, add=["shinobi:working"]),
                    unittest.mock.call(26, remove=["shinobi:ready"]),
                    unittest.mock.call(26, add=["shinobi:needs-human"]),
                    unittest.mock.call(26, remove=["shinobi:working"]),
                ],
            )

    def test_start_mission_surfaces_retryable_state_persist_failure_after_label_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(
                config=config,
                run_id=run_id,
                pid=123,
                now=now,
            )

            with patch(
                "shinobi.mission_start.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["git", "checkout", "-b", "feature/issue-26-task-run-start-phase"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ):
                real_save_state = store.save_state

                def failing_save_state(state):
                    if state.last_error and "failed to normalize start labels" in state.last_error:
                        raise OSError("state write failed twice")
                    real_save_state(state)

                with patch("shinobi.mission_start.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    client.update_issue_labels.side_effect = [
                        None,
                        GitHubClientError("remove failed"),
                        None,
                        None,
                        None,
                    ]
                    with patch.object(store, "save_state", side_effect=failing_save_state):
                        with self.assertRaisesRegex(
                            MissionStartError,
                            "additionally failed to persist retryable local state: state write failed twice",
                        ):
                            start_mission(
                                root=root,
                                store=store,
                                config=config,
                                run_id=run_id,
                                issue={
                                    "number": 26,
                                    "title": "[TASK] run start phase を実装する",
                                    "labels": [{"name": "shinobi:ready"}],
                                    "state": "open",
                                },
                                now=now,
                            )

            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(26, add=["shinobi:working"]),
                    unittest.mock.call(26, remove=["shinobi:ready"]),
                    unittest.mock.call(26, add=["shinobi:needs-human"]),
                    unittest.mock.call(26, remove=["shinobi:ready", "shinobi:working"]),
                ],
            )

    def test_start_mission_writes_retryable_log_when_provisional_state_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(
                config=config,
                run_id=run_id,
                pid=123,
                now=now,
            )

            with patch(
                "shinobi.mission_start.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["git", "checkout", "-b", "feature/issue-26-task-run-start-phase"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ):
                real_save_state = store.save_state

                def failing_first_save(state):
                    if state.last_result == "start_pending":
                        raise OSError("disk full")
                    real_save_state(state)

                with patch.object(store, "save_state", side_effect=failing_first_save):
                    with self.assertRaisesRegex(
                        MissionStartError,
                        "failed to persist local mission state after creating branch",
                    ):
                        start_mission(
                            root=root,
                            store=store,
                            config=config,
                            run_id=run_id,
                            issue={
                                "number": 26,
                                "title": "[TASK] run start phase を実装する",
                                "labels": [{"name": "shinobi:ready"}],
                                "state": "open",
                            },
                            now=now,
                        )

            log_entries = (
                store.paths.logs_dir / "retryable-start-failures.jsonl"
            ).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(log_entries), 1)
            payload = json.loads(log_entries[0])
            self.assertEqual(payload["issue_number"], 26)
            self.assertEqual(payload["branch"], "feature/issue-26-task-run-start-phase")
            self.assertEqual(payload["phase"], "start")
            self.assertTrue(payload["retryable_local_only"])
            self.assertIn("disk full", payload["last_error"])

    def test_start_mission_normalizes_all_non_risky_state_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(
                config=config,
                run_id=run_id,
                pid=123,
                now=now,
            )

            with patch(
                "shinobi.mission_start.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["git", "checkout", "-b", "feature/issue-26-task-run-start-phase"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ):
                with patch("shinobi.mission_start.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    start_mission(
                        root=root,
                        store=store,
                        config=config,
                        run_id=run_id,
                        issue={
                            "number": 26,
                            "title": "[TASK] run start phase を実装する",
                            "labels": [
                                {"name": "shinobi:ready"},
                                {"name": "shinobi:reviewing"},
                                {"name": "shinobi:merged"},
                                {"name": "shinobi:risky"},
                            ],
                            "state": "open",
                        },
                        now=now,
                    )

            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(26, add=["shinobi:working"]),
                    unittest.mock.call(
                        26,
                        remove=["shinobi:merged", "shinobi:ready", "shinobi:reviewing"],
                    ),
                ],
            )

    def test_handoff_started_mission_moves_issue_to_needs_human_and_clears_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(
                config=config,
                run_id=run_id,
                pid=123,
                now=now,
            )
            store.save_state(
                cli.State(
                    issue_number=26,
                    pr_number=None,
                    branch="feature/issue-26-task-run-start-phase",
                    agent_identity=config.agent_identity,
                    run_id=run_id,
                    phase="start",
                    review_loop_count=0,
                    retryable_local_only=False,
                    lease_expires_at="2026-04-09T00:30:00Z",
                    last_result="started",
                    last_error=None,
                )
            )

            with patch("shinobi.mission_start.GitHubClient") as client_cls:
                client = client_cls.return_value
                handoff_started_mission(
                    root=root,
                    store=store,
                    config=config,
                    run_id=run_id,
                    started_mission=StartedMission(
                        issue_number=26,
                        branch="feature/issue-26-task-run-start-phase",
                        lease_expires_at="2026-04-09T00:30:00Z",
                    ),
                    reason="context phase is not implemented",
                )

            state = store.load_state()
            self.assertEqual(state.phase, "idle")
            self.assertIsNone(state.issue_number)
            self.assertEqual(state.last_result, "needs-human")
            self.assertEqual(state.last_mission.issue_number, 26)
            self.assertEqual(state.last_mission.branch, "feature/issue-26-task-run-start-phase")
            self.assertEqual(state.last_mission.conclusion, "needs-human")
            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(26, add=["shinobi:needs-human"]),
                    unittest.mock.call(26, remove=["shinobi:working"]),
                ],
            )

    def test_start_mission_rejects_issue_without_ready_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(
                config=config,
                run_id=run_id,
                pid=123,
                now=now,
            )

            with self.assertRaisesRegex(
                MissionStartError,
                "issue #26 is not labeled shinobi:ready",
            ):
                start_mission(
                    root=root,
                    store=store,
                    config=config,
                    run_id=run_id,
                    issue={
                        "number": 26,
                        "title": "[TASK] run start phase を実装する",
                        "labels": [{"name": "shinobi:blocked"}],
                        "state": "open",
                    },
                    now=now,
                )

    def test_start_mission_rejects_blocked_issue_even_if_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(
                config=config,
                run_id=run_id,
                pid=123,
                now=now,
            )

            with self.assertRaisesRegex(
                MissionStartError,
                "issue #26 has non-startable label\\(s\\): shinobi:blocked",
            ):
                start_mission(
                    root=root,
                    store=store,
                    config=config,
                    run_id=run_id,
                    issue={
                        "number": 26,
                        "title": "[TASK] run start phase を実装する",
                        "labels": [{"name": "shinobi:ready"}, {"name": "shinobi:blocked"}],
                        "state": "open",
                    },
                    now=now,
                )


class MissionPublishTest(unittest.TestCase):
    def test_publish_mission_pushes_branch_creates_pr_updates_labels_comment_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                with patch("pathlib.Path.cwd", return_value=root):
                    with redirect_stdout(io.StringIO()):
                        cli.main(["init"])

            store = StateStore(root)
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            run_id = "run-123"
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            lock_started_at = datetime(2026, 4, 8, 23, 0, tzinfo=timezone.utc)
            store.acquire_lock(config=config, run_id=run_id, pid=123, now=lock_started_at)
            store.save_state(
                cli.State(
                    issue_number=31,
                    pr_number=None,
                    branch="feature/issue-31-publish-phase",
                    agent_identity=config.agent_identity,
                    run_id=run_id,
                    phase="start",
                    retryable_local_only=False,
                    lease_expires_at="2026-04-09T00:30:00Z",
                    last_result="started",
                )
            )
            execution_result = Mock()
            execution_result.change_summary = "Published mission changes."
            execution_result.commands = []

            with patch(
                "shinobi.mission_publish.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    args=["git", "push", "-u", "origin", "feature/issue-31-publish-phase"],
                    returncode=0,
                    stdout="",
                    stderr="",
                ),
            ):
                with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    client.list_pull_requests_by_head.return_value = []
                    client.create_pull_request.return_value = {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                    }
                    client.get_issue.return_value = {
                        "number": 31,
                        "labels": [
                            {"name": "shinobi:ready"},
                            {"name": "shinobi:working"},
                            {"name": "shinobi:risky"},
                        ],
                    }
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 31\n"
                                "branch: feature/issue-31-publish-phase\n"
                                "phase: start\n"
                                "-->\n"
                            ),
                        }
                    ]

                    published = publish_mission(
                        root=root,
                        store=store,
                        config=config,
                        run_id=run_id,
                        state=store.load_state(),
                        execution_result=execution_result,
                        now=now,
                    )

            self.assertEqual(published.pr_number, 44)
            self.assertEqual(published.lease_expires_at, "2026-04-09T00:30:00Z")
            client.create_pull_request.assert_called_once()
            self.assertTrue(client.create_pull_request.call_args.kwargs["draft"])
            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(31, add=["shinobi:reviewing"]),
                    unittest.mock.call(
                        31,
                        remove=["shinobi:ready", "shinobi:working"],
                    ),
                ],
            )
            client.update_issue_comment.assert_called_once()
            self.assertIn("phase: publish", client.update_issue_comment.call_args.args[1])
            self.assertIn("pr: 44", client.update_issue_comment.call_args.args[1])
            state = store.load_state()
            self.assertEqual(state.phase, "publish")
            self.assertEqual(state.pr_number, 44)
            self.assertEqual(state.last_result, "published")
            lock = store.load_lock()
            self.assertIsNotNone(lock)
            self.assertEqual(lock.heartbeat_at, "2026-04-09T00:00:00Z")

    def test_publish_mission_updates_existing_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.config_path.write_text(
                json.dumps({"repo": "owner/repo", "agent_identity": "agent-1"}),
                encoding="utf-8",
            )
            store.paths.state_path.write_text(
                json.dumps({"agent_identity": "agent-1", "phase": "idle"}),
                encoding="utf-8",
            )
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(config=config, run_id="run-123", pid=123, now=now)
            state = cli.State(
                issue_number=31,
                branch="feature/issue-31-publish-phase",
                agent_identity="agent-1",
                run_id="run-123",
                phase="start",
            )
            execution_result = Mock()
            execution_result.change_summary = "Published mission changes."
            execution_result.commands = []

            with patch(
                "shinobi.mission_publish.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ):
                with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    client.list_pull_requests_by_head.return_value = [
                        {
                            "number": 44,
                            "url": "https://github.com/owner/repo/pull/44",
                            "isDraft": True,
                            "headRefName": "feature/issue-31-publish-phase",
                            "baseRefName": "main",
                        }
                    ]
                    client.update_pull_request.return_value = {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                        "isDraft": True,
                    }
                    client.get_issue.side_effect = [
                        {
                            "number": 31,
                            "labels": [{"name": "shinobi:working"}],
                        },
                        {
                            "number": 31,
                            "labels": [{"name": "shinobi:reviewing"}],
                        },
                    ]
                    client.list_issue_comments.return_value = []

                    publish_mission(
                        root=root,
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                        now=now,
                    )

            client.create_pull_request.assert_not_called()
            client.update_pull_request.assert_called_once()
            client.convert_pull_request_to_draft.assert_not_called()
            client.list_pull_requests_by_head.assert_called_once_with(
                "feature/issue-31-publish-phase"
            )
            client.create_issue_comment.assert_called_once()

    def test_publish_mission_creates_new_pr_when_existing_head_targets_other_base(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.config_path.write_text(
                json.dumps({"repo": "owner/repo", "agent_identity": "agent-1"}),
                encoding="utf-8",
            )
            store.paths.state_path.write_text(
                json.dumps({"agent_identity": "agent-1", "phase": "idle"}),
                encoding="utf-8",
            )
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(config=config, run_id="run-123", pid=123, now=now)
            state = cli.State(
                issue_number=31,
                branch="feature/issue-31-publish-phase",
                agent_identity="agent-1",
                run_id="run-123",
                phase="start",
            )
            execution_result = Mock()
            execution_result.change_summary = "Published mission changes."
            execution_result.commands = []

            with patch(
                "shinobi.mission_publish.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ):
                with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    client.list_pull_requests_by_head.return_value = [
                        {
                            "number": 44,
                            "url": "https://github.com/owner/repo/pull/44",
                            "isDraft": True,
                            "headRefName": "feature/issue-31-publish-phase",
                            "baseRefName": "release/1.0",
                        }
                    ]
                    client.create_pull_request.return_value = {
                        "number": 45,
                        "url": "https://github.com/owner/repo/pull/45",
                    }
                    client.get_issue.side_effect = [
                        {
                            "number": 31,
                            "labels": [{"name": "shinobi:working"}],
                        },
                        {
                            "number": 31,
                            "labels": [{"name": "shinobi:reviewing"}],
                        },
                    ]
                    client.list_issue_comments.return_value = []

                    publish_mission(
                        root=root,
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                        now=now,
                    )

            client.update_pull_request.assert_not_called()
            client.create_pull_request.assert_called_once()
            self.assertEqual(client.create_pull_request.call_args.kwargs["base"], "main")

    def test_publish_mission_hands_off_when_multiple_open_prs_match_same_base(self) -> None:
        store = Mock()
        store.format_timestamp.return_value = "2026-04-09T00:30:00Z"
        config = Config(repo="owner/repo", agent_identity="agent-1")
        state = cli.State(
            issue_number=31,
            branch="feature/issue-31-publish-phase",
            agent_identity="agent-1",
            run_id="run-123",
            phase="start",
        )
        execution_result = ExecutionResult(
            commands=[],
            change_summary="Published mission changes.",
        )

        with patch(
            "shinobi.mission_publish.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ):
            with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.side_effect = [
                    {
                        "number": 31,
                        "labels": [{"name": "shinobi:working"}],
                    },
                    {
                        "number": 31,
                        "labels": [{"name": "shinobi:working"}],
                    },
                ]
                client.list_pull_requests_by_head.return_value = [
                    {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                        "isDraft": True,
                        "headRefName": "feature/issue-31-publish-phase",
                        "baseRefName": "main",
                    },
                    {
                        "number": 45,
                        "url": "https://github.com/owner/repo/pull/45",
                        "isDraft": True,
                        "headRefName": "feature/issue-31-publish-phase",
                        "baseRefName": "main",
                    },
                ]

                with self.assertRaisesRegex(
                    MissionPublishError,
                    "multiple open PRs match feature/issue-31-publish-phase -> main",
                ):
                    publish_mission(
                        root=Path("/tmp/repo"),
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                    )

        client.update_pull_request.assert_not_called()
        client.create_pull_request.assert_not_called()
        self.assertEqual(
            client.update_issue_labels.call_args_list,
            [
                unittest.mock.call(31, add=["shinobi:needs-human"]),
                unittest.mock.call(31, remove=["shinobi:working"]),
            ],
        )
        client.create_issue_comment.assert_called_once()
        store.save_state.assert_called_once()

    def test_publish_mission_converts_existing_pr_back_to_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.config_path.write_text(
                json.dumps({"repo": "owner/repo", "agent_identity": "agent-1"}),
                encoding="utf-8",
            )
            store.paths.state_path.write_text(
                json.dumps({"agent_identity": "agent-1", "phase": "idle"}),
                encoding="utf-8",
            )
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(config=config, run_id="run-123", pid=123, now=now)
            state = cli.State(
                issue_number=31,
                branch="feature/issue-31-publish-phase",
                agent_identity="agent-1",
                run_id="run-123",
                phase="start",
            )
            execution_result = Mock()
            execution_result.change_summary = "Published mission changes."
            execution_result.commands = []

            with patch(
                "shinobi.mission_publish.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ):
                with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    client.list_pull_requests_by_head.return_value = [
                        {
                            "number": 44,
                            "url": "https://github.com/owner/repo/pull/44",
                            "isDraft": False,
                            "headRefName": "feature/issue-31-publish-phase",
                            "baseRefName": "main",
                        }
                    ]
                    client.update_pull_request.return_value = {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                        "isDraft": False,
                    }
                    client.convert_pull_request_to_draft.return_value = {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                        "isDraft": True,
                    }
                    client.get_issue.side_effect = [
                        {
                            "number": 31,
                            "labels": [{"name": "shinobi:working"}],
                        },
                        {
                            "number": 31,
                            "labels": [{"name": "shinobi:reviewing"}],
                        },
                    ]
                    client.list_issue_comments.return_value = []

                    publish_mission(
                        root=root,
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                        now=now,
                    )

            client.create_pull_request.assert_not_called()
            client.update_pull_request.assert_called_once()
            client.convert_pull_request_to_draft.assert_called_once_with(44)
            client.list_pull_requests_by_head.assert_called_once_with(
                "feature/issue-31-publish-phase"
            )
            client.create_issue_comment.assert_called_once()

    def test_publish_mission_marks_existing_draft_pr_ready_when_config_disables_drafts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.config_path.write_text(
                json.dumps(
                    {
                        "repo": "owner/repo",
                        "agent_identity": "agent-1",
                        "use_draft_pr": False,
                    }
                ),
                encoding="utf-8",
            )
            store.paths.state_path.write_text(
                json.dumps({"agent_identity": "agent-1", "phase": "idle"}),
                encoding="utf-8",
            )
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(config=config, run_id="run-123", pid=123, now=now)
            state = cli.State(
                issue_number=31,
                branch="feature/issue-31-publish-phase",
                agent_identity="agent-1",
                run_id="run-123",
                phase="start",
            )
            execution_result = Mock()
            execution_result.change_summary = "Published mission changes."
            execution_result.commands = []

            with patch(
                "shinobi.mission_publish.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ):
                with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    client.list_pull_requests_by_head.return_value = [
                        {
                            "number": 44,
                            "url": "https://github.com/owner/repo/pull/44",
                            "isDraft": True,
                            "headRefName": "feature/issue-31-publish-phase",
                            "baseRefName": "main",
                        }
                    ]
                    client.update_pull_request.return_value = {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                        "isDraft": True,
                    }
                    client.convert_pull_request_to_ready.return_value = {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                        "isDraft": False,
                    }
                    client.get_issue.side_effect = [
                        {
                            "number": 31,
                            "labels": [{"name": "shinobi:working"}],
                        },
                        {
                            "number": 31,
                            "labels": [{"name": "shinobi:reviewing"}],
                        },
                    ]
                    client.list_issue_comments.return_value = []

                    publish_mission(
                        root=root,
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                        now=now,
                    )

            client.create_pull_request.assert_not_called()
            client.update_pull_request.assert_called_once()
            client.convert_pull_request_to_ready.assert_called_once_with(44)
            client.convert_pull_request_to_draft.assert_not_called()
            client.list_pull_requests_by_head.assert_called_once_with(
                "feature/issue-31-publish-phase"
            )
            client.create_issue_comment.assert_called_once()

    def test_publish_mission_hands_off_when_publish_label_cleanup_fails(self) -> None:
        store = Mock()
        store.format_timestamp.return_value = "2026-04-09T00:30:00Z"
        config = Config(repo="owner/repo", agent_identity="agent-1")
        state = cli.State(
            issue_number=31,
            branch="feature/issue-31-publish-phase",
            agent_identity="agent-1",
            run_id="run-123",
            phase="start",
        )
        execution_result = ExecutionResult(
            commands=[],
            change_summary="Published mission changes.",
        )

        with patch(
            "shinobi.mission_publish.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ):
            with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.side_effect = [
                    {
                        "number": 31,
                        "labels": [
                            {"name": "shinobi:ready"},
                            {"name": "shinobi:working"},
                        ],
                    },
                    {
                        "number": 31,
                        "labels": [
                            {"name": "shinobi:ready"},
                            {"name": "shinobi:working"},
                            {"name": "shinobi:reviewing"},
                        ],
                    },
                ]
                client.list_pull_requests_by_head.return_value = []
                client.create_pull_request.return_value = {
                    "number": 44,
                    "url": "https://github.com/owner/repo/pull/44",
                }
                client.update_issue_labels.side_effect = [
                    None,
                    GitHubClientError("remove failed"),
                    None,
                    None,
                ]
                client.list_issue_comments.return_value = [
                    {
                        "id": 9001,
                        "body": (
                            "<!-- shinobi:mission-state\n"
                            "issue: 31\n"
                            "branch: feature/issue-31-publish-phase\n"
                            "phase: start\n"
                            "-->\n"
                        ),
                    }
                ]

                with self.assertRaisesRegex(
                    MissionPublishError,
                    "failed to normalize publish labels for issue #31",
                ):
                    publish_mission(
                        root=Path("/tmp/repo"),
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                    )

        self.assertEqual(
            client.update_issue_labels.call_args_list,
            [
                unittest.mock.call(31, add=["shinobi:reviewing"]),
                unittest.mock.call(31, remove=["shinobi:ready", "shinobi:working"]),
                unittest.mock.call(31, add=["shinobi:needs-human"]),
                unittest.mock.call(
                    31,
                    remove=["shinobi:ready", "shinobi:reviewing", "shinobi:working"],
                ),
            ],
        )
        client.update_issue_comment.assert_called_once()
        self.assertIn(
            "failed to complete publish phase",
            client.update_issue_comment.call_args.args[1],
        )
        self.assertIn("phase: publish", client.update_issue_comment.call_args.args[1])
        self.assertIn("pr: 44", client.update_issue_comment.call_args.args[1])
        store.save_state.assert_called_once()
        saved_state = store.save_state.call_args.args[0]
        self.assertEqual(saved_state.phase, "idle")
        self.assertEqual(saved_state.last_result, "needs-human")
        self.assertEqual(saved_state.last_mission.issue_number, 31)
        self.assertEqual(saved_state.last_mission.pr_number, 44)
        self.assertEqual(saved_state.last_mission.branch, "feature/issue-31-publish-phase")
        self.assertEqual(saved_state.last_mission.phase, "publish")
        self.assertEqual(saved_state.last_mission.conclusion, "needs-human")

    def test_publish_mission_hands_off_when_final_state_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.config_path.write_text(
                json.dumps({"repo": "owner/repo", "agent_identity": "agent-1"}),
                encoding="utf-8",
            )
            store.paths.state_path.write_text(
                json.dumps({"agent_identity": "agent-1", "phase": "idle"}),
                encoding="utf-8",
            )
            config, _ = store.try_load_config()
            self.assertIsNotNone(config)
            now = datetime(2026, 4, 9, 0, 0, tzinfo=timezone.utc)
            store.acquire_lock(config=config, run_id="run-123", pid=123, now=now)
            state = cli.State(
                issue_number=31,
                branch="feature/issue-31-publish-phase",
                agent_identity="agent-1",
                run_id="run-123",
                phase="start",
            )
            execution_result = ExecutionResult(
                commands=[],
                change_summary="Published mission changes.",
            )

            with patch(
                "shinobi.mission_publish.subprocess.run",
                return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
            ):
                with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                    client = client_cls.return_value
                    client.get_issue.side_effect = [
                        {
                            "number": 31,
                            "labels": [{"name": "shinobi:working"}],
                        },
                        {
                            "number": 31,
                            "labels": [{"name": "shinobi:reviewing"}],
                        },
                    ]
                    client.list_pull_requests_by_head.return_value = []
                    client.create_pull_request.return_value = {
                        "number": 44,
                        "url": "https://github.com/owner/repo/pull/44",
                    }
                    client.list_issue_comments.return_value = [
                        {
                            "id": 9001,
                            "body": (
                                "<!-- shinobi:mission-state\n"
                                "issue: 31\n"
                                "branch: feature/issue-31-publish-phase\n"
                                "phase: start\n"
                                "-->\n"
                            ),
                        }
                    ]
                    with patch.object(store, "save_state", side_effect=OSError("disk full")):
                        with self.assertRaisesRegex(
                            MissionPublishError,
                            "final local state persistence failed for issue #31: disk full",
                        ):
                            publish_mission(
                                root=root,
                                store=store,
                                config=config,
                                run_id="run-123",
                                state=state,
                                execution_result=execution_result,
                                now=now,
                            )

        self.assertEqual(
            client.update_issue_labels.call_args_list,
            [
                unittest.mock.call(31, add=["shinobi:reviewing"]),
                unittest.mock.call(31, remove=["shinobi:working"]),
                unittest.mock.call(31, add=["shinobi:needs-human"]),
                unittest.mock.call(31, remove=["shinobi:reviewing"]),
            ],
        )
        self.assertEqual(client.create_issue_comment.call_count, 0)
        self.assertEqual(client.update_issue_comment.call_count, 2)
        self.assertIn(
            "failed to persist final local state during publish phase",
            client.update_issue_comment.call_args_list[-1].args[1],
        )

    def test_publish_mission_still_comments_when_handoff_label_cleanup_fails(self) -> None:
        store = Mock()
        store.format_timestamp.return_value = "2026-04-09T00:30:00Z"
        config = Config(repo="owner/repo", agent_identity="agent-1")
        state = cli.State(
            issue_number=31,
            branch="feature/issue-31-publish-phase",
            agent_identity="agent-1",
            run_id="run-123",
            phase="start",
        )
        execution_result = ExecutionResult(
            commands=[],
            change_summary="Published mission changes.",
        )

        with patch(
            "shinobi.mission_publish.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ):
            with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.side_effect = [
                    {
                        "number": 31,
                        "labels": [{"name": "shinobi:working"}],
                    },
                    {
                        "number": 31,
                        "labels": [
                            {"name": "shinobi:working"},
                            {"name": "shinobi:reviewing"},
                        ],
                    },
                ]
                client.list_pull_requests_by_head.return_value = []
                client.create_pull_request.return_value = {
                    "number": 44,
                    "url": "https://github.com/owner/repo/pull/44",
                }
                client.update_issue_labels.side_effect = [
                    None,
                    None,
                    None,
                    GitHubClientError("remove failed"),
                ]
                client.list_issue_comments.side_effect = GitHubClientError("comments failed")

                with self.assertRaisesRegex(
                    MissionPublishError,
                    "failed to hand off publish failure: remove failed; failed to upsert publish failure comment for issue #31: comments failed",
                ):
                    publish_mission(
                        root=Path("/tmp/repo"),
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                    )

        client.create_issue_comment.assert_not_called()
        client.update_issue_comment.assert_not_called()
        store.save_state.assert_not_called()

    def test_publish_mission_rejects_non_start_state(self) -> None:
        with self.assertRaisesRegex(
            MissionPublishError,
            "publish phase requires local state phase start",
        ):
            publish_mission(
                root=Path("/tmp/repo"),
                store=Mock(),
                config=Config(repo="owner/repo", agent_identity="agent-1"),
                run_id="run-123",
                state=cli.State(issue_number=31, branch="feature/issue-31", phase="idle"),
                execution_result=Mock(),
            )

    def test_publish_mission_rejects_failed_verification_before_push(self) -> None:
        store = Mock()
        config = Config(repo="owner/repo", agent_identity="agent-1")
        state = cli.State(
            issue_number=31,
            branch="feature/issue-31-publish-phase",
            agent_identity="agent-1",
            run_id="run-123",
            phase="start",
        )
        execution_result = ExecutionResult(
            commands=[
                VerificationCommandResult(
                    name="lint",
                    command=[],
                    status="not_configured",
                ),
                VerificationCommandResult(
                    name="test",
                    command=["python3", "-m", "unittest"],
                    status="failed",
                    returncode=1,
                ),
            ],
            change_summary="Published mission changes.",
        )

        with patch("shinobi.mission_publish.subprocess.run") as run_mock:
            with self.assertRaisesRegex(
                MissionPublishError,
                "test: failed",
            ):
                publish_mission(
                    root=Path("/tmp/repo"),
                    store=store,
                    config=config,
                    run_id="run-123",
                    state=state,
                    execution_result=execution_result,
                )

        run_mock.assert_not_called()
        store.require_lock_owner.assert_not_called()

    def test_publish_mission_rejects_blocking_issue_label_before_push(self) -> None:
        store = Mock()
        config = Config(repo="owner/repo", agent_identity="agent-1")
        state = cli.State(
            issue_number=31,
            branch="feature/issue-31-publish-phase",
            agent_identity="agent-1",
            run_id="run-123",
            phase="start",
        )
        execution_result = ExecutionResult(
            commands=[],
            change_summary="Published mission changes.",
        )

        with patch("shinobi.mission_publish.subprocess.run") as run_mock:
            with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.return_value = {
                    "number": 31,
                    "labels": [{"name": "shinobi:needs-human"}],
                }

                with self.assertRaisesRegex(
                    MissionPublishError,
                    "has blocking label\\(s\\): shinobi:needs-human",
                ):
                    publish_mission(
                        root=Path("/tmp/repo"),
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                    )

        run_mock.assert_not_called()
        client.list_pull_requests_by_head.assert_not_called()
        client.create_pull_request.assert_not_called()
        client.update_issue_labels.assert_not_called()
        client.create_issue_comment.assert_not_called()
        store.save_state.assert_called_once()
        saved_state = store.save_state.call_args.args[0]
        self.assertEqual(saved_state.phase, "idle")
        self.assertEqual(saved_state.last_result, "needs-human")
        self.assertEqual(saved_state.last_mission.issue_number, 31)
        self.assertEqual(saved_state.last_mission.branch, "feature/issue-31-publish-phase")
        self.assertEqual(saved_state.last_mission.phase, "publish")
        self.assertEqual(saved_state.last_mission.conclusion, "needs-human")

    def test_publish_mission_preserves_blocked_label_before_push(self) -> None:
        store = Mock()
        config = Config(repo="owner/repo", agent_identity="agent-1")
        state = cli.State(
            issue_number=31,
            branch="feature/issue-31-publish-phase",
            agent_identity="agent-1",
            run_id="run-123",
            phase="start",
        )
        execution_result = ExecutionResult(
            commands=[],
            change_summary="Published mission changes.",
        )

        with patch("shinobi.mission_publish.subprocess.run") as run_mock:
            with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.return_value = {
                    "number": 31,
                    "labels": [{"name": "shinobi:blocked"}],
                }

                with self.assertRaisesRegex(
                    MissionPublishError,
                    "has blocking label\\(s\\): shinobi:blocked",
                ):
                    publish_mission(
                        root=Path("/tmp/repo"),
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                    )

        run_mock.assert_not_called()
        client.update_issue_labels.assert_not_called()
        client.create_issue_comment.assert_not_called()
        store.save_state.assert_called_once()
        saved_state = store.save_state.call_args.args[0]
        self.assertEqual(saved_state.phase, "idle")
        self.assertEqual(saved_state.last_result, "blocked")
        self.assertEqual(saved_state.last_mission.issue_number, 31)
        self.assertEqual(saved_state.last_mission.branch, "feature/issue-31-publish-phase")
        self.assertEqual(saved_state.last_mission.phase, "publish")
        self.assertEqual(saved_state.last_mission.conclusion, "blocked")

    def test_publish_mission_hands_off_when_push_fails_before_pr_creation(self) -> None:
        store = Mock()
        store.format_timestamp.return_value = "2026-04-09T00:30:00Z"
        config = Config(repo="owner/repo", agent_identity="agent-1")
        state = cli.State(
            issue_number=31,
            branch="feature/issue-31-publish-phase",
            agent_identity="agent-1",
            run_id="run-123",
            phase="start",
        )
        execution_result = ExecutionResult(
            commands=[],
            change_summary="Published mission changes.",
        )

        with patch(
            "shinobi.mission_publish.subprocess.run",
            return_value=subprocess.CompletedProcess(
                args=["git", "push", "-u", "origin", "feature/issue-31-publish-phase"],
                returncode=1,
                stdout="",
                stderr="non-fast-forward",
            ),
        ):
            with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.return_value = {
                    "number": 31,
                    "labels": [{"name": "shinobi:working"}],
                }

                with self.assertRaisesRegex(
                    MissionPublishError,
                    "failed to push branch feature/issue-31-publish-phase: non-fast-forward",
                ):
                    publish_mission(
                        root=Path("/tmp/repo"),
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                    )

        client.list_pull_requests_by_head.assert_not_called()
        client.create_pull_request.assert_not_called()
        self.assertEqual(
            client.update_issue_labels.call_args_list,
            [
                unittest.mock.call(31, add=["shinobi:needs-human"]),
                unittest.mock.call(31, remove=["shinobi:working"]),
            ],
        )
        client.create_issue_comment.assert_called_once()
        self.assertIn(
            "failed to complete publish phase before creating or updating a PR",
            client.create_issue_comment.call_args.args[1],
        )
        store.save_state.assert_called_once()
        saved_state = store.save_state.call_args.args[0]
        self.assertEqual(saved_state.phase, "idle")
        self.assertEqual(saved_state.last_result, "needs-human")
        self.assertEqual(saved_state.last_mission.issue_number, 31)
        self.assertEqual(saved_state.last_mission.branch, "feature/issue-31-publish-phase")
        self.assertEqual(saved_state.last_mission.phase, "publish")
        self.assertEqual(saved_state.last_mission.conclusion, "needs-human")

    def test_publish_mission_rejects_state_from_different_run_before_push(self) -> None:
        store = Mock()
        config = Config(repo="owner/repo", agent_identity="agent-1")
        state = cli.State(
            issue_number=31,
            branch="feature/issue-31-publish-phase",
            agent_identity="agent-1",
            run_id="previous-run",
            phase="start",
        )
        execution_result = ExecutionResult(
            commands=[],
            change_summary="Published mission changes.",
        )

        with patch("shinobi.mission_publish.subprocess.run") as run_mock:
            with self.assertRaisesRegex(
                MissionPublishError,
                "publish phase requires local state run_id run-123",
            ):
                publish_mission(
                    root=Path("/tmp/repo"),
                    store=store,
                    config=config,
                    run_id="run-123",
                    state=state,
                    execution_result=execution_result,
                )

        run_mock.assert_not_called()
        store.require_lock_owner.assert_not_called()

    def test_publish_mission_rejects_state_from_different_agent_before_push(self) -> None:
        store = Mock()
        config = Config(repo="owner/repo", agent_identity="agent-1")
        state = cli.State(
            issue_number=31,
            branch="feature/issue-31-publish-phase",
            agent_identity="agent-2",
            run_id="run-123",
            phase="start",
        )
        execution_result = ExecutionResult(
            commands=[],
            change_summary="Published mission changes.",
        )

        with patch("shinobi.mission_publish.subprocess.run") as run_mock:
            with self.assertRaisesRegex(
                MissionPublishError,
                "publish phase requires local state agent_identity agent-1",
            ):
                publish_mission(
                    root=Path("/tmp/repo"),
                    store=store,
                    config=config,
                    run_id="run-123",
                    state=state,
                    execution_result=execution_result,
                )

        run_mock.assert_not_called()
        store.require_lock_owner.assert_not_called()

    def test_find_mission_state_comment_matches_issue_and_branch(self) -> None:
        comment = find_mission_state_comment(
            [
                {
                    "id": 1,
                    "body": (
                        "<!-- shinobi:mission-state\n"
                        "issue: 30\n"
                        "branch: feature/issue-30-other\n"
                        "-->"
                    ),
                },
                {
                    "id": 2,
                    "body": (
                        "<!-- shinobi:mission-state\n"
                        "issue: 31\n"
                        "branch: feature/issue-31-publish-phase\n"
                        "-->"
                    ),
                },
            ],
            issue_number=31,
            branch="feature/issue-31-publish-phase",
        )

        self.assertEqual(comment["id"], 2)

    def test_find_mission_state_comment_does_not_use_partial_issue_match(self) -> None:
        comment = find_mission_state_comment(
            [
                {
                    "id": 1,
                    "body": (
                        "<!-- shinobi:mission-state\n"
                        "issue: 31\n"
                        "branch: feature/issue-31-publish-phase\n"
                        "-->"
                    ),
                },
            ],
            issue_number=3,
            branch="feature/issue-31-publish-phase",
        )

        self.assertIsNone(comment)

    def test_publish_mission_hands_off_when_pr_lookup_fails_after_push(self) -> None:
        store = Mock()
        config = Config(repo="owner/repo", agent_identity="agent-1")
        state = cli.State(
            issue_number=31,
            branch="feature/issue-31-publish-phase",
            agent_identity="agent-1",
            run_id="run-123",
            phase="start",
        )
        execution_result = ExecutionResult(
            commands=[],
            change_summary="Published mission changes.",
        )

        with patch(
            "shinobi.mission_publish.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ):
            with patch("shinobi.mission_publish.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.side_effect = [
                    {
                        "number": 31,
                        "labels": [{"name": "shinobi:working"}],
                    },
                    {
                        "number": 31,
                        "labels": [{"name": "shinobi:working"}],
                    },
                ]
                client.list_pull_requests_by_head.side_effect = GitHubClientError(
                    "api unavailable"
                )

                with self.assertRaisesRegex(
                    MissionPublishError,
                    "failed to look up existing PR for issue #31",
                ):
                    publish_mission(
                        root=Path("/tmp/repo"),
                        store=store,
                        config=config,
                        run_id="run-123",
                        state=state,
                        execution_result=execution_result,
                    )

        client.create_pull_request.assert_not_called()
        self.assertEqual(
            client.update_issue_labels.call_args_list,
            [
                unittest.mock.call(31, add=["shinobi:needs-human"]),
                unittest.mock.call(31, remove=["shinobi:working"]),
            ],
        )
        client.create_issue_comment.assert_called_once()
        self.assertIn(
            "failed to complete publish phase after pushing branch",
            client.create_issue_comment.call_args.args[1],
        )
        self.assertIn(
            "Branch: `feature/issue-31-publish-phase`",
            client.create_issue_comment.call_args.args[1],
        )
        store.save_state.assert_called_once()
        saved_state = store.save_state.call_args.args[0]
        self.assertEqual(saved_state.phase, "idle")
        self.assertEqual(saved_state.last_result, "needs-human")
        self.assertEqual(saved_state.last_mission.issue_number, 31)
        self.assertIsNone(saved_state.last_mission.pr_number)
        self.assertEqual(saved_state.last_mission.branch, "feature/issue-31-publish-phase")
        self.assertEqual(saved_state.last_mission.phase, "publish")
        self.assertEqual(saved_state.last_mission.conclusion, "needs-human")

    def test_parse_mission_state_fields_reads_marker_only(self) -> None:
        fields = parse_mission_state_fields(
            "issue: 999\n"
            "<!-- shinobi:mission-state\n"
            "issue: 31\n"
            "branch: feature/issue-31-publish-phase\n"
            "phase: publish\n"
            "-->\n"
        )

        self.assertEqual(fields["issue"], "31")
        self.assertEqual(fields["branch"], "feature/issue-31-publish-phase")
        self.assertEqual(fields["phase"], "publish")

    def test_build_same_repo_head_selector_uses_plain_branch_name(self) -> None:
        self.assertEqual(
            build_same_repo_head_selector("owner/repo", "feature/issue-31-publish-phase"),
            "feature/issue-31-publish-phase",
        )

    def test_build_same_repo_head_selector_falls_back_without_owner(self) -> None:
        self.assertEqual(
            build_same_repo_head_selector("owner-repo", "feature/issue-31-publish-phase"),
            "feature/issue-31-publish-phase",
        )


class MissionFinalizeTest(unittest.TestCase):
    def test_finalize_merged_normalizes_labels_closes_issue_and_clears_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            config = Config(repo="owner/repo", agent_identity="owner/repo#default@host-12345678")
            store.initialize()
            run_id = "run-123"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            store.save_lock(
                store.acquire_lock(config=config, run_id=run_id, pid=123, now=now)[0]
            )
            state = State(
                issue_number=32,
                pr_number=45,
                branch="feature/issue-32-finalize-phase",
                agent_identity=config.agent_identity,
                run_id=run_id,
                phase="publish",
                review_loop_count=1,
                lease_expires_at="2026-04-11T01:00:00Z",
                last_result="merged",
            )

            with patch("shinobi.mission_finalize.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.return_value = {
                    "number": 32,
                    "labels": [
                        {"name": "shinobi:reviewing"},
                        {"name": "shinobi:risky"},
                    ],
                }

                finalized = finalize_mission(
                    root=root,
                    store=store,
                    config=config,
                    run_id=run_id,
                    state=state,
                    conclusion="merged",
                )

            self.assertEqual(finalized.issue_number, 32)
            self.assertEqual(finalized.pr_number, 45)
            self.assertEqual(finalized.conclusion, "merged")
            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(32, add=["shinobi:merged"]),
                    unittest.mock.call(32, remove=["shinobi:reviewing"]),
                ],
            )
            client.create_issue_comment.assert_called_once()
            client.close_issue.assert_called_once_with(32)
            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "merged")
            self.assertIsNone(saved_state.last_error)
            self.assertEqual(saved_state.last_mission.issue_number, 32)
            self.assertEqual(saved_state.last_mission.pr_number, 45)
            self.assertEqual(saved_state.last_mission.branch, "feature/issue-32-finalize-phase")
            self.assertEqual(saved_state.last_mission.phase, "finalize")
            self.assertEqual(saved_state.last_mission.conclusion, "merged")
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_finalize_needs_human_uses_last_mission_without_closing_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            config = Config(repo="owner/repo", agent_identity="owner/repo#default@host-12345678")
            store.initialize()
            run_id = "run-123"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            store.save_lock(
                store.acquire_lock(config=config, run_id=run_id, pid=123, now=now)[0]
            )
            state = State(
                issue_number=None,
                pr_number=None,
                branch=None,
                agent_identity=config.agent_identity,
                run_id=None,
                phase="idle",
                last_result="needs-human",
                last_error="verification failed",
                last_mission=MissionSummary(
                    issue_number=32,
                    pr_number=45,
                    branch="feature/issue-32-finalize-phase",
                    phase="publish",
                    conclusion="needs-human",
                ),
            )

            with patch("shinobi.mission_finalize.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.return_value = {
                    "number": 32,
                    "labels": [
                        {"name": "shinobi:reviewing"},
                        {"name": "shinobi:risky"},
                    ],
                }

                finalized = finalize_mission(
                    root=root,
                    store=store,
                    config=config,
                    run_id=run_id,
                    state=state,
                    conclusion="needs-human",
                    reason="manual follow-up is required",
                )

            self.assertEqual(finalized.issue_number, 32)
            self.assertEqual(finalized.pr_number, 45)
            self.assertEqual(finalized.conclusion, "needs-human")
            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(32, add=["shinobi:needs-human"]),
                    unittest.mock.call(32, remove=["shinobi:reviewing"]),
                ],
            )
            client.close_issue.assert_not_called()
            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "needs-human")
            self.assertEqual(saved_state.last_error, "manual follow-up is required")
            self.assertEqual(saved_state.last_mission.phase, "finalize")
            self.assertEqual(saved_state.last_mission.conclusion, "needs-human")
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_finalize_keeps_lock_when_comment_creation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            config = Config(repo="owner/repo", agent_identity="owner/repo#default@host-12345678")
            store.initialize()
            run_id = "run-123"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            store.save_lock(
                store.acquire_lock(config=config, run_id=run_id, pid=123, now=now)[0]
            )
            state = State(
                issue_number=32,
                pr_number=45,
                branch="feature/issue-32-finalize-phase",
                agent_identity=config.agent_identity,
                run_id=run_id,
                phase="publish",
            )

            with patch("shinobi.mission_finalize.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.return_value = {
                    "number": 32,
                    "labels": [{"name": "shinobi:reviewing"}],
                }
                client.create_issue_comment.side_effect = GitHubClientError("api unavailable")

                with self.assertRaisesRegex(
                    MissionFinalizeError,
                    "failed to create finalize comment on issue #32",
                ):
                    finalize_mission(
                        root=root,
                        store=store,
                        config=config,
                        run_id=run_id,
                        state=state,
                        conclusion="blocked",
                        reason="human approval required",
                    )

            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "initialized")
            self.assertIn(run_id, store.paths.lock_path.read_text(encoding="utf-8"))

    def test_render_finalize_comment_includes_reason_and_branch(self) -> None:
        rendered = render_finalize_comment(
            issue_number=32,
            pr_number=45,
            branch="feature/issue-32-finalize-phase",
            conclusion="needs-human",
            reason="manual follow-up is required",
        )

        self.assertIn("Shinobi Finalize: needs-human", rendered)
        self.assertIn("任務 #32", rendered)
        self.assertIn("`needs-human`", rendered)
        self.assertIn("feature/issue-32-finalize-phase", rendered)
        self.assertIn("PR", rendered.upper())
        self.assertIn("manual follow-up is required", rendered)

    def test_finalize_accepts_custom_terminal_label_value_but_persists_canonical_conclusion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            config = Config(
                repo="owner/repo",
                agent_identity="owner/repo#default@host-12345678",
                labels={
                    "ready": "label:ready",
                    "working": "label:working",
                    "reviewing": "label:reviewing",
                    "blocked": "label:blocked",
                    "needs_human": "label:needs-human",
                    "merged": "label:merged",
                    "risky": "label:risky",
                },
            )
            store.initialize()
            run_id = "run-123"
            now = datetime.now(timezone.utc).replace(microsecond=0)
            store.save_lock(
                store.acquire_lock(config=config, run_id=run_id, pid=123, now=now)[0]
            )
            state = State(
                issue_number=32,
                pr_number=45,
                branch="feature/issue-32-finalize-phase",
                agent_identity=config.agent_identity,
                run_id=run_id,
                phase="publish",
            )

            with patch("shinobi.mission_finalize.GitHubClient") as client_cls:
                client = client_cls.return_value
                client.get_issue.return_value = {
                    "number": 32,
                    "labels": [{"name": "label:reviewing"}],
                }

                finalized = finalize_mission(
                    root=root,
                    store=store,
                    config=config,
                    run_id=run_id,
                    state=state,
                    conclusion="label:needs-human",
                )

            self.assertEqual(finalized.conclusion, "needs-human")
            self.assertEqual(
                client.update_issue_labels.call_args_list,
                [
                    unittest.mock.call(32, add=["label:needs-human"]),
                    unittest.mock.call(32, remove=["label:reviewing"]),
                ],
            )
            saved_state = store.load_state()
            self.assertEqual(saved_state.last_result, "needs-human")
            self.assertEqual(saved_state.last_mission.conclusion, "needs-human")


class MissionLifecycleIntegrationTest(unittest.TestCase):
    def test_lifecycle_happy_path_transitions_start_publish_and_finalize(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                config, _ = store.initialize()

            run_id = "run-123"
            now = datetime(2026, 4, 11, 0, 0, tzinfo=timezone.utc)
            store.save_lock(store.acquire_lock(config=config, run_id=run_id, pid=123, now=now)[0])

            issue = {
                "number": 40,
                "title": "[TASK] mission lifecycle の結合テストを追加する",
                "state": "OPEN",
                "labels": [
                    {"name": "priority:low"},
                    {"name": config.labels["ready"]},
                ],
            }
            fake_client = FakeGitHubClient(
                issue_number=40,
                title=str(issue["title"]),
                labels=["priority:low", config.labels["ready"]],
            )
            execution_result = ExecutionResult(
                commands=[
                    VerificationCommandResult(
                        name="lint",
                        command=["ruff", "check", "."],
                        status="passed",
                        returncode=0,
                    )
                ],
                change_summary="Add lifecycle integration tests.",
            )
            git_success = subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout="",
                stderr="",
            )

            with patch("shinobi.mission_start.GitHubClient", return_value=fake_client):
                with patch("shinobi.mission_publish.GitHubClient", return_value=fake_client):
                    with patch("shinobi.mission_finalize.GitHubClient", return_value=fake_client):
                        with patch("shinobi.mission_start.subprocess.run", return_value=git_success):
                            with patch(
                                "shinobi.mission_publish.subprocess.run",
                                return_value=git_success,
                            ):
                                started = start_mission(
                                    root=root,
                                    store=store,
                                    config=config,
                                    run_id=run_id,
                                    issue=issue,
                                    now=now,
                                )
                                started_comment = fake_client.comments[0]["body"]
                                published = publish_mission(
                                    root=root,
                                    store=store,
                                    config=config,
                                    run_id=run_id,
                                    state=store.load_state(),
                                    execution_result=execution_result,
                                    now=now,
                                )
                                finalized = finalize_mission(
                                    root=root,
                                    store=store,
                                    config=config,
                                    run_id=run_id,
                                    state=store.load_state(),
                                    conclusion="merged",
                                )

            self.assertEqual(started.issue_number, 40)
            self.assertEqual(published.pr_number, 40)
            self.assertEqual(finalized.conclusion, "merged")
            self.assertEqual(fake_client.issue_state, "CLOSED")
            self.assertEqual(fake_client.closed_issues, [40])
            self.assertEqual(fake_client.issue_labels, {"priority:low", config.labels["merged"]})
            self.assertEqual(
                fake_client.label_operations,
                [
                    ("add", 40, (config.labels["working"],)),
                    ("remove", 40, (config.labels["ready"],)),
                    ("add", 40, (config.labels["reviewing"],)),
                    ("remove", 40, (config.labels["working"],)),
                    ("add", 40, (config.labels["merged"],)),
                    ("remove", 40, (config.labels["reviewing"],)),
                ],
            )
            self.assertEqual(len(fake_client.comments), 2)
            self.assertIn("Shinobi Publish", str(fake_client.comments[0]["body"]))
            self.assertIn("Shinobi Finalize: merged", str(fake_client.comments[1]["body"]))
            self.assertIn("Shinobi Start", str(started_comment))
            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "merged")
            self.assertEqual(saved_state.last_mission.issue_number, 40)
            self.assertEqual(saved_state.last_mission.pr_number, 40)
            self.assertEqual(saved_state.last_mission.phase, "finalize")
            self.assertEqual(saved_state.last_mission.conclusion, "merged")
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_lifecycle_publish_failure_hands_off_to_needs_human(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
                config, _ = store.initialize()

            run_id = "run-123"
            now = datetime(2026, 4, 11, 0, 0, tzinfo=timezone.utc)
            store.save_lock(store.acquire_lock(config=config, run_id=run_id, pid=123, now=now)[0])

            issue = {
                "number": 40,
                "title": "[TASK] mission lifecycle の結合テストを追加する",
                "state": "OPEN",
                "labels": [{"name": config.labels["ready"]}],
            }
            fake_client = FakeGitHubClient(
                issue_number=40,
                title=str(issue["title"]),
                labels=[config.labels["ready"]],
            )
            fake_client.raise_on_list_pull_requests = GitHubClientError("lookup failed")
            execution_result = ExecutionResult(
                commands=[
                    VerificationCommandResult(
                        name="test",
                        command=["python3", "-m", "unittest"],
                        status="passed",
                        returncode=0,
                    )
                ],
                change_summary="Add lifecycle integration tests.",
            )
            git_success = subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout="",
                stderr="",
            )

            with patch("shinobi.mission_start.GitHubClient", return_value=fake_client):
                with patch("shinobi.mission_publish.GitHubClient", return_value=fake_client):
                    with patch("shinobi.mission_start.subprocess.run", return_value=git_success):
                        with patch(
                            "shinobi.mission_publish.subprocess.run",
                            return_value=git_success,
                        ):
                            start_mission(
                                root=root,
                                store=store,
                                config=config,
                                run_id=run_id,
                                issue=issue,
                                now=now,
                            )
                            with self.assertRaisesRegex(
                                MissionPublishError,
                                "failed to look up existing PR for issue #40: lookup failed",
                            ):
                                publish_mission(
                                    root=root,
                                    store=store,
                                    config=config,
                                    run_id=run_id,
                                    state=store.load_state(),
                                    execution_result=execution_result,
                                    now=now,
                                )

            self.assertEqual(fake_client.issue_state, "OPEN")
            self.assertEqual(fake_client.issue_labels, {config.labels["needs_human"]})
            self.assertEqual(len(fake_client.comments), 2)
            self.assertIn("Shinobi Start", str(fake_client.comments[0]["body"]))
            self.assertIn("failed to look up existing PR", str(fake_client.comments[1]["body"]))
            saved_state = store.load_state()
            self.assertEqual(saved_state.phase, "idle")
            self.assertEqual(saved_state.last_result, "needs-human")
            self.assertIn("lookup failed", str(saved_state.last_error))
            self.assertEqual(saved_state.last_mission.issue_number, 40)
            self.assertEqual(saved_state.last_mission.phase, "publish")
            self.assertEqual(saved_state.last_mission.conclusion, "needs-human")
            self.assertIn(run_id, store.paths.lock_path.read_text(encoding="utf-8"))


class ContextBuilderTest(unittest.TestCase):
    def test_build_mission_context_reads_issue_and_local_knowledge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.summary_path.write_text("summary text\n", encoding="utf-8")
            store.paths.decisions_path.write_text("decision text\n", encoding="utf-8")

            context = build_mission_context(
                root,
                {
                    "number": 28,
                    "title": "[TASK] context_builder を実装する",
                    "body": (
                        "## 目的\n"
                        "Issue とローカル知識から最小実行コンテキストを生成する。\n\n"
                        "## 対象\n"
                        "- `src/shinobi/context_builder.py`\n"
                        "- `tests/test_cli.py`\n\n"
                        "## 要件\n"
                        "- Issue 本文を読む\n"
                        "- `.shinobi/summary.md` を読む\n\n"
                        "## 完了条件\n"
                        "- context builder が構造化データを返す\n\n"
                        "## スコープ外\n"
                        "- AI 実装エージェント呼び出し\n"
                    ),
                },
            )

        self.assertEqual(context.issue_number, 28)
        self.assertEqual(
            context.mission_summary,
            "Issue とローカル知識から最小実行コンテキストを生成する。",
        )
        self.assertEqual(
            context.candidate_files,
            [
                "src/shinobi/context_builder.py",
                "tests/test_cli.py",
            ],
        )
        self.assertEqual(
            context.reference_files,
            [
                ".shinobi/summary.md",
                ".shinobi/decisions.md",
                "src/shinobi/context_builder.py",
                "tests/test_cli.py",
            ],
        )
        self.assertEqual(context.summary, "summary text\n")
        self.assertEqual(context.decisions, "decision text\n")
        self.assertEqual(
            context.completion_criteria,
            ["context builder が構造化データを返す"],
        )
        self.assertEqual(
            context.prohibited_actions,
            ["Do not include: AI 実装エージェント呼び出し"],
        )
        self.assertFalse(context.needs_human_review)

    def test_build_mission_context_treats_missing_knowledge_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 28,
                    "title": "Context task",
                    "body": "## 対象\n- `src/shinobi/context_builder.py`\n",
                },
            )

        self.assertEqual(context.summary, "")
        self.assertEqual(context.decisions, "")
        self.assertEqual(context.mission_summary, "Context task")

    def test_build_mission_context_excludes_local_knowledge_from_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 28,
                    "title": "Context task",
                    "body": "## 要件\n- `.shinobi/summary.md` を読む\n- `docs/mvp-design.md` を読む\n",
                },
            )

        self.assertEqual(context.candidate_files, ["docs/mvp-design.md"])
        self.assertIn(".shinobi/summary.md", context.reference_files)

    def test_build_mission_context_excludes_relative_local_knowledge_from_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 28,
                    "title": "Context task",
                    "body": (
                        "## 要件\n"
                        "- `./.shinobi/summary.md` を読む\n"
                        "- `./docs/mvp-design.md` を読む\n"
                    ),
                },
            )

        self.assertEqual(context.candidate_files, ["docs/mvp-design.md"])
        self.assertEqual(
            context.reference_files,
            [
                ".shinobi/summary.md",
                ".shinobi/decisions.md",
                "docs/mvp-design.md",
            ],
        )

    def test_build_mission_context_falls_back_when_targets_are_only_local_knowledge(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 28,
                    "title": "Context task",
                    "body": (
                        "## 対象\n"
                        "- `.shinobi/summary.md`\n\n"
                        "## 要件\n"
                        "- `src/shinobi/context_builder.py` を更新\n"
                    ),
                },
            )

        self.assertEqual(context.candidate_files, ["src/shinobi/context_builder.py"])
        self.assertFalse(context.needs_human_review)

    def test_build_mission_context_keeps_repeated_sections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 28,
                    "title": "Context task",
                    "body": (
                        "## 要件\n"
                        "- `src/shinobi/context_builder.py` を追加\n\n"
                        "## 要件\n"
                        "- `tests/test_cli.py` を更新\n"
                    ),
                },
            )

        self.assertEqual(
            context.requirements,
            [
                "`src/shinobi/context_builder.py` を追加",
                "`tests/test_cli.py` を更新",
            ],
        )
        self.assertEqual(
            context.candidate_files,
            [
                "src/shinobi/context_builder.py",
                "tests/test_cli.py",
            ],
        )

    def test_build_mission_context_flags_missing_candidate_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 99,
                    "title": "Broad task",
                    "body": "## 目的\nいい感じに直す\n",
                },
            )

        self.assertEqual(context.candidate_files, [])
        self.assertTrue(context.needs_human_review)
        self.assertEqual(
            context.needs_human_review_reason,
            "issue body does not name candidate files",
        )

    def test_build_mission_context_does_not_fallback_to_scope_out_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 99,
                    "title": "Scoped task",
                    "body": (
                        "## スコープ外\n"
                        "- `src/shinobi/context_builder.py` は変更しない\n\n"
                        "## 禁止事項\n"
                        "- `docs/architecture.md` を編集しない\n"
                    ),
                },
            )

        self.assertEqual(context.candidate_files, [])
        self.assertEqual(
            context.reference_files,
            [".shinobi/summary.md", ".shinobi/decisions.md"],
        )
        self.assertTrue(context.needs_human_review)
        self.assertEqual(
            context.needs_human_review_reason,
            "issue body does not name candidate files",
        )

    def test_build_mission_context_ignores_scope_out_paths_as_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 99,
                    "title": "Scoped task",
                    "body": (
                        "## 目的\n"
                        "小さな修正を行う\n\n"
                        "## スコープ外\n"
                        "- `src/shinobi/context_builder.py` は変更しない\n\n"
                        "## 禁止事項\n"
                        "- `docs/architecture.md` を編集しない\n"
                    ),
                },
            )

        self.assertEqual(context.candidate_files, [])
        self.assertEqual(
            context.reference_files,
            [".shinobi/summary.md", ".shinobi/decisions.md"],
        )
        self.assertTrue(context.needs_human_review)
        self.assertEqual(
            context.needs_human_review_reason,
            "issue body does not name candidate files",
        )

    def test_build_mission_context_does_not_flag_negative_repo_wide_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 28,
                    "title": "Context task",
                    "body": (
                        "## 対象\n"
                        "- `src/shinobi/context_builder.py`\n\n"
                        "## 完了条件\n"
                        "- repo 全体を読まない前提がコードで守られている\n"
                    ),
                },
            )

        self.assertFalse(context.needs_human_review)
        self.assertIsNone(context.needs_human_review_reason)

    def test_build_mission_context_does_not_flag_scope_out_broad_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 28,
                    "title": "Context task",
                    "body": (
                        "## 対象\n"
                        "- `src/shinobi/context_builder.py`\n\n"
                        "## スコープ外\n"
                        "- repo 全体を変更すること\n"
                    ),
                },
            )

        self.assertFalse(context.needs_human_review)
        self.assertIsNone(context.needs_human_review_reason)
        self.assertEqual(
            context.prohibited_actions,
            ["Do not include: repo 全体を変更すること"],
        )

    def test_build_mission_context_flags_explicit_broad_scope_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            context = build_mission_context(
                Path(tmp_dir),
                {
                    "number": 99,
                    "title": "Broad task",
                    "body": (
                        "## 対象\n"
                        "- `src/shinobi/context_builder.py`\n\n"
                        "## 注意点\n"
                        "- repository-wide cleanup を含む\n"
                    ),
                },
            )

        self.assertTrue(context.needs_human_review)
        self.assertEqual(
            context.needs_human_review_reason,
            "issue body contains broad scope marker: repository-wide",
        )


class ExecutorTest(unittest.TestCase):
    def test_run_verification_command_returns_passed_result(self) -> None:
        response = subprocess.CompletedProcess(
            args=["python3", "-m", "unittest"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

        with patch("shinobi.executor.subprocess.run", return_value=response) as run_mock:
            result = run_verification_command(
                Path("/tmp/repo"),
                "test",
                ["python3", "-m", "unittest"],
            )

        self.assertEqual(result.name, "test")
        self.assertEqual(result.command, ["python3", "-m", "unittest"])
        self.assertEqual(result.status, "passed")
        self.assertTrue(result.succeeded)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok\n")
        run_mock.assert_called_once()
        self.assertEqual(run_mock.call_args.kwargs["cwd"], Path("/tmp/repo"))
        self.assertFalse(run_mock.call_args.kwargs["check"])

    def test_run_verification_command_returns_failed_result(self) -> None:
        response = subprocess.CompletedProcess(
            args=["python3", "-m", "unittest"],
            returncode=1,
            stdout="",
            stderr="failed\n",
        )

        with patch("shinobi.executor.subprocess.run", return_value=response):
            result = run_verification_command(
                Path("/tmp/repo"),
                "test",
                ["python3", "-m", "unittest"],
            )

        self.assertEqual(result.status, "failed")
        self.assertFalse(result.succeeded)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "failed\n")

    def test_run_verification_command_reports_missing_command_as_not_configured(self) -> None:
        result = run_verification_command(Path("/tmp/repo"), "lint", [])

        self.assertEqual(result.name, "lint")
        self.assertEqual(result.command, [])
        self.assertEqual(result.status, "not_configured")
        self.assertFalse(result.succeeded)
        self.assertIn("not configured", result.message or "")

    def test_run_verification_command_reports_os_error(self) -> None:
        with patch("shinobi.executor.subprocess.run", side_effect=OSError("missing binary")):
            result = run_verification_command(Path("/tmp/repo"), "lint", ["missing-lint"])

        self.assertEqual(result.status, "error")
        self.assertFalse(result.succeeded)
        self.assertIn("missing binary", result.message or "")

    def test_execute_verification_runs_commands_in_stable_order(self) -> None:
        config = Config(
            repo="owner/repo",
            verification_commands={
                "lint": ["lint-command"],
                "typecheck": ["typecheck-command"],
                "test": ["test-command"],
            },
        )
        responses = [
            subprocess.CompletedProcess(args=["lint-command"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["typecheck-command"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["test-command"], returncode=1, stdout="", stderr="failed"),
        ]

        with patch("shinobi.executor.subprocess.run", side_effect=responses) as run_mock:
            result = execute_verification(Path("/tmp/repo"), config)

        self.assertEqual([command.name for command in result.commands], ["lint", "typecheck", "test"])
        self.assertEqual([command.status for command in result.commands], ["passed", "passed", "failed"])
        self.assertFalse(result.succeeded)
        self.assertIn("No automated code changes", result.change_summary)
        self.assertEqual(
            [call.args[0] for call in run_mock.call_args_list],
            [["lint-command"], ["typecheck-command"], ["test-command"]],
        )

    def test_collect_changed_paths_wraps_git_failures(self) -> None:
        with patch(
            "shinobi.executor.subprocess.run",
            return_value=Mock(returncode=1, stdout="", stderr="unknown revision"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "failed to collect changed paths against main: unknown revision",
            ):
                collect_changed_paths(Path("/tmp/repo"), base_ref="main")

    def test_collect_changed_paths_prefers_origin_main_before_local_main(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="M\tauth/login.py\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff", "--cached"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "ls-files"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        ]

        with patch("shinobi.executor.subprocess.run", side_effect=responses) as run_mock:
            changed = collect_changed_paths(Path("/tmp/repo"), base_ref="main")

        self.assertEqual(changed, ["auth/login.py"])
        self.assertEqual(
            run_mock.call_args_list[0].args[0],
            ["git", "diff", "--name-status", "--diff-filter=ACMRD", "origin/main...HEAD"],
        )

    def test_collect_changed_paths_falls_back_to_local_main_when_origin_main_is_missing(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=1,
                stdout="",
                stderr="unknown revision",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="M\tauth/login.py\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff", "--cached"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "ls-files"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        ]

        with patch("shinobi.executor.subprocess.run", side_effect=responses) as run_mock:
            changed = collect_changed_paths(Path("/tmp/repo"), base_ref="main")

        self.assertEqual(changed, ["auth/login.py"])
        self.assertEqual(
            run_mock.call_args_list[0].args[0],
            ["git", "diff", "--name-status", "--diff-filter=ACMRD", "origin/main...HEAD"],
        )
        self.assertEqual(
            run_mock.call_args_list[1].args[0],
            ["git", "diff", "--name-status", "--diff-filter=ACMRD", "main...HEAD"],
        )

    def test_collect_changed_paths_includes_deleted_staged_unstaged_and_untracked_files(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="M\tsrc/app.py\nD\tauth/deleted.py\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff", "--cached"],
                returncode=0,
                stdout="M\tauth/staged.py\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="M\tauth/working_tree.py\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "ls-files"],
                returncode=0,
                stdout="auth/new_file.py\n",
                stderr="",
            ),
        ]

        with patch("shinobi.executor.subprocess.run", side_effect=responses):
            changed = collect_changed_paths(Path("/tmp/repo"), base_ref="main")

        self.assertEqual(
            changed,
            [
                "auth/deleted.py",
                "auth/new_file.py",
                "auth/staged.py",
                "auth/working_tree.py",
                "src/app.py",
            ],
        )

    def test_collect_changed_paths_includes_both_paths_for_renames_and_copies(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="R100\tauth/legacy.py\tsrc/legacy.py\nC100\tbilling/base.py\tbilling/copied.py\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff", "--cached"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "ls-files"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        ]

        with patch("shinobi.executor.subprocess.run", side_effect=responses):
            changed = collect_changed_paths(Path("/tmp/repo"), base_ref="main")

        self.assertEqual(
            changed,
            [
                "auth/legacy.py",
                "billing/base.py",
                "billing/copied.py",
                "src/legacy.py",
            ],
        )

    def test_find_high_risk_paths_matches_directory_prefixes(self) -> None:
        matched = find_high_risk_paths(
            changed_paths=["src/app.py", "auth/login.py", "billing/invoice.py"],
            high_risk_paths=["auth/", "billing/", "infra/"],
        )

        self.assertEqual(matched, ["auth/", "billing/"])

    def test_find_high_risk_paths_preserves_dot_prefixed_paths(self) -> None:
        matched = find_high_risk_paths(
            changed_paths=[".github/workflows/release.yml", "src/app.py"],
            high_risk_paths=[".github/", "auth/"],
        )

        self.assertEqual(matched, [".github/"])

    def test_detect_high_risk_stop_returns_structured_decision(self) -> None:
        response = subprocess.CompletedProcess(
            args=["git", "diff"],
            returncode=0,
            stdout="M\tsrc/app.py\nM\tauth/login.py\n",
            stderr="",
        )

        with patch("shinobi.executor.subprocess.run", return_value=response):
            decision = detect_high_risk_stop(
                Path("/tmp/repo"),
                Config(repo="owner/repo", high_risk_paths=["auth/", "billing/"]),
            )

        self.assertEqual(
            decision,
            StopDecision(
                reason=(
                    "Shinobi stopped before publish because changed files match "
                    "high-risk path(s): auth/ (files: auth/login.py)"
                ),
                conclusion="needs-human",
                retryable=False,
                changed_paths=["auth/login.py"],
                matched_paths=["auth/"],
            ),
        )

    def test_detect_high_risk_stop_returns_none_for_safe_paths(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="M\tsrc/app.py\nM\ttests/test_cli.py\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff", "--cached"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "ls-files"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        ]

        with patch("shinobi.executor.subprocess.run", side_effect=responses):
            decision = detect_high_risk_stop(
                Path("/tmp/repo"),
                Config(repo="owner/repo", high_risk_paths=["auth/", "billing/"]),
            )

        self.assertIsNone(decision)

    def test_detect_high_risk_stop_detects_deleted_and_untracked_high_risk_paths(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="M\tsrc/app.py\nD\tauth/deleted.py\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff", "--cached"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "ls-files"],
                returncode=0,
                stdout="auth/new_file.py\n",
                stderr="",
            ),
        ]

        with patch("shinobi.executor.subprocess.run", side_effect=responses):
            decision = detect_high_risk_stop(
                Path("/tmp/repo"),
                Config(repo="owner/repo", high_risk_paths=["auth/", "billing/"]),
            )

        self.assertEqual(
            decision,
            StopDecision(
                reason=(
                    "Shinobi stopped before publish because changed files match "
                    "high-risk path(s): auth/ (files: auth/deleted.py, auth/new_file.py)"
                ),
                conclusion="needs-human",
                retryable=False,
                changed_paths=["auth/deleted.py", "auth/new_file.py"],
                matched_paths=["auth/"],
            ),
        )

    def test_detect_high_risk_stop_detects_renamed_paths_moved_out_of_high_risk_directory(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="R100\tauth/login.py\tsrc/login.py\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff", "--cached"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "diff"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["git", "ls-files"],
                returncode=0,
                stdout="",
                stderr="",
            ),
        ]

        with patch("shinobi.executor.subprocess.run", side_effect=responses):
            decision = detect_high_risk_stop(
                Path("/tmp/repo"),
                Config(repo="owner/repo", high_risk_paths=["auth/", "billing/"]),
            )

        self.assertEqual(
            decision,
            StopDecision(
                reason=(
                    "Shinobi stopped before publish because changed files match "
                    "high-risk path(s): auth/ (files: auth/login.py)"
                ),
                conclusion="needs-human",
                retryable=False,
                changed_paths=["auth/login.py"],
                matched_paths=["auth/"],
            ),
        )

    def test_config_preserves_custom_verification_commands(self) -> None:
        config = Config.from_dict(
            {
                "repo": "owner/repo",
                "agent_identity": "agent",
                "verification_commands": {
                    "lint": ["ruff", "check", "."],
                    "test": ["python3", "-m", "unittest"],
                },
            }
        )

        self.assertEqual(config.verification_commands["lint"], ["ruff", "check", "."])
        self.assertEqual(config.verification_commands["test"], ["python3", "-m", "unittest"])
        self.assertEqual(
            config.verification_commands["typecheck"],
            [
                "env",
                "PYTHONPYCACHEPREFIX=/tmp/pycache",
                "python3",
                "-m",
                "compileall",
                "src",
                "tests",
            ],
        )


class GitHubClientTest(unittest.TestCase):
    def test_get_issue_surfaces_parse_failure_with_context(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "api", "repos/owner/repo/issues/6"],
            returncode=0,
            stdout="{broken",
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=result):
                client = GitHubClient(Path("/tmp/repo"))
                with self.assertRaisesRegex(
                    GitHubClientError,
                    "failed to parse GitHub response while trying to load issue #6",
                ):
                    client.get_issue(6)

    def test_get_ci_status_accepts_pending_exit_code_from_gh_pr_checks(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "pr", "checks", "44"],
            returncode=8,
            stdout=json.dumps(
                [
                    {
                        "name": "test",
                        "state": "IN_PROGRESS",
                        "bucket": "pending",
                        "link": "https://ci/test",
                    }
                ]
            ),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=result) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                status = client.get_ci_status(44)

        self.assertEqual(
            status,
            [
                {
                    "name": "test",
                    "state": "IN_PROGRESS",
                    "bucket": "pending",
                    "link": "https://ci/test",
                }
            ],
        )
        command = run_mock.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "pr", "checks", "44"])
        self.assertEqual(command[-2:], ["--repo", "owner/repo"])

    def test_get_ci_status_treats_no_checks_reported_as_empty_status(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "pr", "checks", "44"],
            returncode=1,
            stdout="",
            stderr="no checks reported on the 'feature/test' branch\n",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=result):
                client = GitHubClient(Path("/tmp/repo"))
                status = client.get_ci_status(44)

        self.assertEqual(status, [])

    def test_get_ci_status_accepts_failed_checks_from_exit_code_one(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "pr", "checks", "44"],
            returncode=1,
            stdout=json.dumps(
                [
                    {
                        "name": "test",
                        "state": "FAILURE",
                        "bucket": "fail",
                        "link": "https://ci/test",
                    }
                ]
            ),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=result):
                client = GitHubClient(Path("/tmp/repo"))
                status = client.get_ci_status(44)

        self.assertEqual(
            status,
            [
                {
                    "name": "test",
                    "state": "FAILURE",
                    "bucket": "fail",
                    "link": "https://ci/test",
                }
            ],
        )

    def test_update_issue_labels_runs_add_then_remove_operations(self) -> None:
        responses = [
            subprocess.CompletedProcess(args=["gh", "issue", "edit", "6"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(args=["gh", "issue", "edit", "6"], returncode=0, stdout="", stderr=""),
        ]

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", side_effect=responses) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                client.update_issue_labels(6, add=["shinobi:working"], remove=["shinobi:ready"])

        self.assertEqual(run_mock.call_count, 2)
        first_command = run_mock.call_args_list[0].args[0]
        second_command = run_mock.call_args_list[1].args[0]
        self.assertIn("--add-label", first_command)
        self.assertIn("shinobi:working", first_command)
        self.assertEqual(first_command[-2:], ["--repo", "owner/repo"])
        self.assertIn("--remove-label", second_command)
        self.assertIn("shinobi:ready", second_command)
        self.assertEqual(second_command[-2:], ["--repo", "owner/repo"])

    def test_create_issue_comment_runs_gh_issue_comment(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "issue", "comment", "6"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                client.create_issue_comment(6, "mission started")

        command = run_mock.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "issue", "comment", "6"])
        self.assertIn("--body", command)
        self.assertIn("mission started", command)
        self.assertEqual(command[-2:], ["--repo", "owner/repo"])

    def test_close_issue_runs_gh_issue_close(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "issue", "close", "6"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                client.close_issue(6)

        command = run_mock.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "issue", "close", "6"])
        self.assertEqual(command[-2:], ["--repo", "owner/repo"])

    def test_rerun_workflow_run_scopes_command_to_repo(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "run", "rerun", "123456789"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                client.rerun_workflow_run("123456789")

        self.assertEqual(
            run_mock.call_args.args[0],
            ["gh", "run", "rerun", "123456789", "--failed", "--repo", "owner/repo"],
        )

    def test_rerun_workflow_run_can_rerun_entire_run(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "run", "rerun", "123456789"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                client.rerun_workflow_run("123456789", failed_only=False)

        self.assertEqual(
            run_mock.call_args.args[0],
            ["gh", "run", "rerun", "123456789", "--repo", "owner/repo"],
        )

    def test_list_issue_comments_reads_comments_from_issue_view(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "api", "repos/owner/repo/issues/6/comments"],
            returncode=0,
            stdout=json.dumps(
                [
                    {"id": 101, "body": "first"},
                    {"id": 102, "body": "second"},
                ]
            ),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                comments = client.list_issue_comments(6)

        command = run_mock.call_args.args[0]
        self.assertEqual(
            command[:5],
            ["gh", "api", "repos/owner/repo/issues/6/comments", "--method", "GET"],
        )
        self.assertIn("per_page=100", command)
        self.assertIn("page=1", command)
        self.assertEqual([comment["id"] for comment in comments], [101, 102])

    def test_list_issue_comments_paginates_past_first_page(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["gh", "api", "repos/owner/repo/issues/6/comments"],
                returncode=0,
                stdout=json.dumps([{"id": number} for number in range(1, 101)]),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["gh", "api", "repos/owner/repo/issues/6/comments"],
                returncode=0,
                stdout=json.dumps([{"id": 150}]),
                stderr="",
            ),
        ]

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", side_effect=responses) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                comments = client.list_issue_comments(6)

        self.assertEqual(run_mock.call_count, 2)
        first_command = run_mock.call_args_list[0].args[0]
        second_command = run_mock.call_args_list[1].args[0]
        self.assertIn("page=1", first_command)
        self.assertIn("page=2", second_command)
        self.assertEqual(comments[0]["id"], 1)
        self.assertEqual(comments[-1]["id"], 150)
        self.assertEqual(len(comments), 101)

    def test_list_issue_comments_rejects_non_list_payload(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "api", "repos/owner/repo/issues/6/comments"],
            returncode=0,
            stdout=json.dumps({"comments": "broken"}),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response):
                client = GitHubClient(Path("/tmp/repo"))
                with self.assertRaisesRegex(
                    GitHubClientError,
                    "failed to parse comments for issue #6: expected list payload",
                ):
                    client.list_issue_comments(6)

    def test_update_issue_comment_uses_issue_comment_patch_api(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "api", "repos/owner/repo/issues/comments/123"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                client.update_issue_comment(123, "mission updated")

        command = run_mock.call_args.args[0]
        self.assertEqual(
            command[:5],
            ["gh", "api", "repos/owner/repo/issues/comments/123", "--method", "PATCH"],
        )
        self.assertIn("-f", command)
        self.assertIn("body=mission updated", command)

    def test_create_pull_request_fetches_created_pr_metadata(self) -> None:
        responses = [
            subprocess.CompletedProcess(args=["gh", "pr", "create"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                args=["gh", "pr", "view", "feature/issue-25"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "number": 42,
                        "url": "https://github.com/owner/repo/pull/42",
                        "isDraft": True,
                        "headRefName": "feature/issue-25",
                        "baseRefName": "main",
                    }
                ),
                stderr="",
            ),
        ]

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", side_effect=responses) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                pr = client.create_pull_request(
                    title="Issue 25",
                    body="body",
                    base="main",
                    head="feature/issue-25",
                    draft=True,
                )

        self.assertEqual(pr["number"], 42)
        self.assertEqual(run_mock.call_count, 2)
        create_command = run_mock.call_args_list[0].args[0]
        self.assertIn("--draft", create_command)
        self.assertIn("--head", create_command)
        self.assertIn("feature/issue-25", create_command)
        self.assertEqual(create_command[-2:], ["--repo", "owner/repo"])

    def test_get_pull_request_scopes_view_command_to_repo(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "pr", "view", "42"],
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 42,
                    "url": "https://github.com/owner/repo/pull/42",
                    "isDraft": False,
                    "headRefName": "feature/issue-25",
                    "baseRefName": "main",
                }
            ),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                pr = client.get_pull_request("42")

        self.assertEqual(pr["number"], 42)
        command = run_mock.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "pr", "view", "42"])
        self.assertEqual(command[-2:], ["--repo", "owner/repo"])

    def test_update_pull_request_scopes_edit_command_to_repo(self) -> None:
        responses = [
            subprocess.CompletedProcess(args=["gh", "pr", "edit", "42"], returncode=0, stdout="", stderr=""),
            subprocess.CompletedProcess(
                args=["gh", "pr", "view", "42"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "number": 42,
                        "url": "https://github.com/owner/repo/pull/42",
                        "isDraft": False,
                        "headRefName": "feature/issue-25",
                        "baseRefName": "main",
                    }
                ),
                stderr="",
            ),
        ]

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", side_effect=responses) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                pr = client.update_pull_request(42, title="updated title", body="updated body", base="develop")

        self.assertEqual(pr["number"], 42)
        command = run_mock.call_args_list[0].args[0]
        self.assertEqual(command[:4], ["gh", "pr", "edit", "42"])
        self.assertIn("--title", command)
        self.assertIn("updated title", command)
        self.assertIn("--body", command)
        self.assertIn("updated body", command)
        self.assertIn("--base", command)
        self.assertIn("develop", command)
        self.assertEqual(command[-2:], ["--repo", "owner/repo"])

    def test_list_pull_requests_by_head_returns_open_prs_for_branch(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "pr", "list"],
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "number": 42,
                        "url": "https://github.com/owner/repo/pull/42",
                        "isDraft": True,
                        "headRefName": "feature/issue-25",
                        "baseRefName": "main",
                    }
                ]
            ),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                prs = client.list_pull_requests_by_head("feature/issue-25")

        self.assertEqual(prs[0]["number"], 42)
        command = run_mock.call_args.args[0]
        self.assertEqual(command[:3], ["gh", "pr", "list"])
        self.assertIn("--head", command)
        self.assertIn("feature/issue-25", command)
        self.assertIn("--state", command)
        self.assertIn("open", command)
        self.assertEqual(command[-2:], ["--repo", "owner/repo"])

    def test_list_pull_requests_by_head_rejects_non_list_payload(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "pr", "list"],
            returncode=0,
            stdout=json.dumps({"number": 42}),
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response):
                client = GitHubClient(Path("/tmp/repo"))
                with self.assertRaisesRegex(
                    GitHubClientError,
                    "failed to parse PR list for head feature/issue-25",
                ):
                    client.list_pull_requests_by_head("feature/issue-25")

    def test_convert_pull_request_to_draft_fetches_updated_pr_metadata(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["gh", "pr", "ready", "42", "--undo"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["gh", "pr", "view", "42"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "number": 42,
                        "url": "https://github.com/owner/repo/pull/42",
                        "isDraft": True,
                        "headRefName": "feature/issue-25",
                        "baseRefName": "main",
                    }
                ),
                stderr="",
            ),
        ]

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", side_effect=responses) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                pr = client.convert_pull_request_to_draft(42)

        self.assertEqual(pr["number"], 42)
        self.assertEqual(run_mock.call_count, 2)
        ready_command = run_mock.call_args_list[0].args[0]
        self.assertEqual(ready_command[:4], ["gh", "pr", "ready", "42"])
        self.assertIn("--undo", ready_command)
        self.assertEqual(ready_command[-2:], ["--repo", "owner/repo"])

    def test_convert_pull_request_to_ready_fetches_updated_pr_metadata(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["gh", "pr", "ready", "42"],
                returncode=0,
                stdout="",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["gh", "pr", "view", "42"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "number": 42,
                        "url": "https://github.com/owner/repo/pull/42",
                        "isDraft": False,
                        "headRefName": "feature/issue-25",
                        "baseRefName": "main",
                    }
                ),
                stderr="",
            ),
        ]

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", side_effect=responses) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                pr = client.convert_pull_request_to_ready(42)

        self.assertEqual(pr["number"], 42)
        self.assertEqual(run_mock.call_count, 2)
        ready_command = run_mock.call_args_list[0].args[0]
        self.assertEqual(ready_command[:4], ["gh", "pr", "ready", "42"])
        self.assertNotIn("--undo", ready_command)
        self.assertEqual(ready_command[-2:], ["--repo", "owner/repo"])

    def test_merge_pull_request_scopes_merge_command_to_repo(self) -> None:
        response = subprocess.CompletedProcess(
            args=["gh", "pr", "merge", "42"],
            returncode=0,
            stdout="",
            stderr="",
        )

        with patch("shinobi.github_client.discover_repo_slug", return_value="owner/repo"):
            with patch("shinobi.github_client.subprocess.run", return_value=response) as run_mock:
                client = GitHubClient(Path("/tmp/repo"))
                client.merge_pull_request(42, merge_method="squash", delete_branch=True)

        command = run_mock.call_args.args[0]
        self.assertEqual(command[:4], ["gh", "pr", "merge", "42"])
        self.assertIn("--squash", command)
        self.assertIn("--delete-branch", command)
        self.assertEqual(command[-2:], ["--repo", "owner/repo"])

    def test_init_keeps_shinobi_directory_ignored_by_git(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            exclude_path = root / ".git" / "info" / "exclude"
            exclude_path.write_text(".shinobi/\n", encoding="utf-8")

            with patch("pathlib.Path.cwd", return_value=root):
                with redirect_stdout(io.StringIO()):
                    exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            ignored = subprocess.run(
                ["git", "check-ignore", ".shinobi/state.json"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(ignored.strip(), ".shinobi/state.json")

    def test_init_adds_shinobi_directory_to_git_info_exclude_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            (root / ".gitignore").write_text("dist/\n", encoding="utf-8")
            exclude_path = root / ".git" / "info" / "exclude"
            original_exclude = exclude_path.read_text(encoding="utf-8")

            with patch("pathlib.Path.cwd", return_value=root):
                with redirect_stdout(io.StringIO()):
                    exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            self.assertEqual((root / ".gitignore").read_text(encoding="utf-8"), "dist/\n")
            self.assertEqual(
                exclude_path.read_text(encoding="utf-8"),
                original_exclude + ("" if original_exclude.endswith("\n") or not original_exclude else "\n") + ".shinobi/\n",
            )
            ignored = subprocess.run(
                ["git", "check-ignore", ".shinobi/state.json"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(ignored.strip(), ".shinobi/state.json")
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(status.strip(), "?? .gitignore")

    def test_init_adds_shinobi_directory_to_worktree_git_info_exclude(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            repo = Path(tmp_dir) / "repo"
            worktree = Path(tmp_dir) / "worktree"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "test"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "--allow-empty", "-m", "init"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "remote", "add", "origin", "https://github.com/owner/repo.git"],
                cwd=repo,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "worktree", "add", str(worktree), "-b", "test-worktree"],
                cwd=repo,
                check=True,
                capture_output=True,
            )

            with patch("pathlib.Path.cwd", return_value=worktree):
                with redirect_stdout(io.StringIO()):
                    exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            ignored = subprocess.run(
                ["git", "check-ignore", ".shinobi/state.json"],
                cwd=worktree,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(ignored.strip(), ".shinobi/state.json")

    def test_packaging_declares_shinobi_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            temp_root = Path(tmp_dir)
            project_root = Path(__file__).resolve().parents[1]
            packaged_project = temp_root / "repo"
            wheel_dir = temp_root / "wheel"
            extract_dir = temp_root / "extract"
            workspace = temp_root / "workspace"
            shutil.copytree(
                project_root,
                packaged_project,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc", ".pytest_cache"),
            )
            wheel_dir.mkdir()
            workspace.mkdir()

            original_cwd = Path.cwd()
            try:
                with patch("pathlib.Path.cwd", return_value=packaged_project):
                    with patch.object(
                        sys,
                        "argv",
                        ["pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(wheel_dir)],
                    ):
                        import pip._internal.cli.main as pip_main

                        os.chdir(packaged_project)
                        exit_code = pip_main.main()
            finally:
                os.chdir(original_cwd)

            self.assertEqual(exit_code, 0)
            wheel_path = next(wheel_dir.glob("*.whl"))
            with zipfile.ZipFile(wheel_path) as wheel:
                names = wheel.namelist()
                wheel.extractall(extract_dir)

            self.assertTrue(any(name.startswith("shinobi/") for name in names))
            self.assertTrue(any(name.endswith(".dist-info/entry_points.txt") for name in names))
            self.assertIn("shinobi/bootstrap_templates/review-notes.md", names)
            self.assertIn("shinobi/bootstrap_templates/self-review.md", names)
            self.assertIn("shinobi/bootstrap_templates/review-note-rule.md", names)

            check_code = """
from pathlib import Path
from unittest.mock import patch
from shinobi.state_store import StateStore

workspace = Path(r\"\"\"{workspace}\"\"\")
workspace.mkdir(exist_ok=True)
with patch("shinobi.config.discover_repo_slug", return_value="owner/repo"):
    StateStore(workspace).initialize()
assert (workspace / ".shinobi" / "review-notes.md").exists()
assert (workspace / ".shinobi" / "templates" / "self-review.md").exists()
assert (workspace / ".shinobi" / "templates" / "review-note-rule.md").exists()
""".format(workspace=workspace)
            env = dict(os.environ)
            env["PYTHONPATH"] = str(extract_dir)
            result = subprocess.run(
                [sys.executable, "-c", check_code],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_discover_repo_slug_normalizes_ssh_url(self) -> None:
        with patch("subprocess.run", return_value=Mock(stdout="ssh://git@github.com/owner/repo.git\n")):
            repo = discover_repo_slug(Path("."))

        self.assertEqual(repo, "owner/repo")

    def test_discover_repo_slug_strips_https_credentials(self) -> None:
        remote = "https://x-access-token:ghp_secret@github.com/owner/repo.git\n"
        with patch("subprocess.run", return_value=Mock(stdout=remote)):
            repo = discover_repo_slug(Path("."))

        self.assertEqual(repo, "owner/repo")

    def test_discover_repo_slug_strips_credentials_from_non_github_host(self) -> None:
        remote = "https://token@example.com/owner/repo.git\n"
        with patch("subprocess.run", return_value=Mock(stdout=remote)):
            repo = discover_repo_slug(Path("."))

        self.assertEqual(repo, "owner/repo")

    def test_init_preserves_unknown_config_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.config_path.write_text(
                json.dumps(
                    {
                        "repo": "owner/repo",
                        "agent_identity": "owner/repo#default@testhost-12345678",
                        "future_flag": True,
                    }
                ),
                encoding="utf-8",
            )

            with patch("pathlib.Path.cwd", return_value=root):
                with redirect_stdout(io.StringIO()):
                    exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            repaired_config = json.loads(store.paths.config_path.read_text(encoding="utf-8"))
            self.assertEqual(repaired_config["repo"], "owner/repo")
            self.assertEqual(
                repaired_config["agent_identity"],
                "owner/repo#default@testhost-12345678",
            )
            self.assertTrue(repaired_config["future_flag"])

    def test_init_preserves_unknown_state_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = StateStore(root)
            store.paths.shinobi_dir.mkdir()
            store.paths.state_path.write_text(
                json.dumps(
                    {
                        "agent_identity": "owner/repo#default@testhost-12345678",
                        "phase": "idle",
                        "branch": "feature/test",
                        "future_field": {"enabled": True},
                    }
                ),
                encoding="utf-8",
            )

            with patch("pathlib.Path.cwd", return_value=root):
                with redirect_stdout(io.StringIO()):
                    exit_code = cli.main(["init"])

            self.assertEqual(exit_code, 0)
            repaired_state = json.loads(store.paths.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                repaired_state["agent_identity"],
                "owner/repo#default@testhost-12345678",
            )
            self.assertEqual(repaired_state["branch"], "feature/test")
            self.assertEqual(repaired_state["future_field"], {"enabled": True})


class ReviewerTest(unittest.TestCase):
    def test_parse_numstat_counts_changed_files_and_lines(self) -> None:
        stats = parse_numstat("12\t3\tsrc/shinobi/reviewer.py\n-\t-\tassets/logo.png\n")

        self.assertEqual(
            stats,
            DiffStats(
                changed_files=2,
                added_lines=12,
                deleted_lines=3,
            ),
        )
        self.assertEqual(stats.total_changed_lines, 15)

    def test_collect_diff_stats_wraps_git_failures(self) -> None:
        with patch(
            "shinobi.reviewer.subprocess.run",
            return_value=Mock(returncode=1, stdout="", stderr="unknown revision"),
        ):
            with self.assertRaisesRegex(
                ReviewerError,
                "failed to collect diff stats against origin/main: unknown revision",
            ):
                collect_diff_stats(Path("/tmp/repo"), base_ref="origin/main")

    def test_evaluate_review_allows_safe_diff(self) -> None:
        decision = evaluate_review(
            config=Config(repo="owner/repo"),
            state=State(review_loop_count=1),
            issue={"number": 34, "labels": [{"name": "priority:medium"}]},
            diff_stats=DiffStats(changed_files=3, added_lines=20, deleted_lines=5),
        )

        self.assertEqual(decision, ReviewDecision(should_stop=False, reasons=[]))
        self.assertTrue(decision.can_continue)

    def test_evaluate_review_stops_for_risky_label_and_size_limits(self) -> None:
        config = Config(
            repo="owner/repo",
            max_changed_files=5,
            max_lines_changed=20,
            max_review_loops=2,
        )

        decision = evaluate_review(
            config=config,
            state=State(review_loop_count=2),
            issue={
                "number": 34,
                "labels": [{"name": "shinobi:risky"}],
            },
            diff_stats=DiffStats(changed_files=6, added_lines=12, deleted_lines=15),
        )

        self.assertTrue(decision.should_stop)
        self.assertFalse(decision.can_continue)
        self.assertEqual(
            decision.reasons,
            [
                "issue #34 has label shinobi:risky, requiring human review",
                "changed files 6 exceed max_changed_files 5",
                "total changed lines 27 exceed max_lines_changed 20",
                "review loop count 2 reached max_review_loops 2",
            ],
        )

    def test_evaluate_merge_stops_for_ci_risk_and_size_limits(self) -> None:
        config = Config(
            repo="owner/repo",
            auto_merge=False,
            max_changed_files=4,
            max_lines_changed=10,
            max_review_loops=2,
        )

        decision = evaluate_merge(
            config=config,
            state=State(review_loop_count=2),
            issue={
                "number": 34,
                "labels": [{"name": "shinobi:risky"}],
            },
            ci_status=CIStatus(
                checks=[PullRequestCheck(name="test", state="FAILURE", bucket="fail")],
                status="failure",
            ),
            diff_stats=DiffStats(changed_files=5, added_lines=7, deleted_lines=9),
        )

        self.assertEqual(
            decision,
            MergeDecision(
                should_merge=False,
                reasons=[
                    "auto_merge is disabled in config",
                    "CI status is failure, not success",
                    "issue #34 has label shinobi:risky, requiring human merge",
                    "changed files 5 exceed max_changed_files 4",
                    "total changed lines 16 exceed max_lines_changed 10",
                ],
            ),
        )

    def test_evaluate_merge_stops_for_human_stop_labels(self) -> None:
        config = Config(repo="owner/repo")

        decision = evaluate_merge(
            config=config,
            state=State(review_loop_count=0),
            issue={
                "number": 34,
                "labels": [
                    {"name": "shinobi:reviewing"},
                    {"name": "shinobi:blocked"},
                ],
            },
            ci_status=CIStatus(
                checks=[PullRequestCheck(name="test", state="SUCCESS", bucket="pass")],
                status="success",
            ),
            diff_stats=DiffStats(changed_files=1, added_lines=2, deleted_lines=1),
        )

        self.assertEqual(
            decision,
            MergeDecision(
                should_merge=False,
                reasons=["issue #34 has blocking label(s): shinobi:blocked"],
                conclusion="blocked",
            ),
        )

    def test_evaluate_merge_allows_success_after_retry_limit_is_reached(self) -> None:
        config = Config(
            repo="owner/repo",
            max_review_loops=2,
        )

        decision = evaluate_merge(
            config=config,
            state=State(review_loop_count=2),
            issue={
                "number": 34,
                "labels": [{"name": "shinobi:reviewing"}],
            },
            ci_status=CIStatus(
                checks=[PullRequestCheck(name="test", state="SUCCESS", bucket="pass")],
                status="success",
            ),
            diff_stats=DiffStats(changed_files=1, added_lines=2, deleted_lines=1),
        )

        self.assertEqual(
            decision,
            MergeDecision(
                should_merge=True,
                reasons=[],
            ),
        )

    def test_merge_pull_request_marks_draft_pr_ready_before_merging(self) -> None:
        client = Mock()
        config = Config(repo="owner/repo", merge_method="squash")
        client.get_pull_request.return_value = {
            "number": 44,
            "isDraft": True,
        }
        client.convert_pull_request_to_ready.return_value = {
            "number": 44,
            "isDraft": False,
        }

        merge_pull_request(client=client, pr_number=44, config=config)

        client.get_pull_request.assert_called_once_with("44")
        client.convert_pull_request_to_ready.assert_called_once_with(44)
        client.merge_pull_request.assert_called_once_with(
            44,
            merge_method="squash",
            delete_branch=True,
        )

    def test_merge_pull_request_wraps_ready_transition_failure(self) -> None:
        client = Mock()
        config = Config(repo="owner/repo", merge_method="squash")
        client.get_pull_request.return_value = {
            "number": 44,
            "isDraft": True,
        }
        client.convert_pull_request_to_ready.side_effect = GitHubClientError(
            "draft pull requests cannot be merged"
        )

        with self.assertRaisesRegex(
            MergerError,
            "failed to merge PR #44: draft pull requests cannot be merged",
        ):
            merge_pull_request(client=client, pr_number=44, config=config)

        client.merge_pull_request.assert_not_called()

    def test_collect_ci_status_classifies_checks(self) -> None:
        client = Mock()
        client.get_ci_status.return_value = [
            {"name": "lint", "state": "SUCCESS", "bucket": "pass", "link": "https://ci/lint"},
            {"name": "test", "state": "IN_PROGRESS", "bucket": "pending"},
        ]

        status = collect_ci_status(client, 51)

        self.assertEqual(
            status,
            CIStatus(
                checks=[
                    PullRequestCheck(
                        name="lint",
                        state="SUCCESS",
                        bucket="pass",
                        link="https://ci/lint",
                    ),
                    PullRequestCheck(
                        name="test",
                        state="IN_PROGRESS",
                        bucket="pending",
                        link=None,
                    ),
                ],
                status="pending",
            ),
        )
        self.assertTrue(status.is_pending)

    def test_collect_ci_status_wraps_github_errors(self) -> None:
        client = Mock()
        client.get_ci_status.side_effect = GitHubClientError("api unavailable")

        with self.assertRaisesRegex(
            ReviewerError,
            "failed to load CI status for PR #51: api unavailable",
        ):
            collect_ci_status(client, 51)

    def test_wait_for_ci_polls_until_success_and_calls_heartbeat(self) -> None:
        client = Mock()
        client.get_ci_status.side_effect = [
            [{"name": "test", "state": "IN_PROGRESS", "bucket": "pending"}],
            [{"name": "test", "state": "SUCCESS", "bucket": "pass"}],
        ]
        heartbeats: list[str] = []
        slept: list[float] = []
        clock = {"now": 0.0}

        def heartbeat() -> None:
            heartbeats.append("tick")

        def monotonic() -> float:
            return clock["now"]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock["now"] += seconds

        status = wait_for_ci(
            client,
            51,
            timeout_seconds=30,
            poll_interval_seconds=5,
            heartbeat=heartbeat,
            monotonic=monotonic,
            sleep=sleep,
        )

        self.assertEqual(
            status,
            CIStatus(
                checks=[
                    PullRequestCheck(
                        name="test",
                        state="SUCCESS",
                        bucket="pass",
                        link=None,
                    )
                ],
                status="success",
            ),
        )
        self.assertEqual(heartbeats, ["tick", "tick"])
        self.assertEqual(slept, [5])

    def test_wait_for_ci_returns_timed_out_pending_result(self) -> None:
        client = Mock()
        client.get_ci_status.return_value = [
            {"name": "test", "state": "QUEUED", "bucket": "pending"}
        ]
        clock = {"now": 0.0}

        def monotonic() -> float:
            return clock["now"]

        def sleep(seconds: float) -> None:
            clock["now"] += seconds

        status = wait_for_ci(
            client,
            51,
            timeout_seconds=4,
            poll_interval_seconds=2,
            monotonic=monotonic,
            sleep=sleep,
        )

        self.assertEqual(
            status,
            CIStatus(
                checks=[
                    PullRequestCheck(
                        name="test",
                        state="QUEUED",
                        bucket="pending",
                        link=None,
                    )
                ],
                status="pending",
                timed_out=True,
            ),
        )

    def test_collect_ci_status_treats_missing_checks_as_pending(self) -> None:
        client = Mock()
        client.get_ci_status.return_value = []

        status = collect_ci_status(client, 51)

        self.assertEqual(status, CIStatus(checks=[], status="pending"))
        self.assertTrue(status.is_pending)

    def test_collect_ci_status_falls_back_to_failure_for_terminal_states(self) -> None:
        client = Mock()
        client.get_ci_status.return_value = [
            {"name": "deploy", "state": "ACTION_REQUIRED"},
            {"name": "build", "state": "STARTUP_FAILURE"},
        ]

        status = collect_ci_status(client, 51)

        self.assertEqual(status.status, "failure")
        self.assertTrue(status.is_failed)
        self.assertEqual([check.bucket for check in status.checks], ["fail", "fail"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import io
import json
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
from shinobi.state_store import StateStore


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
            self.assertTrue(store.paths.lock_path.exists())
            self.assertIn("Initialized Shinobi", output.getvalue())

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
            self.assertIn("github_status: unavailable in foundations MVP", rendered)

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
                            with redirect_stdout(output):
                                exit_code = cli.main(["run"])

            self.assertEqual(exit_code, 0)
            rendered = output.getvalue()
            self.assertIn("run lock: took over stale lock during select phase", rendered)
            self.assertIn("selected_issue: 6", rendered)
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
                "run aborted: another active GitHub mission exists for #5",
                output.getvalue(),
            )
            issue_mock.assert_not_called()
            self.assertEqual(StateStore(root).paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_with_issue_refuses_same_issue_when_github_mission_is_active(self) -> None:
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
                    with patch("shinobi.cli.list_open_issues_with_any_label", return_value=[6]):
                        with patch("shinobi.cli.select_ready_issue") as select_mock:
                            with patch(
                                "shinobi.cli.ensure_open_issue",
                                side_effect=RuntimeError(
                                    "issue #6 already has active mission label(s): "
                                    "shinobi:working, shinobi:reviewing"
                                ),
                            ) as issue_mock:
                                with redirect_stdout(output):
                                    exit_code = cli.main(["run", "--issue", "6"])

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "run aborted: issue #6 already has active mission label(s):",
                output.getvalue(),
            )
            select_mock.assert_not_called()
            issue_mock.assert_called_once_with(
                root,
                6,
                active_labels=("shinobi:working", "shinobi:reviewing"),
            )
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

    def test_run_with_issue_uses_requested_issue_when_local_state_matches(self) -> None:
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
                        with patch("shinobi.cli.select_ready_issue") as select_mock:
                            with patch("shinobi.cli.ensure_open_issue", return_value=6) as issue_mock:
                                with redirect_stdout(output):
                                    exit_code = cli.main(["run", "--issue", "6"])

            self.assertEqual(exit_code, 0)
            self.assertIn("selected_issue: 6", output.getvalue())
            select_mock.assert_not_called()
            issue_mock.assert_called_once_with(
                root,
                6,
                active_labels=("shinobi:working", "shinobi:reviewing"),
            )
            self.assertEqual(store.paths.lock_path.read_text(encoding="utf-8"), "")

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

    def test_select_ready_issue_prefers_high_priority_labels(self) -> None:
        issue_list = json.dumps(
            [
                {"number": 12, "labels": [{"name": "shinobi:ready"}]},
                {"number": 8, "labels": [{"name": "shinobi:ready"}, {"name": "priority:medium"}]},
                {"number": 15, "labels": [{"name": "shinobi:ready"}, {"name": "priority:high"}]},
            ]
        )
        result = subprocess.CompletedProcess(
            args=["gh", "issue", "list"],
            returncode=0,
            stdout=issue_list,
            stderr="",
        )

        with patch("shinobi.issue_selector.subprocess.run", return_value=result) as run_mock:
            selected_issue = cli.select_ready_issue(Path("/tmp/repo"), "shinobi:ready")

        self.assertEqual(selected_issue, 15)
        run_mock.assert_called_once()

    def test_ensure_open_issue_rejects_closed_issue(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "issue", "view", "6"],
            returncode=0,
            stdout=json.dumps({"number": 6, "state": "CLOSED", "labels": []}),
            stderr="",
        )

        with patch("shinobi.issue_selector.subprocess.run", return_value=result):
            with self.assertRaisesRegex(RuntimeError, "issue #6 is not open"):
                cli.ensure_open_issue(Path("/tmp/repo"), 6)

    def test_ensure_open_issue_rejects_issue_with_active_label(self) -> None:
        result = subprocess.CompletedProcess(
            args=["gh", "issue", "view", "6"],
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 6,
                    "state": "OPEN",
                    "labels": [{"name": "shinobi:working"}],
                }
            ),
            stderr="",
        )

        with patch("shinobi.issue_selector.subprocess.run", return_value=result):
            with self.assertRaisesRegex(
                RuntimeError,
                "issue #6 already has active mission label\\(s\\): shinobi:working",
            ):
                cli.ensure_open_issue(
                    Path("/tmp/repo"),
                    6,
                    active_labels=("shinobi:working", "shinobi:reviewing"),
                )

    def test_list_open_issues_with_any_label_merges_issue_numbers(self) -> None:
        responses = [
            subprocess.CompletedProcess(
                args=["gh", "issue", "list", "--label", "shinobi:working"],
                returncode=0,
                stdout=json.dumps([{"number": 6}, {"number": 8}]),
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=["gh", "issue", "list", "--label", "shinobi:reviewing"],
                returncode=0,
                stdout=json.dumps([{"number": 8}, {"number": 10}]),
                stderr="",
            ),
        ]

        with patch("shinobi.issue_selector.subprocess.run", side_effect=responses):
            issue_numbers = cli.list_open_issues_with_any_label(
                Path("/tmp/repo"),
                ("shinobi:working", "shinobi:reviewing"),
            )

        self.assertEqual(issue_numbers, [6, 8, 10])

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
            self.assertIn("run lock is held by live run", next(message for status, message in results if status == "error"))

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
            wheel_dir = Path(tmp_dir)
            project_root = Path(__file__).resolve().parents[1]
            with patch("pathlib.Path.cwd", return_value=project_root):
                with patch.object(sys, "argv", ["pip", "wheel", ".", "--no-deps", "--no-build-isolation", "-w", str(wheel_dir)]):
                    import pip._internal.cli.main as pip_main

                    exit_code = pip_main.main()

            self.assertEqual(exit_code, 0)
            wheel_path = next(wheel_dir.glob("*.whl"))
            with zipfile.ZipFile(wheel_path) as wheel:
                names = wheel.namelist()

            self.assertTrue(any(name.startswith("shinobi/") for name in names))
            self.assertTrue(any(name.endswith(".dist-info/entry_points.txt") for name in names))

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


if __name__ == "__main__":
    unittest.main()

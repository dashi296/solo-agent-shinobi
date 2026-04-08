from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()

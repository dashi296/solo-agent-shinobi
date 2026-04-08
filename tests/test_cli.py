from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from shinobi import cli
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


if __name__ == "__main__":
    unittest.main()

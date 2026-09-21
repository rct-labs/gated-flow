"""Task-local acceptance and the commit hook that consumes it.

The hook checks three things when a commit flips a task to DONE: the diff
touches declared code, no frozen path moved, and a fresh PASS verdict exists
for that task whose declared files still hold the tested bytes. It never
hashes the environment, the interpreter or the Git toolchain, so a commit made
from a different shell than the verification passes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_compat import GATE, committed_runner_repo, load_gate_module, run


LOCAL_CHECK = "python -c \"print('7 passed')\""


def local_repo(root: Path) -> Path:
    """committed_runner_repo plus one admitted local check and the real hook."""
    repo = committed_runner_repo(root, ["codex"])
    queue = repo / "TASK_QUEUE.md"
    text = queue.read_text(encoding="utf-8")
    text += ('<!-- task:EAV-2 verify: {"cmd": "python -c \\"print(\'7 passed\')\\"", '
             '"timeout_s": 60} -->\n')
    queue.write_text(text, encoding="utf-8")
    cfg_path = repo / ".gate" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["verify_cmd"] = "python -c \"print('99 passed')\""
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    run("git", "-C", str(repo), "add", "TASK_QUEUE.md", ".gate/config.json")
    run("git", "-C", str(repo), "commit", "-q", "-m", "declare local check")
    result = run(sys.executable, str(GATE), "install-hook", "--repo", str(repo))
    if result.returncode:
        raise AssertionError(result.stderr)
    return repo


def flip_done(repo: Path, task: str) -> None:
    queue = repo / "TASK_QUEUE.md"
    text = queue.read_text(encoding="utf-8")
    queue.write_text(text.replace(f"{task} | ", f"{task} | ", 1).replace(
        {"EAV-2": "EAV-2 | loader | `TODO`", "EAV-3": "EAV-3 | diff | `TODO`"}[task],
        {"EAV-2": "EAV-2 | loader | `DONE`", "EAV-3": "EAV-3 | diff | `DONE`"}[task]),
        encoding="utf-8")


def commit(repo: Path, message: str, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    merged = dict(os.environ)
    if env:
        merged.update(env)
    return subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", message],
        text=True, encoding="utf-8", errors="replace", capture_output=True,
        check=False, env=merged,
    )


class TaskLocalVerifyTests(unittest.TestCase):
    def test_parse_declarations_in_the_shape_projects_already_use(self) -> None:
        gate = load_gate_module()
        text = ('<!-- task:AN-058A verify: {"cmd": "python -m pytest -q tests/test_a.py '
                '--durations=20", "timeout_s": 1800} -->\n'
                '<!-- task:AN-058B verify: {"cmd": ""} -->\n'
                '<!-- task:AN-058C verify: not json -->\n'
                '<!-- task:AN-058D verify: {"cmd": "pytest -q"} -->\n')
        parsed = gate.parse_task_verify(text)
        self.assertEqual(parsed["AN-058A"]["timeout_s"], 1800)
        self.assertTrue(parsed["AN-058A"]["cmd"].startswith("python -m pytest"))
        self.assertNotIn("AN-058B", parsed)
        self.assertNotIn("AN-058C", parsed)
        self.assertEqual(parsed["AN-058D"], {"cmd": "pytest -q", "timeout_s": 900})

    def test_local_pass_closes_the_task_through_the_real_hook_from_another_shell(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = local_repo(Path(td))
            (repo / "eav2.txt").write_text("implemented\n", encoding="utf-8")
            result = run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "EAV-2")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            verdict = json.loads((repo / ".gate" / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(verdict["scope"], "task")
            self.assertEqual(verdict["task"], "EAV-2")
            self.assertEqual(verdict["count"], 7)
            flip_done(repo, "EAV-2")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "eav2.txt")
            other_home = Path(td) / "other-home"
            other_home.mkdir()
            result = commit(repo, "close EAV-2", env={
                "PATH": str(Path(td) / "not-a-dir") + os.pathsep + os.environ.get("PATH", ""),
                "HOME": str(other_home),
                "GATE_TEST_EXTRA": "a value the verification never saw",
            })
            self.assertEqual(result.returncode, 0, result.stderr)
            log = run("git", "-C", str(repo), "log", "--oneline", "-1").stdout
            self.assertIn("close EAV-2", log)

    def test_editing_a_declared_file_after_the_check_blocks_the_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = local_repo(Path(td))
            (repo / "eav2.txt").write_text("implemented\n", encoding="utf-8")
            run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "EAV-2")
            (repo / "eav2.txt").write_text("changed after the check\n", encoding="utf-8")
            flip_done(repo, "EAV-2")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "eav2.txt")
            result = commit(repo, "close EAV-2")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed after its local check", result.stderr)

    def test_a_task_verdict_cannot_close_another_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = local_repo(Path(td))
            (repo / "eav2.txt").write_text("implemented\n", encoding="utf-8")
            run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--task", "EAV-2")
            (repo / "eav3.txt").write_text("other\n", encoding="utf-8")
            flip_done(repo, "EAV-3")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "eav3.txt")
            result = commit(repo, "close EAV-3")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("verdict is for task EAV-2", result.stderr)

    def test_queue_scope_runs_the_full_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = local_repo(Path(td))
            result = run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--queue")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            verdict = json.loads((repo / ".gate" / "verdict.json").read_text(encoding="utf-8"))
            self.assertEqual(verdict["scope"], "queue")
            self.assertEqual(verdict["count"], 99)

    def test_full_verdict_cannot_close_code_changed_after_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = local_repo(Path(td))
            (repo / "eav2.txt").write_text("tested bytes", encoding="utf-8")
            result = run(sys.executable, str(GATE), "verify", "--repo", str(repo), "--queue")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            (repo / "eav2.txt").write_text("different bytes", encoding="utf-8")
            flip_done(repo, "EAV-2")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "eav2.txt")
            result = commit(repo, "close after stale full check")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("changed after full acceptance", result.stderr)

    def test_full_candidate_fingerprint_tracks_non_ascii_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = local_repo(Path(td))
            gate = load_gate_module()
            cfg = gate.load_config(repo)
            path = repo / "caf\u00e9.py"
            path.write_text("x = 1\n", encoding="utf-8")
            before = gate.candidate_fingerprint(repo, cfg)
            path.write_text("x = 2\n", encoding="utf-8")
            self.assertNotEqual(before, gate.candidate_fingerprint(repo, cfg))

    def test_stage_verdict_cannot_close_an_individual_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = local_repo(Path(td))
            gate = load_gate_module()
            cfg = gate.load_config(repo)
            (repo / "eav2.txt").write_text("implemented", encoding="utf-8")
            gate.run_acceptance(repo, cfg, LOCAL_CHECK, scope="stage")
            flip_done(repo, "EAV-2")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md", "eav2.txt")
            result = commit(repo, "close using checkpoint")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stage checkpoint cannot close", result.stderr)


if __name__ == "__main__":
    unittest.main()

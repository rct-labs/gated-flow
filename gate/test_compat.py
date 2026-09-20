"""Compatibility regression tests for flow onboarding and gate hook chaining."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


HERE = Path(__file__).resolve().parent
# The full oracle takes a machine-wide turn (~/.gate). The suites must neither
# wait for a real project's oracle nor make one wait: give them a private turn.
os.environ["GATE_MACHINE_DIR"] = tempfile.mkdtemp(prefix="gate-machine-")
GATE = HERE / "gate.py"
FLOW = HERE.parent / "flow" / "flow.py"


def run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def new_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    result = run("git", "init", "-q", str(repo))
    if result.returncode:
        raise AssertionError(result.stderr)
    return repo


def committed_runner_repo(root: Path, workers: list[str]) -> Path:
    repo = new_repo(root)
    run("git", "-C", str(repo), "config", "user.name", "Gate Test")
    run("git", "-C", str(repo), "config", "user.email", "gate@example.invalid")
    queue = (
        "# TASK_QUEUE\n\n"
        "| # | ID | name | status | start baseline | end baseline |\n"
        "|---|---|---|---|---|---|\n"
        "| 1 | EAV-2 | loader | `TODO` | 1 passed | — |\n"
        "| 2 | EAV-3 | diff | `TODO` | 1 passed | — |\n\n"
        "<!-- task:EAV-2 files: eav2.txt -->\n"
        "<!-- task:EAV-3 files: eav3.txt -->\n"
    )
    (repo / "TASK_QUEUE.md").write_text(queue, encoding="utf-8")
    gate_dir = repo / ".gate"
    gate_dir.mkdir()
    (gate_dir / "config.json").write_text(
        json.dumps(
            {
                "workers": workers,
                "worker_prompt": (
                    "Execute exactly task {task}. Never select another TODO task."
                ),
                "verify_cmd": "python -c \"print('1 passed')\"",
                "max_tasks": 1,
                "max_attempts_per_task": 2,
                "strict_admit": True,
                "quota_cooldown_s": 3600,
                "probe": {"enabled": False},
            }
        ),
        encoding="utf-8",
    )
    result = run("git", "-C", str(repo), "add", "TASK_QUEUE.md", ".gate/config.json")
    if result.returncode:
        raise AssertionError(result.stderr)
    result = run("git", "-C", str(repo), "commit", "-q", "-m", "initial")
    if result.returncode:
        raise AssertionError(result.stderr)
    return repo


def load_gate_module():
    spec = importlib.util.spec_from_file_location("gate_compat_under_test", GATE)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FlowGateCompatibilityTests(unittest.TestCase):
    def test_project_without_quality_skips_optional_bridge(self) -> None:
        module = load_gate_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            self.assertIsNone(module.quality_phase(repo, {}, "commit"))

    def test_required_quality_failure_blocks_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = new_repo(Path(td))
            gate_dir = repo / ".gate"
            gate_dir.mkdir()
            config = {
                "quality": {
                    "version": 1,
                    "enabled": True,
                    "mode": "required",
                    "profile": "content",
                    "rubric_version": "1.0.0",
                    "scope": {"label": "fixture", "include": ["."], "exclude": []},
                    "evidence_coverage": {"minimum": 0, "required_dimensions": ["architecture"]},
                    "commands": [{
                        "id": "deliberate-failure",
                        "command": f'"{sys.executable}" -c "import sys; sys.exit(9)"',
                        "phases": ["commit"],
                        "timeout_s": 10,
                    }],
                    "dimension_floors": {},
                    "regression_budgets": {"overall": 0, "dimensions": {}},
                    "milestone_triggers": [],
                }
            }
            (gate_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            (repo / "file.txt").write_text("change\n", encoding="utf-8")
            run("git", "-C", str(repo), "add", "file.txt")
            result = run(sys.executable, str(GATE), "check-commit", "--repo", str(repo))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("continuous-quality", result.stderr)

    def test_advisory_quality_failure_does_not_block_commit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = new_repo(Path(td))
            gate_dir = repo / ".gate"
            gate_dir.mkdir()
            config = {
                "quality": {
                    "version": 1,
                    "enabled": True,
                    "mode": "advisory",
                    "profile": "content",
                    "rubric_version": "1.0.0",
                    "scope": {"label": "fixture", "include": ["."], "exclude": []},
                    "evidence_coverage": {"minimum": 0, "required_dimensions": ["architecture"]},
                    "commands": [{
                        "id": "deliberate-advisory-failure",
                        "command": f'"{sys.executable}" -c "import sys; sys.exit(9)"',
                        "phases": ["commit"],
                        "timeout_s": 10,
                    }],
                    "dimension_floors": {},
                    "regression_budgets": {"overall": 0, "dimensions": {}},
                    "milestone_triggers": [],
                }
            }
            (gate_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            (repo / "file.txt").write_text("change\n", encoding="utf-8")
            run("git", "-C", str(repo), "add", "file.txt")
            result = run(sys.executable, str(GATE), "check-commit", "--repo", str(repo))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no task flipped to DONE", result.stdout)

    def test_external_context_queue_mapping_and_hook_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = new_repo(Path(td))
            (repo / "GLOSSARY.md").write_text("project-owned\n", encoding="utf-8")
            hook = repo / ".git" / "hooks" / "pre-commit"
            original = "#!/bin/sh\n# File generated by pre-commit\nexec pre-commit hook-impl\n"
            hook.write_text(original, encoding="utf-8", newline="\n")

            result = run(
                sys.executable,
                str(FLOW),
                "init",
                "--repo",
                str(repo),
                "--verify-cmd",
                "python -m unittest -q",
                "--queue-file",
                "ops/QUEUE.md",
                "--context-file",
                "GLOSSARY.md",
                "--external-context",
                "--handoff-glob",
                "memory/*",
                "--hook-mode",
                "preserve",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(hook.read_text(encoding="utf-8"), original)
            self.assertTrue((repo / "ops" / "QUEUE.md").exists())
            self.assertFalse((repo / "CONTEXT.md").exists())
            cfg = json.loads((repo / ".gate" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(cfg["queue_file"], "ops/QUEUE.md")
            self.assertEqual(cfg["context_file"], "GLOSSARY.md")
            self.assertTrue(cfg["external_context"])
            self.assertIn("memory/*", cfg["handoff_globs"])
            self.assertIn("memory/*", cfg["doc_only_globs"])

    def test_existing_hook_refuses_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = new_repo(Path(td))
            hook = repo / ".git" / "hooks" / "pre-commit"
            original = "#!/bin/sh\n# owned elsewhere\nexit 0\n"
            hook.write_text(original, encoding="utf-8", newline="\n")
            result = run(sys.executable, str(GATE), "install-hook", "--repo", str(repo))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing to overwrite", result.stderr)
            self.assertEqual(hook.read_text(encoding="utf-8"), original)

    def test_pre_commit_chain_is_recognized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = new_repo(Path(td))
            hook = repo / ".git" / "hooks" / "pre-commit"
            hook.write_text(
                "#!/bin/sh\n# File generated by pre-commit\nexec pre-commit hook-impl\n",
                encoding="utf-8",
                newline="\n",
            )
            (repo / ".pre-commit-config.yaml").write_text(
                "repos:\n"
                "  - repo: local\n"
                "    hooks:\n"
                "      - id: gate-check-commit\n"
                f"        entry: {sys.executable} {GATE} check-commit --repo .\n"
                "        language: system\n",
                encoding="utf-8",
            )
            module = load_gate_module()
            self.assertEqual(module.hook_installation(repo), (True, "pre-commit-chain"))

    def test_direct_install_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = new_repo(Path(td))
            first = run(sys.executable, str(GATE), "install-hook", "--repo", str(repo))
            second = run(sys.executable, str(GATE), "install-hook", "--repo", str(repo))
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("already installed", second.stdout)


class GateRunnerRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = load_gate_module()

    @staticmethod
    def args(repo: Path) -> SimpleNamespace:
        return SimpleNamespace(
            repo=str(repo), max_tasks=1, strict_admit=True, force=True
        )

    def test_codex_writer_uses_full_access_and_prompt_binds_task(self) -> None:
        command = self.gate.WORKER_CMDS["codex"]
        self.assertIn("danger-full-access", command)
        self.assertNotIn("workspace-write", command)
        self.assertTrue(
            any("TaskOutput" in part for part in self.gate.WORKER_CMDS["claude"])
        )
        prompt = self.gate.DEFAULT_CONFIG["worker_prompt"].format(
            skill="run-queue", task="EAV-2"
        )
        self.assertIn("exactly task EAV-2", prompt)
        self.assertIn("Never select a different TODO task", prompt)
        self.assertIn("Never return while a test", prompt)

    def test_background_work_pending_stops_without_concurrent_retry(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["claude"])
            calls = 0

            def fake_spawn(repo_arg, cfg, worker, prompt, prompt_file, **kwargs):
                nonlocal calls
                calls += 1
                return (
                    self.gate.WorkerResult(
                        0,
                        "Full suite still running in background. Waiting before commit.",
                    ),
                    None,
                )

            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
                self.gate, "resolve_tool", side_effect=lambda name: name
            ), mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                with self.assertRaisesRegex(SystemExit, "3"):
                    self.gate.cmd_run(self.args(repo))

            self.assertEqual(calls, 1)
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("worker_incomplete:EAV-2", report)

    def test_partial_worktree_gets_one_retry_with_a_hint_then_stops(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["claude"])
            prompts: list[str] = []

            def fake_spawn(repo_arg, cfg, worker, prompt, prompt_file, **kwargs):
                prompts.append(prompt)
                (repo_arg / "eav2.txt").write_text(f"partial {len(prompts)}\n", encoding="utf-8")
                return self.gate.WorkerResult(0, "I could not finish."), None

            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
                self.gate, "resolve_tool", side_effect=lambda name: name
            ), mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                with self.assertRaisesRegex(SystemExit, "3"):
                    self.gate.cmd_run(self.args(repo))

            self.assertEqual(len(prompts), 2)
            self.assertNotIn("left uncommitted changes", prompts[0])
            self.assertIn("left uncommitted changes for this task", prompts[1])
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("worker_left_changes:EAV-2", report)

    def test_git_write_denial_is_tool_outage_but_stale_lock_is_not(self) -> None:
        denied = (
            "fatal: Unable to create '/repo/.git/index.lock': Permission denied"
        )
        stale = "fatal: Unable to create '/repo/.git/index.lock': File exists"
        self.assertEqual(self.gate.tool_outage_reason(denied), "git_write_denied")
        self.assertIsNone(self.gate.tool_outage_reason(stale))
        self.assertIsNone(self.gate.tool_outage_reason("quota handling test passed", 0))
        self.assertEqual(self.gate.tool_outage_reason("rate limit", 1), "quota")

    def test_restore_task_row_preserves_other_queue_changes(self) -> None:
        before = (
            "| 1 | EAV-2 | loader | `TODO` |\n"
            "| 2 | EAV-3 | diff | `TODO` |\n"
        )
        current = (
            "| 1 | EAV-2 | loader | `BLOCKED` |\n"
            "| 2 | EAV-3 | diff | `DONE` |\n"
        )
        restored = self.gate.restore_task_row_text(before, current, "EAV-2")
        self.assertIn("EAV-2 | loader | `TODO`", restored)
        self.assertIn("EAV-3 | diff | `DONE`", restored)

    def test_git_denial_restores_same_task_and_rotates_worker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex", "claude"])
            calls: list[tuple[str, str]] = []

            def fake_spawn(repo_arg, cfg, worker, prompt, prompt_file, **kwargs):
                calls.append((worker, prompt))
                queue_path = repo_arg / "TASK_QUEUE.md"
                queue = queue_path.read_text(encoding="utf-8")
                if worker == "codex":
                    queue_path.write_text(
                        queue.replace("EAV-2 | loader | `TODO`", "EAV-2 | loader | `BLOCKED`"),
                        encoding="utf-8",
                    )
                    return (
                        self.gate.WorkerResult(
                            0,
                            "fatal: Unable to create "
                            "'/repo/.git/index.lock': Permission denied",
                        ),
                        None,
                    )

                self.assertIn("EAV-2 | loader | `TODO`", queue)
                self.assertIn("exactly task EAV-2", prompt)
                (repo_arg / "eav2.txt").write_text("done\n", encoding="utf-8")
                queue_path.write_text(
                    queue.replace("EAV-2 | loader | `TODO`", "EAV-2 | loader | `DONE`"),
                    encoding="utf-8",
                )
                added = run(
                    "git", "-C", str(repo_arg), "add", "TASK_QUEUE.md", "eav2.txt"
                )
                self.assertEqual(added.returncode, 0, added.stderr)
                committed = run(
                    "git", "-C", str(repo_arg), "commit", "-q", "-m", "finish EAV-2"
                )
                self.assertEqual(committed.returncode, 0, committed.stderr)
                return self.gate.WorkerResult(0, "done"), None

            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
                self.gate, "resolve_tool", side_effect=lambda name: name
            ), mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                self.gate.cmd_run(self.args(repo))

            self.assertEqual([worker for worker, _ in calls], ["codex", "claude"])
            final_queue = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
            self.assertIn("EAV-2 | loader | `DONE`", final_queue)
            self.assertIn("EAV-3 | diff | `TODO`", final_queue)
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("| EAV-2 | done | 1 | 2 |", report)
            state = json.loads((repo / ".gate" / "tool-status.json").read_text())
            self.assertEqual(state["codex"]["reason"], "git_write_denied")

    def test_wrong_task_completion_stops_as_task_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["claude", "grok"])
            calls = 0

            def fake_spawn(repo_arg, cfg, worker, prompt, prompt_file, **kwargs):
                nonlocal calls
                calls += 1
                queue_path = repo_arg / "TASK_QUEUE.md"
                queue = queue_path.read_text(encoding="utf-8")
                (repo_arg / "eav3.txt").write_text("wrong task\n", encoding="utf-8")
                queue_path.write_text(
                    queue.replace("EAV-3 | diff | `TODO`", "EAV-3 | diff | `DONE`"),
                    encoding="utf-8",
                )
                run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md", "eav3.txt")
                committed = run(
                    "git", "-C", str(repo_arg), "commit", "-q", "-m", "wrong task"
                )
                self.assertEqual(committed.returncode, 0, committed.stderr)
                return self.gate.WorkerResult(0, "done"), None

            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
                self.gate, "resolve_tool", side_effect=lambda name: name
            ), mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                with self.assertRaisesRegex(SystemExit, "3"):
                    self.gate.cmd_run(self.args(repo))

            self.assertEqual(calls, 1)
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("task_mismatch:EAV-2", report)

    def test_real_blocker_stops_without_dispatching_next_todo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["claude", "grok"])
            calls = 0

            def fake_spawn(repo_arg, cfg, worker, prompt, prompt_file, **kwargs):
                nonlocal calls
                calls += 1
                queue_path = repo_arg / "TASK_QUEUE.md"
                queue = queue_path.read_text(encoding="utf-8")
                queue_path.write_text(
                    queue.replace("EAV-2 | loader | `TODO`", "EAV-2 | loader | `BLOCKED`"),
                    encoding="utf-8",
                )
                return self.gate.WorkerResult(0, "human design decision required"), None

            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
                self.gate, "resolve_tool", side_effect=lambda name: name
            ), mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                with self.assertRaisesRegex(SystemExit, "3"):
                    self.gate.cmd_run(self.args(repo))

            self.assertEqual(calls, 1)
            queue = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
            self.assertIn("EAV-2 | loader | `BLOCKED`", queue)
            self.assertIn("EAV-3 | diff | `TODO`", queue)


def pinned_runner_repo(root: Path, workers: list[str], pins: dict[str, str]) -> Path:
    """Like committed_runner_repo, but the queue table carries a `worker`
    column; pins maps task id -> cell text (empty string means unpinned)."""
    repo = committed_runner_repo(root, workers)
    queue = (
        "# TASK_QUEUE\n\n"
        "| # | ID | name | status | start baseline | end baseline |\n"
        "|---|---|---|---|---|---|\n"
        "| 0 | OLD-1 | closed before the column existed | `DONE` | 1 passed | 1 passed |\n\n"
        "| # | ID | name | status | worker | start baseline | end baseline |\n"
        "|---|---|---|---|---|---|---|\n"
        f"| 1 | EAV-2 | loader | `TODO` | {pins.get('EAV-2', '')} | 1 passed | \u2014 |\n"
        f"| 2 | EAV-3 | diff | `TODO` | {pins.get('EAV-3', '')} | 1 passed | \u2014 |\n\n"
        "<!-- task:EAV-2 files: eav2.txt -->\n"
        "<!-- task:EAV-3 files: eav3.txt -->\n"
    )
    (repo / "TASK_QUEUE.md").write_text(queue, encoding="utf-8")
    run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
    result = run("git", "-C", str(repo), "commit", "-q", "-m", "pinned queue")
    if result.returncode:
        raise AssertionError(result.stderr)
    return repo


class WorkerPinTests(unittest.TestCase):
    """Per-task `worker` column (2026-09-02): one run routes each task to the
    CLI named on its row, falls back to the run-wide list, and refuses an
    unknown id at admission."""

    def setUp(self) -> None:
        self.gate = load_gate_module()

    @staticmethod
    def args(repo: Path) -> SimpleNamespace:
        return SimpleNamespace(
            repo=str(repo), max_tasks=1, strict_admit=True, force=True
        )

    def _finishing_spawn(self, calls: list[str]):
        def fake_spawn(repo_arg, cfg, worker, prompt, prompt_file, **kwargs):
            calls.append(worker)
            queue_path = repo_arg / "TASK_QUEUE.md"
            queue = queue_path.read_text(encoding="utf-8")
            (repo_arg / "eav2.txt").write_text("done\n", encoding="utf-8")
            queue_path.write_text(
                queue.replace("EAV-2 | loader | `TODO`", "EAV-2 | loader | `DONE`"),
                encoding="utf-8",
            )
            run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md", "eav2.txt")
            committed = run("git", "-C", str(repo_arg), "commit", "-q", "-m", "finish EAV-2")
            self.assertEqual(committed.returncode, 0, committed.stderr)
            return self.gate.WorkerResult(0, "done"), None

        return fake_spawn

    def test_parse_task_workers_is_per_table_and_tolerates_dashes(self) -> None:
        text = (
            "| # | ID | name | status | start | end |\n"
            "|---|---|---|---|---|---|\n"
            "| 1 | OLD-1 | x | `DONE` | 1 | 1 |\n\n"
            "| # | ID | name | status | worker | start | end |\n"
            "|---|---|---|---|---|---|---|\n"
            "| 2 | NEW-1 | a | `TODO` | `Codex` | 1 | \u2014 |\n"
            "| 3 | NEW-2 | b | `TODO` | \u2014 | 1 | \u2014 |\n"
            "| 4 | NEW-3 | c | `TODO` | | 1 | \u2014 |\n"
            "| 5 | NEW-4 | d | `TODO` | kimi | 1 | \u2014 |\n\n"
            "| # | ID | name | status | start | end |\n"
            "|---|---|---|---|---|---|\n"
            "| 6 | LATER-1 | e | `TODO` | claude | 1 |\n"
        )
        pins = self.gate.parse_task_workers(text)
        self.assertEqual(pins, {"NEW-1": "codex", "NEW-4": "kimi"})
        self.assertEqual(self.gate.parse_task_workers("no table here"), {})

    def test_pinned_worker_dispatches_first_even_outside_run_wide_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pinned_runner_repo(Path(td), ["codex"], {"EAV-2": "kimi"})
            calls: list[str] = []
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
                self.gate, "resolve_tool", side_effect=lambda name: name
            ), mock.patch.object(
                self.gate, "spawn_worker", side_effect=self._finishing_spawn(calls)
            ):
                self.gate.cmd_run(self.args(repo))
            self.assertEqual(calls, ["kimi"])
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("| EAV-2 | done | 1 | 1 | kimi | kimi |", report)
            journal = (repo / ".gate" / "journal.ndjson").read_text(encoding="utf-8")
            starts = [
                json.loads(line)
                for line in journal.splitlines()
                if json.loads(line).get("event") == "attempt_start"
            ]
            self.assertEqual(starts[-1]["worker"], "kimi")
            self.assertTrue(starts[-1]["pinned"])

    def test_unpinned_row_in_pinned_table_uses_run_wide_list(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pinned_runner_repo(Path(td), ["codex"], {"EAV-3": "kimi"})
            calls: list[str] = []
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
                self.gate, "resolve_tool", side_effect=lambda name: name
            ), mock.patch.object(
                self.gate, "spawn_worker", side_effect=self._finishing_spawn(calls)
            ):
                self.gate.cmd_run(self.args(repo))
            self.assertEqual(calls, ["codex"])

    def test_benched_pin_falls_through_to_run_wide_worker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pinned_runner_repo(Path(td), ["codex"], {"EAV-2": "kimi"})
            (repo / ".gate" / "tool-status.json").write_text(
                json.dumps({"kimi": {"disabled_until": 4102444800, "reason": "quota"}}),
                encoding="utf-8",
            )
            calls: list[str] = []
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
                self.gate, "resolve_tool", side_effect=lambda name: name
            ), mock.patch.object(
                self.gate, "spawn_worker", side_effect=self._finishing_spawn(calls)
            ):
                self.gate.cmd_run(self.args(repo))
            self.assertEqual(calls, ["codex"])
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("| EAV-2 | done | 1 | 1 | codex | kimi |", report)

    def test_failed_pinned_attempt_rotates_to_run_wide_worker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pinned_runner_repo(Path(td), ["codex"], {"EAV-2": "claude"})
            calls: list[str] = []
            finishing = self._finishing_spawn(calls)

            def fake_spawn(repo_arg, cfg, worker, prompt, prompt_file, **kwargs):
                if worker == "claude":
                    calls.append(worker)
                    return self.gate.WorkerResult(1, "gave up without committing"), None
                return finishing(repo_arg, cfg, worker, prompt, prompt_file, **kwargs)

            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
                self.gate, "resolve_tool", side_effect=lambda name: name
            ), mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                self.gate.cmd_run(self.args(repo))
            self.assertEqual(calls, ["claude", "codex"])

    def test_unknown_pin_is_refused_at_admission(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = pinned_runner_repo(Path(td), ["codex"], {"EAV-2": "gpt5"})
            calls: list[str] = []
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), mock.patch.object(
                self.gate, "resolve_tool", side_effect=lambda name: name
            ), mock.patch.object(
                self.gate, "spawn_worker", side_effect=self._finishing_spawn(calls)
            ):
                with self.assertRaisesRegex(SystemExit, "3"):
                    self.gate.cmd_run(self.args(repo))
            self.assertEqual(calls, [])
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("admit_refused:EAV-2", report)
            admit = run(sys.executable, str(GATE), "admit", "--repo", str(repo))
            self.assertIn("unknown-worker: 'gpt5'", admit.stdout)
            self.assertIn("[worker: gpt5]", admit.stdout)


class DeclaredOrderTests(unittest.TestCase):
    """`<!-- task:X after: Y -->` lets sequential slices share a file."""

    def setUp(self) -> None:
        self.gate = load_gate_module()

    def _cfg(self) -> dict:
        cfg = dict(self.gate.DEFAULT_CONFIG)
        cfg["verify_cmd"] = "echo 1 passed"
        return cfg

    def test_shared_file_refused_without_order_and_admitted_with_it(self) -> None:
        base = (
            "| # | ID | name | status | start | end |\n"
            "|---|---|---|---|---|---|\n"
            "| 1 | A-1 | shell | `TODO` | 1 | 1 |\n"
            "| 2 | A-2 | panel | `TODO` | 1 | 1 |\n\n"
            "<!-- task:A-1 files: src/App.tsx, src/main.tsx -->\n"
            "<!-- task:A-2 files: src/App.tsx, src/Panel.tsx -->\n"
        )
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            g = self.gate
            v = g.admit_verdicts(
                self._cfg(), repo, g.parse_queue(base), g.parse_task_files(base),
                {}, g.parse_task_after(base),
            )
            self.assertEqual(v["A-1"]["verdict"], "refuse")
            self.assertIn("overlaps", v["A-1"]["reason"])

            ordered = base + "<!-- task:A-2 after: A-1 -->\n"
            v = g.admit_verdicts(
                self._cfg(), repo, g.parse_queue(ordered), g.parse_task_files(ordered),
                {}, g.parse_task_after(ordered),
            )
            self.assertEqual(v["A-1"]["verdict"], "admit", v["A-1"])
            self.assertEqual(v["A-2"]["verdict"], "admit", v["A-2"])

    def test_dependency_after_dependent_or_missing_is_refused(self) -> None:
        text = (
            "| # | ID | name | status | start | end |\n"
            "|---|---|---|---|---|---|\n"
            "| 1 | B-1 | first | `TODO` | 1 | 1 |\n"
            "| 2 | B-2 | second | `TODO` | 1 | 1 |\n\n"
            "<!-- task:B-1 files: a.txt -->\n"
            "<!-- task:B-2 files: b.txt -->\n"
            "<!-- task:B-1 after: B-2 -->\n"
            "<!-- task:B-2 after: B-9 -->\n"
        )
        with tempfile.TemporaryDirectory() as td:
            g = self.gate
            v = g.admit_verdicts(
                self._cfg(), Path(td), g.parse_queue(text), g.parse_task_files(text),
                {}, g.parse_task_after(text),
            )
            self.assertIn("out-of-order: B-2 must come before B-1", v["B-1"]["reason"])
            self.assertIn("unknown-dependency: B-9", v["B-2"]["reason"])


if __name__ == "__main__":
    unittest.main()

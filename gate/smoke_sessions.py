"""Two real Codex tasks in a disposable repository, with real gates/judges.

Run explicitly: python gate/smoke_sessions.py
Uses the installed Codex account; retains the temporary repo and report.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE = HERE / "gate.py"


def main():
    repo = Path(tempfile.mkdtemp(prefix="gate-session-pilot-"))
    print(f"Pilot repository: {repo}", flush=True)
    env = dict(os.environ)
    env.pop("GATE_CONFIG", None)

    def command(*argv):
        subprocess.run(list(argv), cwd=repo, env=env, check=True)

    def write(name, content):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    command("git", "init", "-q", "-b", "main")
    command("git", "config", "user.name", "Gate Session Pilot")
    command("git", "config", "user.email", "gate-pilot@example.invalid")
    write(".gitignore", ".gate/\n__pycache__/\n")
    write("AGENTS.md", "# Pilot\nWork on main. Execute only the dispatched task. "
          "Do not push, create PRs, install packages, or access another repository. "
          "The gate.py verify command provided in your task is the only external code "
          "needed for this fixture. Use Python stdlib unittest. Keep source readable.\n")
    write("CONTEXT.md", "# Pilot\nTwo related string utility tasks; no external data. "
          "Keep normalise_label's default behavior case-preserving. "
          "The queue is authoritative for progress.\n")
    write("docs/work/labels/spec.md", "# Labels\nSMK-1: normalise_label(text) must trim "
          "and collapse internal whitespace to one space. Add a test for mixed "
          "spaces/tabs. SMK-2: add an optional keyword-only casefold=False argument; "
          "True uses str.casefold, including non-ASCII text. Add a test exercising "
          "that option and preserve the default. Each task should add one test "
          "method; total counts should be 2 then 3. Do not modify this spec.\n")
    write("label_utils.py", "def normalise_label(text):\n    return text.strip()\n")
    write("tests/test_labels.py", "import unittest\nfrom label_utils import normalise_label\n\n"
          "class LabelsTest(unittest.TestCase):\n"
          "    def test_trim_preserves_case(self):\n"
          "        self.assertEqual(normalise_label('  Hello  '), 'Hello')\n")
    write("TASK_QUEUE.md", "# TASK_QUEUE\n\n"
          "| # | ID | name | status | start baseline | end baseline |\n"
          "|---|---|---|---|---|---|\n"
          "| 1 | SMK-1 | collapse whitespace | `TODO` | 1 passed | 2 passed |\n"
          "| 2 | SMK-2 | optional case folding | `TODO` | 2 passed | 3 passed |\n\n"
          "<!-- task:SMK-1 files: label_utils.py, tests/test_labels.py -->\n"
          "<!-- task:SMK-2 files: label_utils.py, tests/test_labels.py -->\n"
          "<!-- task:SMK-2 after: SMK-1 -->\n"
          "<!-- task:SMK-1 session: docs/work/labels -->\n"
          "<!-- task:SMK-2 session: docs/work/labels -->\n")
    cfg = {
        "workers": ["codex"], "session_reuse": {"enabled": True},
        "probe": {"enabled": False}, "notify_cmd": "", "strict_admit": True,
        "max_tasks": 2, "max_attempts_per_task": 1,
        "task_timeout_s": 480, "run_timeout_s": 1800, "heartbeat_s": 30,
        "verify_cmd": f'"{sys.executable}" -m unittest discover -s tests -q',
        "verify_timeout_s": 60, "count_regex": r"Ran (\d+) tests?",
        "doc_only_globs": ["*.md", "docs/**", ".gitignore"],
        "judge": {"enabled": True, "chain": ["codex"], "timeout_s": 240},
        "worker_prompt": (
            "Execute exactly task {task} in this disposable pilot. Read AGENTS.md, "
            "CONTEXT.md, TASK_QUEUE.md and docs/work/labels/spec.md. "
            "Mark only this task IN_PROGRESS and commit that queue claim. Implement "
            "its acceptance, adding its test. Prove the new test fails with the "
            "implementation reverted, then restore the implementation. Run this "
            f"acceptance command in the foreground:\npython {GATE.as_posix()} verify --repo .\n"
            "After PASS, mark only this task DONE with measured counts and commit "
            "the code/tests/queue together, enumerating those paths. Never use "
            "--no-verify, push, or open PRs. Stop after this task."
        ),
    }
    write(".gate/config.json", json.dumps(cfg, indent=2))
    command("git", "add", ".gitignore", "AGENTS.md", "CONTEXT.md", "TASK_QUEUE.md",
            "docs/work/labels/spec.md", "label_utils.py", "tests/test_labels.py")
    command("git", "commit", "-qm", "initialize session pilot")
    command(sys.executable, str(GATE), "install-hook", "--repo", str(repo))
    command(sys.executable, str(GATE), "verify", "--repo", str(repo))
    command(sys.executable, str(GATE), "run", "--repo", str(repo), "--max-tasks", "2", "--strict-admit")
    events = [json.loads(line) for line in (repo / ".gate/journal.ndjson").read_text(encoding="utf-8").splitlines()]
    checkpoints = [e for e in events if e["event"] == "session_checkpoint"]
    assert len(checkpoints) == 2, checkpoints
    assert [e["mode"] for e in checkpoints] == ["fresh", "resume"], checkpoints
    assert checkpoints[0]["session_id"] == checkpoints[1]["session_id"], checkpoints
    assert all(e["reusable"] for e in checkpoints), checkpoints
    assert len([e for e in events if e["event"] == "task_done"]) == 2
    command(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q")
    print(f"PILOT PASS: two tasks, one worker session, separate gates and judges. Report: {repo / '.gate/RUN-REPORT.md'}", flush=True)


if __name__ == "__main__":
    main()

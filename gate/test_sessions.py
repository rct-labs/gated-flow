"""Session reuse must save context without bypassing the one-task gate."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_compat import committed_runner_repo, load_gate_module, run

SID = "12345678-1234-1234-1234-123456789abc"


def session_repo(root: Path):
    gate = load_gate_module()
    repo = committed_runner_repo(root, ["codex"])
    package = repo / "docs/work/demo"
    package.mkdir(parents=True)
    (package / "spec.md").write_text("Implement both independent files.\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Keep task boundaries.\n", encoding="utf-8")
    (repo / "unrelated.txt").write_text("original\n", encoding="utf-8")
    queue = repo / "TASK_QUEUE.md"
    queue.write_text(queue.read_text(encoding="utf-8") +
                     "<!-- task:EAV-2 session: docs/work/demo -->\n"
                     "<!-- task:EAV-3 session: docs/work/demo -->\n", encoding="utf-8")
    config = repo / ".gate/config.json"
    cfg = json.loads(config.read_text(encoding="utf-8"))
    cfg.update(session_reuse={"enabled": True}, judge={"enabled": True}, max_tasks=2)
    config.write_text(json.dumps(cfg), encoding="utf-8")
    result = run("git", "-C", str(repo), "add", "docs/work/demo/spec.md", "AGENTS.md",
                 "unrelated.txt", "TASK_QUEUE.md", ".gate/config.json")
    assert result.returncode == 0, result.stderr
    result = run("git", "-C", str(repo), "commit", "-qm", "session fixture")
    assert result.returncode == 0, result.stderr
    return gate, repo, gate.load_config(repo)


def events(sid=SID, input_tokens=1000):
    return "\n".join(json.dumps(e) for e in [
        {"type": "thread.started", "thread_id": sid},
        {"type": "turn.completed", "usage": {"input_tokens": input_tokens,
                                              "cached_input_tokens": 800,
                                              "output_tokens": 100}},
    ])


def pass_judge():
    return {"final": "pass", "rounds": [{"score": 95, "worker": "fable"}]}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.gate, self.repo, self.cfg = session_repo(Path(self.tmp.name))
        self.run_dir = self.repo / ".gate/runs/test"
        self.run_dir.mkdir(parents=True)
        self.sessions = self.gate.CodexTaskSession(self.repo, self.cfg, self.run_dir)

    def select(self, task="EAV-2", worker="codex", dispatch=1, files=None):
        return self.sessions.select(task, worker, (self.repo / "TASK_QUEUE.md").read_text(encoding="utf-8"),
                                    dispatch, files or ["eav2.txt"])

    def checkpoint(self, **overrides):
        decision = self.select()
        values = dict(decision=decision, evidence=self.sessions.read_events(events()),
                      outcome="done", judge=pass_judge(), dispatches=1, returncode=0,
                      started=time.time() - 10, worker_head=decision["before"]["head"])
        self.gate.write_verdict(self.repo, {"result": "PASS", "at_epoch": time.time()})
        values.update(overrides)
        return self.sessions.finish(**values)

    def test_resume_has_explicit_id_same_exec_permissions_and_incremental_prompt(self):
        self.checkpoint()
        decision = self.select("EAV-3")
        self.assertEqual(decision["mode"], "resume")
        argv = self.sessions.template(decision)
        self.assertEqual(argv[:7], self.gate.WORKER_CMDS["codex"][:7])
        self.assertEqual(argv[-3:], ["resume", SID, "-"])
        self.assertNotIn("--last", argv)
        prompt = self.sessions.prompt(decision, "LONG COMMON BACKGROUND", ["eav3.txt"])
        self.assertIn("ONLY task EAV-3", prompt)
        self.assertIn("Previous task EAV-2", prompt)
        self.assertNotIn("LONG COMMON BACKGROUND", prompt)
        self.assertIn("Previous test results do not validate this task", prompt)

    def test_disabled_unmarked_other_workers_and_custom_commands_keep_old_path(self):
        self.sessions.options["enabled"] = False
        self.assertEqual(self.select()["mode"], "off")
        self.sessions.options["enabled"] = True
        for worker in ("claude", "kimi", "grok"):
            self.assertEqual(self.select(worker=worker)["mode"], "off")
        self.assertEqual(self.select("EAV-99")["mode"], "off")
        self.cfg["worker_cmds"] = {"codex": ["codex", "exec", "-"]}
        self.assertEqual(self.select()["mode"], "off")

    def test_invalid_duplicate_or_escaping_group_never_resumes(self):
        path = self.repo / "TASK_QUEUE.md"
        for marker in ("../../outside", "docs/work/missing", "docs/work/demo -->\n<!-- task:EAV-2 session: docs/work/demo"):
            path.write_text(f"<!-- task:EAV-2 session: {marker} -->\n")
            self.assertEqual(self.select()["mode"], "off")

    def test_rules_content_change_is_seen_even_with_same_git_status(self):
        path = self.repo / "AGENTS.md"
        path.write_text("version one\n")
        self.checkpoint()
        before = self.gate.git(self.repo, "status", "--porcelain")
        path.write_text("version two\n")
        self.assertEqual(before, self.gate.git(self.repo, "status", "--porcelain"))
        self.assertEqual(self.select("EAV-3")["reason"], "background_changed")

    def test_unrelated_dirty_file_may_remain_but_second_edit_invalidates(self):
        path = self.repo / "unrelated.txt"
        path.write_text("first edit\n")
        self.checkpoint()
        self.assertEqual(self.select("EAV-3")["mode"], "resume")
        path.write_text("second edit\n")
        self.assertEqual(self.select("EAV-3")["reason"], "workspace_changed")

    def test_ignored_rule_file_content_change_also_invalidates(self):
        self.sessions.options["context_files"] = ["AGENTS.md", ".gate/shared-rule.md"]
        path = self.repo / ".gate/shared-rule.md"
        path.write_text("first rule\n")
        self.checkpoint()
        before = self.sessions.snapshot()
        path.write_text("second rule\n")
        self.assertEqual(before, self.sessions.snapshot())
        self.assertEqual(self.select("EAV-3")["mode"], "fresh")

    def test_wrong_resumed_thread_cannot_seed_checkpoint(self):
        self.checkpoint()
        decision = self.select("EAV-3")
        summary = self.sessions.finish(
            decision, self.sessions.read_events(events("87654321-1234-1234-1234-123456789abc")),
            "done", pass_judge(), 1, 0, time.time() - 10, decision["before"]["head"])
        self.assertFalse(summary["reusable"])

    @unittest.skipIf(sys.version_info < (3, 11), "TOML normalization uses stdlib tomllib")
    def test_codex_trusted_registration_is_bookkeeping_but_untrusted_is_not(self):
        self.sessions.options["context_files"] = ["AGENTS.md", ".gate/config.toml"]
        path = self.repo / ".gate/config.toml"
        path.write_text('model = "same-model"\n', encoding="utf-8")
        self.checkpoint()
        path.write_text('model = "same-model"\n[projects."test-repo"]\ntrust_level = "trusted"\n', encoding="utf-8")
        self.assertEqual(self.select("EAV-3")["mode"], "resume")
        path.write_text('model = "same-model"\n[projects."test-repo"]\ntrust_level = "untrusted"\n', encoding="utf-8")
        self.assertEqual(self.select("EAV-3")["mode"], "fresh")
        path.write_text('model = "different-model"\n', encoding="utf-8")
        self.assertEqual(self.select("EAV-3")["mode"], "fresh")

    def test_staged_edit_with_worktree_restored_is_not_invisible(self):
        self.checkpoint()
        path = self.repo / "unrelated.txt"
        path.write_text("staged\n")
        run("git", "-C", str(self.repo), "add", "unrelated.txt")
        path.write_text("original\n")
        self.assertEqual(self.select("EAV-3")["reason"], "workspace_changed")

    def test_untracked_second_edit_invalidates(self):
        path = self.repo / "new.txt"
        path.write_text("first\n")
        self.checkpoint()
        path.write_text("second\n")
        self.assertEqual(self.select("EAV-3")["reason"], "workspace_changed")

    def test_retry_budget_and_irreversible_never_resume(self):
        self.checkpoint()
        self.assertEqual(self.select("EAV-3", dispatch=2)["reason"], "retry")
        self.sessions.options["max_tasks"] = 1
        self.assertEqual(self.select("EAV-3")["reason"], "task_budget")
        self.sessions.options["max_tasks"] = 3
        self.sessions.options["max_input_tokens"] = 500
        self.assertEqual(self.select("EAV-3")["reason"], "input_budget")
        self.assertEqual(self.select("EAV-3", files=["data/dev.db"])["mode"], "off")

    def test_failure_revision_skipped_judge_or_missing_usage_cannot_seed_checkpoint(self):
        for changes in ({"outcome": "worker_left_changes"}, {"returncode": 1},
                        {"dispatches": 2}, {"judge": None},
                        {"judge": {"final": "skipped", "rounds": []}},
                        {"judge": {"final": "pass", "rounds": [{}, {}]}},
                        {"evidence": {"completed": True, "session_id": SID, "usage": {}}},
                        {"evidence": self.sessions.read_events("not json")},
                        {"worker_head": "unexpected"}):
            with self.subTest(changes=changes):
                self.assertFalse(self.checkpoint(**changes)["reusable"])
                self.assertIsNone(self.sessions.previous)

    def test_stale_verdict_cannot_seed_checkpoint(self):
        self.assertFalse(self.checkpoint(started=time.time() + 100)["reusable"])

    def test_existing_checkpoint_is_not_loaded_on_new_run(self):
        self.checkpoint()
        self.assertTrue((self.run_dir / "session.json").exists())
        other = self.gate.CodexTaskSession(self.repo, self.cfg, self.run_dir)
        self.assertIsNone(other.previous)

    def test_changed_group_resets_even_when_same_files(self):
        self.checkpoint()
        group = self.repo / "docs/work/second"
        group.mkdir()
        (group / "spec.md").write_text("second")
        queue = self.repo / "TASK_QUEUE.md"
        queue.write_text(queue.read_text(encoding="utf-8").replace("task:EAV-3 session: docs/work/demo",
                          "task:EAV-3 session: docs/work/second"), encoding="utf-8")
        self.assertEqual(self.select("EAV-3")["reason"], "group_changed")


class RunnerSessionTests(unittest.TestCase):
    def test_judge_schema_is_accepted_by_codex_strict_output_contract(self):
        def check(schema):
            if schema.get("type") == "object":
                self.assertEqual(set(schema["properties"]), set(schema["required"]))
                self.assertFalse(schema["additionalProperties"])
                for child in schema["properties"].values():
                    check(child)
            if schema.get("type") == "array":
                check(schema["items"])
        check(load_gate_module().JUDGE_SCHEMA)

    def exercise(self, judge_revision=False, fail_first=False):
        with tempfile.TemporaryDirectory() as temp:
            gate, repo, cfg = session_repo(Path(temp))
            seen = []

            def spawn(repo, cfg, worker, prompt, prompt_file, **kwargs):
                tid = "EAV-2" if "ONLY task EAV-2" in prompt else "EAV-3"
                seen.append((tid, kwargs["template"], prompt))
                if fail_first:
                    (repo / "eav2.txt").write_text("unfinished")
                    return gate.WorkerResult(1, events()), None
                queue = repo / "TASK_QUEUE.md"
                lines = queue.read_text(encoding="utf-8").splitlines()
                queue.write_text("\n".join(line.replace("`TODO`", "`DONE`") if f"| {tid} |" in line else line
                                            for line in lines) + "\n", encoding="utf-8")
                name = "eav2.txt" if tid == "EAV-2" else "eav3.txt"
                (repo / name).write_text("done\n")
                gate.write_verdict(repo, {"result": "PASS", "at_epoch": time.time(),
                                          "cmd": "fixture", "exit_code": 0, "count": 1})
                run("git", "-C", str(repo), "add", "TASK_QUEUE.md", name)
                result = run("git", "-C", str(repo), "commit", "-qm", tid)
                self.assertEqual(result.returncode, 0, result.stderr)
                return gate.WorkerResult(0, events()), None

            def judge(*args):
                result = pass_judge()
                if judge_revision:
                    result["rounds"].append({"score": 95, "worker": "codex"})
                return result

            with mock.patch.object(gate, "resolve_tool", side_effect=lambda n: n), \
                 mock.patch.object(gate, "spawn_worker", side_effect=spawn), \
                 mock.patch.object(gate, "judge_task", side_effect=judge), \
                 mock.patch.dict("os.environ", {"GATE_CONFIG": ""}), redirect_stdout(io.StringIO()):
                args = SimpleNamespace(repo=str(repo), max_tasks=2, strict_admit=True, force=True)
                if fail_first:
                    with self.assertRaises(SystemExit):
                        gate.cmd_run(args)
                else:
                    gate.cmd_run(args)
            report = (repo / ".gate/RUN-REPORT.md").read_text(encoding="utf-8")
            journal = [json.loads(line) for line in (repo / ".gate/journal.ndjson").read_text(encoding="utf-8").splitlines()]
            return seen, report, journal

    def test_two_tasks_close_separately_and_second_resumes(self):
        seen, report, journal = self.exercise()
        self.assertEqual([t[0] for t in seen], ["EAV-2", "EAV-3"])
        self.assertNotIn("resume", seen[0][1])
        self.assertIn("resume", seen[1][1])
        self.assertIn("| EAV-3 | resume | eligible", report)
        self.assertEqual(len([e for e in journal if e["event"] == "task_done"]), 2)

    def test_judge_revision_forces_next_task_fresh(self):
        seen, report, _ = self.exercise(judge_revision=True)
        self.assertNotIn("resume", seen[1][1])

    def test_partial_failure_does_not_dispatch_next_task(self):
        seen, report, _ = self.exercise(fail_first=True)
        self.assertEqual(len(seen), 1)
        self.assertIn("worker_left_changes:EAV-2", report)


if __name__ == "__main__":
    unittest.main()

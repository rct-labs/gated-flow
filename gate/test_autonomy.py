"""Regression tests for the 2026-09-03 autonomy upgrade of gate.py:
judge chain + bounded revision loop, usage probe, reversibility approval.
Design: docs/autonomy.md."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_compat import GATE, committed_runner_repo, load_gate_module, run


def judged_runner_repo(root: Path, workers: list[str], judge: dict | None = None,
                       verify_cmd: str | None = None) -> Path:
    """committed_runner_repo with the judge switched on and the probe off."""
    repo = committed_runner_repo(root, workers)
    cfg_path = repo / ".gate" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["judge"] = {"enabled": True, "chain": ["fable", "opus", "codex"],
                    "pass_score": 85, "max_revisions": 2, "min_gain": 5, "timeout_s": 60}
    if judge:
        cfg["judge"].update(judge)
    if verify_cmd:
        cfg["verify_cmd"] = verify_cmd
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    run("git", "-C", str(repo), "add", ".gate/config.json")
    run("git", "-C", str(repo), "commit", "-q", "-m", "judge on")
    return repo


def verdict(score: int, kind: str, findings=None, brief: str = "") -> dict:
    return {"score": score, "verdict": kind,
            "findings": findings if findings is not None else
            [{"severity": "medium", "file": "eav2.txt", "issue": "x", "fix": "y"}],
            "revision_brief": brief}


class JudgeLoopTests(unittest.TestCase):
    """Judge after every task_done, bounded iteration (initial + 2 revisions),
    worker rotation, no-progress early stop, and the runner's own re-verify
    after a revision commit (flow-run-autonomy.md section 3)."""

    def setUp(self) -> None:
        self.gate = load_gate_module()

    @staticmethod
    def args(repo: Path, max_tasks: int = 1) -> SimpleNamespace:
        return SimpleNamespace(repo=str(repo), max_tasks=max_tasks, strict_admit=True, force=True)

    def _spawn(self, workers_seen: list, revision_commits: bool = True,
               break_file: bool = False):
        """Fake worker: the task attempt closes EAV-2; a revision prompt
        rewrites eav2.txt and commits (or not)."""
        gate = self.gate

        def fake_spawn(repo_arg, cfg, worker, prompt, prompt_file, **kwargs):
            is_revision = "revising ONE task" in prompt
            workers_seen.append(("rev" if is_revision else "task", worker))
            if is_revision:
                if revision_commits:
                    (repo_arg / "eav2.txt").write_text("revised\n", encoding="utf-8")
                    paths = ["eav2.txt"]
                    if break_file:
                        (repo_arg / "broken.txt").write_text("x\n", encoding="utf-8")
                        paths.append("broken.txt")
                    run("git", "-C", str(repo_arg), "add", *paths)
                    run("git", "-C", str(repo_arg), "commit", "-q", "-m", "revise EAV-2")
                return gate.WorkerResult(0, "revised"), None
            queue_path = repo_arg / "TASK_QUEUE.md"
            queue = queue_path.read_text(encoding="utf-8")
            (repo_arg / "eav2.txt").write_text("done\n", encoding="utf-8")
            queue_path.write_text(
                queue.replace("EAV-2 | loader | `TODO`", "EAV-2 | loader | `DONE`"),
                encoding="utf-8",
            )
            run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md", "eav2.txt")
            run("git", "-C", str(repo_arg), "commit", "-q", "-m", "finish EAV-2")
            return gate.WorkerResult(0, "done"), None
        return fake_spawn

    def _judge(self, verdicts: list[dict], members: list[str] | None = None):
        calls = {"n": 0}

        def fake_judge_once(repo, cfg, prompt, run_dir, tag, run_id):
            i = calls["n"]
            calls["n"] += 1
            if i >= len(verdicts):
                raise AssertionError("judge called more often than planned")
            return verdicts[i], (members or ["fable"] * len(verdicts))[i]
        return fake_judge_once, calls

    def _run(self, repo, spawn, judge, expect_exit: str | None = None):
        patches = [
            mock.patch.dict(os.environ, {"GATE_CONFIG": ""}),
            mock.patch.object(self.gate, "resolve_tool", side_effect=lambda name: name),
            mock.patch.object(self.gate, "spawn_worker", side_effect=spawn),
            mock.patch.object(self.gate, "judge_once", side_effect=judge),
        ]
        for p in patches:
            p.start()
        try:
            if expect_exit:
                with self.assertRaisesRegex(SystemExit, expect_exit):
                    self.gate.cmd_run(self.args(repo))
            else:
                self.gate.cmd_run(self.args(repo))
        finally:
            for p in patches:
                p.stop()
        return (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")

    def test_pass_first_round_dispatches_no_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude", "codex"])
            seen: list = []
            judge, calls = self._judge([verdict(92, "pass", findings=[])])
            report = self._run(repo, self._spawn(seen), judge)
            self.assertEqual(calls["n"], 1)
            self.assertEqual([k for k, _ in seen], ["task"])
            self.assertIn("| EAV-2 | pass | 1 | 92 |", report)
            self.assertIn("stopped because: **budget**", report)
            self.assertIn("## Waiting on you\n\n- nothing", report)

    def test_revise_rotates_worker_then_passes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude", "codex"])
            seen: list = []
            judge, calls = self._judge([verdict(70, "revise", brief="fix x"),
                                        verdict(90, "pass", findings=[])])
            report = self._run(repo, self._spawn(seen), judge)
            self.assertEqual(calls["n"], 2)
            self.assertEqual(seen, [("task", "claude"), ("rev", "codex")])
            self.assertIn("| EAV-2 | pass | 2 | 70 → 90 | claude, codex |", report)
            log = run("git", "-C", str(repo), "log", "--format=%s").stdout.splitlines()
            self.assertEqual(log[0], "revise EAV-2")
            queue = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
            self.assertIn("EAV-2 | loader | `DONE`", queue)
            self.assertIn("EAV-3 | diff | `TODO`", queue)

    def test_revision_cap_escalates_and_stops_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude", "codex"])
            seen: list = []
            judge, calls = self._judge([verdict(60, "revise"), verdict(70, "revise"),
                                        verdict(80, "revise")])
            report = self._run(repo, self._spawn(seen), judge, expect_exit="3")
            self.assertEqual(calls["n"], 3)
            self.assertEqual([k for k, _ in seen], ["task", "rev", "rev"])
            self.assertIn("judge_escalated:EAV-2", report)
            self.assertIn("revision cap 2 reached", report)
            self.assertIn("### EAV-2 — last findings", report)

    def test_no_gain_stops_before_cap(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude", "codex"])
            seen: list = []
            judge, calls = self._judge([verdict(60, "revise"), verdict(62, "revise")])
            report = self._run(repo, self._spawn(seen), judge, expect_exit="3")
            self.assertEqual(calls["n"], 2)
            self.assertEqual([k for k, _ in seen], ["task", "rev"])
            self.assertIn("no progress: 60 → 62", report)

    def test_escalate_verdict_stops_without_revision(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude", "codex"])
            seen: list = []
            judge, calls = self._judge([verdict(50, "escalate")])
            report = self._run(repo, self._spawn(seen), judge, expect_exit="3")
            self.assertEqual([k for k, _ in seen], ["task"])
            self.assertIn("judge_escalated:EAV-2", report)
            self.assertIn("judge asked for a human", report)

    def test_revision_that_fails_acceptance_stops_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            verify = (
                f'"{sys.executable}" -c "import os,sys; print(\'1 passed\'); '
                "sys.exit(1 if os.path.exists('broken.txt') else 0)\""
            )
            repo = judged_runner_repo(Path(td), ["claude", "codex"], verify_cmd=verify)
            seen: list = []
            judge, calls = self._judge([verdict(60, "revise"), verdict(95, "pass")])
            report = self._run(repo, self._spawn(seen, break_file=True), judge, expect_exit="3")
            self.assertEqual(calls["n"], 1, "a failing revision is never re-judged")
            self.assertIn("revision_broke_verify:EAV-2", report)
            self.assertIn("fails acceptance", report)
            self.assertIn("Inspect `git log`", report)

    def test_revision_without_commit_counts_and_rotates(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude", "codex"])
            seen: list = []
            judge, calls = self._judge([verdict(60, "revise"), verdict(70, "revise"),
                                        verdict(80, "revise")])
            report = self._run(repo, self._spawn(seen, revision_commits=False), judge,
                               expect_exit="3")
            self.assertEqual(seen, [("task", "claude"), ("rev", "codex"), ("rev", "claude")])
            self.assertIn("revision cap 2 reached", report)

    def test_single_worker_revision_is_flagged_not_rotated(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["codex"])
            seen: list = []
            judge, calls = self._judge([verdict(70, "revise"), verdict(90, "pass", findings=[])])
            report = self._run(repo, self._spawn(seen), judge)
            self.assertEqual(seen, [("task", "codex"), ("rev", "codex")])
            self.assertIn("codex (not rotated), codex", report)
            journal = (repo / ".gate" / "journal.ndjson").read_text(encoding="utf-8")
            self.assertIn('"event": "revision_start"', journal)
            self.assertIn('"rotated": false', journal)

    def test_judge_unavailable_skips_loudly_and_continues(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude", "codex"])
            seen: list = []

            def down(repo_, cfg, prompt, run_dir, tag, run_id):
                return None, "fable:quota; opus:quota; codex:not found"
            report = self._run(repo, self._spawn(seen), down)
            self.assertIn("| EAV-2 | skipped | 0 |", report)
            self.assertIn("closed without a judge review", report)
            self.assertIn("stopped because: **budget**", report)

    def test_judge_disabled_by_default_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["claude"])
            seen: list = []

            def never(*a, **k):
                raise AssertionError("judge must not run when disabled")
            report = self._run(repo, self._spawn(seen), never)
            self.assertNotIn("## Judge", report)
            self.assertEqual([k for k, _ in seen], ["task"])


class JudgeChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.gate = load_gate_module()

    def test_chain_falls_through_on_outage_and_unparsable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            cfg = self.gate.load_config(repo)
            run_dir = repo / ".gate" / "runs" / "t"
            run_dir.mkdir(parents=True)
            seen: list[str] = []

            def fake_spawn(repo_arg, cfg_, tool, prompt, pf, **kw):
                template = kw["template"]
                member = template[template.index("--model") + 1] if "--model" in template else "codex"
                seen.append(member)
                if member == "claude-fable-5-1":
                    return self.gate.WorkerResult(1, "API Error: 429 rate_limit"), None
                if member == "opus":
                    return self.gate.WorkerResult(0, "I think it's fine, no JSON here"), None
                Path(kw["extra"]["out_file"]).write_text(
                    json.dumps({"score": 88, "verdict": "pass", "findings": [],
                                "revision_brief": ""}), encoding="utf-8")
                return self.gate.WorkerResult(0, "ok"), None
            with mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n), \
                 mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                v, member = self.gate.judge_once(repo, cfg, "p", run_dir, "tag", "run")
            # fable's 429 benches the claude tool, so opus (same tool) is skipped
            # as benched and the chain lands on codex.
            self.assertEqual(seen, ["claude-fable-5-1", "codex"])
            self.assertEqual(member, "codex")
            self.assertEqual(v["score"], 88)
            self.assertEqual(self.gate.tool_state(repo)["claude"]["reason"], "quota")

    def test_unparsable_member_falls_through(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            cfg = self.gate.load_config(repo)
            run_dir = repo / ".gate" / "runs" / "t"
            run_dir.mkdir(parents=True)
            seen: list[str] = []

            def fake_spawn(repo_arg, cfg_, tool, prompt, pf, **kw):
                template = kw["template"]
                member = template[template.index("--model") + 1] if "--model" in template else "codex"
                seen.append(member)
                if member == "claude-fable-5-1":
                    return self.gate.WorkerResult(0, "prose only, no JSON"), None
                env = {"type": "result", "structured_output":
                       {"score": 91, "verdict": "pass", "findings": [], "revision_brief": ""}}
                return self.gate.WorkerResult(0, json.dumps(env)), None
            with mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n), \
                 mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                v, member = self.gate.judge_once(repo, cfg, "p", run_dir, "tag", "run")
            self.assertEqual(seen, ["claude-fable-5-1", "opus"])
            self.assertEqual((member, v["score"]), ("opus", 91))

    def test_parse_claude_envelope_and_codex_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "o.json"
            env = json.dumps({"type": "result", "result": "{\"score\":90,\"verdict\":\"pass\"}",
                              "structured_output": {"score": 90, "verdict": "pass",
                                                    "findings": [{"severity": "low", "issue": "i"}],
                                                    "revision_brief": ""}})
            v = self.gate.parse_judge_output("noise\n" + env, out)
            self.assertEqual((v["score"], v["verdict"], len(v["findings"])), (90, "pass", 1))
            out.write_text(json.dumps({"score": 40, "verdict": "revise", "findings": [],
                                       "revision_brief": "b"}), encoding="utf-8")
            v = self.gate.parse_judge_output("", out)
            self.assertEqual((v["score"], v["verdict"]), (40, "revise"))
            self.assertIsNone(self.gate.parse_judge_output(
                "{\"score\": 300, \"verdict\": \"pass\"}", Path(td) / "none"))

    def test_judge_models_remain_explicit(self) -> None:
        self.assertIn("claude-fable-5-1", self.gate.JUDGE_CMDS["fable"])
        self.assertIn("opus", self.gate.JUDGE_CMDS["opus"])
        self.assertEqual(self.gate.JUDGE_CMDS["codex"][2:4], ["--sandbox", "read-only"])

    def test_worker_model_inheritance_and_explicit_pins(self) -> None:
        for flags in [None, [], ["--model", "opus"], ["--model", "claude-fable-5-1"], ["--model=custom"]]:
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as td:
                repo = Path(td)
                cfg = dict(self.gate.DEFAULT_CONFIG)
                if flags is not None:
                    cfg["worker_cmds"] = {"claude": ["claude", "-p", "{prompt}", *flags]}
                proc = SimpleNamespace(stdout=["ok\n"], poll=lambda: 0, returncode=0)
                with mock.patch.object(self.gate, "resolve_tool", return_value="claude"), mock.patch.object(self.gate.subprocess, "Popen", return_value=proc) as spawn:
                    result, error = self.gate.spawn_worker(repo, cfg, "claude", "test prompt", repo / "prompt.txt")
                self.assertIsNone(error)
                self.assertIsNotNone(result)
                argv = spawn.call_args.args[0]
                if flags is None:
                    self.assertFalse(any(arg == "--model" or arg.startswith("--model=") for arg in argv))
                    self.assertIn("acceptEdits", argv)
                else:
                    self.assertEqual(argv, ["claude", "-p", "test prompt", *flags])
                self.assertEqual(spawn.call_args.kwargs["cwd"], str(repo))
                self.assertNotIn("env", spawn.call_args.kwargs)

    def test_sub_cfg_fills_defaults_under_partial_override(self) -> None:
        cfg = dict(self.gate.DEFAULT_CONFIG)
        cfg["judge"] = {"enabled": True}
        j = self.gate.sub_cfg(cfg, "judge")
        self.assertTrue(j["enabled"])
        self.assertEqual(j["chain"], ["fable", "opus", "codex"])
        self.assertEqual(j["max_revisions"], 2)


class ProbeTests(unittest.TestCase):
    """Probe before spend: quota benches for the cooldown, a probe that never
    reached the provider benches for retry_s only (section 2)."""

    def setUp(self) -> None:
        self.gate = load_gate_module()

    @staticmethod
    def args(repo: Path) -> SimpleNamespace:
        return SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=True)

    def _enable_probe(self, repo: Path) -> None:
        cfg_path = repo / ".gate" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["probe"] = {"enabled": True, "timeout_s": 5, "retry_s": 600}
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")

    def test_quota_probe_benches_before_any_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["claude", "codex"])
            self._enable_probe(repo)
            dispatched: list[str] = []

            def fake_spawn(repo_arg, cfg, worker, prompt, pf, **kw):
                if prompt == self.gate.PROBE_PROMPT:
                    if worker == "claude":
                        return self.gate.WorkerResult(1, "You have hit your usage limit"), None
                    return self.gate.WorkerResult(1, "codex: timed out", timed_out=True), None
                dispatched.append(worker)
                return self.gate.WorkerResult(0, "nothing"), None
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), \
                 mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n), \
                 mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                with self.assertRaisesRegex(SystemExit, "3"):
                    self.gate.cmd_run(self.args(repo))
            self.assertEqual(dispatched, [], "no worker may be dispatched when all probes fail")
            st = self.gate.tool_state(repo)
            now = time.time()
            self.assertEqual(st["claude"]["reason"], "probe:quota")
            self.assertGreater(st["claude"]["disabled_until"] - now, 3000)
            self.assertEqual(st["codex"]["reason"], "probe:timeout")
            self.assertLess(st["codex"]["disabled_until"] - now, 700)
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("no_workers:EAV-2", report)

    def test_probe_runs_once_per_run_and_ok_lets_dispatch_through(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["claude"])
            self._enable_probe(repo)
            probes = {"n": 0}
            attempts = {"n": 0}

            def fake_spawn(repo_arg, cfg, worker, prompt, pf, **kw):
                if prompt == self.gate.PROBE_PROMPT:
                    probes["n"] += 1
                    return self.gate.WorkerResult(0, "OK"), None
                attempts["n"] += 1
                return self.gate.WorkerResult(0, f"did nothing {attempts['n']}"), None
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), \
                 mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n), \
                 mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                with self.assertRaisesRegex(SystemExit, "3"):
                    self.gate.cmd_run(self.args(repo))
            self.assertEqual(attempts["n"], 2)
            self.assertEqual(probes["n"], 1, "two attempts, one probe")
            journal = (repo / ".gate" / "journal.ndjson").read_text(encoding="utf-8")
            self.assertIn('"event": "probe"', journal)

    def test_probe_disabled_never_spawns(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["claude"])  # fixture: probe off
            prompts: list[str] = []

            def fake_spawn(repo_arg, cfg, worker, prompt, pf, **kw):
                prompts.append(prompt)
                return self.gate.WorkerResult(0, "nothing"), None
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), \
                 mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n), \
                 mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                with self.assertRaisesRegex(SystemExit, "3"):
                    self.gate.cmd_run(self.args(repo))
            self.assertNotIn(self.gate.PROBE_PROMPT, prompts)


class ApprovalTests(unittest.TestCase):
    """Reversibility tier: irreversible paths need a per-task approval line and
    stop the run even in advisory admission mode (section 4)."""

    def setUp(self) -> None:
        self.gate = load_gate_module()

    def _repo(self, td: Path, approved: bool) -> Path:
        repo = committed_runner_repo(td, ["claude"])
        queue = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
        queue = queue.replace(
            "<!-- task:EAV-2 files: eav2.txt -->",
            "<!-- task:EAV-2 files: drizzle/0001.sql, eav2.txt -->"
            + ("\n<!-- task:EAV-2 approved: 2026-09-03 user -->" if approved else ""))
        (repo / "TASK_QUEUE.md").write_text(queue, encoding="utf-8")
        cfg_path = repo / ".gate" / "config.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["strict_admit"] = False
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        run("git", "-C", str(repo), "add", "TASK_QUEUE.md", ".gate/config.json")
        run("git", "-C", str(repo), "commit", "-q", "-m", "irreversible task")
        return repo

    def test_parse_approved(self) -> None:
        self.assertEqual(
            self.gate.parse_task_approved("<!-- task:X approved: me -->\n<!-- task:Y approved:  -->"),
            {"X": "me"})

    def test_unapproved_irreversible_stops_even_in_advisory_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td), approved=False)
            dispatched: list[str] = []

            def fake_spawn(repo_arg, cfg, worker, prompt, pf, **kw):
                dispatched.append(worker)
                return self.gate.WorkerResult(0, "x"), None
            args = SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=False, force=True)
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), \
                 mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n), \
                 mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                with self.assertRaisesRegex(SystemExit, "3"):
                    self.gate.cmd_run(args)
            self.assertEqual(dispatched, [])
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("needs_approval:EAV-2", report)
            self.assertIn("<!-- task:EAV-2 approved:", report)
            admit = run(sys.executable, str(GATE), "admit", "--repo", str(repo))
            self.assertIn("APPROVAL   EAV-2", admit.stdout)
            self.assertIn("drizzle/0001.sql", admit.stdout)

    def test_approved_irreversible_admits_and_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td), approved=True)
            dispatched: list[str] = []

            def fake_spawn(repo_arg, cfg, worker, prompt, pf, **kw):
                dispatched.append(worker)
                return self.gate.WorkerResult(0, "x"), None
            args = SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=False, force=True)
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}), \
                 mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n), \
                 mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                with self.assertRaisesRegex(SystemExit, "3"):
                    self.gate.cmd_run(args)
            self.assertEqual(dispatched, ["claude", "claude"])
            admit = run(sys.executable, str(GATE), "admit", "--repo", str(repo))
            self.assertIn("ok         EAV-2", admit.stdout)


if __name__ == "__main__":
    unittest.main()

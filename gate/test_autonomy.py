"""Regression tests for the unattended half of gate.py: one package review
per run, usage probe, model call budget, reversibility approval.
Design: docs/autonomy.md."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from test_compat import GATE, committed_runner_repo, load_gate_module, run


def judged_runner_repo(root: Path, workers: list[str], judge: dict | None = None,
                       verify_cmd: str | None = None, max_tasks: int = 1,
                       extra: dict | None = None) -> Path:
    """committed_runner_repo with the review switched on and the probe off."""
    repo = committed_runner_repo(root, workers)
    cfg_path = repo / ".gate" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    cfg["judge"] = {"enabled": True, "chain": ["fable", "opus", "codex"], "timeout_s": 60}
    if judge:
        cfg["judge"].update(judge)
    if verify_cmd:
        cfg["verify_cmd"] = verify_cmd
    cfg["max_tasks"] = max_tasks
    cfg.update(extra or {})
    cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
    run("git", "-C", str(repo), "add", ".gate/config.json")
    run("git", "-C", str(repo), "commit", "-q", "-m", "review on")
    return repo


def verdict(score: int, kind: str, findings=None, brief: str = "") -> dict:
    return {"score": score, "verdict": kind,
            "findings": findings if findings is not None else
            [{"severity": "medium", "file": "eav2.txt", "issue": "x", "fix": "y"}],
            "revision_brief": brief}


HIGH = [{"severity": "high", "file": "eav2.txt", "issue": "wrong", "fix": "redo"}]
NAMES = {"EAV-2": "loader", "EAV-3": "diff"}


class ReviewTests(unittest.TestCase):
    """One package review per run gated on high findings, lesser findings
    become queue rows, the full oracle runs once afterwards, and the model
    call budget bounds the whole run (docs/autonomy.md)."""

    def setUp(self) -> None:
        self.gate = load_gate_module()

    @staticmethod
    def args(repo: Path, max_tasks: int | None = None) -> SimpleNamespace:
        return SimpleNamespace(repo=str(repo), max_tasks=max_tasks, strict_admit=True, force=True)

    def _spawn(self, seen: list, prompts: list | None = None):
        """Fake worker: closes whichever task the prompt names and commits."""
        gate = self.gate

        def fake_spawn(repo_arg, cfg, worker, prompt, prompt_file, **kwargs):
            tid = re.search(r"task (EAV-\d)", prompt).group(1)
            seen.append((tid, worker))
            if prompts is not None:
                prompts.append(prompt)
            queue_path = repo_arg / "TASK_QUEUE.md"
            queue = queue_path.read_text(encoding="utf-8")
            path = repo_arg / f"{tid.lower().replace('-', '')}.txt"
            path.write_text("done\n", encoding="utf-8")
            queue_path.write_text(
                queue.replace(f"{tid} | {NAMES[tid]} | `TODO`", f"{tid} | {NAMES[tid]} | `DONE`"),
                encoding="utf-8",
            )
            run("git", "-C", str(repo_arg), "add", "TASK_QUEUE.md", path.name)
            run("git", "-C", str(repo_arg), "commit", "-q", "-m", f"finish {tid}")
            return gate.WorkerResult(0, "done"), None
        return fake_spawn

    def _judge(self, verdicts: list[dict], members: list[str] | None = None):
        calls = {"n": 0, "prompts": []}

        def fake_judge_once(repo, cfg, prompt, run_dir, tag, run_id):
            i = calls["n"]
            calls["n"] += 1
            calls["prompts"].append(prompt)
            if i >= len(verdicts):
                raise AssertionError("review called more often than planned")
            return verdicts[i], (members or ["fable"] * len(verdicts))[i]
        return fake_judge_once, calls

    def _run(self, repo, spawn, judge, expect_exit: str | None = None, max_tasks=None):
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
                    self.gate.cmd_run(self.args(repo, max_tasks))
            else:
                self.gate.cmd_run(self.args(repo, max_tasks))
        finally:
            for p in patches:
                p.stop()
        return (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")

    def test_two_tasks_one_review_one_full_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude", "codex"], max_tasks=2)
            seen: list = []
            prompts: list = []
            judge, calls = self._judge([verdict(80, "pass", findings=[])])
            report = self._run(repo, self._spawn(seen, prompts), judge)
            self.assertEqual([t for t, _ in seen], ["EAV-2", "EAV-3"])
            self.assertEqual(calls["n"], 1)
            self.assertIn("EAV-2, EAV-3", calls["prompts"][0])
            self.assertIn("| EAV-2, EAV-3 | pass | pass | 80 |", report)
            self.assertIn("## Full acceptance", report)
            self.assertIn("-> **PASS**", report)
            self.assertIn("stopped because: **budget**", report)
            self.assertIn("## Waiting on you\n\n- nothing", report)
            log = run("git", "-C", str(repo), "log", "--oneline").stdout
            self.assertIn("finish EAV-2", log)
            self.assertIn("finish EAV-3", log)
            for prompt in prompts:
                self.assertLess(len(prompt.encode("utf-8")), 6000)
                self.assertIn("[TASK PACKET]", prompt)
                self.assertIn("verify --task EAV-", prompt)
            events = [json.loads(ln) for ln in
                      (repo / ".gate" / "journal.ndjson").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(sum(e["event"] == "full_acceptance" for e in events), 1)
            self.assertEqual(sum(e["event"] == "review_start" for e in events), 1)

    def test_score_below_old_threshold_passes_without_high_findings(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            judge, _ = self._judge([verdict(72, "pass")])  # one medium finding
            report = self._run(repo, self._spawn([]), judge)
            self.assertIn("| EAV-2 | pass | pass | 72 |", report)
            queue = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
            self.assertIn("| EAV-2-R1 | x | `TODO` |", queue)
            self.assertIn("<!-- task:EAV-2-R1 files: eav2.txt -->", queue)
            self.assertIn("<!-- task:EAV-2-R1 origin: review -->", queue)
            self.assertIn("## Waiting on you\n\n- nothing", report)
            # The queue commit leaves a clean tree for the next run.
            self.assertEqual(run("git", "-C", str(repo), "status", "--porcelain",
                                 "--", "TASK_QUEUE.md").stdout.strip(), "")
            self.assertIn("review findings as tasks", run("git", "-C", str(repo), "log",
                                                            "--oneline", "-1").stdout)

    def test_review_rows_inherit_the_local_check_and_old_prompts_are_reset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], extra={
                "judge_prompt": "Judge task {task} at {commit}; pass at {pass_score}."})
            queue = repo / "TASK_QUEUE.md"
            queue.write_text(queue.read_text(encoding="utf-8")
                             + '<!-- task:EAV-2 verify: {"cmd": "python -c \\"print(\'2 passed\')\\"", "timeout_s": 30} -->\n',
                             encoding="utf-8")
            run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
            run("git", "-C", str(repo), "commit", "-q", "-m", "local check")
            judge, calls = self._judge([verdict(70, "pass")])
            self._run(repo, self._spawn([]), judge)
            self.assertIn("Tasks under review: EAV-2.", calls["prompts"][0])
            text = queue.read_text(encoding="utf-8")
            self.assertIn('<!-- task:EAV-2-R1 verify: {"cmd": "python -c', text)
            events = [json.loads(ln) for ln in
                      (repo / ".gate" / "journal.ndjson").read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(e["event"] == "judge_prompt_reset" for e in events))

    def test_high_finding_fails_the_review_and_stops(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude", "codex"])
            seen: list = []
            judge, calls = self._judge([verdict(95, "pass", findings=HIGH)])
            report = self._run(repo, self._spawn(seen), judge, expect_exit="3")
            self.assertEqual(len(seen), 1)  # no revision dispatch
            self.assertIn("stopped because: **review_failed:EAV-2**", report)
            self.assertIn("[high] eav2.txt: wrong", report)
            self.assertNotIn("## Full acceptance", report)
            queue = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
            self.assertNotIn("EAV-2-R1", queue)

    def test_revise_or_escalate_verdict_fails_the_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            judge, _ = self._judge([verdict(60, "escalate")])
            report = self._run(repo, self._spawn([]), judge, expect_exit="3")
            self.assertIn("review_failed:EAV-2", report)
            self.assertIn("did not pass (verdict escalate)", report)

    def test_review_unavailable_skips_loudly_and_still_accepts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])

            def unavailable(repo_, cfg, prompt, run_dir, tag, run_id):
                return None, "fable:quota; opus:quota; codex:not found"
            report = self._run(repo, self._spawn([]), unavailable)
            self.assertIn("| EAV-2 | skipped |", report)
            self.assertIn("## Full acceptance", report)
            self.assertIn("closed without a review", report)

    def test_checkpoint_task_is_reviewed_right_after_it_closes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], judge={"checkpoints": ["EAV-2"]},
                                      max_tasks=2)
            judge, calls = self._judge([verdict(90, "pass", findings=[]),
                                        verdict(88, "pass", findings=[])])
            report = self._run(repo, self._spawn([]), judge)
            self.assertEqual(calls["n"], 2)
            self.assertIn("Tasks under review: EAV-2.", calls["prompts"][0])
            self.assertIn("Tasks under review: EAV-3.", calls["prompts"][1])
            self.assertIn("| EAV-2 | pass |", report)

    def test_failed_full_acceptance_needs_a_human(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"],
                                      verify_cmd="python -c \"import sys; print('1 passed'); sys.exit(1)\"")
            judge, _ = self._judge([verdict(90, "pass", findings=[])])
            report = self._run(repo, self._spawn([]), judge, expect_exit="3")
            self.assertIn("full_acceptance_failed:EAV-2", report)
            self.assertIn("the full oracle fails", report)

    def test_model_call_budget_stops_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], max_tasks=2,
                                      extra={"max_model_calls": 1})
            seen: list = []
            judge, calls = self._judge([verdict(90, "pass", findings=[])])
            gate = self.gate
            real = self._spawn(seen)

            def counting(repo_arg, cfg, worker, prompt, prompt_file, **kw):
                if gate.RUN_BUDGET["max"] and gate.RUN_BUDGET["calls"] >= gate.RUN_BUDGET["max"]:
                    return None, "budget"
                gate.RUN_BUDGET["calls"] += 1
                return real(repo_arg, cfg, worker, prompt, prompt_file, **kw)
            report = self._run(repo, counting, judge)
            self.assertEqual([t for t, _ in seen], ["EAV-2"])
            self.assertIn("stopped because: **budget:model_calls**", report)
            self.assertEqual(calls["n"], 0)  # the review would be the second call

    def test_oversized_packet_is_refused_not_sent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], extra={"worker_packet_max_bytes": 120})
            seen: list = []
            judge, _ = self._judge([])
            report = self._run(repo, self._spawn(seen), judge, expect_exit="3")
            self.assertEqual(seen, [])
            self.assertIn("prompt_too_large:EAV-2", report)


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

    def test_scope_request_detection(self) -> None:
        ask = self.gate.worker_scope_request
        self.assertIsNone(ask("Implemented and committed. Done."))
        self.assertEqual(ask("Blocked: this needs your decision on the declared files; "
                             "I would also touch src/extra.py"), {"files": ["src/extra.py"]})
        chinese = "| \u8981\u4f60\u51b3\u5b9a | \u6269\u56f4 tests/test_new.py |"
        self.assertEqual(ask(chinese), {"files": ["tests/test_new.py"]})
        self.assertIsNone(ask("| \u8981\u4f60\u51b3\u5b9a | \u65e0 |"))

    def test_judge_contracts_are_read_only(self) -> None:
        self.assertIn("claude-fable-5-1", self.gate.JUDGE_CMDS["fable"])
        self.assertEqual(self.gate.JUDGE_CMDS["codex"][2:4], ["--sandbox", "read-only"])

    def test_sub_cfg_fills_defaults_under_partial_override(self) -> None:
        cfg = dict(self.gate.DEFAULT_CONFIG)
        cfg["judge"] = {"enabled": True}
        j = self.gate.sub_cfg(cfg, "judge")
        self.assertTrue(j["enabled"])
        self.assertEqual(j["chain"], ["fable", "opus", "codex"])
        self.assertEqual(j["checkpoints"], [])
        self.assertEqual(j["timeout_s"], 1200)


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

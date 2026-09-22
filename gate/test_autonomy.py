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
                       extra: dict | None = None, rows: int | None = None) -> Path:
    """committed_runner_repo with the review switched on and the probe off.
    The review and the full oracle run at the package boundary (no TODO left),
    so the queue holds as many rows as the run takes unless `rows` says more."""
    repo = committed_runner_repo(root, workers)
    if (rows or max_tasks) == 1:
        queue = repo / "TASK_QUEUE.md"
        queue.write_text("".join(
            ln for ln in queue.read_text(encoding="utf-8").splitlines(keepends=True)
            if "EAV-3" not in ln), encoding="utf-8")
        run("git", "-C", str(repo), "add", "TASK_QUEUE.md")
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


class ReviewHarness(unittest.TestCase):
    """Fake worker and fake review chain around cmd_run. No tests of its own."""

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

        def fake_judge_once(repo, cfg, prompt, run_dir, tag, run_id, n_tasks=1):
            i = calls["n"]
            calls.setdefault("n_tasks", []).append(n_tasks)
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

class ReviewTests(ReviewHarness):
    """One package review per run gated on high findings, lesser findings
    become queue rows, the full oracle runs once afterwards, and the model
    call budget bounds the whole run (docs/autonomy.md)."""

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
            # The task limit was reached on the last row: the package is closed.
            self.assertIn("stopped because: **queue_empty**", report)
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
            self.assertIn("## Waiting on you\n\n- nothing", report)
            self.assertNotIn("TODO", (repo / "TASK_QUEUE.md").read_text(encoding="utf-8"))

    def test_old_per_task_prompts_are_reset(self) -> None:
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

            def unavailable(repo_, cfg, prompt, run_dir, tag, run_id, n_tasks=1):
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

    def test_a_failed_full_acceptance_keeps_its_whole_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cmd = ("python -c \"import sys; print('TRACEBACK-LINE-1'); "
                   "[print('noise', i) for i in range(40)]; print('1 passed'); sys.exit(1)\"")
            repo = judged_runner_repo(Path(td), ["claude"], verify_cmd=cmd)
            judge, _ = self._judge([verdict(90, "pass", findings=[])])
            report = self._run(repo, self._spawn([]), judge, expect_exit="3")
            verdict_json = json.loads((repo / ".gate" / "verdict.json").read_text(encoding="utf-8"))
            self.assertNotIn("TRACEBACK-LINE-1", verdict_json["tail"])   # the tail is 15 lines
            log = repo / verdict_json["log"]
            self.assertEqual(log.name, "full-acceptance.log")
            self.assertIn("TRACEBACK-LINE-1", log.read_text(encoding="utf-8"))
            self.assertIn(f"The whole output, tracebacks included, is in `{verdict_json['log']}`",
                          report)
            events = [json.loads(ln) for ln in
                      (repo / ".gate" / "journal.ndjson").read_text(encoding="utf-8").splitlines()]
            accepted = [e for e in events if e["event"] == "full_acceptance"]
            self.assertEqual(accepted[-1]["log"], verdict_json["log"])

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

    def test_host_driven_review_of_a_done_task(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            base = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
            # Close EAV-2 by hand, as a worker would.
            self._spawn([])(repo, {}, "claude", "task EAV-2", None)
            judge, calls = self._judge([verdict(88, "pass")])
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}),                  mock.patch.object(self.gate, "judge_once", side_effect=judge):
                self.gate.cmd_review(SimpleNamespace(repo=str(repo), tasks="EAV-2", base=base,
                                                     no_acceptance=False, acceptance_only=False))
            self.assertEqual(calls["n"], 1)
            report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
            self.assertIn("| EAV-2 | pass | pass | 88 |", report)
            self.assertIn("## Full acceptance", report)
            self.assertNotIn("EAV-2-R1", (repo / "TASK_QUEUE.md").read_text(encoding="utf-8"))
            self.assertIn("issue: x", (repo / "REVIEW-NOTES.md").read_text(encoding="utf-8"))
            with self.assertRaises(SystemExit):
                self.gate.cmd_review(SimpleNamespace(repo=str(repo), tasks="EAV-3", base=base,
                                                     no_acceptance=True, acceptance_only=False))  # not DONE

    def test_oversized_packet_is_refused_not_sent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], extra={"worker_packet_max_bytes": 120})
            seen: list = []
            judge, _ = self._judge([])
            report = self._run(repo, self._spawn(seen), judge, expect_exit="3")
            self.assertEqual(seen, [])
            self.assertIn("prompt_too_large:EAV-2", report)


class ReviewIsAGateTests(ReviewHarness):
    """A review blocks on a high finding and does nothing else to the queue:
    findings never become rows, the review and the full oracle run once per
    package rather than once per run, and a failed review gets one repair
    round (docs/autonomy.md section 5)."""

    @staticmethod
    def _finding(sev: str, issue: str, action: str | None = None, file: str = "eav2.txt") -> dict:
        f = {"severity": sev, "file": file, "issue": issue, "fix": "fix " + issue}
        if action:
            f["action"] = action
        return f

    @staticmethod
    def _events(repo: Path, name: str) -> list[dict]:
        path = repo / ".gate" / "journal.ndjson"
        if not path.exists():
            return []
        return [e for e in map(json.loads, path.read_text(encoding="utf-8").splitlines())
                if e["event"] == name]

    def _mark_repair_row(self, repo: Path, tid: str = "EAV-2") -> None:
        queue = repo / "TASK_QUEUE.md"
        queue.write_text(queue.read_text(encoding="utf-8")
                         + f"<!-- task:{tid} origin: review -->\n", encoding="utf-8")
        run("git", "-C", str(repo), "commit", "-q", "-am", f"{tid} repairs a failed review")

    def test_schema_requires_action_and_missing_action_is_required(self) -> None:
        item = self.gate.JUDGE_SCHEMA["properties"]["findings"]["items"]
        self.assertIn("action", item["required"])
        self.assertEqual(item["properties"]["action"]["enum"], ["required", "optional", "none"])
        out = self.gate.normalize_judge_verdict({"score": 80, "verdict": "pass", "findings": [
            {"severity": "low", "file": "a", "issue": "i1", "fix": ""},
            {"severity": "low", "file": "a", "issue": "i2", "fix": "", "action": "bogus"},
            {"severity": "low", "file": "a", "issue": "i3", "fix": "", "action": "none"}]})
        self.assertEqual([f["action"] for f in out["findings"]], ["required", "required", "none"])

    def test_action_definition_survives_a_judge_prompt_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], extra={
                "judge_prompt": "Project reviewer. Look at {tasks} since {base}."})
            judge, calls = self._judge([verdict(80, "pass", findings=[])])
            self._run(repo, self._spawn([]), judge)
            prompt = calls["prompts"][0]
            self.assertTrue(prompt.startswith("Project reviewer. Look at EAV-2"))
            self.assertIn("Every finding carries `action`", prompt)
            self.assertIn("No finding becomes a task on its own", prompt)
            self.assertTrue(prompt.rstrip().endswith("Return ONLY JSON matching the schema."))

    def test_findings_are_recorded_in_full_and_never_become_rows(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            judge, _ = self._judge([verdict(85, "pass", findings=[
                self._finding("medium", "real defect", "required"),
                self._finding("low", "nicer wording", "optional"),
                self._finding("low", "No change is required", "none")])])
            report = self._run(repo, self._spawn([]), judge)
            queue = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
            self.assertNotIn("TODO", queue)
            self.assertNotIn("-R1", queue)
            self.assertNotIn("origin: review", queue)
            self.assertIn("[medium] eav2.txt: real defect", report)
            self.assertIn("(action: optional)", report)
            self.assertIn("a record, not tasks; they are also in `REVIEW-NOTES.md`", report)
            self.assertIn("stopped because: **queue_empty**", report)
            self.assertIn("## Waiting on you\n\n- nothing", report)
            self.assertEqual([f["issue"] for f in self._events(repo, "review_findings")[0]["findings"]],
                             ["real defect", "nicer wording", "No change is required"])
            self.assertEqual(self._events(repo, "review_rows_added"), [])
            notes = (repo / "REVIEW-NOTES.md").read_text(encoding="utf-8")
            for text in ("severity: medium | action: required", "issue: nicer wording",
                         "fix: fix No change is required"):
                self.assertIn(text, notes)
            # Committed, so the next run starts from a clean tree.
            self.assertEqual(run("git", "-C", str(repo), "status", "--porcelain", "--",
                                 "TASK_QUEUE.md", "REVIEW-NOTES.md").stdout.strip(), "")
            self.assertIn("review notes of EAV-2",
                          run("git", "-C", str(repo), "log", "--oneline", "-1").stdout)

    def test_high_blocks_whatever_its_action_and_is_not_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            judge, _ = self._judge([verdict(95, "pass", findings=[
                self._finding("high", "wrong", "none"),
                self._finding("low", "cosmetic", "none")])])
            report = self._run(repo, self._spawn([]), judge, expect_exit="3")
            self.assertIn("stopped because: **review_failed:EAV-2**", report)
            self.assertNotIn("## Full acceptance", report)
            notes = (repo / "REVIEW-NOTES.md").read_text(encoding="utf-8")
            self.assertIn("issue: cosmetic", notes)   # no finding is dropped
            self.assertIn("issue: wrong", notes)   # safety defects persist until verified resolved

    def test_failed_repair_prevents_renamed_followup_dispatch(self):
        never = mock.Mock(side_effect=AssertionError("unexpected model call"))
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            self._mark_repair_row(repo)
            self.gate.journal(repo, {"event": "review_verdict", "passed": False,
                                    "repair_tasks": ["OLD-REPAIR"], "blocker_ids": []})
            report = self._run(repo, never, never, expect_exit="3")
            self.assertIn("repair_paused:EAV-2", report)
            self.assertEqual(self._events(repo, "attempt_start"), [])

    def test_failed_repair_worker_cannot_restart_without_authorization(self):
        never = mock.Mock(side_effect=AssertionError("unexpected model call"))
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            self._mark_repair_row(repo)
            self._run(repo, lambda *a, **kw: (SimpleNamespace(stdout="", stderr="", returncode=0), None), never, expect_exit="3")
            self.assertTrue(self.gate.repair_continuation_required(repo))
            report = self._run(repo, never, never, expect_exit="3")
            self.assertIn("repair_paused:EAV-2", report)

    def test_continuation_grant_requires_explicit_scope_and_expiry(self):
        grant = {"enabled": True, "approved_by": "user, current request",
                 "tasks": ["FIX-2"], "expires_at": time.time() + 60}
        allowed = self.gate.repair_continuation_allowed
        self.assertFalse(allowed({}, "FIX-2"))
        self.assertTrue(allowed({"repair_continuation": grant}, "FIX-2"))
        self.assertFalse(allowed({"repair_continuation": grant}, "FIX-3"))
        for change in ({"enabled": False}, {"approved_by": ""},
                       {"expires_at": time.time() - 1}, {"tasks": []},
                       {"expires_at": "tomorrow"}):
            self.assertFalse(allowed({"repair_continuation": grant | change}, "FIX-2"))

    def test_explicit_continuation_can_run_a_named_repair(self):
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], extra={
                "repair_continuation": {"enabled": True, "approved_by": "user",
                    "tasks": ["EAV-2"], "expires_at": time.time() + 600}})
            self._mark_repair_row(repo)
            self.gate.journal(repo, {"event": "review_verdict", "passed": False,
                                    "repair_tasks": ["OLD-REPAIR"]})
            judge, calls = self._judge([verdict(95, "pass")])
            report = self._run(repo, self._spawn([]), judge)
            self.assertNotIn("repair_paused:", report)
            self.assertEqual(calls["n"], 1)
            self.assertFalse(self.gate.repair_continuation_required(repo))

    def test_a_repair_stops_only_when_recorded_blockers_do_not_improve(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "own").mkdir()
            (Path(td) / "other").mkdir()
            repo = judged_runner_repo(Path(td) / "own", ["claude"])
            self._mark_repair_row(repo)
            self.gate.journal(repo, {"event": "review_verdict", "passed": False,
                                    "blocker_ids": [self.gate.defect_id(HIGH[0])]})
            judge, _ = self._judge([verdict(90, "pass", findings=HIGH)])  # high on eav2.txt
            report = self._run(repo, self._spawn([]), judge, expect_exit="3")
            self.assertIn("stopped because: **review_loop:eav2.txt**", report)
            self.assertIn("Repair made no progress", report)
            self.assertNotIn("## Full acceptance", report)
            self.assertEqual(self._events(repo, "run_end")[-1]["stop"], "review_loop:eav2.txt")
            # A genuinely different blocker is not automatically no-progress.
            repo = judged_runner_repo(Path(td) / "other", ["claude"])
            self._mark_repair_row(repo)
            judge, _ = self._judge([verdict(90, "pass", findings=[
                self._finding("high", "wrong", file="other.txt")])])
            report = self._run(repo, self._spawn([]), judge, expect_exit="3")
            self.assertIn("stopped because: **review_failed:EAV-2**", report)

    def test_repair_failure_with_line_number_or_no_location_stops(self) -> None:
        for location, kind, findings, expected in [
            ("eav2.txt:359", "revise", True, "eav2.txt"),
            ("eav2.txt:359:12", "revise", True, "eav2.txt"),
            ("", "revise", True, "EAV-2"),
            ("", "escalate", False, "EAV-2"),
        ]:
            with self.subTest(location=location, kind=kind), tempfile.TemporaryDirectory() as td:
                repo = judged_runner_repo(Path(td), ["claude"])
                self._mark_repair_row(repo)
                if findings:
                    old = self._finding("high", "not fixed", file=location)
                    self.gate.journal(repo, {"event": "review_verdict", "passed": False,
                                            "blocker_ids": [self.gate.defect_id(old)]})
                judge, calls = self._judge([verdict(70, kind, findings=[
                    self._finding("high", "not fixed", file=location)] if findings else [])])
                report = self._run(repo, self._spawn([]), judge, expect_exit="3")
                stop = f"review_loop:{expected}" if findings else "review_failed:EAV-2"
                self.assertIn(f"stopped because: **{stop}**", report)
                self.assertEqual(calls["n"], 1)
                self.assertNotIn("## Full acceptance", report)

    def test_repair_passes_targeted_verification_without_another_audit(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], extra={
                "judge_prompt": "Project reviewer. Look at {tasks} since {base}."})
            self._mark_repair_row(repo)
            judge, calls = self._judge([verdict(80, "pass", findings=[])])
            report = self._run(repo, self._spawn([]), judge)
            self.assertEqual(calls["n"], 1)
            self.assertIn("TARGETED REPAIR VERIFICATION for: EAV-2", calls["prompts"][0])
            self.assertIn("## Full acceptance", report)
            self.assertIn("## Waiting on you\n\n- nothing", report)

    def test_runs_of_one_task_share_one_review_and_one_full_oracle(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], max_tasks=1, rows=2)
            never, _ = self._judge([])
            report = self._run(repo, self._spawn([]), never)
            self.assertIn("stopped because: **budget**", report)
            self.assertIn("deferred to the package boundary: 1 closed task(s)", report)
            self.assertNotIn("## Full acceptance", report)
            self.assertIn("## Waiting on you\n\n- nothing", report)
            judge, calls = self._judge([verdict(90, "pass", findings=[])])
            report = self._run(repo, self._spawn([]), judge)
            self.assertEqual(calls["n"], 1)
            self.assertIn("Tasks under review: EAV-2, EAV-3.", calls["prompts"][0])
            self.assertEqual(calls["n_tasks"], [2])   # the review's spend ceiling scales
            self.assertIn("| EAV-2, EAV-3 | pass | pass | 90 |", report)
            self.assertIn("-> **PASS**", report)
            self.assertEqual(len(self._events(repo, "review_start")), 1)
            accepted = self._events(repo, "full_acceptance")
            self.assertEqual([e["tasks"] for e in accepted], [["EAV-2", "EAV-3"]])

    def test_a_spent_call_budget_defers_the_boundary_instead_of_skipping_the_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], max_tasks=2, rows=1,
                                      extra={"max_model_calls": 1})
            gate = self.gate
            real = self._spawn([])

            def counting(repo_arg, cfg, worker, prompt, prompt_file, **kw):
                gate.RUN_BUDGET["calls"] += 1
                return real(repo_arg, cfg, worker, prompt, prompt_file, **kw)
            never, _ = self._judge([])
            report = self._run(repo, counting, never)
            self.assertIn("stopped because: **budget:model_calls**", report)
            self.assertIn("wait for a run with model calls left for the review (EAV-2)", report)
            self.assertEqual(self._events(repo, "full_acceptance"), [])
            judge, calls = self._judge([verdict(90, "pass", findings=[])])
            report = self._run(repo, counting, judge)
            self.assertEqual(calls["n"], 1)
            self.assertIn("| EAV-2 | pass | pass | 90 |", report)
            self.assertEqual(len(self._events(repo, "full_acceptance")), 1)

    def test_a_run_with_nothing_to_dispatch_closes_the_package(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            base = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
            self._spawn([])(repo, {}, "claude", "task EAV-2", None)
            head = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
            self.gate.journal(repo, {"event": "task_done", "task": "EAV-2", "worker": "claude",
                                     "base": base, "commit": head, "seconds": 1})
            judge, calls = self._judge([verdict(90, "pass", findings=[])])
            seen: list = []
            report = self._run(repo, self._spawn(seen), judge)
            self.assertEqual(seen, [])
            self.assertEqual(calls["n"], 1)
            self.assertIn("stopped because: **queue_empty**", report)
            self.assertIn("| EAV-2 | pass | pass | 90 |", report)
            self.assertIn("-> **PASS**", report)
            # The boundary is closed: a further run has nothing to review.
            never, _ = self._judge([])
            self._run(repo, self._spawn(seen), never, expect_exit="3")

    def test_replay_of_the_alltom_incident_ends_after_round_one(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            judge, calls = self._judge([verdict(86, "pass", findings=[
                self._finding("medium", "r1 medium"), self._finding("low", "r1 low a"),
                self._finding("low", "r1 low b")])])
            self._run(repo, self._spawn([]), judge)
            queue = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
            self.assertNotIn("TODO", queue)   # rounds 2 to 4 have nothing to run
            seen: list = []
            self._run(repo, self._spawn(seen), judge, expect_exit="3")
            self.assertEqual((seen, calls["n"]), ([], 1))
            self.assertEqual(len(self._events(repo, "full_acceptance")), 1)
            notes = (repo / "REVIEW-NOTES.md").read_text(encoding="utf-8")
            for issue in ("r1 medium", "r1 low a", "r1 low b"):
                self.assertIn("issue: " + issue, notes)


class StagePolicyTests(ReviewHarness):
    LOCAL = "python -c \"from pathlib import Path; Path('.gate/local-called').write_text('yes'); print('2 passed')\""
    FULL = "python -c \"from pathlib import Path; Path('.gate/full-called').write_text('yes'); print('9 passed')\""

    def _repo(self, root, stage="development", impact="local", policy=None):
        repo = judged_runner_repo(root, ["claude"], verify_cmd=self.FULL,
                                  extra={"delivery": {"stage": stage, **(policy or {})}})
        queue = repo / "TASK_QUEUE.md"
        queue.write_text(queue.read_text(encoding="utf-8") +
                         f"<!-- task:EAV-2 impact: {impact} -->\n" +
                         "<!-- task:EAV-2 verify: " + json.dumps({"cmd": self.LOCAL, "timeout_s": 30}) + " -->\n",
                         encoding="utf-8")
        run("git", "-C", str(repo), "commit", "-qam", "declare impact")
        return repo

    def test_local_development_check_does_not_execute_full_suite(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            judge, calls = self._judge([verdict(90, "pass", [])])
            report = self._run(repo, self._spawn([]), judge)
            self.assertTrue((repo / ".gate/local-called").exists())
            self.assertFalse((repo / ".gate/full-called").exists())
            self.assertIn("## Stage acceptance", report)
            self.assertIn("full delivery acceptance not established", report)
            self.assertEqual(self.gate.pending_closeout(repo)[0], [])
            never, _ = self._judge([])
            self._run(repo, self._spawn([]), never, expect_exit="3")
            self.assertEqual(calls["n"], 1)

    def test_delivery_runs_full_after_a_local_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td), stage="module")
            judge, _ = self._judge([verdict(90, "pass", [])])
            self._run(repo, self._spawn([]), judge)
            (repo / "eav2.txt").write_text("changed after module check", encoding="utf-8")
            self.gate.cmd_verify(SimpleNamespace(repo=str(repo), stage="delivery", task=None,
                                                queue=False, cmd=None, tasks=None))
            self.assertTrue((repo / ".gate/full-called").exists())
            saved = self.gate.read_verdict(repo)
            self.assertTrue(saved["full_suite"])
            self.assertEqual(saved["stage"], "delivery")

    def test_shared_impact_runs_mapped_callers_and_unknown_runs_full(self):
        for impact, mapped, full in [("shared", True, False), ("shared", False, True), ("unknown", False, True)]:
            with self.subTest(impact=impact, mapped=mapped), tempfile.TemporaryDirectory() as td:
                cmd = "python -c \"from pathlib import Path; Path('.gate/caller-called').write_text('yes'); print('3 passed')\""
                policy = {"checks": [{"files": ["eav2.txt"], "cmd": cmd}]} if mapped else {}
                repo = self._repo(Path(td), impact=impact, policy=policy)
                judge, _ = self._judge([verdict(90, "pass", [])])
                self._run(repo, self._spawn([]), judge)
                self.assertEqual((repo / ".gate/full-called").exists(), full)
                self.assertEqual((repo / ".gate/caller-called").exists(), mapped)

    def test_shared_infrastructure_and_undeclared_actual_changes_escalate(self):
        for drift in (False, True):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as td:
                repo = self._repo(Path(td), policy={} if drift else {"full_globs": ["eav2.txt"]})
                spawn = self._spawn([])
                def changed(*args, **kwargs):
                    result = spawn(*args, **kwargs)
                    if drift:
                        (repo / "outside.txt").write_text("shared change", encoding="utf-8")
                        run("git", "-C", str(repo), "add", "outside.txt")
                        run("git", "-C", str(repo), "commit", "-qm", "outside scope")
                    return result
                judge, _ = self._judge([verdict(90, "pass", [])])
                self._run(repo, changed, judge)
                self.assertTrue((repo / ".gate/full-called").exists())

    def test_integration_adds_flow_check_without_full_suite(self):
        with tempfile.TemporaryDirectory() as td:
            cmd = "python -c \"from pathlib import Path; Path('.gate/integration-called').write_text('yes'); print('4 passed')\""
            repo = self._repo(Path(td), stage="integration", policy={"integration_cmd": cmd})
            judge, _ = self._judge([verdict(90, "pass", [])])
            self._run(repo, self._spawn([]), judge)
            self.assertTrue((repo / ".gate/integration-called").exists())
            self.assertFalse((repo / ".gate/full-called").exists())

    def test_ordinary_defect_defers_until_checkpoint_and_does_not_spawn_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            finding = {"severity": "medium", "file": "eav2.txt:42", "issue": "export label incorrect",
                       "fix": "correct label", "action": "required", "blocking": "stage", "due": "integration"}
            judge, _ = self._judge([verdict(75, "revise", [finding])])
            report = self._run(repo, self._spawn([]), judge)
            self.assertIn("stopped because: **queue_empty**", report)
            self.assertNotIn("TODO", (repo / "TASK_QUEUE.md").read_text(encoding="utf-8"))
            cfg = self.gate.load_config(repo)
            cfg["delivery"] = {"stage": "delivery"}
            result = self.gate.run_stage_acceptance(repo, cfg, ["EAV-2"], None)
            self.assertEqual(result["result"], "FAIL")
            self.assertIn("Unresolved checkpoint defects", result["tail"])
            self.assertFalse((repo / ".gate/full-called").exists())
            notes = self.gate.review_notes_path(repo, cfg)
            text = notes.read_text(encoding="utf-8")
            item = next(iter(self.gate.recorded_defects(repo, cfg).values()))
            item.update(status="resolved", resolution="regression test for export label passes")
            notes.write_text(text + "\n<!-- defect: " + json.dumps(item) + " -->\n", encoding="utf-8")
            result = self.gate.run_stage_acceptance(repo, cfg, ["EAV-2"], None)
            self.assertEqual(result["result"], "PASS")

    def test_optional_suggestion_does_not_block_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td), stage="delivery")
            finding = {"severity": "low", "file": "eav2.txt", "issue": "naming preference",
                       "fix": "rename", "action": "optional", "blocking": "stage", "due": "delivery"}
            judge, _ = self._judge([verdict(80, "revise", [finding])])
            report = self._run(repo, self._spawn([]), judge)
            self.assertIn("stopped because: **queue_empty**", report)
            self.assertTrue((repo / ".gate/full-called").exists())

    def test_identical_notes_are_deduplicated_and_high_defects_cannot_be_deferred(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            cfg = self.gate.load_config(repo)
            data = {"tasks": ["EAV-2"], "findings": HIGH, "member": "test"}
            self.gate.record_review_findings(repo, cfg, data)
            path = self.gate.review_notes_path(repo, cfg)
            before = path.read_bytes()
            self.gate.record_review_findings(repo, cfg, data)
            self.assertEqual(path.read_bytes(), before)
            item = next(iter(self.gate.recorded_defects(repo, cfg).values()))
            item.update(status="deferred", due="backlog")
            with path.open("a", encoding="utf-8") as out:
                out.write("\n<!-- defect: " + json.dumps(item) + " -->\n")
            self.assertTrue(self.gate.open_stage_defects(repo, cfg))

    def test_unrelated_located_blocker_does_not_stop_local_work_but_blocks_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td), stage="module")
            finding = dict(HIGH[0], file="other-module.txt")
            judge, _ = self._judge([verdict(70, "revise", [finding])])
            report = self._run(repo, self._spawn([]), judge)
            self.assertIn("stopped because: **queue_empty**", report)
            cfg = self.gate.load_config(repo)
            cfg["delivery"] = {"stage": "delivery"}
            outcome = self.gate.run_stage_acceptance(repo, cfg, [], None)
            self.assertEqual(outcome["result"], "FAIL")
            self.assertFalse((repo / ".gate/full-called").exists())

    def test_due_defect_without_resolution_evidence_and_invalid_stage_fail_closed(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            cfg = self.gate.load_config(repo)
            self.gate.record_review_findings(repo, cfg, {"tasks": ["EAV-2"], "findings": HIGH})
            item = next(iter(self.gate.recorded_defects(repo, cfg).values()))
            item["status"] = "resolved"
            with self.gate.review_notes_path(repo, cfg).open("a", encoding="utf-8") as out:
                out.write("\n<!-- defect: " + json.dumps(item) + " -->\n")
            self.assertTrue(self.gate.open_stage_defects(repo, cfg))
            cfg["delivery"] = {"stage": "typo"}
            with self.assertRaises(SystemExit):
                self.gate.run_stage_acceptance(repo, cfg, ["EAV-2"], None)

    def test_failed_scoped_check_preserves_boundary_without_another_review(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            queue = repo / "TASK_QUEUE.md"
            cmd = "python -c \"from pathlib import Path; import sys; print('1 passed'); sys.exit(0 if Path('.gate/ready').exists() else 1)\""
            text = queue.read_text(encoding="utf-8").replace(json.dumps(self.LOCAL), json.dumps(cmd))
            queue.write_text(text, encoding="utf-8")
            run("git", "-C", str(repo), "commit", "-qam", "checkpoint fails until ready")
            judge, calls = self._judge([verdict(90, "pass", [])])
            self._run(repo, self._spawn([]), judge, expect_exit="3")
            self.assertTrue(self.gate.pending_closeout(repo)[0])
            (repo / ".gate/ready").touch()
            never, _ = self._judge([])
            report = self._run(repo, self._spawn([]), never)
            self.assertEqual(calls["n"], 1)
            self.assertIn("## Stage acceptance", report)

    def test_a_repair_that_reduces_blockers_is_not_a_loop(self):
        with tempfile.TemporaryDirectory() as td:
            repo = self._repo(Path(td))
            queue = repo / "TASK_QUEUE.md"
            queue.write_text(queue.read_text(encoding="utf-8") + "<!-- task:EAV-2 origin: review -->\n", encoding="utf-8")
            run("git", "-C", str(repo), "commit", "-qam", "repair")
            self.gate.journal(repo, {"event": "review_verdict", "passed": False,
                                    "blocker_ids": [self.gate.defect_id(HIGH[0]), "BUG-000000000000"]})
            judge, _ = self._judge([verdict(80, "revise", HIGH)])
            report = self._run(repo, self._spawn([]), judge, expect_exit="3")
            self.assertIn("stopped because: **review_failed:EAV-2**", report)
            self.assertNotIn("review_loop:", report)


class FullOracleTurnTests(unittest.TestCase):
    """One full oracle at a time on the machine; task checks never wait."""

    def setUp(self) -> None:
        self.gate = load_gate_module()

    def _repo(self, root: Path) -> tuple[Path, dict]:
        repo = committed_runner_repo(root, ["claude"])
        with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}):
            return repo, self.gate.load_config(repo)

    def test_a_second_full_oracle_waits_for_the_first_and_says_so_once(self) -> None:
        import threading
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as machine, \
                mock.patch.dict(os.environ, {"GATE_MACHINE_DIR": machine}):
            repo, cfg = self._repo(Path(td))
            inside = threading.Event()
            release = threading.Event()

            def first() -> None:
                with self.gate.full_oracle_turn(Path("other-project"), cfg):
                    inside.set()
                    release.wait(20)
            holder = threading.Thread(target=first)
            holder.start()
            self.assertTrue(inside.wait(10))
            result: dict = {}
            second = threading.Thread(
                target=lambda: result.update(self.gate.run_acceptance(repo, cfg)))
            second.start()
            second.join(3)
            self.assertTrue(second.is_alive())          # still waiting for its turn
            self.assertFalse((repo / ".gate" / "verdict.json").exists())
            release.set()
            holder.join(10)
            second.join(30)
            self.assertEqual(result["result"], "PASS")
            self.assertGreater(result["waited_s"], 2)
            events = [json.loads(ln) for ln in
                      (repo / ".gate" / "journal.ndjson").read_text(encoding="utf-8").splitlines()]
            waits = [e for e in events if e["event"] == "full_acceptance_wait"]
            self.assertEqual(len(waits), 1)
            self.assertEqual(waits[0]["holder"], "other-project")

    def test_task_checks_and_an_opted_out_project_never_wait(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as machine, \
                mock.patch.dict(os.environ, {"GATE_MACHINE_DIR": machine}):
            repo, cfg = self._repo(Path(td))
            with self.gate.full_oracle_turn(Path("other-project"), cfg):
                task = self.gate.run_acceptance(repo, cfg, task="EAV-2", files=["eav2.txt"])
                self.assertEqual((task["result"], task["waited_s"]), ("PASS", 0.0))
                off = self.gate.run_acceptance(repo, {**cfg, "serialize_full_acceptance": False})
                self.assertEqual((off["result"], off["waited_s"]), ("PASS", 0.0))

    def test_the_turn_is_free_again_after_a_holder_that_raised(self) -> None:
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as machine, \
                mock.patch.dict(os.environ, {"GATE_MACHINE_DIR": machine}):
            repo, cfg = self._repo(Path(td))
            with self.assertRaises(RuntimeError):
                with self.gate.full_oracle_turn(repo, cfg):
                    raise RuntimeError("oracle crashed")
            self.assertEqual(self.gate.run_acceptance(repo, cfg)["waited_s"], 0.0)


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

    def _no_access_chain(self, blind: set[str]):
        """judge_once over a chain where the members in `blind` answer that they
        could not read anything (the Codex read-only sandbox on Windows did)."""
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        repo = judged_runner_repo(Path(td.name), ["claude"])
        with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}):
            cfg = self.gate.load_config(repo)
        run_dir = repo / ".gate" / "runs" / "t"
        run_dir.mkdir(parents=True)
        seen: list[str] = []

        def fake_spawn(repo_arg, cfg_, tool, prompt, pf, **kw):
            template = kw["template"]
            member = template[template.index("--model") + 1] if "--model" in template else "codex"
            seen.append(member)
            body = ({"score": 0, "verdict": "escalate", "findings": [],
                     "revision_brief": "exec_command failed: CreateProcess rejected"}
                    if member in blind else
                    {"score": 87, "verdict": "pass", "findings": [], "revision_brief": ""})
            return self.gate.WorkerResult(0, json.dumps({"structured_output": body})), None
        with mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n), \
             mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
            verdict, member = self.gate.judge_once(repo, cfg, "p", run_dir, "tag", "run")
        return repo, seen, verdict, member

    def test_a_reviewer_that_could_not_read_is_an_outage_not_a_verdict(self) -> None:
        repo, seen, verdict, member = self._no_access_chain({"claude-fable-5-1"})
        self.assertEqual(seen, ["claude-fable-5-1", "opus"])
        self.assertEqual((member, verdict["score"]), ("opus", 87))
        # Not benched: the same CLI may be a fine worker.
        self.assertNotIn("claude", self.gate.tool_state(repo))

    def test_a_chain_that_could_not_read_at_all_skips_the_review(self) -> None:
        _, seen, verdict, why = self._no_access_chain({"claude-fable-5-1", "opus", "codex"})
        self.assertEqual(len(seen), 3)
        self.assertIsNone(verdict)
        self.assertEqual(why, "fable:no_access; opus:no_access; codex:no_access")

    def test_an_escalate_with_a_basis_is_still_a_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"])
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}):
                cfg = self.gate.load_config(repo)
            run_dir = repo / ".gate" / "runs" / "t"
            run_dir.mkdir(parents=True)
            body = {"score": 40, "verdict": "escalate", "findings": [],
                    "revision_brief": "the acceptance is ambiguous"}

            def fake_spawn(repo_arg, cfg_, tool, prompt, pf, **kw):
                return self.gate.WorkerResult(0, json.dumps({"structured_output": body})), None
            with mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n), \
                 mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                verdict, member = self.gate.judge_once(repo, cfg, "p", run_dir, "tag", "run")
            self.assertEqual((member, verdict["verdict"]), ("fable", "escalate"))

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

    def test_review_budget_scales_with_tasks_and_a_cli_error_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["claude"], judge={"chain": ["opus"]})
            with mock.patch.dict(os.environ, {"GATE_CONFIG": ""}):
                cfg = self.gate.load_config(repo)
            run_dir = repo / ".gate" / "runs" / "t"
            run_dir.mkdir(parents=True)
            seen: list[str] = []

            def fake_spawn(repo_arg, cfg_, tool, prompt, pf, **kw):
                seen.append(kw["extra"]["budget_usd"])
                self.assertIn("{budget_usd}", kw["template"])
                return self.gate.WorkerResult(1, json.dumps({
                    "type": "result", "subtype": "error_max_budget_usd", "is_error": True,
                    "errors": ["Reached maximum budget"]})), None
            with mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n), \
                 mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn):
                verdict, why = self.gate.judge_once(repo, cfg, "p", run_dir, "tag", "run", n_tasks=4)
            self.assertEqual(seen, ["20"])
            self.assertIsNone(verdict)
            self.assertEqual(why, "opus:error_max_budget_usd")

    def test_braces_inside_a_finding_do_not_hide_the_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            body = {"score": 87, "verdict": "pass", "revision_brief": "",
                    "findings": [{"severity": "low", "file": "a.py", "fix": "close the brace }",
                                  "issue": "`raise Error({` is never closed; see `}` at line 9"}]}
            env = json.dumps({"type": "result", "structured_output": body,
                              "result": json.dumps(body), "usage": {"input_tokens": 1}})
            v = self.gate.parse_judge_output("banner { not json\n" + env + "\n", Path(td) / "none")
            self.assertEqual((v["score"], v["verdict"], len(v["findings"])), (87, "pass", 1))
            # A plain transcript still ends in its verdict object.
            v = self.gate.parse_judge_output(
                'thinking {"draft": 1} ...\nfinal: ' + json.dumps(body), Path(td) / "none")
            self.assertEqual(v["score"], 87)
            self.assertIsNone(self.gate._last_json_object("no json { here"))

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

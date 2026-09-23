"""Efficiency regressions: no live CLI/provider calls or business-project edits."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test_autonomy import ReviewHarness, judged_runner_repo, verdict
from test_compat import FLOW, committed_runner_repo, load_gate_module, run


def configure(repo, **changes):
    path = repo / ".gate/config.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.update(changes)
    path.write_text(json.dumps(cfg), encoding="utf-8")


class LazyProbeTests(ReviewHarness):
    def test_success_probes_only_selected_worker_not_backup_or_future_review(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex", "kimi", "grok"])
            configure(repo, probe={"enabled": True}, judge={"enabled": True})
            probes, workers = [], []
            close = self._spawn(workers)

            def spawn(repo, cfg, worker, prompt, pf, **kw):
                if prompt == self.gate.PROBE_PROMPT:
                    probes.append(worker)
                    return self.gate.WorkerResult(0, "OK"), None
                return close(repo, cfg, worker, prompt, pf, **kw)

            judge, calls = self._judge([])
            self._run(repo, spawn, judge)
            self.assertEqual(probes, ["codex"])
            self.assertEqual(workers, [("EAV-2", "codex")])
            self.assertEqual(calls["n"], 0)

    def test_quota_falls_through_lazily_and_preserves_cooldown(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["claude", "codex", "kimi"])
            configure(repo, probe={"enabled": True})
            probes, workers = [], []
            close = self._spawn(workers)

            def spawn(repo, cfg, worker, prompt, pf, **kw):
                if prompt == self.gate.PROBE_PROMPT:
                    probes.append(worker)
                    return self.gate.WorkerResult(1, "usage limit") if worker == "claude" else self.gate.WorkerResult(0, "OK"), None
                return close(repo, cfg, worker, prompt, pf, **kw)

            judge, _ = self._judge([])
            self._run(repo, spawn, judge)
            self.assertEqual(probes, ["claude", "codex"])
            self.assertEqual(workers, [("EAV-2", "codex")])
            self.assertEqual(self.gate.tool_state(repo)["claude"]["reason"], "probe:quota")

    def test_retry_rotates_then_probes_only_the_new_candidate(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["claude", "codex", "kimi"])
            configure(repo, probe={"enabled": True})
            probes, workers = [], []
            close = self._spawn(workers)

            def spawn(repo, cfg, worker, prompt, pf, **kw):
                if prompt == self.gate.PROBE_PROMPT:
                    probes.append(worker)
                    return self.gate.WorkerResult(0, "OK"), None
                if worker == "claude":
                    return self.gate.WorkerResult(0, "no changes yet"), None
                return close(repo, cfg, worker, prompt, pf, **kw)

            judge, _ = self._judge([])
            self._run(repo, spawn, judge)
            self.assertEqual(probes, ["claude", "codex"])
            self.assertEqual(workers, [("EAV-2", "codex")])

    def test_ineligible_or_oversized_work_never_probes(self):
        for kind in ("blocked", "refused", "packet", "malformed"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                repo = committed_runner_repo(Path(td), ["codex"])
                configure(repo, probe={"enabled": True}, strict_admit=kind != "malformed")
                q = repo / "TASK_QUEUE.md"
                text = q.read_text(encoding="utf-8")
                if kind == "blocked":
                    q.write_text(text.replace("`TODO`", "`BLOCKED`", 1), encoding="utf-8")
                elif kind == "refused":
                    q.write_text(text.replace("<!-- task:EAV-2 files: eav2.txt -->", ""), encoding="utf-8")
                elif kind == "packet":
                    configure(repo, worker_packet_max_bytes=10)
                else:
                    q.write_text(text.replace("| 1 passed |", "| codex | 1 passed |", 1), encoding="utf-8")
                judge, _ = self._judge([])
                spawn = mock.Mock(side_effect=AssertionError("must not call any CLI"))
                self._run(repo, spawn, judge, expect_exit="3")
                spawn.assert_not_called()

    def test_final_call_is_not_wasted_on_probe_or_marked_as_outage(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            configure(repo, probe={"enabled": True}, max_model_calls=1)
            judge, _ = self._judge([])
            spawn = mock.Mock(side_effect=AssertionError("no useful call allowance"))
            report = self._run(repo, spawn, judge, expect_exit="3")
            self.assertIn("budget:model_calls", report)
            self.assertEqual(self.gate.tool_state(repo), {})
            spawn.assert_not_called()

    def test_review_probes_only_first_usable_member(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            cfg = self.gate.load_config(repo)
            cfg.update(probe={"enabled": True}, judge={"enabled": True, "chain": ["fable", "codex"]})
            self.gate.RUN_BUDGET.update(max=20, calls=0)
            with mock.patch.object(self.gate, "probe_worker", return_value=(True, "ok")) as probe, \
                 mock.patch.object(self.gate, "spawn_worker", return_value=(self.gate.WorkerResult(0, json.dumps(verdict(90, "pass", []))), None)):
                answer, member = self.gate.judge_once(repo, cfg, "review", repo / ".gate", "review", "r")
            self.assertEqual(member, "fable")
            self.assertEqual(answer["verdict"], "pass")
            self.assertEqual([c.args[2] for c in probe.call_args_list], ["claude"])

    def test_budget_during_boundary_review_preserves_pending_acceptance(self):
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ["codex"], max_tasks=2)
            report = self._run(repo, self._spawn([]), lambda *a, **k: (None, "budget"))
            self.assertIn("budget:model_calls", report)
            self.assertIn("review and acceptance remain pending", report)
            self.assertNotIn('"event": "full_acceptance"', (repo / ".gate/journal.ndjson").read_text())
            self.assertEqual(len(self.gate.pending_closeout(repo)[0]), 2)

    def test_review_can_use_final_call_on_already_probed_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            cfg = self.gate.load_config(repo)
            cfg.update(probe={"enabled": True}, judge={"enabled": True, "chain": ["fable", "codex"]})
            self.gate.RUN_BUDGET.update(max=3, calls=2)
            self.gate.set_tool_state(repo, {"codex": {"probed_run": "r", "reason": "ok"}})
            with mock.patch.object(self.gate, "probe_worker", side_effect=AssertionError("no probe allowance")), \
                 mock.patch.object(self.gate, "spawn_worker", return_value=(self.gate.WorkerResult(0, json.dumps(verdict(90, "pass", []))), None)) as spawn:
                answer, member = self.gate.judge_once(repo, cfg, "review", repo / ".gate", "review", "r")
            self.assertEqual(member, "codex")
            self.assertEqual(answer["verdict"], "pass")
            spawn.assert_called_once()


class PlanningTests(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate_module()

    def test_plan_previews_checks_without_executing_or_changing_state(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            configure(repo, delivery={"stage": "module"})
            q = repo / "TASK_QUEUE.md"
            extra = ""
            for tid in ("EAV-2", "EAV-3"):
                extra += f'<!-- task:{tid} impact: local -->\n<!-- task:{tid} verify: {{"cmd": "echo 2 passed"}} -->\n'
            q.write_text(q.read_text(encoding="utf-8") + extra, encoding="utf-8")
            before = {str(p): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
            result = run("python", str(FLOW), "admit", "--repo", str(repo), "--plan")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("scope: stage", result.stdout)
            self.assertEqual(result.stdout.count("check (900s): echo 2 passed"), 1)
            after = {str(p): p.read_bytes() for p in repo.rglob("*") if p.is_file()}
            self.assertEqual(before, after)
            with self.assertRaisesRegex(SystemExit, "1"):
                self.gate.stage_check_plan(repo, self.gate.load_config(repo), ["EAV-2"], None)

    def test_planning_catches_packet_spec_and_unresolved_scope(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            configure(repo, worker_packet_max_bytes=10)
            q = repo / "TASK_QUEUE.md"
            q.write_text(q.read_text() + '<!-- task:EAV-2 spec: docs/missing.md -->\n' +
                         '<!-- task:EAV-2 verify: {"cmd": ""} -->\n', encoding="utf-8")
            self.gate.journal(repo, {"event": "scope_request", "task": "EAV-2", "files": ["callers.py"]})
            notes = self.gate.planning_diagnostics(repo, self.gate.load_config(repo), q.read_text(), ["EAV-2"])
            messages = "\n".join(n["message"] for n in notes)
            self.assertIn("invalid task check", messages)
            self.assertIn("spec acceptance", messages)
            self.assertIn("packet", messages)
            self.assertIn("callers.py", messages)

    def test_malformed_worker_table_fails_plan_before_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            q = repo / "TASK_QUEUE.md"
            q.write_text(q.read_text().replace("| status | start", "| status | worker | start"), encoding="utf-8")
            result = run("python", str(FLOW), "admit", "--repo", str(repo), "--plan")
            self.assertEqual(result.returncode, 2)
            self.assertIn("table-width", result.stdout)
            self.assertFalse((repo / ".gate/journal.ndjson").exists())


class MetricsTests(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate_module()

    def test_timings_are_separate_and_legacy_unknown_is_not_zero(self):
        events = [
            {"event": "run_start", "run": "r", "metrics_version": 1, "at": "2026-01-01T00:00:00Z"},
            {"event": "probe", "seconds": 2},
            {"event": "model_call", "role": "worker"},
            {"event": "attempt_start", "at": "2026-01-01T00:00:03Z"},
            {"event": "acceptance_check", "seconds": 10, "waited_s": 4},
            {"event": "model_call_end", "role": "worker", "seconds": 20},
            {"event": "task_done"}, {"event": "scope_request"},
            {"event": "run_end", "stop": "queue_empty", "seconds": 30},
        ]
        measured = self.gate.run_metrics(events)
        self.assertEqual([measured[k] for k in ("elapsed_s", "startup_s", "worker_s", "check_s", "lock_wait_s")],
                         [30, 3, 20, 10, 4])
        self.assertEqual(measured["model_calls"], {"worker": 1})
        events[0].pop("metrics_version")
        self.assertIsNone(self.gate.run_metrics(events)["check_s"])
        self.assertIsNone(self.gate.run_metrics(events)["model_calls"])

    def test_unfinished_model_call_has_unknown_duration(self):
        data = self.gate.run_metrics([
            {"event": "run_start", "metrics_version": 1},
            {"event": "model_call", "role": "worker"},
        ])
        self.assertEqual(data["model_calls"], {"worker": 1})
        self.assertIsNone(data["worker_s"])

    def test_local_process_emits_call_measurement_without_provider(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["stub"])
            cfg = self.gate.load_config(repo)
            cfg["worker_cmds"] = {"stub": ["python", "-c", "print('finished')"]}
            self.gate.RUN_BUDGET.update(max=2, calls=0)
            with mock.patch.object(self.gate, "resolve_tool", return_value=sys.executable):
                result, err = self.gate.spawn_worker(repo, cfg, "stub", "do work", repo / ".gate/prompt.md")
            self.assertIsNone(err)
            self.assertEqual(result.returncode, 0)
            events = list(self.gate.iter_journal(repo))
            self.assertEqual([e["event"] for e in events], ["model_call", "model_call_end"])
            self.assertEqual(events[0]["call"], events[1]["call"])
            self.assertEqual(events[1]["role"], "worker")
            self.assertGreater(events[1]["seconds"], 0)

    def test_cli_reads_last_runs_handles_active_and_malformed_records(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            path = repo / ".gate/journal.ndjson"
            for number in range(3):
                self.gate.journal(repo, {"event": "run_start", "run": str(number)})
                if number < 2:
                    self.gate.journal(repo, {"event": "run_end", "stop": "queue_empty"})
            with path.open("a") as stream:
                stream.write("not-json\n[]\n")
            before = path.read_bytes()
            result = run("python", str(FLOW), "metrics", "--repo", str(repo), "--last", "2", "--json")
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual([r["run"] for r in data["runs"]], ["1", "2"])
            self.assertFalse(data["runs"][-1]["finished"])
            self.assertIsNone(data["runs"][-1]["elapsed_s"])
            self.assertEqual(data["journal_warnings"], 2)
            self.assertEqual(before, path.read_bytes())

    def test_report_uses_latest_segment_when_a_fast_restart_reuses_run_id(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            for task in ("OLD", "NEW"):
                self.gate.journal(repo, {"event": "run_start", "run": "same-second"})
                self.gate.journal(repo, {"event": "task_done", "task": task})
                self.gate.journal(repo, {"event": "run_end", "run": "same-second"})
            self.gate.journal(repo, {"event": "run_start", "run": "unrelated"})
            events = self.gate._journal_tail(repo, "same-second")
            self.assertEqual([e["task"] for e in events if e["event"] == "task_done"], ["NEW"])
            self.assertEqual(self.gate._journal_tail(repo, "missing"), [])

    def test_acceptance_records_one_command_with_duration_without_double_count(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            self.gate.journal(repo, {"event": "run_start", "run": "r", "metrics_version": 1})
            cfg = self.gate.load_config(repo)
            cfg["delivery"] = {"stage": "delivery"}
            with contextlib.redirect_stdout(io.StringIO()):
                accepted = self.gate.run_stage_acceptance(repo, cfg, [], None)
            events = list(self.gate.iter_journal(repo))
            self.assertEqual(sum(e.get("event") == "acceptance_check" for e in events), 1)
            self.assertEqual(self.gate.run_metrics(events)["check_s"], accepted["elapsed_s"])


if __name__ == "__main__":
    unittest.main()

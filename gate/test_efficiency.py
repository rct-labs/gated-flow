"""Efficiency regressions: no live CLI/provider calls or business-project edits."""
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
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


class OutageBoundaryTests(ReviewHarness):
    @staticmethod
    def transcript(*events):
        return '\n'.join(json.dumps(e) for e in events)

    def test_recoverable_tool_errors_are_not_provider_outages(self):
        for failure in (
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'is_error': True, 'content': 'test quota rate limit failed'}]}},
            {'type': 'item.completed', 'item': {'type': 'command_execution',
                'exit_code': 1, 'status': 'failed', 'aggregated_output': 'test quota rate limit failed'}},
        ):
            text = self.transcript(failure, {'type': 'result', 'is_error': False, 'result': 'done'})
            with self.subTest(failure=failure):
                self.assertIsNone(self.gate.tool_outage_reason(text, 0))
                self.assertIsNone(self.gate.tool_outage_reason(text, 1))

    def test_terminal_and_provider_failures_remain_outages(self):
        for event, expected in (
            ({'type': 'turn.failed', 'error': {'message': 'usage limit'}}, 'quota'),
            ({'type': 'error', 'message': 'provider disconnected'}, 'terminal_failure'),
            ({'type': 'result', 'is_error': True, 'result': 'rate limit'}, 'quota'),
            ({'type': 'result', 'is_error': True, 'subtype': 'error_during_execution'}, 'terminal_failure'),
        ):
            with self.subTest(event=event):
                self.assertEqual(self.gate.tool_outage_reason(self.transcript(event), 0), expected)

    def test_nested_git_write_denial_remains_an_outage_after_success(self):
        denied = "fatal: Unable to create '/repo/.git/index.lock': Permission denied"
        for failure in (
            {'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'is_error': True, 'content': denied}]}},
            {'type': 'item.completed', 'item': {'type': 'command_execution',
                'exit_code': 128, 'status': 'failed', 'aggregated_output': denied}},
        ):
            with self.subTest(failure=failure):
                text = self.transcript(failure, {'type': 'result', 'is_error': False})
                self.assertEqual(self.gate.tool_outage_reason(text, 0), 'git_write_denied')

    def test_successful_worker_with_recovered_tool_error_closes_normally(self):
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ['codex'])
            close = self._spawn([])
            def spawn(*args, **kwargs):
                result, error = close(*args, **kwargs)
                result.stdout = self.transcript(
                    {'type': 'user', 'message': {'content': [
                        {'type': 'tool_result', 'is_error': True, 'content': 'quota test failed'}]}},
                    {'type': 'result', 'is_error': False, 'result': 'done'})
                return result, error
            judge, calls = self._judge([verdict(90, 'pass', [])])
            with mock.patch.object(self.gate, 'tool_outage_reason', wraps=self.gate.tool_outage_reason) as classify:
                report = self._run(repo, spawn, judge)
            self.assertTrue(any(call.args[1] == 0 for call in classify.call_args_list))
            self.assertEqual(calls['n'], 1)
            self.assertNotIn('tool_state_conflict', report)
            self.assertEqual(self.gate.pending_closeout(repo)[0], [])


class LazyProbeTests(ReviewHarness):
    def test_default_dispatch_spends_final_call_on_useful_work(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex", "kimi"])
            cfg_path = repo / ".gate/config.json"
            cfg = json.loads(cfg_path.read_text())
            cfg.pop("probe")
            cfg["max_model_calls"] = 1
            cfg_path.write_text(json.dumps(cfg))
            workers = []
            judge, _ = self._judge([])
            with mock.patch.object(self.gate, "probe_worker", side_effect=AssertionError("default must not probe")):
                self._run(repo, self._spawn(workers), judge)
            self.assertEqual(workers, [("EAV-2", "codex")])
            self.assertFalse(any(e["event"] == "probe" for e in self.gate.iter_journal(repo)))

    def test_default_review_uses_final_call_without_probe(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            cfg = self.gate.load_config(repo)
            cfg.pop("probe")
            cfg["judge"] = {"enabled": True, "chain": ["fable"]}
            self.gate.RUN_BUDGET.update(max=1, calls=0)
            with mock.patch.object(self.gate, "probe_worker", side_effect=AssertionError("default must not probe")), \
                 mock.patch.object(self.gate, "spawn_worker", return_value=(self.gate.WorkerResult(0, json.dumps(verdict(90, "pass", []))), None)) as spawn:
                answer, member = self.gate.judge_once(repo, cfg, "review", repo / ".gate", "review", "r")
            self.assertEqual(member, "fable")
            self.assertEqual(answer["verdict"], "pass")
            spawn.assert_called_once()

    def test_explicit_usage_still_probes_configured_clis(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex", "kimi"])
            with mock.patch.object(self.gate, "probe_worker", return_value=(True, "ok")) as probe, \
                 contextlib.redirect_stdout(io.StringIO()):
                self.gate.cmd_usage(type("Args", (), {"repo": str(repo)})())
            self.assertEqual([c.args[2] for c in probe.call_args_list], ["codex", "kimi"])

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
        for kind in ("blocked", "refused", "packet", "handoff", "malformed"):
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
                elif kind == "handoff":
                    (repo / "spec.md").write_text("## Acceptance\nKeep API.\n## Handoff EAV-2\n"
                                                  + "x" * 7000 + "\nStop conditions: retain approvals.\n")
                    q.write_text(text + "<!-- task:EAV-2 spec: spec.md -->\n")
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


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate_module()

    def spec(self, repo, metadata=None, body="Entry/callers: eav2.txt; reuse existing loader.\n"
             "Decisions: retain API.\nStop conditions: request scope for callers."):
        if not (repo / "eav2.txt").exists():
            (repo / "eav2.txt").write_text("original")
            run("git", "add", "eav2.txt", cwd=repo)
            result = run("git", "commit", "-qm", "handoff source", cwd=repo)
            self.assertEqual(result.returncode, 0, result.stderr)
        base = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
        metadata = metadata if metadata is not None else {"base": base, "files": ["eav2.txt"]}
        path = repo / "spec.md"
        path.write_text("## Acceptance\nKeep acceptance intact.\n\n## Handoff EAV-2\n"
                        + "<!-- handoff: " + json.dumps(metadata) + " -->\n" + body
                        + "\n## Handoff EAV-3\nOther task secret.\n## Decisions\nOld history.\n")
        q = repo / "TASK_QUEUE.md"
        q.write_text(q.read_text() + "\n<!-- task:EAV-2 spec: spec.md -->\n")
        return base

    def packet(self, repo, cfg=None):
        return self.gate.task_packet(repo, cfg or self.gate.load_config(repo),
                                     (repo / "TASK_QUEUE.md").read_text(), "EAV-2", ["eav2.txt"])

    def test_current_task_only_and_embedded_acceptance_is_sufficient(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            base = self.spec(repo)
            packet = self.packet(repo)
            self.assertIn("related files unchanged", packet)
            self.assertIn(base, packet)
            self.assertIn("Stop conditions: request scope", packet)
            self.assertNotIn("Other task secret", packet)
            self.assertNotIn("Old history", packet)
            self.assertEqual(packet.count("Keep acceptance intact."), 1)
            self.assertNotIn("read its acceptance", packet)

    def test_related_drift_requires_rereading_and_replanning(self):
        for kind in ("working", "staged", "committed", "deleted", "untracked"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as td:
                repo = committed_runner_repo(Path(td), ["codex"])
                self.spec(repo)
                path = repo / "eav2.txt"
                if kind == "deleted":
                    path.unlink()
                elif kind == "untracked":
                    # A related directory must include newly created files too.
                    self.spec(repo, {"base": run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip(),
                                     "files": ["related"]})
                    (repo / "related").mkdir()
                    path = repo / "related/new.txt"
                    path.write_text("new")
                else:
                    path.write_text("changed")
                if kind in ("staged", "committed"):
                    self.assertEqual(run("git", "add", "eav2.txt", cwd=repo).returncode, 0)
                if kind == "committed":
                    self.assertEqual(run("git", "commit", "-qm", "related change", cwd=repo).returncode, 0)
                packet = self.packet(repo)
                self.assertIn("STALE/UNVERIFIED", packet)
                self.assertIn("reread", packet)
                self.assertIn("replan", packet)
                self.assertIn("Stop conditions: request scope", packet)

    def test_invalid_missing_or_nonancestor_anchor_is_unverified(self):
        for metadata in ({}, {"base": "missing", "files": ["eav2.txt"]},
                         {"base": "0" * 40, "files": ["eav2.txt"]},
                         {"base": "HEAD", "files": []},
                         {"base": "HEAD", "files": ["../outside"]}):
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as td:
                repo = committed_runner_repo(Path(td), ["codex"])
                self.spec(repo, metadata)
                self.assertIn("STALE/UNVERIFIED", self.packet(repo))

    def test_nonexistent_related_path_is_unverified_but_existing_directory_is_current(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            base = self.spec(repo)
            self.spec(repo, {"base": base, "files": ["never-existed.txt"]})
            self.assertIn("STALE/UNVERIFIED", self.packet(repo))
            (repo / "related").mkdir()
            (repo / "related/file.txt").write_text("source")
            run("git", "add", "related", cwd=repo)
            run("git", "commit", "-qm", "related directory", cwd=repo)
            base = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
            self.spec(repo, {"base": base, "files": ["related"]})
            self.assertIn("related files unchanged", self.packet(repo))

    def test_rendered_check_executes_with_spaces_and_no_python_alias(self):
        with tempfile.TemporaryDirectory(prefix="portable ' check ") as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            self.spec(repo)
            # Keep only Git available; the rendered command and its oracle
            # must use an absolute interpreter without a python alias.
            git = Path(td) / ("git.exe" if os.name == "nt" else "git")
            if os.name == "nt":
                shutil.copyfile(shutil.which("git"), git)
            else:
                git.symlink_to(shutil.which("git"))
            cfg = self.gate.load_config(repo)
            oracle = [sys.executable, "-c", "print('2 passed')"]
            cfg["verify_cmd"] = subprocess.list2cmdline(oracle) if os.name == "nt" else shlex.join(oracle)
            engine_dir = Path(td) / "engine with spaces"
            engine_dir.mkdir()
            for name in ("gate.py", "model_policy.py", "usage.py", "review_packet.py"):
                shutil.copyfile(Path(self.gate.__file__).parent / name, engine_dir / name)
            with mock.patch.object(self.gate, "__file__", str(engine_dir / "gate.py")):
                check = self.packet(repo, cfg).split("local check: ", 1)[1].split("  (runs:", 1)[0]
            config = repo / ".gate/config.json"
            config.write_text(json.dumps(cfg))
            result = subprocess.run(check, shell=True, cwd=repo, env={**os.environ, "PATH": td},
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads((repo / ".gate/verdict.json").read_text())["result"], "PASS")

    def test_legacy_spec_has_no_handoff_requirement(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            self.spec(repo)
            path = repo / "spec.md"
            path.write_text("## Acceptance\nKeep acceptance intact.\n")
            packet = self.packet(repo)
            self.assertNotIn("handoff", packet.lower())
            self.assertIn("Keep acceptance intact.", packet)

    def test_nonancestor_and_git_errors_cannot_claim_freshness(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            self.spec(repo)
            tree = run("git", "rev-parse", "HEAD^{tree}", cwd=repo).stdout.strip()
            orphan = run("git", "commit-tree", tree, "-m", "unrelated root", cwd=repo)
            self.assertEqual(orphan.returncode, 0, orphan.stderr)
            self.spec(repo, {"base": orphan.stdout.strip(), "files": ["eav2.txt"]})
            self.assertIn("STALE/UNVERIFIED", self.packet(repo))
            self.spec(repo)
            with mock.patch.object(self.gate.subprocess, "run", side_effect=OSError("Git unavailable")):
                handoff = self.gate.spec_handoff(repo, self.gate.load_config(repo),
                                                (repo / "TASK_QUEUE.md").read_text(), "EAV-2")
            self.assertIn("STALE/UNVERIFIED", handoff)

    def test_legacy_discovery_skips_specs_without_acceptance_and_honors_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            base = self.spec(repo)
            first = repo / "app/docs/work/a/spec.md"
            selected = repo / "app/docs/work/b/spec.md"
            first.parent.mkdir(parents=True)
            selected.parent.mkdir(parents=True)
            first.write_text("## Notes\nEAV-2 unrelated history.\n")
            (repo / "app/related.txt").write_text("source")
            run("git", "add", "app/related.txt", cwd=repo)
            run("git", "commit", "-qm", "prefixed source", cwd=repo)
            base = run("git", "rev-parse", "HEAD", cwd=repo).stdout.strip()
            selected.write_text("## Acceptance\nLegacy acceptance.\n## Handoff EAV-2\n"
                                + '<!-- handoff: ' + json.dumps({"base": base, "files": ["related.txt"]})
                                + ' -->\nStop conditions: retain API.\n')
            cfg = self.gate.load_config(repo)
            cfg["project_prefix"] = "app"
            self.assertEqual(self.gate.spec_acceptance(repo, cfg, "", "EAV-2"), "Legacy acceptance.")
            self.assertIn("related files unchanged", self.gate.spec_handoff(repo, cfg, "", "EAV-2"))
            (repo / "app/related.txt").write_text("drift")
            self.assertIn("STALE/UNVERIFIED", self.gate.spec_handoff(repo, cfg, "", "EAV-2"))

    def test_handoff_is_not_truncated_and_counts_toward_existing_cap(self):
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ["codex"])
            body = "Entry: " + "x" * 7000 + "\nStop conditions: NEVER OMIT."
            self.spec(repo, body=body)
            packet = self.packet(repo)
            self.assertIn(body, packet)
            notes = self.gate.planning_diagnostics(repo, self.gate.load_config(repo),
                                                  (repo / "TASK_QUEUE.md").read_text(), ["EAV-2"])
            self.assertTrue(any(n["level"] == "error" and "packet" in n["message"] for n in notes))


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
            cfg["worker_cmds"] = {"stub": [sys.executable, "-c", "print('finished')"]}
            self.gate.RUN_BUDGET.update(max=2, calls=0)
            # This one local Python measurement stub is deliberately not a model CLI.
            with mock.patch.object(self.gate, "resolve_tool", return_value=sys.executable), \
                 mock.patch.object(self.gate, "validate_worker", return_value=None):
                result, err = self.gate.spawn_worker(repo, cfg, "stub", "do work", repo / ".gate/prompt.md")
            self.assertIsNone(err)
            self.assertEqual(result.returncode, 0)
            events = list(self.gate.iter_journal(repo))
            self.assertEqual([e["event"] for e in events], ["model_call", "model_call_end"])
            self.assertEqual(events[0]["call"], events[1]["call"])
            self.assertEqual(events[1]["role"], "worker")
            self.assertGreater(events[1]["seconds"], 0)
            self.assertEqual(events[1]["prompt_bytes"], len("do work".encode()))
            self.assertEqual(events[1]["output_bytes"], len("finished\n".encode()))
            self.assertIsNone(events[1]["usage"]["total_tokens"])

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


class ScopeDecisionBoundaryTests(ReviewHarness):
    """Exercise the real runner with offline worker/provider transcripts."""

    @staticmethod
    def marker(**changes):
        body = dict(version=1, task='EAV-2', status='scope_request',
                    files=['gate/test_efficiency.py'], reason='Required regression outside admitted scope')
        body.update(changes)
        return 'FLOW_RESULT: ' + json.dumps(body)

    @staticmethod
    def final(text):
        return json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}})

    def test_scope_and_protocol_stops_preserve_edits_without_retry_or_settlement(self):
        legacy = ('EV-8 is blocked by a required scope expansion for gate/test_autonomy.py. '
                  'Please add it to EV-8’s scope. Existing changes remain intact')
        cases = [(self.marker(), 'scope_request'), (self.final(legacy), 'scope_request'),
                 ('Blocked: needs your decision on declared files src/extra.py', 'scope_request'),
                 (self.marker(task='OTHER'), 'worker_protocol_error'),
                 (self.marker(files=['../unsafe']), 'worker_protocol_error'),
                 ('FLOW_RESULT: {bad', 'worker_protocol_error')]
        for output, expected in cases:
            with self.subTest(output=output), tempfile.TemporaryDirectory() as td:
                repo = judged_runner_repo(Path(td), ['codex', 'kimi'])
                configure(repo, max_attempts_per_task=4)
                head = self.gate.git(repo, 'rev-parse', 'HEAD')
                seen = []
                def spawn(*args, **kwargs):
                    seen.append(args[2])
                    (repo / 'eav2.txt').write_bytes(b'partial edits\x00preserved\n')
                    return self.gate.WorkerResult(0, output), None
                judge, calls = self._judge([])
                report = self._run(repo, spawn, judge, expect_exit='3')
                self.assertEqual(len(seen), 1)
                self.assertEqual(calls['n'], 0)
                self.assertEqual((repo / 'eav2.txt').read_bytes(), b'partial edits\x00preserved\n')
                self.assertEqual(self.gate.git(repo, 'rev-parse', 'HEAD'), head)
                events = list(self.gate.iter_journal(repo))
                decision = next(e for e in events if e['event'] == expected)
                self.assertTrue(decision['reason'])
                self.assertTrue(decision['log'])
                if expected == 'scope_request':
                    self.assertTrue(decision['files'])
                    self.assertIn(decision['reason'], report)
                    for path in decision['files']:
                        self.assertIn(path, report)
                self.assertFalse(any(e['event'] in ('task_done', 'full_acceptance',
                                                   'stage_acceptance', 'review_start') for e in events))
                self.assertIn(expected + ':EAV-2', report)

    def test_done_conflict_cannot_settle(self):
        for output in (self.marker(), 'FLOW_RESULT: {bad'):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as td:
                repo = judged_runner_repo(Path(td), ['codex'])
                seen = []
                close = self._spawn(seen)
                def spawn(*args, **kwargs):
                    result, error = close(*args, **kwargs)
                    result.stdout = output
                    return result, error
                judge, calls = self._judge([])
                report = self._run(repo, spawn, judge, expect_exit='3')
                self.assertEqual(len(seen), 1)
                self.assertEqual(calls['n'], 0)
                events = list(self.gate.iter_journal(repo))
                self.assertFalse(any(e['event'] == 'task_done' for e in events))
                self.assertIn('worker_protocol_error:EAV-2', report)

    def test_final_success_and_tool_examples_follow_normal_review_and_verification(self):
        for output in (
            self.final(self.marker()) + '\n' + self.final('Implemented and committed.'),
            json.dumps({'type': 'item.completed', 'item': {'type': 'command_execution',
                        'exit_code': 0, 'aggregated_output': self.marker() + '\n' +
                        'EV-8 is blocked by a required scope expansion. Please add it to EV-8’s scope. '
                        'Existing changes remain intact'}}) + '\n' + self.final('Done'),
        ):
            with self.subTest(output=output), tempfile.TemporaryDirectory() as td:
                repo = judged_runner_repo(Path(td), ['codex'])
                close = self._spawn([])
                def spawn(*args, **kwargs):
                    result, error = close(*args, **kwargs)
                    result.stdout = output
                    return result, error
                judge, calls = self._judge([verdict(90, 'pass', [])])
                self._run(repo, spawn, judge)
                self.assertEqual(calls['n'], 1)
                events = list(self.gate.iter_journal(repo))
                self.assertTrue(any(e['event'] == 'full_acceptance' for e in events))
                self.assertFalse(any(e['event'] in ('scope_request', 'worker_protocol_error') for e in events))
                self.assertEqual(self.gate.pending_closeout(repo)[0], [])

    def test_verified_done_takes_precedence_over_legacy_prose(self):
        texts = ['Committed. Nothing needs your decision; declared files were sufficient.']
        texts.extend('| \u8981\u4f60\u51b3\u5b9a | ' + value + ' |\nfiles: eav2.txt'
                     for value in ('\u6ca1\u6709', '\u6682\u65e0', 'N/A'))
        for text in texts:
            for output in (text, self.final(text)):
                with self.subTest(output=output), tempfile.TemporaryDirectory() as td:
                    repo = judged_runner_repo(Path(td), ['codex'])
                    installed = run(sys.executable, self.gate.__file__, 'install-hook',
                                    '--repo', str(repo))
                    self.assertEqual(installed.returncode, 0, installed.stderr)
                    seen = []
                    close = self._spawn(seen)
                    def spawn(*args, **kwargs):
                        (repo / 'eav2.txt').write_text('done\n', encoding='utf-8')
                        checked = self.gate.run_acceptance(
                            repo, self.gate.load_config(repo), task='EAV-2', files=['eav2.txt'])
                        self.assertEqual(checked['result'], 'PASS')
                        result, error = close(*args, **kwargs)
                        result.stdout = output
                        return result, error
                    judge, calls = self._judge([verdict(90, 'pass', [])])
                    self._run(repo, spawn, judge)
                    self.assertEqual(len(seen), 1)
                    self.assertEqual(calls['n'], 1)
                    events = list(self.gate.iter_journal(repo))
                    self.assertEqual(sum(e['event'] == 'task_done' for e in events), 1)
                    self.assertTrue(any(e['event'] == 'full_acceptance' for e in events))
                    self.assertFalse(any(e['event'] in ('scope_request', 'worker_protocol_error')
                                         for e in events))
                    self.assertEqual(self.gate.pending_closeout(repo)[0], [])

    def test_scope_marker_cannot_bypass_approval_before_dispatch(self):
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ['codex'])
            configure(repo, irreversible_globs=['eav2.txt'])
            judge, _ = self._judge([])
            def forbidden(*args, **kwargs):
                self.fail('approval must precede dispatch')
            report = self._run(repo, forbidden, judge, expect_exit='3')
            self.assertIn('needs_approval:EAV-2', report)

    def test_old_packages_receive_concrete_scope_contract(self):
        with tempfile.TemporaryDirectory() as td:
            repo = judged_runner_repo(Path(td), ['codex'])
            text = (repo / 'TASK_QUEUE.md').read_text()
            packet = self.gate.task_packet(repo, self.gate.load_config(repo), text, 'EAV-2', ['eav2.txt'])
            line = next(line for line in packet.splitlines() if 'FLOW_RESULT:' in line)
            self.assertIn('"task": "EAV-2"', line)
            self.assertIn('"version": 1', line)
            self.assertIn('preserve partial changes', line)


if __name__ == "__main__":
    unittest.main()

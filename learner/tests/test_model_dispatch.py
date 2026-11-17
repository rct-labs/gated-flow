"""Offline regression tests for tools-off model dispatch routing context, provenance, and tool rejection."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from learner import model_dispatch
from learner.assessment import assess_pending_work
from learner.contracts import sha256_bytes, sha256_json
from learner.model_dispatch import (
    AgyCliRunner,
    build_tools_off_prompt,
    resolve_routing_context,
)
from learner.sources import FixtureTransport
from learner.store import LearnerStore
from learner.tests.fixtures import make_profile
from learner.tests.test_assessment_and_cli import _primary_routes_for_standard_fixtures


class ModelDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1_794_950_000.0

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_routing_files(
        self,
        *,
        policy_note: str = "v1",
        subscription_only: bool = True,
        automatic_paid_fallback: bool = False,
        include_md: bool = True,
    ) -> tuple[Path, Path | None]:
        routing_dir = self.root / "routing"
        routing_dir.mkdir(parents=True, exist_ok=True)
        policy_path = routing_dir / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "effective_date": "2026-11-13",
                    "authority": f"Test routing policy {policy_note}",
                    "coding": {"gemini": {"model": "gemini-4-hydrogen-high", "effort": "high"}},
                    "design": {"gemini": {"model": "gemini-4-carbon-medium", "effort": "medium"}},
                    "subscription_only": subscription_only,
                    "automatic_paid_fallback": automatic_paid_fallback,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        doc_path: Path | None = None
        if include_md:
            doc_path = routing_dir / "POLICY.md"
            doc_path.write_text(
                f"# Machine model routing ({policy_note})\n"
                "Coding uses Hydrogen high; design/review uses Carbon medium.\n"
                "Subscription only; no paid fallback.\n",
                encoding="utf-8",
            )
        return policy_path, doc_path

    def _make_fake_cli(self, name: str, script_body: str) -> Path:
        cli_path = self.root / name
        cli_path.write_text(f"#!{sys.executable}\n{script_body}", encoding="utf-8")
        os.chmod(cli_path, os.stat(cli_path).st_mode | stat.S_IEXEC)
        return cli_path

    def test_tools_off_prompt_supplies_current_routing_docs_digests_and_blind_symmetry(self) -> None:
        policy_path, doc_path = self._write_routing_files(policy_note="rev1")
        assert doc_path is not None

        captured_dir = self.root / "captured"
        captured_dir.mkdir()
        fake_cli = self._make_fake_cli(
            "agy-flow",
            f"""
import json, pathlib, sys
args = sys.argv[1:]
assert '--mode' in args and args[args.index('--mode') + 1] == 'plan'
assert '--disable-slash-commands' in args
settings = json.loads(args[args.index('--settings') + 1])
assert 'read_file(*)' in settings['permissions']['deny']
prompt = args[args.index('-p') + 1]
model = args[args.index('--model') + 1]
idx = len(list(pathlib.Path({str(captured_dir)!r}).glob('*.txt')))
pathlib.Path({str(captured_dir)!r}).joinpath(f'prompt_{{idx}}.txt').write_text(prompt, encoding='utf-8')
print(json.dumps({{'event':'init','init':{{'model':model,'tools':[]}}}}))
print(json.dumps({{'event':'result','result':{{'status':'SUCCESS','response':json.dumps({{'queries':['evaluation harness']}}),'usage':{{'input_tokens':50,'output_tokens':10,'total_tokens':60}},'duration_seconds':0.05}}}}))
""",
        )

        runner = AgyCliRunner(policy_path=policy_path, cli_path=fake_cli)
        task_prompt = json.dumps({"task": "query_planning", "project": {"project_alias": "P1"}}, sort_keys=True)

        rcpt_h = runner.invoke("hydrogen", task_prompt, timeout_s=5.0)
        rcpt_c = runner.invoke("carbon", task_prompt, timeout_s=5.0)
        self.assertEqual(rcpt_h.status, "ok")
        self.assertEqual(rcpt_c.status, "ok")

        sent_h = (captured_dir / "prompt_0.txt").read_text(encoding="utf-8")
        sent_c = (captured_dir / "prompt_1.txt").read_text(encoding="utf-8")
        # Blind symmetry: Hydrogen and Carbon receive byte-for-byte identical prompt text.
        self.assertEqual(sent_h, sent_c)

        expected_json_sha = sha256_bytes(policy_path.read_bytes())
        expected_md_sha = sha256_bytes(doc_path.read_bytes())
        self.assertIn(str(policy_path), sent_h)
        self.assertIn(str(doc_path), sent_h)
        self.assertIn(expected_json_sha, sent_h)
        self.assertIn(expected_md_sha, sent_h)
        self.assertIn("Test routing policy rev1", sent_h)
        self.assertIn("Machine model routing (rev1)", sent_h)
        self.assertIn("No tool access is permitted", sent_h)
        self.assertIn("satisfy the required policy read", sent_h)
        self.assertTrue(sent_h.endswith(task_prompt))

        self.assertEqual(rcpt_h.prompt_digest, sha256_json({"role": "hydrogen", "prompt": sent_h}))
        assert rcpt_h.policy_context is not None
        self.assertEqual(rcpt_h.policy_context["policy_json"]["sha256"], expected_json_sha)
        self.assertEqual(rcpt_h.policy_context["policy_md"]["sha256"], expected_md_sha)
        self.assertTrue(rcpt_h.policy_context["tools_off"])

        # Updating routing docs on disk immediately reflects in the next dispatch on the same runner.
        self._write_routing_files(policy_note="rev2")
        rcpt_h2 = runner.invoke("hydrogen", task_prompt, timeout_s=5.0)
        self.assertEqual(rcpt_h2.status, "ok")
        sent_h2 = (captured_dir / "prompt_2.txt").read_text(encoding="utf-8")
        new_md_sha = sha256_bytes(doc_path.read_bytes())
        self.assertNotEqual(new_md_sha, expected_md_sha)
        self.assertIn(new_md_sha, sent_h2)
        self.assertIn("Machine model routing (rev2)", sent_h2)
        self.assertNotEqual(rcpt_h2.prompt_digest, rcpt_h.prompt_digest)

    def test_tool_rejection_preserved_even_when_valid_json_follows_denied_policy_reads(self) -> None:
        policy_path, doc_path = self._write_routing_files(policy_note="tool-reject")
        assert doc_path is not None

        # Simulate the exact live failure shape: denied read_file calls on POLICY.md & policy.json
        # followed by an otherwise valid JSON response.
        fake_cli = self._make_fake_cli(
            "agy-flow",
            f"""
import json
print(json.dumps({{'event':'init','init':{{'model':'gemini-4-hydrogen-high','tools':['read_file','write_file','run_command']}}}}))
print(json.dumps({{'event':'step_update','step_update':{{'step_index':2,'state':'ACTIVE','step_type':'tool','tool_name':'read_file','tool_info':{{'parameters':{{'Path':{str(doc_path)!r}}}}}}}}}))
print(json.dumps({{'event':'step_update','step_update':{{'step_index':2,'state':'ERROR','step_type':'tool','tool_name':'read_file','tool_info':{{'error':{{'type':'TOOL_ERROR','message':'Permission denied'}}}}}}}}))
print(json.dumps({{'event':'step_update','step_update':{{'step_index':3,'state':'ACTIVE','step_type':'tool','tool_name':'read_file','tool_info':{{'parameters':{{'Path':{str(policy_path)!r}}}}}}}}}))
print(json.dumps({{'event':'step_update','step_update':{{'step_index':3,'state':'ERROR','step_type':'tool','tool_name':'read_file','tool_info':{{'error':{{'type':'TOOL_ERROR','message':'Permission denied'}}}}}}}}))
print(json.dumps({{'event':'result','result':{{'status':'SUCCESS','response':json.dumps({{'queries':['evaluation harness']}}),'usage':{{'input_tokens':100,'output_tokens':20,'total_tokens':120}}}}}}))
""",
        )

        runner = AgyCliRunner(policy_path=policy_path, cli_path=fake_cli)
        rcpt = runner.invoke("hydrogen", json.dumps({"task": "query_planning"}), timeout_s=5.0)
        self.assertEqual(rcpt.status, "invalid_response")
        self.assertIsNone(rcpt.parsed_json)
        self.assertEqual(rcpt.tool_use_count, 4)
        self.assertEqual(rcpt.advertised_tools_count, 3)
        self.assertEqual(rcpt.raw_error, "unexpected_tool_use_in_tools_off_call:4")
        self.assertTrue(rcpt.dispatched)
        self.assertIsNotNone(rcpt.policy_context)

    def test_missing_or_invalid_routing_policy_fails_closed_without_subprocess(self) -> None:
        policy_path, _ = self._write_routing_files(include_md=False)
        marker = self.root / "should_not_run.txt"
        fake_cli = self._make_fake_cli(
            "agy-flow",
            f"import pathlib\npathlib.Path({str(marker)!r}).write_text('RAN', encoding='utf-8')\n",
        )

        # Explicit missing policy_doc_path fails closed before spawning subprocess.
        missing_doc = self.root / "routing" / "MISSING_POLICY.md"
        runner_explicit = AgyCliRunner(policy_path=policy_path, policy_doc_path=missing_doc, cli_path=fake_cli)
        ok, err, _ = runner_explicit.check_available("hydrogen")
        self.assertFalse(ok)
        self.assertIn("policy_doc_missing", err or "")
        rcpt = runner_explicit.invoke("hydrogen", "hello", timeout_s=5.0)
        self.assertEqual(rcpt.status, "unconfigured")
        self.assertFalse(rcpt.dispatched)
        self.assertIn("policy_doc_missing", rcpt.raw_error or "")
        self.assertFalse(marker.exists())

        # Default policy path missing sibling POLICY.md also fails closed before dispatch.
        with mock.patch.object(model_dispatch, "DEFAULT_POLICY_PATH", policy_path):
            runner_default = AgyCliRunner(policy_path=policy_path, cli_path=fake_cli)
            ok_def, err_def, _ = runner_default.check_available("hydrogen")
            self.assertFalse(ok_def)
            self.assertIn("policy_doc_missing", err_def or "")
            rcpt_def = runner_default.invoke("hydrogen", "hello", timeout_s=5.0)
            self.assertEqual(rcpt_def.status, "unconfigured")
            self.assertFalse(rcpt_def.dispatched)
        self.assertFalse(marker.exists())

        # Paid fallback allowed in policy.json fails closed before dispatch.
        paid_policy, _ = self._write_routing_files(automatic_paid_fallback=True, include_md=True)
        runner_paid = AgyCliRunner(policy_path=paid_policy, cli_path=fake_cli)
        rcpt_paid = runner_paid.invoke("hydrogen", "hello", timeout_s=5.0)
        self.assertEqual(rcpt_paid.status, "unconfigured")
        self.assertFalse(rcpt_paid.dispatched)
        self.assertEqual(rcpt_paid.raw_error, "policy_paid_fallback_forbidden")
        self.assertFalse(marker.exists())

        # Helper direct sanity check when sibling POLICY.md is absent on non-default custom policy.json.
        valid_policy, _ = self._write_routing_files(automatic_paid_fallback=False, include_md=False)
        only_json_policy = self.root / "isolated" / "policy.json"
        only_json_policy.parent.mkdir(parents=True, exist_ok=True)
        only_json_policy.write_text(valid_policy.read_text(encoding="utf-8"), encoding="utf-8")
        pin, ctx, ctx_err = resolve_routing_context("hydrogen", policy_path=only_json_policy)
        self.assertIsNotNone(pin)
        self.assertIsNone(ctx_err)
        assert ctx is not None
        self.assertFalse(ctx["policy_md"]["present"])
        rendered = build_tools_off_prompt('{"task":"ping"}', ctx)
        self.assertIn("POLICY.md not present", rendered)
        self.assertIn("No tool access is permitted", rendered)

    def test_end_to_end_assessment_with_agy_runner_preserves_receipts_budgets_and_privacy(self) -> None:
        policy_path, doc_path = self._write_routing_files(policy_note="e2e")
        assert doc_path is not None

        fake_cli = self._make_fake_cli(
            "agy-flow",
            f"""
import json, sys
args = sys.argv[1:]
prompt = args[args.index('-p') + 1]
model = args[args.index('--model') + 1]
# Must always have pre-supplied POLICY.md & policy.json digests and no-tool notice.
assert {sha256_bytes(doc_path.read_bytes())!r} in prompt
assert {sha256_bytes(policy_path.read_bytes())!r} in prompt
assert 'No tool access is permitted' in prompt
assert 'SECRET_PRIVATE_MARKER_99' not in prompt
assert 'PrivateSecretApp' not in prompt

task_marker = '[TASK INPUT — RETURN ONLY VALID JSON, NO TOOL CALLS]\\n'
task_json = json.loads(prompt.split(task_marker, 1)[1])
task = task_json['task']
if task == 'batch_triage':
    alias = task_json['projects'][0]['project_alias']
    decisions = []
    for cand in task_json['untrusted_source_data']:
        if 'harness-bench' in cand['title'].lower():
            decisions.append({{
                'source_id': cand['source_id'],
                'project_alias': alias,
                'decision': 'qualify',
                'goal_ref': 'improve automated evaluation harnesses',
                'capability_ref': 'deterministic evaluation replay',
                'rationale': 'Adjacent offline harness matches goal.',
                'uncertainty': 'Verify README.',
                'already_implemented': False,
                'reopen_triggers': ['new_source_version'],
            }})
        else:
            decisions.append({{
                'source_id': cand['source_id'],
                'project_alias': alias,
                'decision': 'reject',
                'goal_ref': '',
                'capability_ref': '',
                'rationale': 'Unrelated.',
                'uncertainty': 'Low.',
                'already_implemented': False,
                'reopen_triggers': ['profile_goal_changed'],
            }})
    resp = {{'decisions': decisions}}
elif task == 'independent_blind_assessment':
    ev = task_json['untrusted_source_data']['retrieved_evidence'][0]
    resp = {{
        'recommendation': 'borrow',
        'alternatives': ['Keep existing runner'],
        'borrowable_change': 'Borrow JSON case replay check.',
        'affected_existing_capability': 'deterministic test runner',
        'expected_benefit': 'Clearer offline verification.',
        'effort_and_risk': 'Low effort.',
        'test_baseline_rollback': {{'test_plan': 'unittest', 'baseline': '40 tests', 'rollback': 'revert'}},
        'citations': [{{'evidence_id': ev['evidence_id'], 'url': ev['url'], 'digest': ev['digest'], 'claim': 'README cites replay'}}],
        'unresolved_claims': [],
        'uncertainty': 'Offline only.',
        'claim_kind': 'model_inference',
    }}
elif task == 'bounded_proposal_synthesis':
    ev = task_json['retrieved_evidence'][0]
    h_eval = task_json['independent_assessments']['hydrogen']
    resp = {{
        'recommendation': 'borrow',
        'alternatives': h_eval['alternatives'],
        'borrowable_change': h_eval['borrowable_change'],
        'affected_existing_capability': h_eval['affected_existing_capability'],
        'expected_benefit': h_eval['expected_benefit'],
        'effort_and_risk': h_eval['effort_and_risk'],
        'test_baseline_rollback': h_eval['test_baseline_rollback'],
        'citations': [{{'evidence_id': ev['evidence_id'], 'url': ev['url'], 'digest': ev['digest'], 'claim': 'Primary README'}}],
        'dissent': [],
        'unresolved_claims': [],
        'uncertainty': h_eval['uncertainty'],
        'claim_kind': 'model_inference',
    }}
else:
    raise AssertionError(f'Unexpected task: {{task}}')

print(json.dumps({{'event':'init','init':{{'model':model,'tools':[]}}}}))
print(json.dumps({{'event':'result','result':{{'status':'SUCCESS','response':json.dumps(resp),'usage':{{'input_tokens':200,'output_tokens':80,'total_tokens':280}},'duration_seconds':0.05}}}}))
""",
        )

        store = LearnerStore(self.root / "store", clock=lambda: self.now)
        try:
            profile = make_profile(
                project_id="priv-proj",
                name="PrivateSecretApp",
                mission="Protect PrivateSecretApp secrets SECRET_PRIVATE_MARKER_99",
                private_terms=("SECRET_PRIVATE_MARKER_99",),
                execution_paused=True,
            )
            store.register_profile(profile)
            store.set_trial_grant(max_model_calls=5, expires_at=self.now + 3600.0, grant_id="il6-test")
            transport = FixtureTransport(_primary_routes_for_standard_fixtures())
            runner = AgyCliRunner(policy_path=policy_path, cli_path=fake_cli)

            from learner.pipeline import run_intake

            run_intake(store, "project-led", transport, project_id=profile.project_id)
            res = assess_pending_work(store, transport, runner, project_ids=(profile.project_id,))
            self.assertEqual(res["proposed"], 1, res)

            # Shared triage receipt stored in internal evidence must have provenance digests and zero private leaks.
            triage_dispatches = [d for d in store.list_dispatches() if d.scope_type == "shared_triage"]
            self.assertEqual(len(triage_dispatches), 1)
            triage_receipt = triage_dispatches[0].receipt
            assert triage_receipt is not None
            self.assertIn("policy_context", triage_receipt)
            self.assertEqual(
                triage_receipt["policy_context"]["policy_md"]["sha256"],
                sha256_bytes(doc_path.read_bytes()),
            )
            ev_bytes = store.get_evidence(triage_receipt["evidence_digest"]).decode("utf-8")
            self.assertNotIn("SECRET_PRIVATE_MARKER_99", ev_bytes)
            self.assertNotIn("PrivateSecretApp", ev_bytes)
            self.assertNotIn("Machine model routing (e2e)", ev_bytes)
        finally:
            store.close()

    def test_timeout_bytes_and_text_communicate_normalized_without_duplication_and_no_blind_retry(self) -> None:
        policy_path, doc_path = self._write_routing_files(policy_note="timeout-recovery")
        assert doc_path is not None

        # 1. Real subprocess timeout after emitting partial stdout/stderr (Python 3.14 TimeoutExpired
        # delivers bytes while post-kill communicate() delivers cumulative str).
        pid_file = self.root / "timeout_child.pid"
        fake_cli = self._make_fake_cli(
            "agy-flow",
            f"""
import json, os, sys, time
open({str(pid_file)!r}, 'w').write(str(os.getpid()))
print(json.dumps({{'event':'init','init':{{'model':'gemini-4-carbon-medium','tools':[]}}}}), flush=True)
print(json.dumps({{'event':'step_update','step_update':{{'step_index':1,'state':'ACTIVE','step_type':'thinking'}}}}), flush=True)
sys.stderr.write('partial stderr diagnostic\\n')
sys.stderr.flush()
time.sleep(30)
""",
        )
        runner = AgyCliRunner(policy_path=policy_path, cli_path=fake_cli)
        rcpt = runner.invoke("carbon", json.dumps({"task": "independent_blind_assessment"}), timeout_s=0.35)
        self.assertEqual(rcpt.status, "timeout")
        self.assertTrue(rcpt.timed_out)
        self.assertTrue(rcpt.dispatched)
        self.assertIsNone(rcpt.parsed_json)
        self.assertEqual(rcpt.returned_model, "gemini-4-carbon-medium")
        self.assertEqual(rcpt.usage["status"], "unknown")
        self.assertIsNone(rcpt.usage["total_tokens"])
        self.assertEqual(rcpt.raw_output.count('"event": "init"'), 1)
        self.assertEqual(rcpt.raw_output.count('"step_type": "thinking"'), 1)
        self.assertIn("partial stderr diagnostic", rcpt.raw_error or "")
        self.assertEqual((rcpt.raw_error or "").count("partial stderr diagnostic"), 1)
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

        # 2. Explicit mocked Popen TimeoutExpired regressions:
        #    - bytes TimeoutExpired + overlapping cumulative str communicate (including terminal event)
        #    - missing (None) TimeoutExpired output + str communicate
        #    - bytes TimeoutExpired + nested second TimeoutExpired
        #    - bytes TimeoutExpired containing denied tool use (tools-off rejection preserved)
        init_line = json.dumps({"event": "init", "init": {"model": "gemini-4-carbon-medium", "tools": ["read_file"]}}) + "\n"
        think_line = json.dumps({"event": "step_update", "step_update": {"step_type": "thinking"}}) + "\n"
        term_line = json.dumps({"event": "result", "result": {"status": "ERROR", "response": "", "usage": {"input_tokens": 11}}}) + "\n"
        tool_line = json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_calls": [{"name": "read_file"}]}}) + "\n"

        class _FakeProc:
            def __init__(self, first_exc: subprocess.TimeoutExpired, second: tuple[str | bytes | None, str | bytes | None] | Exception):
                self.pid = 999999
                self.returncode = -9
                self._first_exc = first_exc
                self._second = second
                self._calls = 0

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                self._calls += 1
                if self._calls == 1:
                    raise self._first_exc
                if isinstance(self._second, Exception):
                    raise self._second
                return self._second  # type: ignore[return-value]

        cases = [
            (
                "overlapping_bytes_and_str_with_terminal",
                subprocess.TimeoutExpired(cmd="agy", timeout=300, output=init_line.encode("utf-8"), stderr=b"err1\n"),
                (init_line + think_line + term_line, "err1\nerr2\n"),
                lambda r: (
                    self.assertEqual(r.status, "timeout"),
                    self.assertIsNone(r.parsed_json),
                    self.assertEqual(r.raw_output.count('"event": "init"'), 1),
                    self.assertIsNotNone(r.raw_terminal),
                    self.assertEqual(r.usage["status"], "observed"),
                    self.assertEqual(r.usage["input_tokens"], 11),
                    self.assertEqual(r.raw_error, "err1\nerr2"),
                ),
            ),
            (
                "missing_timeout_bytes_plus_str",
                subprocess.TimeoutExpired(cmd="agy", timeout=300, output=None, stderr=None),
                (init_line, ""),
                lambda r: (
                    self.assertEqual(r.status, "timeout"),
                    self.assertEqual(r.returned_model, "gemini-4-carbon-medium"),
                    self.assertEqual(r.usage["status"], "unknown"),
                    self.assertIn("timeout_after_", r.raw_error or ""),
                ),
            ),
            (
                "nested_timeout_preserves_bytes",
                subprocess.TimeoutExpired(cmd="agy", timeout=300, output=init_line.encode("utf-8"), stderr=b"hung\n"),
                subprocess.TimeoutExpired(cmd="agy", timeout=5, output=(init_line + think_line).encode("utf-8"), stderr=None),
                lambda r: (
                    self.assertEqual(r.status, "timeout"),
                    self.assertEqual(r.raw_output.count('"event": "init"'), 1),
                    self.assertIn('"step_type": "thinking"', r.raw_output),
                    self.assertEqual(r.raw_error, "hung"),
                ),
            ),
            (
                "tools_off_rejection_during_timeout",
                subprocess.TimeoutExpired(cmd="agy", timeout=300, output=(init_line + tool_line).encode("utf-8"), stderr=b""),
                (init_line + tool_line, ""),
                lambda r: (
                    self.assertEqual(r.status, "timeout"),
                    self.assertIsNone(r.parsed_json),
                    self.assertEqual(r.advertised_tools_count, 1),
                    self.assertEqual(r.tool_use_count, 2),
                    self.assertIn("unexpected_tool_use_in_tools_off_call:2", r.raw_error or ""),
                ),
            ),
        ]
        for label, first_exc, second_outcome, check_fn in cases:
            with self.subTest(label=label):
                fake_proc = _FakeProc(first_exc, second_outcome)
                with mock.patch("learner.model_dispatch.subprocess.Popen", return_value=fake_proc), mock.patch(
                    "learner.model_dispatch._kill_process_group"
                ) as kill_mock:
                    res = runner.invoke("carbon", "ping", timeout_s=300.0)
                kill_mock.assert_called_once_with(fake_proc)
                check_fn(res)

        # 3. End-to-end deep Carbon timeout in assess_pending_work:
        #    - deep call uses up to 300s bounded by remaining 20min wall deadline (not 120s)
        #    - global ModelRunner/AgyCliRunner default remains 120.0
        #    - Carbon timeout creates durable unknown dispatch + receipt evidence and defers assessment
        #    - Subsequent assess_pending_work never blindly retries the unknown dispatch
        import inspect
        from learner.contracts import SharedEnvelope
        from learner.model_dispatch import FixtureModelRunner
        from learner.pipeline import run_intake
        from learner.tests.test_assessment_and_cli import _standard_model_handler

        self.assertEqual(inspect.signature(AgyCliRunner.invoke).parameters["timeout_s"].default, 120.0)
        self.assertEqual(inspect.signature(FixtureModelRunner.invoke).parameters["timeout_s"].default, 120.0)

        store = LearnerStore(self.root / "deep-timeout-store", clock=lambda: self.now)
        try:
            prof = make_profile(project_id="flow", name="flow", execution_paused=True)
            store.register_profile(prof)
            store.set_trial_grant(unlimited=True, grant_id="il11-to", authority="Owner test")
            transport = FixtureTransport(_primary_routes_for_standard_fixtures())
            run_intake(store, "project-led", transport, project_id="flow")

            base_handler = _standard_model_handler()
            call_count = {"n": 0}

            def _carbon_timeout_handler(role: str, prompt: str) -> dict[str, object]:
                call_count["n"] += 1
                if role == "carbon":
                    raise TimeoutError("carbon timed out at 300s")
                return base_handler(role, prompt)

            fx = FixtureModelRunner(_carbon_timeout_handler, requires_trial_grant=True)
            res1 = assess_pending_work(
                store,
                transport,
                fx,
                project_ids=("flow",),
                envelope=SharedEnvelope(wall_limit_s=1200.0),
                deadline_monotonic=1000.0 + 1200.0,
                clock_monotonic=lambda: 1000.0,
            )
            self.assertEqual(res1["status"], "degraded")
            self.assertIn("carbon_timeout_unknown", res1["errors"])
            triage_call = next(c for c in fx.calls if '"batch_triage"' in c.prompt)
            h_call = next(c for c in fx.calls if c.role == "hydrogen" and '"independent_blind_assessment"' in c.prompt)
            c_call = next(c for c in fx.calls if c.role == "carbon" and '"independent_blind_assessment"' in c.prompt)
            self.assertEqual(triage_call.timeout_s, 120.0)
            self.assertEqual(h_call.timeout_s, 300.0)
            self.assertEqual(c_call.timeout_s, 300.0)

            unknowns = store.list_dispatches(state="unknown", include_shared=True)
            self.assertEqual(len(unknowns), 1)
            self.assertEqual(unknowns[0].scope_type, "deep_carbon")
            assert unknowns[0].receipt is not None
            self.assertEqual(unknowns[0].receipt["status"], "timeout")
            self.assertTrue(unknowns[0].receipt["timed_out"])

            asmts = store.list_assessments("flow")
            bench = next(a for a in asmts if a.lifecycle_state == "matched")
            self.assertEqual(bench.decision, "defer")
            self.assertEqual(bench.assessment_status, "unknown_pending_reconciliation")

            # Re-running with tight deadline (45s remaining) must NOT retry unknown carbon!
            calls_before = call_count["n"]
            res2 = assess_pending_work(
                store,
                transport,
                fx,
                project_ids=("flow",),
                deadline_monotonic=1000.0 + 45.0,
                clock_monotonic=lambda: 1000.0,
            )
            self.assertEqual(call_count["n"], calls_before)
            self.assertIn("unknown_pending_reconciliation", res2["errors"])
        finally:
            store.close()

        # Verify deep timeout is bounded by remaining run deadline when < 300s.
        store_bound = LearnerStore(self.root / "deep-bound-store", clock=lambda: self.now)
        try:
            prof_b = make_profile(project_id="flow", name="flow", execution_paused=True)
            store_bound.register_profile(prof_b)
            store_bound.set_trial_grant(unlimited=True, grant_id="il11-bound", authority="Owner test")
            transport_b = FixtureTransport(_primary_routes_for_standard_fixtures())
            run_intake(store_bound, "project-led", transport_b, project_id="flow")
            fx_b = FixtureModelRunner(_standard_model_handler(), requires_trial_grant=True)
            res_b = assess_pending_work(
                store_bound,
                transport_b,
                fx_b,
                project_ids=("flow",),
                deadline_monotonic=2000.0 + 45.0,
                clock_monotonic=lambda: 2000.0,
            )
            self.assertEqual(res_b["proposed"], 1)
            deep_calls = [c for c in fx_b.calls if '"independent_blind_assessment"' in c.prompt or '"bounded_proposal_synthesis"' in c.prompt]
            self.assertTrue(deep_calls)
            for dc in deep_calls:
                self.assertEqual(dc.timeout_s, 45.0)
        finally:
            store_bound.close()


if __name__ == "__main__":
    unittest.main()

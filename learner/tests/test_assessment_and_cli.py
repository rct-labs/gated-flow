"""Meaningful offline tests for IL-2 shared assessment, model dispatch, privacy, caps, scheduling, and UI API."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

from learner.assessment import (
    apply_feedback_preference,
    assess_pending_work,
    format_user_recommendations,
    get_language_preferences,
    get_recommendations,
    run_due,
    run_review,
    set_language_preferences,
)
from learner.cli import main as cli_main
from learner.contracts import (
    BudgetPolicy,
    DispatchBlockedError,
    LeaseConflictError,
    PrivacyViolationError,
    SharedEnvelope,
    SourcePolicy,
    ValidationError,
)
from learner.model_dispatch import AgyCliRunner, FixtureModelRunner, resolve_policy_pin
from learner.pipeline import draft_profile_from_docs, run_intake, summarize_intake_fit
from learner.privacy import assert_safe_model_packet, derive_sanitized_concepts
from learner.sources import FixtureTransport, TransportResponse
from learner.store import LearnerStore
from learner.tests.fixtures import build_standard_transport, make_profile


def _primary_routes_for_standard_fixtures(*, fail_readme: bool = False) -> dict[str, TransportResponse]:
    base = build_standard_transport().routes
    routes: dict[str, TransportResponse] = dict(base)
    routes["https://api.github.com/repos/eval-org/harness-bench"] = TransportResponse(
        status_code=200,
        body=json.dumps(
            {
                "full_name": "eval-org/harness-bench",
                "description": "Lightweight evaluation harness for deterministic agent verification",
                "license": {"spdx_id": "MIT"},
                "pushed_at": "2026-11-15T10:00:00Z",
                "archived": False,
                "open_issues_count": 4,
                "default_branch": "main",
                "stargazers_count": 420,
            }
        ).encode("utf-8"),
        headers={"content-type": "application/json"},
        final_url="https://api.github.com/repos/eval-org/harness-bench",
    )
    routes["https://raw.githubusercontent.com/eval-org/harness-bench/HEAD/README.md"] = (
        TransportResponse(
            status_code=404,
            body=b"Not Found",
            headers={},
            final_url="https://raw.githubusercontent.com/eval-org/harness-bench/HEAD/README.md",
        )
        if fail_readme
        else TransportResponse(
            status_code=200,
            body=(
                "# harness-bench\n"
                "Deterministic offline evaluation harness with JSON case replay.\n"
                "Limitation: requires Python 3.11+ and offline fixture files.\n"
            ).encode("utf-8"),
            headers={"content-type": "text/plain"},
            final_url="https://raw.githubusercontent.com/eval-org/harness-bench/HEAD/README.md",
        )
    )
    routes["https://arxiv.org/abs/2611.00123"] = TransportResponse(
        status_code=200,
        body=(
            "<html><body><h1>Bounded Evaluation Harnesses for Offline Task Scheduling</h1>"
            "<p>We study deterministic evaluation harnesses and bounded scheduling.</p>"
            "<p>Caveat: evaluated on synthetic benchmark suites only.</p></body></html>"
        ).encode("utf-8"),
        headers={"content-type": "text/html"},
        final_url="https://arxiv.org/abs/2611.00123",
    )
    return routes


def _standard_model_handler(
    *,
    carbon_recommendation: str = "borrow",
    invalid_citations: bool = False,
    claim_verified_improvement: bool = False,
    fail_on_role: str | None = None,
    timeout_on_role: str | None = None,
):
    def _handle(role: str, prompt: str) -> dict[str, object]:
        req = json.loads(prompt)
        task = req["task"]
        if task == "query_planning":
            return {"queries": ["evaluation harness", "task scheduling"]}

        if task == "batch_triage":
            alias = req["projects"][0]["project_alias"]
            decisions = []
            for cand in req["untrusted_source_data"]:
                title_low = cand["title"].lower()
                if "harness-bench" in title_low:
                    decisions.append(
                        {
                            "source_id": cand["source_id"],
                            "project_alias": alias,
                            "decision": "qualify",
                            "goal_ref": "improve automated evaluation harnesses",
                            "capability_ref": "deterministic evaluation replay",
                            "rationale": "Adjacent offline evaluation harness complements goal without predeclared gap.",
                            "uncertainty": "Need primary README and license verification.",
                            "already_implemented": False,
                            "reopen_triggers": ["new_source_version", "profile_goal_changed"],
                        }
                    )
                elif "deterministic-runner" in title_low:
                    decisions.append(
                        {
                            "source_id": cand["source_id"],
                            "project_alias": alias,
                            "decision": "reject",
                            "goal_ref": "",
                            "capability_ref": "deterministic test runner",
                            "rationale": "Duplicates existing deterministic test runner capability.",
                            "uncertainty": "Low uncertainty from summary.",
                            "already_implemented": True,
                            "reopen_triggers": ["profile_capabilities_changed"],
                        }
                    )
                else:
                    decisions.append(
                        {
                            "source_id": cand["source_id"],
                            "project_alias": alias,
                            "decision": "reject",
                            "goal_ref": "",
                            "capability_ref": "",
                            "rationale": "Unrelated domain despite high popularity.",
                            "uncertainty": "Low uncertainty.",
                            "already_implemented": False,
                            "reopen_triggers": ["profile_goal_changed"],
                        }
                    )
            return {"decisions": decisions}

        if task == "independent_blind_assessment":
            if timeout_on_role == role:
                raise TimeoutError(f"{role} timed out mid-dispatch")
            if fail_on_role == role:
                raise RuntimeError(f"{role} provider 503 error")
            ev_list = req["untrusted_source_data"]["retrieved_evidence"]
            first_ev = ev_list[0]
            cite_id = "ev_nonexistent_999" if invalid_citations else first_ev["evidence_id"]
            rec = carbon_recommendation if role == "carbon" else "borrow"
            benefit = (
                "This is a verified improvement of 40%."
                if claim_verified_improvement
                else "May improve offline evaluation harness determinism."
            )
            return {
                "recommendation": rec,
                "alternatives": ["Keep existing harness", "Adopt minimal replay helper"],
                "borrowable_change": "Borrow deterministic JSON case replay check.",
                "affected_existing_capability": "deterministic test runner",
                "expected_benefit": benefit,
                "effort_and_risk": "Low effort; risk of schema mismatch mitigated by unit tests.",
                "test_baseline_rollback": {
                    "test_plan": "Run offline unittest suite on frozen evaluation cases",
                    "baseline": "Current 17 offline unit tests",
                    "rollback": "Revert replay adapter helper commit",
                },
                "citations": [
                    {
                        "evidence_id": cite_id,
                        "url": first_ev["url"],
                        "digest": first_ev["digest"],
                        "claim": "Primary README describes deterministic JSON case replay.",
                    }
                ],
                "unresolved_claims": [],
                "uncertainty": "Measured offline performance not yet tested locally.",
                "claim_kind": "verified_observation" if claim_verified_improvement else "model_inference",
            }

        if task == "bounded_proposal_synthesis":
            ev = req["retrieved_evidence"][0]
            h_eval = req["independent_assessments"]["hydrogen"]
            c_eval = req["independent_assessments"]["carbon"]
            dissent = []
            if h_eval["recommendation"] != c_eval["recommendation"]:
                dissent.append(f"hydrogen={h_eval['recommendation']} vs carbon={c_eval['recommendation']}")
            cite_id = "ev_nonexistent_999" if invalid_citations else ev["evidence_id"]
            return {
                "recommendation": "borrow",
                "alternatives": h_eval["alternatives"],
                "borrowable_change": h_eval["borrowable_change"],
                "affected_existing_capability": h_eval["affected_existing_capability"],
                "expected_benefit": h_eval["expected_benefit"],
                "effort_and_risk": f"H: {h_eval['effort_and_risk']} | C: {c_eval['effort_and_risk']}",
                "test_baseline_rollback": h_eval["test_baseline_rollback"],
                "citations": [
                    {
                        "evidence_id": cite_id,
                        "url": ev["url"],
                        "digest": ev["digest"],
                        "claim": "Cited from primary evidence bundle.",
                    }
                ],
                "dissent": dissent,
                "unresolved_claims": [],
                "uncertainty": h_eval["uncertainty"],
                "claim_kind": "verified_observation" if claim_verified_improvement else "model_inference",
            }
        raise ValueError(f"Unexpected task: {task}")

    return _handle


class AssessmentAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1_794_900_000.0
        self.store = LearnerStore(self.root / "store", clock=lambda: self.now)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def _write_policy(
        self,
        *,
        hydrogen_enabled: bool = True,
        carbon_enabled: bool = True,
        filename: str = "policy.json",
    ) -> Path:
        path = self.root / filename
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "subscription_only": True,
                    "automatic_paid_fallback": False,
                    "coding": {
                        "gemini": {
                            "model": "gemini-4-hydrogen-high",
                            "effort": "high",
                            "enabled": hydrogen_enabled,
                        }
                    },
                    "design": {
                        "gemini": {
                            "model": "gemini-4-carbon-medium",
                            "effort": "medium",
                            "enabled": carbon_enabled,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_no_url_no_gap_adjacent_proposal_and_popular_rejection(self) -> None:
        p = make_profile(known_gaps=(), execution_paused=True)
        self.store.register_profile(p)
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())
        runner = FixtureModelRunner(_standard_model_handler())

        res = run_due(self.store, transport, runner)
        self.assertEqual(res["status"], "ok")
        self.assertGreaterEqual(res["assessment"]["proposed"], 1)

        asmts = self.store.list_assessments(p.project_id)
        by_url = {a.canonical_url: a for a in asmts}
        self.assertEqual(by_url["https://github.com/eval-org/harness-bench"].lifecycle_state, "proposed")
        self.assertEqual(by_url["https://github.com/hyped-org/mega-popular-toy"].lifecycle_state, "closed")
        self.assertEqual(by_url["https://github.com/hyped-org/mega-popular-toy"].decision, "reject")
        self.assertEqual(by_url["https://github.com/meme-org/ascii-cat-party"].lifecycle_state, "closed")

        # No routine deep calls on rejected items; execution_paused remains True and no tasks created.
        prof_after = self.store.get_profile(p.project_id)
        assert prof_after is not None
        self.assertTrue(prof_after.execution_paused)

        recs = get_recommendations(self.store, project_id=p.project_id)
        self.assertEqual(recs["total_available"], 1)
        rec = recs["recommendations"][0]
        self.assertEqual(rec["claim_kind"], "model_inference")
        self.assertFalse(rec["auto_promote_eligible"])
        self.assertFalse(rec["creates_project_tasks"])
        self.assertFalse(rec["adoption_permission_granted"])
        self.assertIn("Execution paused", rec["next_action"])

    def test_existing_capability_suppression(self) -> None:
        p = make_profile(existing_capabilities=("deterministic test runner", "atomic state store"))
        self.store.register_profile(p)
        routes = _primary_routes_for_standard_fixtures()
        routes["https://api.github.com/search/repositories*"] = TransportResponse(
            status_code=200,
            body=json.dumps(
                {
                    "items": [
                        {
                            "full_name": "test-org/deterministic-runner",
                            "html_url": "https://github.com/test-org/deterministic-runner",
                            "description": "Deterministic test runner with atomic state store",
                            "stargazers_count": 5000,
                            "license": {"spdx_id": "MIT"},
                        }
                    ]
                }
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
            final_url="https://api.github.com/search/repositories",
        )
        routes["https://github.com/trending*"] = TransportResponse(503, b"down", {}, "https://github.com/trending")
        routes["https://export.arxiv.org/api/query*"] = TransportResponse(
            200, b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>', {}, "https://export.arxiv.org/api/query"
        )
        transport = FixtureTransport(routes)
        runner = FixtureModelRunner(_standard_model_handler())

        res = run_review(self.store, transport, runner, project_id=p.project_id, mode="project-led")
        self.assertEqual(res["assessment"]["proposed"], 0)
        self.assertEqual(res["assessment"]["rejected"], 1)
        asmt = self.store.list_assessments(p.project_id)[0]
        self.assertEqual(asmt.lifecycle_state, "closed")
        self.assertIn("already_implemented", asmt.reason)
        # Only 1 triage call; zero deep calls for already-implemented item.
        self.assertEqual(len(runner.calls), 1)

    def test_primary_fetch_failure_defers_without_deep_model_calls(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        transport = FixtureTransport(_primary_routes_for_standard_fixtures(fail_readme=True))
        runner = FixtureModelRunner(_standard_model_handler())

        res = run_due(self.store, transport, runner)
        self.assertEqual(res["assessment"]["proposed"], 0)
        self.assertGreaterEqual(res["assessment"]["deferred"], 1)
        by_url = {a.canonical_url: a for a in self.store.list_assessments(p.project_id)}
        bench = by_url["https://github.com/eval-org/harness-bench"]
        self.assertEqual(bench.lifecycle_state, "matched")
        self.assertEqual(bench.decision, "defer")
        self.assertEqual(bench.last_completed_step, "primary_fetch_deferred")
        self.assertIn("primary_evidence_inaccessible", bench.reason)
        # Only triage ran (1 Hydrogen call); zero deep assessor or synthesis calls!
        self.assertEqual(len(runner.calls), 1)

    def test_private_sentinel_and_secret_leak_prevention_across_docs_queries_and_packets(self) -> None:
        proj_dir = self.root / "PrivateSecretProject"
        proj_dir.mkdir()
        (proj_dir / "README.md").write_text(
            "# PrivateSecretProject\n"
            "Internal token sk-secret1234567890abcdef and SENTINEL_DOC_LEAK_77.\n"
            "Proprietary algorithm QuantumFluxEngine for evaluation harness and task scheduling.\n"
            "## Goals\n- Improve deterministic verification and offline test runner.\n",
            encoding="utf-8",
        )
        draft = draft_profile_from_docs("priv-proj", proj_dir, name="PrivateSecretProject")
        # IL-1 fix: derive_sanitized_concepts ONLY emits approved generic concepts, NEVER arbitrary doc words or secrets!
        for c in draft.profile.public_concepts:
            self.assertNotIn("quantumfluxengine", c.lower())
            self.assertNotIn("sentinel", c.lower())
            self.assertNotIn("sk-secret", c.lower())
            self.assertNotIn("privatesecret", c.lower())
        self.assertIn("evaluation harness", draft.profile.public_concepts)

        # Direct call to derive_sanitized_concepts never echoes arbitrary unknown words or secrets.
        derived = derive_sanitized_concepts(["ProprietaryUnknownToken ghp_1234567890abcdef1234 evaluation benchmark"])
        self.assertEqual(derived, ("evaluation harness",))

        p = make_profile(
            project_id="priv-proj",
            name="PrivateSecretProject",
            mission="Optimize PrivateSecretProject using SENTINEL_MISSION_88",
            private_terms=("SENTINEL_PRIVATE_TERM_99", "QuantumFluxEngine"),
            goals=("improve evaluation harness with sk-abcdef123456789012",),
        )
        self.store.register_profile(p)
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())
        runner = FixtureModelRunner(_standard_model_handler())

        run_due(self.store, transport, runner)
        for called_url in transport.calls:
            self.assertNotIn("SENTINEL", called_url)
            self.assertNotIn("QuantumFluxEngine", called_url)
            self.assertNotIn("sk-abcdef", called_url)
        for call in runner.calls:
            self.assertNotIn("SENTINEL", call.prompt)
            self.assertNotIn("QuantumFluxEngine", call.prompt)
            self.assertNotIn("sk-abcdef", call.prompt)
            self.assertNotIn("priv-proj", call.prompt)
            self.assertNotIn("PrivateSecretProject", call.prompt)

        with self.assertRaises(PrivacyViolationError):
            assert_safe_model_packet("Leak SENTINEL_ANY_999", (p,))

    def test_blind_identical_assessor_packets_and_dissent_preserved(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())
        runner = FixtureModelRunner(_standard_model_handler(carbon_recommendation="reject"))

        res = run_due(self.store, transport, runner)
        self.assertEqual(res["status"], "ok")

        assess_calls = [c for c in runner.calls if "independent_blind_assessment" in c.prompt]
        self.assertEqual(len(assess_calls), 2)
        h_call, c_call = assess_calls
        self.assertEqual(h_call.role, "hydrogen")
        self.assertEqual(c_call.role, "carbon")
        # Identical factual packet byte-for-byte; no peer answer in either packet.
        self.assertEqual(h_call.prompt, c_call.prompt)
        self.assertNotIn("hydrogen", h_call.prompt.lower())
        self.assertNotIn("carbon", c_call.prompt.lower())

        recs = get_recommendations(self.store, project_id=p.project_id)
        self.assertEqual(len(recs["recommendations"]), 1)
        self.assertTrue(any("hydrogen=borrow vs carbon=reject" in d for d in recs["recommendations"][0]["dissent"]))

    def test_invalid_citations_and_verified_improvement_claims_blocked(self) -> None:
        for kwargs in ({"invalid_citations": True}, {"claim_verified_improvement": True}):
            with self.subTest(**kwargs):
                tmp_store = LearnerStore(self.root / f"sub_{len(kwargs)}_{kwargs}", clock=lambda: self.now)
                try:
                    p = make_profile()
                    tmp_store.register_profile(p)
                    transport = FixtureTransport(_primary_routes_for_standard_fixtures())
                    runner = FixtureModelRunner(_standard_model_handler(**kwargs))
                    run_due(tmp_store, transport, runner)
                    recs = get_recommendations(tmp_store, project_id=p.project_id)
                    self.assertEqual(recs["recommendations"], [])
                    asmts = [
                        a for a in tmp_store.list_assessments(p.project_id)
                        if a.canonical_url == "https://github.com/eval-org/harness-bench"
                    ]
                    self.assertEqual(asmts[0].lifecycle_state, "matched")
                    self.assertEqual(asmts[0].decision, "deferred_invalid_citations")
                finally:
                    tmp_store.close()

    def test_provider_unknown_and_recovery_preserves_hydrogen_assessor_and_blocks_blind_retry(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())

        # Carbon times out -> marked unknown, Hydrogen already persisted.
        runner1 = FixtureModelRunner(_standard_model_handler(timeout_on_role="carbon"))
        res1 = run_due(self.store, transport, runner1)
        self.assertEqual(res1["status"], "degraded")

        h_assess_calls_1 = [c for c in runner1.calls if c.role == "hydrogen" and "independent_blind_assessment" in c.prompt]
        c_assess_calls_1 = [c for c in runner1.calls if c.role == "carbon"]
        self.assertEqual(len(h_assess_calls_1), 1)
        self.assertEqual(len(c_assess_calls_1), 1)

        unknowns = self.store.list_dispatches(state="unknown")
        self.assertEqual(len(unknowns), 1)
        self.assertEqual(unknowns[0].scope_type, "deep_carbon")

        # Subsequent run BEFORE reconciliation must NOT blindly retry Carbon or repeat Hydrogen.
        self.now += 100.0
        runner2 = FixtureModelRunner(_standard_model_handler())
        assess_pending_work(self.store, transport, runner2)
        self.assertEqual(runner2.calls, [])

        # Operator reconciles unknown -> failed (confirmed not executed).
        self.store.reconcile_dispatch(unknowns[0].reservation_id, outcome="failed", actual_units=0, note="timeout_confirmed")

        # Resume runs ONLY Carbon + Synthesis; Hydrogen blind assessment is NOT repeated!
        runner3 = FixtureModelRunner(_standard_model_handler())
        res3 = assess_pending_work(self.store, transport, runner3)
        self.assertEqual(res3["proposed"], 1)
        h_blind_3 = [c for c in runner3.calls if c.role == "hydrogen" and "independent_blind_assessment" in c.prompt]
        c_blind_3 = [c for c in runner3.calls if c.role == "carbon" and "independent_blind_assessment" in c.prompt]
        h_synth_3 = [c for c in runner3.calls if c.role == "hydrogen" and "bounded_proposal_synthesis" in c.prompt]
        self.assertEqual(len(h_blind_3), 0)
        self.assertEqual(len(c_blind_3), 1)
        self.assertEqual(len(h_synth_3), 1)

    def test_shared_envelope_caps_and_manipulation_resistance(self) -> None:
        # Reject bools and oversized caps in policy validation.
        with self.assertRaises(ValidationError):
            BudgetPolicy(daily_calls=True)  # type: ignore[arg-type]
        with self.assertRaises(ValidationError):
            SourcePolicy(max_items_per_sweep=500)
        with self.assertRaises(ValidationError):
            SharedEnvelope(daily_hydrogen_calls=10)

        # Two active profiles with inflated budgets must NOT multiply shared caps (5/day H, 2/week deep, 60 metadata).
        p1 = make_profile(project_id="p1", budget=BudgetPolicy(daily_calls=50, weekly_deep_calls=10, daily_fetches=50))
        p2 = make_profile(
            project_id="p2",
            name="Project Two",
            root_path="/tmp/p2",
            mission="Optimize Project Two harnesses",
            private_terms=("P2_SENTINEL",),
            budget=BudgetPolicy(daily_calls=50, weekly_deep_calls=10, daily_fetches=50),
        )
        self.store.register_profile(p1)
        self.store.register_profile(p2)

        # Shared daily_calls cap is still 5 total across both profiles.
        for i in range(5):
            r, should = self.store.reserve_dispatch(
                idempotency_key=f"h-cap-{i}",
                project_id="__shared__",
                scope_type="shared_triage",
                budget_bucket="daily_calls",
                units=1,
            )
            self.assertTrue(should)
            self.store.settle_dispatch(r.reservation_id, actual_units=1)
        with self.assertRaises(DispatchBlockedError) as ctx:
            self.store.reserve_dispatch(
                idempotency_key="h-cap-6",
                project_id="__shared__",
                scope_type="deep_hydrogen",
                budget_bucket="daily_calls",
                units=1,
            )
        self.assertEqual(ctx.exception.reason, "budget_exhausted")

        # Shared weekly_deep_calls cap is 2 across all profiles.
        for i in range(2):
            r, _ = self.store.reserve_dispatch(
                idempotency_key=f"deep-cap-{i}",
                project_id="__shared__",
                scope_type="deep_candidate",
                budget_bucket="weekly_deep_calls",
                units=1,
            )
            self.store.settle_dispatch(r.reservation_id, actual_units=1)
        with self.assertRaises(DispatchBlockedError):
            self.store.reserve_dispatch(
                idempotency_key="deep-cap-3",
                project_id="__shared__",
                scope_type="deep_candidate",
                budget_bucket="weekly_deep_calls",
                units=1,
            )

        # Shared metadata items cap is 60 across both modes.
        r_meta, _ = self.store.reserve_dispatch(
            idempotency_key="meta-55",
            project_id="__shared__",
            scope_type="metadata_items",
            budget_bucket="daily_metadata_items",
            units=55,
        )
        self.store.settle_dispatch(r_meta.reservation_id, actual_units=55)
        self.assertEqual(self.store.remaining_shared_budget("daily_metadata_items"), 5)

    def test_both_modes_fairness_dedup_wall_limit_and_zero_profiles_cli(self) -> None:
        # Unregistered -> CLI `due` does zero network/inference.
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli_main(["--storage-root", str(self.root / "empty-store"), "--json", "due"])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(buf.getvalue())["status"], "noop")

        # Register profile, seed opportunity-led newer than project-led -> project-led runs first (fairness),
        # and shared candidate across both modes is NOT double-counted.
        p = make_profile()
        self.store.register_profile(p)
        self.store.set_setting("schedule:last_run", {"opportunity-led": self.now - 90_000.0, "project-led": {"alpha-proj": 0.0}})
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())
        runner = FixtureModelRunner(_standard_model_handler())

        res = run_due(self.store, transport, runner)
        self.assertEqual(res["modes_run"], ["project-led", "opportunity-led"])
        self.assertEqual(res["assessment"]["proposed"], 1)  # harness-bench proposed once, not twice.

        # Wall limit check via deterministic monotonic clock exceeding 20-minute wall bound.
        ticks = iter([0.0, 1201.0, 1202.0])
        self.now += 8 * 86_400.0
        wall_res = run_due(self.store, transport, runner, clock_monotonic=lambda: next(ticks, 1205.0))
        self.assertEqual(wall_res["status"], "wall_limit_reached")

        # Overlap lease conflict.
        self.store.acquire_lease("schedule:due", "other-cron", ttl_s=60.0)
        with self.assertRaises(LeaseConflictError):
            run_due(self.store, transport, runner)

    def test_backlog_stop_negative_outcomes_top3_quiet_languages_and_preferences(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())
        runner = FixtureModelRunner(_standard_model_handler())

        # Quiet mode when 0 recommendations: empty string by default, coverage on request.
        quiet = get_recommendations(self.store, project_id=p.project_id)
        self.assertTrue(quiet["quiet"])
        self.assertEqual(format_user_recommendations(quiet), "")
        quiet_cov = get_recommendations(self.store, project_id=p.project_id, include_coverage=True)
        self.assertIn("Coverage:", format_user_recommendations(quiet_cov))

        # Seed 3 unvalidated proposals to trigger backlog limit on 4th matched candidate.
        for i in range(3):
            url = f"https://github.com/org/tool-{i}"
            art_res = self.store.record_source_artifact(
                type(self.store.list_source_artifacts()[0])(
                    source_id=f"src_{i}",
                    canonical_url=url,
                    raw_url=url,
                    source_class="github_popular",
                    publisher="org",
                    title=f"org/tool-{i}",
                    summary="Evaluation harness helper",
                    canonical_version="",
                    raw_version="",
                    content_digest=f"cd_{i}",
                    evidence_digest=f"ed_{i}",
                    trust_class="public_community",
                    observed_at=self.now,
                )
                if False
                else __import__("learner.sources", fromlist=["_build_artifact"])._build_artifact(
                    raw_url=url,
                    source_class="github_popular",
                    publisher="org",
                    title=f"org/tool-{i}",
                    summary="Evaluation harness helper",
                    raw_version="",
                    trust_class="public_community",
                    observed_at=self.now,
                    raw_bytes=f"tool-{i}".encode("utf-8"),
                )
            )[0]
            asmt, _ = self.store.ensure_assessment_for_artifact(p, art_res, goal_ref="evaluation")
            self.store.transition_assessment(
                asmt.assessment_id,
                lifecycle_state="proposed",
                last_completed_step="proposed",
                decision="proposed",
                reason="benefit",
                model_inferences=[
                    {
                        "step": "verified_proposal",
                        "citations_valid": True,
                        "title": art_res.title,
                        "expected_benefit": f"Benefit {i}",
                        "effort_and_risk": "Low",
                        "uncertainty": "Uncertain",
                        "valid_citations": [{"evidence_id": f"ev_{i}"}],
                        "test_baseline_rollback": {"baseline": "b", "test_plan": "t", "rollback": "r"},
                    }
                ],
            )
        # 4th candidate should be deferred by backlog limit without spending deep model calls.
        run_due(self.store, transport, runner)
        by_url = {a.canonical_url: a for a in self.store.list_assessments(p.project_id)}
        self.assertEqual(by_url["https://github.com/eval-org/harness-bench"].decision, "deferred_proposal_backlog")

        # Record negative outcome -> reduces unvalidated count & preserved in preferences/summary.
        first_prop = [a for a in self.store.list_assessments(p.project_id) if a.lifecycle_state == "proposed"][0]
        self.store.record_outcome_reference(
            project_id=p.project_id,
            assessment_id=first_prop.assessment_id,
            external_experiment_ref="exp-neg-1",
            status="rejected",
            baseline_summary="baseline 17",
            outcome_summary="no improvement",
            evaluator="unittest",
            feedback_note="too noisy",
        )
        self.assertEqual(self.store.count_unvalidated_proposals(p.project_id), 2)

        # Natural-language feedback updates bounded preferences without touching mission or authority.
        pref = apply_feedback_preference(self.store, p.project_id, "focus more on evaluation")
        self.assertTrue(pref["mission_unchanged"])
        self.assertTrue(pref["authority_unchanged"])
        self.assertIn("evaluation", pref["preferences"]["focus_more_on"])
        pref2 = apply_feedback_preference(self.store, p.project_id, "less of css animations")
        self.assertIn("css animations", pref2["preferences"]["less_like"])

        # Top-3 cap + independent user_language vs deliverable_language while internals stay English.
        set_language_preferences(self.store, project_id=p.project_id, user_language="zh", deliverable_language="es")
        langs = get_language_preferences(self.store, project_id=p.project_id)
        self.assertEqual(langs["user_language"], "zh")
        self.assertEqual(langs["deliverable_language"], "es")
        self.assertEqual(langs["internal_language"], "en")

        recs = get_recommendations(self.store, project_id=p.project_id)
        self.assertLessEqual(len(recs["recommendations"]), 3)
        rendered = format_user_recommendations(recs)
        self.assertIn("\u5b66\u4e60\u6539\u8fdb\u5efa\u8bae", rendered)

    def test_agy_cli_subprocess_dispatch_tools_off_unknown_model_and_timeout_cleanup(self) -> None:
        policy_path = self._write_policy()
        pin_h, err_h = resolve_policy_pin("hydrogen", policy_path=policy_path)
        pin_c, err_c = resolve_policy_pin("carbon", policy_path=policy_path)
        self.assertIsNone(err_h)
        self.assertIsNone(err_c)
        assert pin_h and pin_c
        self.assertEqual(pin_h.model, "gemini-4-hydrogen-high")
        self.assertEqual(pin_c.model, "gemini-4-carbon-medium")

        # Disabled model in policy honestly defers with zero subprocess execution.
        disabled_policy = self._write_policy(hydrogen_enabled=False, filename="disabled_policy.json")
        runner_disabled = AgyCliRunner(policy_path=disabled_policy)
        rcpt_dis = runner_disabled.invoke("hydrogen", "hello")
        self.assertEqual(rcpt_dis.status, "unconfigured")
        self.assertFalse(rcpt_dis.dispatched)

        # Fake agy-flow executable writing stream-json (and optionally omitting model or sleeping for timeout).
        fake_cli = self.root / "agy-flow"
        pid_file = self.root / "child.pid"
        fake_cli.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, time\n"
            "args = sys.argv[1:]\n"
            "assert '--mode' in args and args[args.index('--mode') + 1] == 'plan'\n"
            "assert '--disable-slash-commands' in args\n"
            "prompt = args[args.index('-p') + 1]\n"
            "if 'HANG_FOREVER' in prompt:\n"
            f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "    time.sleep(30)\n"
            "if 'OMIT_MODEL' in prompt:\n"
            "    print(json.dumps({'event':'init','init':{}}))\n"
            "else:\n"
            "    print(json.dumps({'event':'init','init':{'model':'gemini-4-hydrogen-high','tools':[]}}))\n"
            "if 'CALL_TOOL' in prompt:\n"
            "    print(json.dumps({'event':'step_update','step_update':{'step_type':'tool_use','tool_calls':[{'name':'run_command'}]}}))\n"
            "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':json.dumps({'queries':['evaluation harness']}),'usage':{'input_tokens':10,'output_tokens':5,'total_tokens':15},'duration_seconds':0.02}}))\n",
            encoding="utf-8",
        )
        os.chmod(fake_cli, os.stat(fake_cli).st_mode | stat.S_IEXEC)

        runner = AgyCliRunner(policy_path=policy_path, cli_path=fake_cli)
        rcpt_ok = runner.invoke("hydrogen", "NORMAL_PROMPT", timeout_s=5.0)
        self.assertEqual(rcpt_ok.status, "ok")
        self.assertEqual(rcpt_ok.requested_model, "gemini-4-hydrogen-high")
        self.assertEqual(rcpt_ok.returned_model, "gemini-4-hydrogen-high")
        self.assertEqual(rcpt_ok.usage["total_tokens"], 15)

        rcpt_unk = runner.invoke("hydrogen", "OMIT_MODEL", timeout_s=5.0)
        self.assertEqual(rcpt_unk.status, "ok")
        self.assertEqual(rcpt_unk.returned_model, "unknown")

        rcpt_tool = runner.invoke("hydrogen", "CALL_TOOL", timeout_s=5.0)
        self.assertEqual(rcpt_tool.status, "invalid_response")
        self.assertIn("unexpected_tool_use", rcpt_tool.raw_error or "")

        rcpt_to = runner.invoke("hydrogen", "HANG_FOREVER", timeout_s=0.3)
        self.assertEqual(rcpt_to.status, "timeout")
        self.assertTrue(rcpt_to.timed_out)
        self.assertTrue(rcpt_to.dispatched)
        # Verify timed-out process was actually terminated.
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_agy_cli_supplies_policy_doc_and_rejects_denied_policy_read_attempts(self) -> None:
        from learner.contracts import sha256_bytes

        policy_dir = self.root / "routing_ctx"
        policy_dir.mkdir()
        policy_path = policy_dir / "policy.json"
        policy_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "subscription_only": True,
                    "automatic_paid_fallback": False,
                    "coding": {"gemini": {"model": "gemini-4-hydrogen-high", "effort": "high"}},
                    "design": {"gemini": {"model": "gemini-4-carbon-medium", "effort": "medium"}},
                }
            ),
            encoding="utf-8",
        )
        policy_doc = policy_dir / "POLICY.md"
        policy_doc.write_text("# Authoritative Routing\nUse Hydrogen high for coding.\n", encoding="utf-8")

        prompt_out = self.root / "captured_prompt.txt"
        fake_cli = self.root / "agy-flow-ctx"
        fake_cli.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "prompt = args[args.index('-p') + 1]\n"
            f"pathlib.Path({str(prompt_out)!r}).write_text(prompt, encoding='utf-8')\n"
            "print(json.dumps({'event':'init','init':{'model':'gemini-4-hydrogen-high','tools':['read_file']}}))\n"
            "if 'ATTEMPT_POLICY_READ' in prompt:\n"
            "    print(json.dumps({'event':'step_update','step_update':{'step_index':2,'state':'ACTIVE','step_type':'tool','tool_name':'read_file'}}))\n"
            "    print(json.dumps({'event':'step_update','step_update':{'step_index':2,'state':'ERROR','step_type':'tool','tool_name':'read_file'}}))\n"
            "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':json.dumps({'queries':['evaluation harness']}),'usage':{'input_tokens':20,'output_tokens':5,'total_tokens':25}}}))\n",
            encoding="utf-8",
        )
        os.chmod(fake_cli, os.stat(fake_cli).st_mode | stat.S_IEXEC)

        runner = AgyCliRunner(policy_path=policy_path, cli_path=fake_cli)
        rcpt_ok = runner.invoke("hydrogen", "CLEAN_CALL", timeout_s=5.0)
        self.assertEqual(rcpt_ok.status, "ok")
        captured = prompt_out.read_text(encoding="utf-8")
        self.assertIn(sha256_bytes(policy_doc.read_bytes()), captured)
        self.assertIn(sha256_bytes(policy_path.read_bytes()), captured)
        self.assertIn("Use Hydrogen high for coding.", captured)
        self.assertIn("No tool access is permitted", captured)
        self.assertIn("satisfy the required policy read", captured)

        rcpt_rej = runner.invoke("hydrogen", "ATTEMPT_POLICY_READ", timeout_s=5.0)
        self.assertEqual(rcpt_rej.status, "invalid_response")
        self.assertIsNone(rcpt_rej.parsed_json)
        self.assertEqual(rcpt_rej.raw_error, "unexpected_tool_use_in_tools_off_call:2")

    def test_frozen_adjudicated_relevance_fixture_reports_actual_misses(self) -> None:
        fixture_path = Path(__file__).resolve().parent / "relevance_fixtures.json"
        data = json.loads(fixture_path.read_text(encoding="utf-8"))
        p_raw = data["profile"]
        profile = make_profile(
            project_id=p_raw["project_id"],
            name=p_raw["name"],
            root_path=p_raw["root_path"],
            mission=p_raw["mission"],
            existing_capabilities=tuple(p_raw["existing_capabilities"]),
            goals=tuple(p_raw["goals"]),
            known_gaps=tuple(p_raw["known_gaps"]),
            exclusions=tuple(p_raw["exclusions"]),
            public_concepts=tuple(p_raw["public_concepts"]),
        )

        # Evaluate deterministic lexical intake cues against frozen manual adjudication and record actual misses.
        _build = __import__("learner.sources", fromlist=["_build_artifact"])._build_artifact
        misses: list[dict[str, str]] = []
        for item in data["items"]:
            art = _build(
                raw_url=item["url"],
                source_class=item["source_class"],
                publisher="fixture",
                title=item["title"],
                summary=item["summary"],
                raw_version="",
                trust_class="public_community",
                observed_at=self.now,
                raw_bytes=item["id"].encode("utf-8"),
                popularity_signal_only=item["popularity_signal_only"],
            )
            fit = summarize_intake_fit(profile, art)["fit"]
            predicted = (
                "qualify"
                if fit == "adjacent_concept_match"
                else ("reject" if fit in ("excluded_by_profile", "existing_capability_overlap", "unmatched_popularity_only") else "defer")
            )
            if predicted != item["expected_decision"]:
                misses.append({"id": item["id"], "expected": item["expected_decision"], "predicted": predicted})

        # Report actual misses honestly (deterministic cue-only misses adjacent_replay_tool without exact lexical overlap).
        self.assertGreaterEqual(len(data["items"]), 10)
        self.assertTrue(any(m["id"] == "adjacent_replay_tool" for m in misses), f"Expected lexical-only miss on adjacent_replay_tool, got {misses}")

    def test_repeated_intake_profile_revision_and_query_planning_share_5_hydrogen_cap(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())
        runner = FixtureModelRunner(_standard_model_handler())

        # Run review with query planning: 1 query_plan + 1 triage + 1 deep H + 1 synth H = 4 H calls (<= 5/day).
        res1 = run_review(self.store, transport, runner, project_id=p.project_id, mode="project-led", plan_queries=True)
        self.assertEqual(res1["status"], "ok")
        self.assertEqual(self.store.budget_used("__shared__", "daily_calls"), 4)
        calls_after_first = len(runner.calls)

        # Repeated identical intake -> zero duplicate deep or triage model calls.
        self.now += 60.0
        res2 = run_review(self.store, transport, runner, project_id=p.project_id, mode="project-led", plan_queries=False)
        self.assertEqual(res2["assessment"]["deep_assessed"], 0)
        self.assertEqual(len(runner.calls), calls_after_first)

        # Revising goals reopens affected assessments with lineage.
        _, reopened = self.store.revise_profile(p.project_id, {"goals": ["new benchmark goal"]}, note="goal shift")
        self.assertGreaterEqual(len(reopened), 1)
        self.assertTrue(all(a.predecessor_id is not None for a in self.store.list_assessments(p.project_id)))

    def test_cli_due_review_results_prefer_language_and_status_with_fixture_model_and_no_subprocess(self) -> None:
        storage = self.root / "cli-il2"
        policy_path = self._write_policy(filename="cli_policy.json")
        model_fixture_path = self.root / "model_fixture.json"
        model_fixture_path.write_text(
            json.dumps(
                {
                    "tasks": {
                        "batch_triage": {
                            "qualify_contains": ["harness-bench"],
                            "goal_ref": "eval",
                            "capability_ref": "replay",
                            "qualify_rationale": "Adjacent evaluation tool.",
                            "uncertainty": "Need README.",
                        },
                        "independent_blind_assessment": {
                            "recommendation": "borrow",
                            "alternatives": ["Keep"],
                            "borrowable_change": "Borrow replay.",
                            "affected_existing_capability": "runner",
                            "expected_benefit": "Clearer replay.",
                            "effort_and_risk": "Low.",
                            "test_baseline_rollback": {
                                "test_plan": "unittest",
                                "baseline": "17 tests",
                                "rollback": "git revert",
                            },
                            "unresolved_claims": [],
                            "uncertainty": "Offline only.",
                            "claim_kind": "model_inference",
                        },
                        "bounded_proposal_synthesis": {
                            "recommendation": "borrow",
                            "alternatives": ["Keep"],
                            "borrowable_change": "Borrow replay.",
                            "affected_existing_capability": "runner",
                            "expected_benefit": "Clearer replay.",
                            "effort_and_risk": "Low.",
                            "test_baseline_rollback": {
                                "test_plan": "unittest",
                                "baseline": "17 tests",
                                "rollback": "git revert",
                            },
                            "dissent": [],
                            "unresolved_claims": [],
                            "uncertainty": "Offline only.",
                            "claim_kind": "model_inference",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

        routes = _primary_routes_for_standard_fixtures()
        fixture_map = {
            url: {
                "status_code": resp.status_code,
                "body": resp.body.decode("utf-8"),
                "headers": resp.headers,
                "final_url": resp.final_url,
            }
            for url, resp in routes.items()
        }
        fixture_path = self.root / "cli_fixtures.json"
        fixture_path.write_text(json.dumps(fixture_map), encoding="utf-8")

        def _deny(*args, **kwargs):
            raise AssertionError(f"Denied subprocess/network call in offline fixture mode: {args}")

        with (
            mock.patch.object(subprocess, "Popen", side_effect=_deny),
            mock.patch.object(socket, "create_connection", side_effect=_deny),
            mock.patch.object(urllib.request, "urlopen", side_effect=_deny),
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(
                cli_main(
                    [
                        "--storage-root",
                        str(storage),
                        "register",
                        "--project-id",
                        "cli-proj",
                        "--name",
                        "CLI Project",
                        "--root-path",
                        str(self.root),
                        "--mission",
                        "Improve automated evaluation harnesses",
                        "--goals",
                        "improve automated evaluation harnesses",
                        "--public-concepts",
                        "evaluation harness",
                    ]
                ),
                0,
            )
            self.assertEqual(
                cli_main(
                    [
                        "--storage-root",
                        str(storage),
                        "--json",
                        "due",
                        "--fixture-map",
                        str(fixture_path),
                        "--model-fixture",
                        str(model_fixture_path),
                        "--policy-path",
                        str(policy_path),
                    ]
                ),
                0,
            )
            self.assertEqual(
                cli_main(["--storage-root", str(storage), "prefer", "--project-id", "cli-proj", "--note", "focus more on evaluation"]),
                0,
            )
            self.assertEqual(
                cli_main(["--storage-root", str(storage), "language", "--project-id", "cli-proj", "--user-language", "zh", "--deliverable-language", "en"]),
                0,
            )

        res_buf = io.StringIO()
        with redirect_stdout(res_buf):
            self.assertEqual(cli_main(["--storage-root", str(storage), "--json", "results", "--project-id", "cli-proj"]), 0)
        res_payload = json.loads(res_buf.getvalue())
        self.assertEqual(res_payload["user_language"], "zh")
        self.assertEqual(res_payload["deliverable_language"], "en")
        self.assertEqual(len(res_payload["recommendations"]), 1)

        st_buf = io.StringIO()
        with redirect_stdout(st_buf):
            self.assertEqual(cli_main(["--storage-root", str(storage), "--json", "status", "--project-id", "cli-proj"]), 0)
        st_payload = json.loads(st_buf.getvalue())
        self.assertEqual(st_payload["assessment_status"], "assessed")
        self.assertEqual(st_payload["shared_budget_used"]["daily_calls"], 3)
        self.assertEqual(st_payload["shared_budget_used"]["weekly_deep_calls"], 1)
        self.assertEqual(st_payload["trial_grant"]["state"], "inactive")

    def test_cli_fixture_mode_denies_subprocess_and_defers_honestly_without_model_fixture(self) -> None:
        storage = self.root / "cli-no-infer"
        routes = _primary_routes_for_standard_fixtures()
        fixture_map = {
            url: {
                "status_code": resp.status_code,
                "body": resp.body.decode("utf-8"),
                "headers": resp.headers,
                "final_url": resp.final_url,
            }
            for url, resp in routes.items()
        }
        fixture_path = self.root / "no_infer_fixtures.json"
        fixture_path.write_text(json.dumps(fixture_map), encoding="utf-8")

        def _deny(*args, **kwargs):
            raise AssertionError("Subprocess/network must never run in --fixture-map mode")

        with (
            mock.patch.object(subprocess, "Popen", side_effect=_deny),
            mock.patch.object(socket, "create_connection", side_effect=_deny),
            mock.patch.object(urllib.request, "urlopen", side_effect=_deny),
        ):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli_main(
                        [
                            "--storage-root",
                            str(storage),
                            "register",
                            "--project-id",
                            "fixture-proj-beta",
                            "--name",
                            "Fixture Beta Workspace",
                            "--root-path",
                            str(self.root),
                            "--mission",
                            "Improve automated evaluation harnesses",
                            "--goals",
                            "improve automated evaluation harnesses",
                            "--public-concepts",
                            "evaluation harness",
                        ]
                    ),
                    0,
                )

            due_buf = io.StringIO()
            with redirect_stdout(due_buf):
                rc_due = cli_main(["--storage-root", str(storage), "--json", "due", "--fixture-map", str(fixture_path)])
            self.assertEqual(rc_due, 0)
            due_payload = json.loads(due_buf.getvalue())
            self.assertEqual(due_payload["status"], "unconfigured")
            self.assertEqual(due_payload["assessment"]["reason"], "offline_fixture_no_model_data")

            rev_buf = io.StringIO()
            with redirect_stdout(rev_buf):
                rc_rev = cli_main(
                    ["--storage-root", str(storage), "--json", "review", "--project-id", "fixture-proj-beta", "--fixture-map", str(fixture_path)]
                )
            self.assertEqual(rc_rev, 0)
            rev_payload = json.loads(rev_buf.getvalue())
            self.assertEqual(rev_payload["status"], "unconfigured")

            # Passing --cli-path alongside --fixture-map is refused.
            with mock.patch("sys.stderr", io.StringIO()):
                rc_refused = cli_main(
                    [
                        "--storage-root",
                        str(storage),
                        "due",
                        "--fixture-map",
                        str(fixture_path),
                        "--cli-path",
                        "/bin/true",
                    ]
                )
            self.assertEqual(rc_refused, 1)

    def test_default_release_discovery_without_repo_flag_and_honest_missing_coverage(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())
        runner = FixtureModelRunner(_standard_model_handler())

        res = run_due(self.store, transport, runner)
        self.assertEqual(res["status"], "ok")
        rel_covs = [c for c in res["coverage"] if c["source_class"] == "github_releases"]
        self.assertTrue(rel_covs, "Default discovery must cover github_releases without daily --repo list")
        self.assertEqual(rel_covs[0]["query_key"], "github_releases:eval-org/harness-bench")
        self.assertEqual(rel_covs[0]["status"], "ok")

        # Separate store with zero GitHub repos returned -> retains missing github_releases coverage honestly.
        empty_store = LearnerStore(self.root / "empty-rel", clock=lambda: self.now)
        try:
            empty_store.register_profile(p)
            empty_transport = FixtureTransport(
                {
                    "https://github.com/trending*": TransportResponse(503, b"down", {}, "https://github.com/trending"),
                    "https://api.github.com/search/repositories*": TransportResponse(
                        200, json.dumps({"items": []}).encode("utf-8"), {}, "https://api.github.com/search/repositories"
                    ),
                    "https://export.arxiv.org/api/query*": TransportResponse(
                        200, b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>', {}, "https://export.arxiv.org/api/query"
                    ),
                }
            )
            empty_res = run_review(empty_store, empty_transport, runner, project_id=p.project_id, mode="project-led")
            self.assertEqual(empty_res["status"], "degraded")
            missing_cov = [c for c in empty_res["coverage"] if c["source_class"] == "github_releases"]
            self.assertTrue(missing_cov)
            self.assertEqual(missing_cov[0]["status"], "missing")
            self.assertTrue(missing_cov[0]["incomplete"])
            self.assertEqual(missing_cov[0]["error"], "no_repository_candidates_derived")
        finally:
            empty_store.close()

    def test_due_metadata_slot_fair_split_prevents_first_mode_starving_second_mode(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        # Seed opportunity-led with 60 trending items and project-led with its own search item.
        trending_rows = "".join(
            f"<article class='Box-row'><h2><a href='/trend-org/repo-{i}'>trend-org / repo-{i}</a></h2>"
            f"<p>Trending item {i}</p><span>10 stars today</span></article>"
            for i in range(60)
        )
        proj_search = json.dumps(
            {
                "items": [
                    {
                        "full_name": "eval-org/harness-bench",
                        "html_url": "https://github.com/eval-org/harness-bench",
                        "description": "Evaluation harness for project-led coverage",
                        "stargazers_count": 300,
                    }
                ]
            }
        ).encode("utf-8")
        opp_search = json.dumps({"items": []}).encode("utf-8")

        def _search_handler(url: str) -> TransportResponse:
            body = opp_search if "stars%3A%3E200" in url else proj_search
            return TransportResponse(200, body, {"content-type": "application/json"}, url)

        transport = FixtureTransport(
            {
                "https://github.com/trending*": TransportResponse(
                    200, f"<html><body>{trending_rows}</body></html>".encode("utf-8"), {}, "https://github.com/trending"
                ),
                "https://api.github.com/search/repositories*": _search_handler,
                "https://export.arxiv.org/api/query*": TransportResponse(
                    200, b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>', {}, "https://export.arxiv.org/api/query"
                ),
                "https://api.github.com/repos/eval-org/harness-bench/releases*": TransportResponse(
                    200, b"[]", {"content-type": "application/json"}, "https://api.github.com/repos/eval-org/harness-bench/releases"
                ),
            }
        )
        runner = FixtureModelRunner(available=False, unavailable_reason="skip_model_for_intake_fairness")
        # Force opportunity-led to run first while project-led is also due.
        self.store.set_setting("schedule:last_run", {"opportunity-led": 0.0, "project-led": {"alpha-proj": 100.0}})
        res = run_due(self.store, transport, runner)
        self.assertEqual(res["modes_run"], ["opportunity-led", "project-led"])
        modes_with_cov = {c["mode"] for c in res["coverage"] if c["status"] == "ok"}
        self.assertIn("opportunity-led", modes_with_cov)
        self.assertIn("project-led", modes_with_cov)
        self.assertLessEqual(self.store.budget_used("__shared__", "daily_metadata_items"), 60)

    def test_finite_trial_ceiling_expiry_restart_persistence_and_language_precedence(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        # Default trial grant is inactive before explicit host grant.
        self.assertEqual(self.store.get_trial_status()["state"], "inactive")

        # Grant finite trial of 2 cumulative model calls across due/review/restarts.
        grant = self.store.set_trial_grant(max_model_calls=2, expires_at=self.now + 3600.0, grant_id="pilot-1")
        self.assertEqual(grant["state"], "active")
        self.assertEqual(grant["remaining"], 2)

        # Implicit reset or modification of existing grant_id is refused.
        with self.assertRaises(ValidationError):
            self.store.set_trial_grant(max_model_calls=10, expires_at=self.now + 7200.0, grant_id="pilot-1")

        transport = FixtureTransport(_primary_routes_for_standard_fixtures())
        runner = FixtureModelRunner(_standard_model_handler())
        # 1st triage + 1st deep Hydrogen = 2 model calls (3rd model call: synthesis or 2nd deep) blocked!
        res1 = run_due(self.store, transport, runner)
        self.assertEqual(res1["status"], "degraded")
        self.assertIn("trial_ceiling_exhausted", res1["assessment"]["errors"])
        self.assertEqual(len(runner.calls), 2)
        self.store.close()

        # Restart store -> ceiling exhaustion persists across restarts & review mode without reset.
        reopened = LearnerStore(self.root / "store", clock=lambda: self.now)
        try:
            st = reopened.get_trial_status()
            self.assertEqual(st["state"], "exhausted")
            self.assertEqual(st["used"], 2)
            self.assertEqual(st["remaining"], 0)
            calls_before = len(runner.calls)
            res2 = run_review(reopened, transport, runner, project_id=p.project_id, mode="project-led")
            self.assertEqual(res2["status"], "degraded")
            self.assertEqual(len(runner.calls), calls_before)

            # Separate explicit scope grant_id can grant new finite budget, and expiry blocks before dispatch.
            reopened.set_trial_grant(max_model_calls=5, expires_at=self.now + 60.0, grant_id="pilot-2")
            self.now += 120.0
            with self.assertRaises(DispatchBlockedError) as exp_ctx:
                reopened.reserve_dispatch(
                    idempotency_key="after-expiry-call",
                    project_id="__shared__",
                    scope_type="shared_triage",
                    budget_bucket="daily_calls",
                )
            self.assertEqual(exp_ctx.exception.reason, "trial_expired")

            # Language precedence: explicit user > project pref > user default > input fallback > en.
            set_language_preferences(reopened, user_language="es", deliverable_language="en")
            self.assertEqual(
                get_language_preferences(reopened, project_id=p.project_id, input_language="zh")["user_language"],
                "es",
            )
            set_language_preferences(reopened, project_id=p.project_id, user_language="zh", deliverable_language="es")
            self.assertEqual(
                get_language_preferences(reopened, project_id=p.project_id, user_language="en")["user_language"],
                "en",
            )
            self.assertEqual(
                get_language_preferences(reopened, project_id=p.project_id)["user_language"],
                "zh",
            )
            self.assertEqual(
                get_language_preferences(reopened, project_id="unconfigured-proj", input_language="zh")["user_language"],
                "es",  # global default beats input fallback
            )
            reopened.set_setting("global:languages", {})
            self.assertEqual(
                get_language_preferences(reopened, project_id="unconfigured-proj", input_language="zh")["user_language"],
                "zh",  # input fallback beats default en when no project/global preference set
            )
        finally:
            reopened.close()

    def test_triage_and_query_plan_unknown_block_across_batch_change_day_rollover_and_restarts(self) -> None:
        p = make_profile(project_id="flow", name="flow")
        self.store.register_profile(p)
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())

        # 1. Timeout on query_planning marks unknown and blocks next-day due/review --plan-queries.
        def _qp_timeout(role: str, prompt: str) -> dict[str, object]:
            req = json.loads(prompt)
            if req["task"] == "query_planning":
                raise TimeoutError("query planning timed out")
            return _standard_model_handler()(role, prompt)

        res1 = run_review(
            self.store,
            transport,
            FixtureModelRunner(_qp_timeout),
            project_id="flow",
            mode="project-led",
            plan_queries=True,
        )
        self.assertEqual(res1["status"], "degraded")
        self.assertIn("plan_errors", res1)
        qp_unknowns = [d for d in self.store.list_dispatches(project_id="flow", state="unknown", include_shared=True) if d.scope_type == "query_planning"]
        self.assertEqual(len(qp_unknowns), 1)

        # Next day + restart -> query_planning MUST still block without dispatching.
        self.now += 86_400.0
        self.store.close()
        reopened = LearnerStore(self.root / "store", clock=lambda: self.now)
        try:
            probe_runner = FixtureModelRunner(_standard_model_handler())
            res2 = run_review(reopened, transport, probe_runner, project_id="flow", mode="project-led", plan_queries=True)
            self.assertIn("unknown_pending_reconciliation", res2.get("plan_errors", []))
            self.assertFalse(any("query_planning" in c.prompt for c in probe_runner.calls))
            reopened.reconcile_dispatch(qp_unknowns[0].reservation_id, outcome="failed", actual_units=0, note="confirmed_not_billed")

            # 2. Timeout on shared_triage blocks across changed batch composition AND day rollover.
            triage_art = __import__("learner.sources", fromlist=["_build_artifact"])._build_artifact(
                raw_url="https://github.com/eval-org/triage-item-1",
                source_class="github_popular",
                publisher="eval-org",
                title="eval-org/triage-item-1",
                summary="Evaluation harness triage item 1",
                raw_version="",
                trust_class="public_community",
                observed_at=self.now,
                raw_bytes=b"triage-item-1",
            )
            saved_t1, _ = reopened.record_source_artifact(triage_art)
            reopened.ensure_assessment_for_artifact(p, saved_t1)

            def _triage_timeout(role: str, prompt: str) -> dict[str, object]:
                req = json.loads(prompt)
                if req["task"] == "batch_triage":
                    raise TimeoutError("triage timed out")
                return _standard_model_handler()(role, prompt)

            res_t1 = assess_pending_work(reopened, transport, FixtureModelRunner(_triage_timeout), project_ids=("flow",))
            self.assertEqual(res_t1["triage_status"], "unknown")

            # Add a new artifact (changing batch composition) AND advance UTC day.
            extra_art = __import__("learner.sources", fromlist=["_build_artifact"])._build_artifact(
                raw_url="https://github.com/eval-org/extra-tool",
                source_class="github_popular",
                publisher="eval-org",
                title="eval-org/extra-tool",
                summary="Extra evaluation harness tool",
                raw_version="",
                trust_class="public_community",
                observed_at=self.now,
                raw_bytes=b"extra-tool",
            )
            saved_extra, _ = reopened.record_source_artifact(extra_art)
            reopened.ensure_assessment_for_artifact(p, saved_extra)
            self.now += 86_400.0

            probe_runner2 = FixtureModelRunner(_standard_model_handler())
            res_t2 = assess_pending_work(reopened, transport, probe_runner2, project_ids=("flow",))
            self.assertEqual(res_t2["triage_status"], "blocked")
            self.assertEqual(res_t2["triage_error"], "unknown_pending_reconciliation")
            self.assertEqual(probe_runner2.calls, [])

            # Reconcile unknown shared_triage -> next assessment succeeds!
            t_unknowns = [d for d in reopened.list_dispatches(project_id="flow", state="unknown", include_shared=True) if d.scope_type == "shared_triage"]
            self.assertEqual(len(t_unknowns), 1)
            reopened.reconcile_dispatch(t_unknowns[0].reservation_id, outcome="failed", actual_units=0, note="triage_failed")
            res_t3 = assess_pending_work(reopened, transport, probe_runner2, project_ids=("flow",))
            self.assertEqual(res_t3["triage_status"], "ok")
            self.assertGreaterEqual(len(probe_runner2.calls), 1)
        finally:
            reopened.close()

    def test_cli_live_requires_active_trial_grant_and_revoke_cannot_lift_exhaustion_or_expiry(self) -> None:
        storage = self.root / "cli-trial-gate"
        policy_path = self._write_policy(filename="live_trial_policy.json")
        marker_file = self.root / "cli_invoked.txt"
        fake_cli = self.root / "fake-agy-flow"
        fake_cli.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            f"pathlib.Path({str(marker_file)!r}).write_text('CALLED', encoding='utf-8')\n"
            "print(json.dumps({'event':'init','init':{'model':'gemini-4-hydrogen-high','tools':[]}}))\n"
            "print(json.dumps({'event':'result','result':{'status':'SUCCESS','response':json.dumps({'decisions':[]}),'usage':{'input_tokens':5,'output_tokens':5,'total_tokens':10}}}))\n",
            encoding="utf-8",
        )
        os.chmod(fake_cli, os.stat(fake_cli).st_mode | stat.S_IEXEC)

        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli_main(
                    [
                        "--storage-root",
                        str(storage),
                        "register",
                        "--project-id",
                        "flow",
                        "--name",
                        "flow",
                        "--root-path",
                        str(self.root),
                        "--mission",
                        "Maintain deterministic task gating",
                        "--goals",
                        "improve evaluation harness",
                        "--public-concepts",
                        "evaluation harness",
                    ]
                ),
                0,
            )

        # Seed one pending assessment offline without calling live network/CLI.
        s = LearnerStore(storage, clock=lambda: self.now)
        try:
            art = __import__("learner.sources", fromlist=["_build_artifact"])._build_artifact(
                raw_url="https://github.com/eval-org/harness-bench",
                source_class="github_popular",
                publisher="eval-org",
                title="eval-org/harness-bench",
                summary="Evaluation harness",
                raw_version="",
                trust_class="public_community",
                observed_at=self.now,
                raw_bytes=b"hb",
            )
            saved_art, _ = s.record_source_artifact(art)
            prof = s.get_profile("flow")
            assert prof is not None
            s.ensure_assessment_for_artifact(prof, saved_art, goal_ref="improve evaluation harness")
        finally:
            s.close()

        # 1. Without any trial grant (default inactive), `--live` MUST NOT invoke fake_cli!
        from learner.sources import HttpTransport

        buf = io.StringIO()
        with (
            mock.patch.object(socket, "create_connection", side_effect=AssertionError("no live network")),
            mock.patch.object(
                HttpTransport,
                "fetch",
                return_value=TransportResponse(
                    status_code=200,
                    body=json.dumps({"items": []}).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    final_url="https://api.github.com/search/repositories",
                ),
            ),
            redirect_stdout(buf),
        ):
            rc = cli_main(
                [
                    "--storage-root",
                    str(storage),
                    "--json",
                    "review",
                    "--project-id",
                    "flow",
                    "--mode",
                    "project-led",
                    "--live",
                    "--policy-path",
                    str(policy_path),
                    "--cli-path",
                    str(fake_cli),
                ]
            )
        self.assertEqual(rc, 0)
        rev_live_payload = json.loads(buf.getvalue())
        self.assertEqual(rev_live_payload["assessment"]["status"], "blocked")
        self.assertEqual(rev_live_payload["assessment"]["reason"], "trial_inactive")
        self.assertFalse(marker_file.exists(), "Live CLI must not be invoked when trial grant is inactive")

        # Grant a finite 1-call trial, run 1 call, then revoke -> revoke must NOT lift exhaustion or allow dispatch!
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli_main(
                    [
                        "--storage-root",
                        str(storage),
                        "trial",
                        "--grant",
                        "--max-calls",
                        "1",
                        "--expires-at",
                        "2099-01-01T00:00:00Z",
                        "--grant-id",
                        "cli-pilot-1",
                    ]
                ),
                0,
            )

        s2 = LearnerStore(storage, clock=lambda: self.now)
        try:
            r, _ = s2.reserve_dispatch(
                idempotency_key="consume-1",
                project_id="__shared__",
                scope_type="shared_triage",
                budget_bucket="daily_calls",
                units=1,
                require_trial_grant=True,
            )
            s2.mark_dispatched(r.reservation_id)
            s2.settle_dispatch(r.reservation_id, actual_units=1, receipt={"status": "ok"})
        finally:
            s2.close()

        rev_buf = io.StringIO()
        with redirect_stdout(rev_buf):
            self.assertEqual(cli_main(["--storage-root", str(storage), "--json", "trial", "--revoke"]), 0)
        rev_status = json.loads(rev_buf.getvalue())
        self.assertEqual(rev_status["state"], "exhausted")
        self.assertEqual(rev_status["used"], 1)
        self.assertFalse(rev_status["enabled"])

        # Both live CLI and store reservation remain blocked after --revoke!
        s3 = LearnerStore(storage, clock=lambda: self.now)
        try:
            with self.assertRaises(DispatchBlockedError) as blk:
                s3.reserve_dispatch(
                    idempotency_key="after-revoke-exhausted",
                    project_id="__shared__",
                    scope_type="shared_triage",
                    budget_bucket="daily_calls",
                    units=1,
                )
            self.assertEqual(blk.exception.reason, "trial_ceiling_exhausted")

            # New non-exhausted grant that is then revoked -> blocked with trial_revoked!
            s3.set_trial_grant(max_model_calls=5, expires_at=self.now + 3600.0, grant_id="cli-pilot-2")
            s3.revoke_trial_grant()
            with self.assertRaises(DispatchBlockedError) as rev_blk:
                s3.reserve_dispatch(
                    idempotency_key="after-revoke-active",
                    project_id="__shared__",
                    scope_type="shared_triage",
                    budget_bucket="daily_calls",
                    units=1,
                )
            self.assertEqual(rev_blk.exception.reason, "trial_revoked")

            # Expired grant that is revoked -> still blocked with trial_expired!
            s3.set_trial_grant(max_model_calls=5, expires_at=self.now + 10.0, grant_id="cli-pilot-3")
            self.now += 20.0
            s3.revoke_trial_grant()
            with self.assertRaises(DispatchBlockedError) as exp_blk:
                s3.reserve_dispatch(
                    idempotency_key="after-revoke-expired",
                    project_id="__shared__",
                    scope_type="shared_triage",
                    budget_bucket="daily_calls",
                    units=1,
                )
            self.assertEqual(exp_blk.exception.reason, "trial_expired")
            # Cumulative usage and receipts preserved.
            self.assertEqual(s3.get_trial_status()["used"], 0)
            self.assertEqual(len([d for d in s3.list_dispatches() if d.scope_type != "shared_fetch"]), 1)
        finally:
            s3.close()
        self.assertFalse(marker_file.exists())

    def test_il7_explicit_unlimited_resumes_deferred_synthesis_without_repeating_deep_calls(self) -> None:
        p = make_profile(execution_paused=True)
        self.store.register_profile(p)
        storage_path = self.root / "il7-resume-store"
        store = LearnerStore(storage_path, clock=lambda: self.now)
        try:
            store.register_profile(p)
            # Finite 3-call grant: 1 triage + 1 deep Hydrogen + 1 deep Carbon -> synthesis blocked!
            store.set_trial_grant(max_model_calls=3, expires_at=self.now + 3600.0, grant_id="pilot-finite-3")
            transport = FixtureTransport(_primary_routes_for_standard_fixtures())
            runner1 = FixtureModelRunner(_standard_model_handler(), requires_trial_grant=True)
            res1 = run_due(store, transport, runner1)
            self.assertEqual(res1["status"], "degraded")
            self.assertIn("trial_ceiling_exhausted", res1["assessment"]["errors"])
            self.assertEqual(len(runner1.calls), 3)

            by_url = {a.canonical_url: a for a in store.list_assessments(p.project_id)}
            bench = by_url["https://github.com/eval-org/harness-bench"]
            self.assertEqual(bench.lifecycle_state, "matched")
            self.assertEqual(bench.last_completed_step, "carbon_assessed")
            self.assertEqual(bench.decision, "deferred_budget_exhausted")
            reservations_before = len(store.list_dispatches())
        finally:
            store.close()

        # CLI validation: --unlimited requires --authority, forbids finite flags / --revoke / duplicate --grant-id.
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(cli_main(["--storage-root", str(storage_path), "trial", "--unlimited", "--grant-id", "u1"]), 1)
            self.assertEqual(
                cli_main(["--storage-root", str(storage_path), "trial", "--unlimited", "--authority", "owner", "--max-calls", "99"]),
                1,
            )
            self.assertEqual(
                cli_main(["--storage-root", str(storage_path), "trial", "--unlimited", "--revoke", "--authority", "owner"]),
                1,
            )
            self.assertEqual(
                cli_main(
                    [
                        "--storage-root",
                        str(storage_path),
                        "trial",
                        "--unlimited",
                        "--grant-id",
                        "pilot-finite-3",
                        "--authority",
                        "Owner: Cancel the limit and continue",
                    ]
                ),
                1,
            )

        # Explicit CLI opt-in to uncapped mode preserves prior grant history, reservations, and cumulative usage.
        trial_buf = io.StringIO()
        with redirect_stdout(trial_buf):
            rc = cli_main(
                [
                    "--storage-root",
                    str(storage_path),
                    "--json",
                    "trial",
                    "--unlimited",
                    "--grant-id",
                    "il7-unlimited-1",
                    "--authority",
                    "Owner: Cancel the limit and continue",
                ]
            )
        self.assertEqual(rc, 0)
        t_payload = json.loads(trial_buf.getvalue())
        self.assertEqual(t_payload["state"], "active")
        self.assertEqual(t_payload["mode"], "unlimited")
        self.assertTrue(t_payload["unlimited"])
        self.assertTrue(t_payload["active"])
        self.assertIsNone(t_payload["max_model_calls"])
        self.assertIsNone(t_payload["remaining"])
        self.assertIsNone(t_payload["expires_at"])
        self.assertEqual(t_payload["authority"], "Owner: Cancel the limit and continue")
        self.assertEqual(t_payload["used"], 0)
        self.assertEqual(t_payload["cumulative_model_calls"], 3)
        self.assertEqual(len(t_payload["grant_history"]), 1)
        self.assertEqual(t_payload["grant_history"][0]["grant_id"], "pilot-finite-3")
        self.assertEqual(t_payload["grant_history"][0]["max_model_calls"], 3)

        # Status CLI truthfully reports null/unlimited model quotas while preserving metadata/fetch bounds.
        st_buf = io.StringIO()
        with redirect_stdout(st_buf):
            self.assertEqual(cli_main(["--storage-root", str(storage_path), "--json", "status"]), 0)
        st_payload = json.loads(st_buf.getvalue())
        self.assertIsNone(st_payload["shared_budget_status"]["daily_calls"]["limit"])
        self.assertIsNone(st_payload["shared_budget_status"]["weekly_deep_calls"]["limit"])
        self.assertIsNone(st_payload["shared_budget_status"]["weekly_carbon_calls"]["limit"])
        self.assertTrue(st_payload["shared_budget_status"]["daily_calls"]["unlimited"])
        self.assertEqual(st_payload["shared_budget_status"]["daily_metadata_items"]["limit"], 60)
        self.assertEqual(st_payload["shared_budget_status"]["daily_fetches"]["limit"], 10)

        # Resume on the SAME day (feeds not due, Carbon unavailable): finishes synthesis via Hydrogen ONLY without repeating primary/H/C!
        reopened = LearnerStore(storage_path, clock=lambda: self.now)
        try:
            self.assertEqual(len(reopened.list_dispatches()), reservations_before)
            self.assertIsNone(reopened.remaining_shared_budget("daily_calls"))
            # 10 daily_fetches - (2 shared_fetch + 1 post-intake release_fetch + 1 primary_fetch) = 6 (BUG-0b04dbda5e84).
            self.assertEqual(reopened.remaining_shared_budget("daily_fetches"), 6)

            def _synth_only_handler(role: str, prompt: str) -> dict[str, object]:
                req = json.loads(prompt)
                if req["task"] != "bounded_proposal_synthesis":
                    raise AssertionError(f"Unexpected non-synthesis model call: {req['task']}")
                return _standard_model_handler()(role, prompt)

            runner2 = FixtureModelRunner(_synth_only_handler, requires_trial_grant=True)
            # Even if Carbon isn't needed for already carbon_assessed synthesis, check_available("carbon") returning False does not block synthesis.
            orig_check = runner2.check_available
            runner2.check_available = lambda role: (False, "carbon_temporarily_absent", None) if role == "carbon" else orig_check(role)  # type: ignore[method-assign]
            transport2 = FixtureTransport({})  # Zero HTTP fetches allowed!
            res2 = run_due(reopened, transport2, runner2)
            self.assertEqual(res2["status"], "ok")
            self.assertEqual(res2["reason"], "resumed_pending_work")
            self.assertEqual(res2["assessment"]["proposed"], 1)
            self.assertEqual(len(runner2.calls), 1)
            self.assertEqual(runner2.calls[0].role, "hydrogen")
            self.assertIn("bounded_proposal_synthesis", runner2.calls[0].prompt)
            self.assertEqual(transport2.calls, [])

            st_after = reopened.get_trial_status()
            self.assertEqual(st_after["used"], 1)
            self.assertEqual(st_after["cumulative_model_calls"], 4)
            prof_after = reopened.get_profile(p.project_id)
            assert prof_after is not None
            self.assertTrue(prof_after.execution_paused)
        finally:
            reopened.close()

    def test_il7_unlimited_lifts_shared_and_project_model_quotas_while_preserving_all_safeguards(self) -> None:
        p = make_profile(budget=BudgetPolicy(daily_calls=1, weekly_deep_calls=1, daily_fetches=2))
        self.store.register_profile(p)

        # Default finite mode unaffected before opt-in: daily_calls=1 cap on project, 5 on __shared__.
        r1, _ = self.store.reserve_dispatch(
            idempotency_key="proj-finite-1",
            project_id=p.project_id,
            scope_type="project_call",
            budget_bucket="daily_calls",
        )
        self.store.settle_dispatch(r1.reservation_id, actual_units=1)
        with self.assertRaises(DispatchBlockedError) as finite_blk:
            self.store.reserve_dispatch(
                idempotency_key="proj-finite-2",
                project_id=p.project_id,
                scope_type="project_call",
                budget_bucket="daily_calls",
            )
        self.assertEqual(finite_blk.exception.reason, "budget_exhausted")

        # Enable uncapped mode explicitly.
        self.store.set_trial_grant(
            unlimited=True,
            grant_id="il7-unbounded-test",
            authority="Owner: Cancel the limit and continue",
        )

        # Both shared and project reservations honor uncapped setting beyond 5 daily Hydrogen / 2 weekly deep / 2 weekly Carbon.
        for idx in range(7):
            r_sh, _ = self.store.reserve_dispatch(
                idempotency_key=f"shared-h-{idx}",
                project_id="__shared__",
                scope_type="deep_hydrogen",
                budget_bucket="daily_calls",
            )
            self.store.settle_dispatch(r_sh.reservation_id, actual_units=1)
        for idx in range(3):
            r_dc, _ = self.store.reserve_dispatch(
                idempotency_key=f"shared-deep-{idx}",
                project_id="__shared__",
                scope_type="deep_candidate",
                budget_bucket="weekly_deep_calls",
            )
            self.store.settle_dispatch(r_dc.reservation_id, actual_units=1)
            r_cb, _ = self.store.reserve_dispatch(
                idempotency_key=f"shared-carbon-{idx}",
                project_id="__shared__",
                scope_type="deep_carbon",
                budget_bucket="weekly_carbon_calls",
            )
            self.store.settle_dispatch(r_cb.reservation_id, actual_units=1)
        for idx in range(3):
            r_pr, _ = self.store.reserve_dispatch(
                idempotency_key=f"proj-model-{idx}",
                project_id=p.project_id,
                scope_type="model_call",
                budget_bucket="daily_calls",
            )
            self.store.settle_dispatch(r_pr.reservation_id, actual_units=1)

        # Preserved safeguards:
        # 1. daily_fetches (2) and daily_metadata_items (60) remain enforced!
        for idx in range(2):
            rf, _ = self.store.reserve_dispatch(
                idempotency_key=f"fetch-{idx}",
                project_id="__shared__",
                scope_type="shared_fetch",
                budget_bucket="daily_fetches",
            )
            self.store.settle_dispatch(rf.reservation_id, actual_units=1)
        with self.assertRaises(DispatchBlockedError) as fetch_blk:
            self.store.reserve_dispatch(
                idempotency_key="fetch-3",
                project_id="__shared__",
                scope_type="shared_fetch",
                budget_bucket="daily_fetches",
            )
        self.assertEqual(fetch_blk.exception.reason, "budget_exhausted")

        rm, _ = self.store.reserve_dispatch(
            idempotency_key="meta-60",
            project_id="__shared__",
            scope_type="metadata_items",
            budget_bucket="daily_metadata_items",
            units=60,
        )
        self.store.settle_dispatch(rm.reservation_id, actual_units=60)
        with self.assertRaises(DispatchBlockedError) as meta_blk:
            self.store.reserve_dispatch(
                idempotency_key="meta-61",
                project_id="__shared__",
                scope_type="metadata_items",
                budget_bucket="daily_metadata_items",
                units=1,
            )
        self.assertEqual(meta_blk.exception.reason, "budget_exhausted")

        # 2. Unknown dispatch is still blocked from blind retry!
        r_unk, _ = self.store.reserve_dispatch(
            idempotency_key="unk-key-1",
            project_id="__shared__",
            scope_type="deep_hydrogen",
            budget_bucket="daily_calls",
        )
        self.store.mark_dispatched(r_unk.reservation_id)
        self.store.mark_unknown(r_unk.reservation_id, error="timeout")
        with self.assertRaises(DispatchBlockedError) as unk_blk:
            self.store.reserve_dispatch(
                idempotency_key="unk-key-1",
                project_id="__shared__",
                scope_type="deep_hydrogen",
                budget_bucket="daily_calls",
            )
        self.assertEqual(unk_blk.exception.reason, "unknown_pending_reconciliation")
        self.store.reconcile_dispatch(r_unk.reservation_id, outcome="failed", actual_units=0, note="clean")

        # 3. Paused or unregistered project still fails closed!
        with self.assertRaises(DispatchBlockedError) as unreg_blk:
            self.store.reserve_dispatch(
                idempotency_key="unreg-1",
                project_id="missing-proj",
                scope_type="model_call",
                budget_bucket="daily_calls",
            )
        self.assertEqual(unreg_blk.exception.reason, "not_registered")

        self.store.set_learning_enabled(p.project_id, False)
        with self.assertRaises(DispatchBlockedError) as pause_blk:
            self.store.reserve_dispatch(
                idempotency_key="paused-proj-1",
                project_id=p.project_id,
                scope_type="model_call",
                budget_bucket="daily_calls",
            )
        self.assertEqual(pause_blk.exception.reason, "learning_disabled")
        with self.assertRaises(DispatchBlockedError) as no_active_blk:
            self.store.reserve_dispatch(
                idempotency_key="no-active-1",
                project_id="__shared__",
                scope_type="shared_triage",
                budget_bucket="daily_calls",
            )
        self.assertEqual(no_active_blk.exception.reason, "no_active_profiles")
        self.store.set_learning_enabled(p.project_id, True)

        # 4. Wall timeout and overlap lease remain enforced!
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())
        runner = FixtureModelRunner(_standard_model_handler(), requires_trial_grant=True)
        ticks = iter([0.0, 1201.0, 1202.0])
        wall_res = run_review(self.store, transport, runner, project_id=p.project_id, clock_monotonic=lambda: next(ticks, 1205.0))
        self.assertEqual(wall_res["status"], "wall_limit_reached")
        self.store.acquire_lease("schedule:due", "other-owner", ttl_s=60.0)
        with self.assertRaises(LeaseConflictError):
            run_review(self.store, transport, runner, project_id=p.project_id)
        self.store.release_lease("schedule:due", "other-owner")

        # 5. Revoking unlimited grant immediately blocks dispatch and restores finite caps without clearing usage!
        rev_status = self.store.revoke_trial_grant()
        self.assertEqual(rev_status["state"], "revoked")
        self.assertEqual(rev_status["mode"], "unlimited")
        self.assertFalse(rev_status["unlimited"])
        self.assertFalse(rev_status["active"])
        self.assertEqual(rev_status["cumulative_model_calls"], 13)
        self.assertEqual(self.store.get_shared_budget_status()["daily_calls"]["limit"], 5)

        with self.assertRaises(DispatchBlockedError) as rev_blk:
            self.store.reserve_dispatch(
                idempotency_key="after-unlimited-revoke",
                project_id="__shared__",
                scope_type="shared_triage",
                budget_bucket="daily_calls",
                require_trial_grant=True,
            )
        self.assertEqual(rev_blk.exception.reason, "trial_revoked")

        rev_run = run_review(self.store, transport, runner, project_id=p.project_id)
        self.assertEqual(rev_run["assessment"]["status"], "blocked")
        self.assertEqual(rev_run["assessment"]["reason"], "trial_revoked")

    def test_il7_unlimited_pipeline_exceeds_pilot_daily_weekly_deep_and_carbon_caps(self) -> None:
        from learner.sources import _build_artifact

        store = LearnerStore(self.root / "il7-e2e-3cand", clock=lambda: self.now)
        try:
            p = make_profile(project_id="flow", name="flow")
            store.register_profile(p)
            store.set_trial_grant(
                unlimited=True,
                grant_id="il7-e2e-unlimited",
                authority="Owner: Cancel the limit and continue",
            )
            routes = _primary_routes_for_standard_fixtures()
            for idx in range(3):
                url = f"https://github.com/eval-org/cand-{idx}"
                routes[f"https://raw.githubusercontent.com/eval-org/cand-{idx}/HEAD/README.md"] = TransportResponse(
                    status_code=200,
                    body=f"# cand-{idx}\nEvaluation harness candidate {idx}.\n".encode("utf-8"),
                    headers={"content-type": "text/plain"},
                    final_url=f"https://raw.githubusercontent.com/eval-org/cand-{idx}/HEAD/README.md",
                )
                routes[f"https://api.github.com/repos/eval-org/cand-{idx}"] = TransportResponse(
                    status_code=200,
                    body=json.dumps({"license": {"spdx_id": "MIT"}, "archived": False, "pushed_at": "2026-11-10T00:00:00Z"}).encode("utf-8"),
                    headers={"content-type": "application/json"},
                    final_url=f"https://api.github.com/repos/eval-org/cand-{idx}",
                )
                art = _build_artifact(
                    raw_url=url,
                    source_class="github_popular",
                    publisher="eval-org",
                    title=f"eval-org/cand-{idx}",
                    summary=f"Evaluation harness helper {idx}",
                    raw_version="",
                    trust_class="public_community",
                    observed_at=self.now,
                    raw_bytes=f"cand-{idx}".encode("utf-8"),
                )
                saved, _ = store.record_source_artifact(art)
                store.ensure_assessment_for_artifact(
                    p,
                    saved,
                    goal_ref="improve evaluation harnesses",
                    reason="intake:adjacent_concept_match",
                )

            def _multi_handler(role: str, prompt: str) -> dict[str, object]:
                req = json.loads(prompt)
                if req["task"] == "batch_triage":
                    alias = req["projects"][0]["project_alias"]
                    return {
                        "decisions": [
                            {
                                "source_id": cand["source_id"],
                                "project_alias": alias,
                                "decision": "qualify",
                                "goal_ref": "improve evaluation harnesses",
                                "capability_ref": "evaluation harness",
                                "rationale": "Adjacent evaluation harness candidate.",
                                "uncertainty": "Requires primary evidence check.",
                                "already_implemented": False,
                                "reopen_triggers": ["new_source_version"],
                            }
                            for cand in req["untrusted_source_data"]
                        ]
                    }
                return _standard_model_handler()(role, prompt)

            transport = FixtureTransport(routes)
            runner = FixtureModelRunner(_multi_handler, requires_trial_grant=True)
            res = assess_pending_work(store, transport, runner, project_ids=("flow",))
            self.assertEqual(res["status"], "ok")
            # 3 candidates proposed (>2 weekly_deep_calls, >2 weekly_carbon_calls, and 1 triage + 6 H deep/synth = 7 > 5 daily_calls)!
            self.assertEqual(res["proposed"], 3)
            self.assertEqual(res["deep_assessed"], 3)
            self.assertGreater(store.budget_used("__shared__", "daily_calls"), 5)
            self.assertEqual(store.budget_used("__shared__", "weekly_deep_calls"), 3)
            self.assertEqual(store.budget_used("__shared__", "weekly_carbon_calls"), 3)
            self.assertEqual(store.get_trial_status()["cumulative_model_calls"], 10)
        finally:
            store.close()

    def test_il8_negative_verification_prose_accepted_while_positive_mixed_and_structured_blocked(self) -> None:
        from learner.assessment import _verify_citations_and_claims
        from learner.sources import PrimaryEvidenceResult

        prof = make_profile()
        ev_item = {
            "evidence_id": "ev_1",
            "url": "https://github.com/eval-org/harness-bench",
            "digest": "sha256:abc123",
            "excerpt": "Deterministic offline evaluation harness.",
        }
        primary = PrimaryEvidenceResult(
            ok=True,
            source_id="src_1",
            canonical_url=ev_item["url"],
            evidence_items=[ev_item],
            raw_blobs={},
            license_id="MIT",
            maintenance={"archived": False},
            version="v1.0.0",
            limitations=[],
            untrusted_directive_flags=(),
            observed_at=self.now,
            error=None,
        )
        base_eval = {"recommendation": "defer"}

        def _proposal(
            *,
            expected_benefit: str,
            borrowable_change: str = "Defer pending offline benchmark.",
            claim_kind: str = "model_inference",
            recommendation: str = "defer",
            evidence_id: str = "ev_1",
            digest: str = "sha256:abc123",
        ) -> dict[str, object]:
            return {
                "recommendation": recommendation,
                "expected_benefit": expected_benefit,
                "borrowable_change": borrowable_change,
                "claim_kind": claim_kind,
                "citations": [
                    {
                        "evidence_id": evidence_id,
                        "url": ev_item["url"],
                        "digest": digest,
                        "claim": "Primary metadata and README reviewed.",
                    }
                ],
                "dissent": [],
                "unresolved_claims": [],
            }

        # Actual live negative examples and explicit local negations must pass citation/claim checks.
        for label, eb, bc in (
            ("live_exact", "No verified improvement is established.", "Defer adoption until benchmarked."),
            (
                "live_metadata",
                "While relevant to evaluation harnesses, no verified improvement is established from the retrieved metadata for Flow.",
                "No project task or adoption is warranted.",
            ),
            ("not_a", "This is not a verified improvement.", "Keep under defer."),
            ("from_retrieved", "There is no verified improvement from retrieved evidence.", "Defer."),
            ("no_evidence_of", "No evidence of a verified improvement from retrieved evidence.", "Defer."),
            ("not_established_as", "It is not established as a verified improvement.", "Defer."),
            ("non_verified", "Non-verified improvement only; requires local run.", "Defer."),
        ):
            with self.subTest(allowed=label):
                v = _verify_citations_and_claims(
                    _proposal(expected_benefit=eb, borrowable_change=bc),
                    primary,
                    base_eval,
                    base_eval,
                    prof,
                )
                self.assertTrue(v["citations_valid"], v)
                self.assertEqual(v["citation_errors"], [])
                self.assertFalse(v["auto_promote_eligible"])
                self.assertFalse(v["creates_project_tasks"])
                self.assertFalse(v["adoption_permission_granted"])

        # Affirmative, mixed intra-field, cross-field, double-negative, and structured claims must stay blocked.
        for label, kw in (
            ("positive_improvement", {"expected_benefit": "This is a verified improvement of 40%."}),
            ("positive_gain", {"expected_benefit": "Provides a verified gain in latency."}),
            ("positive_speedup", {"expected_benefit": "Delivers a verified speedup."}),
            ("positive_observation", {"expected_benefit": "Based on verified observation, adopt."}),
            ("comma_after_no", {"expected_benefit": "No, this is a verified improvement."}),
            ("not_only", {"expected_benefit": "Not only is this a verified improvement, it is simple."}),
            ("no_one_denies", {"expected_benefit": "No one denies this is a verified improvement."}),
            ("double_neg_not_without", {"expected_benefit": "It is not without a verified improvement."}),
            (
                "mixed_neg_then_pos_comma",
                {"expected_benefit": "No verified improvement is established, yet it delivers a verified improvement in CI."},
            ),
            (
                "mixed_neg_then_pos_semicolon",
                {"expected_benefit": "No verified improvement is established; however this is a verified gain."},
            ),
            (
                "mixed_pos_then_neg",
                {"expected_benefit": "This is a verified speedup; no verified improvement is established elsewhere."},
            ),
            (
                "mixed_cross_fields",
                {
                    "expected_benefit": "No verified improvement is established.",
                    "borrowable_change": "Adopt this verified improvement.",
                },
            ),
            (
                "cross_field_boundary_split",
                {
                    "expected_benefit": "This is not",
                    "borrowable_change": "a verified improvement.",
                },
            ),
            (
                "structured_claim_kind_despite_negative_prose",
                {
                    "expected_benefit": "No verified improvement is established.",
                    "borrowable_change": "Not a verified improvement.",
                    "claim_kind": "verified_observation",
                },
            ),
        ):
            with self.subTest(blocked=label):
                v = _verify_citations_and_claims(_proposal(**kw), primary, base_eval, base_eval, prof)
                self.assertFalse(v["citations_valid"], v)
                self.assertIn("model_output_cannot_claim_verified_improvement", v["citation_errors"])

        # Invalid citations and digest mismatches still fail even when prose is negative.
        v_bad_cite = _verify_citations_and_claims(
            _proposal(expected_benefit="No verified improvement is established.", evidence_id="ev_missing"),
            primary,
            base_eval,
            base_eval,
            prof,
        )
        self.assertFalse(v_bad_cite["citations_valid"])
        self.assertEqual(v_bad_cite["citation_errors"], ["unknown_evidence_id:ev_missing", "missing_valid_citations"])

        v_bad_digest = _verify_citations_and_claims(
            _proposal(expected_benefit="No verified improvement is established.", digest="sha256:wrong"),
            primary,
            base_eval,
            base_eval,
            prof,
        )
        self.assertFalse(v_bad_digest["citations_valid"])
        self.assertEqual(v_bad_digest["citation_errors"], ["mismatched_digest:ev_1", "missing_valid_citations"])

        # Dissent remains preserved alongside negative verification prose.
        v_dissent = _verify_citations_and_claims(
            _proposal(expected_benefit="No verified improvement is established."),
            primary,
            {"recommendation": "borrow"},
            {"recommendation": "reject"},
            prof,
        )
        self.assertTrue(v_dissent["citations_valid"])
        self.assertTrue(any("hydrogen=borrow vs carbon=reject" in d for d in v_dissent["dissent"]))

    def test_il8_e2e_negative_synthesis_remains_defer_and_revalidates_saved_synthesis_without_model_calls(self) -> None:
        from learner.sources import _build_artifact

        p = make_profile(execution_paused=True)
        self.store.register_profile(p)
        transport = FixtureTransport(_primary_routes_for_standard_fixtures())

        def _neg_defer_handler(role: str, prompt: str) -> dict[str, object]:
            base = _standard_model_handler(carbon_recommendation="defer")(role, prompt)
            req = json.loads(prompt)
            if req["task"] == "bounded_proposal_synthesis":
                base["recommendation"] = "defer"
                base["expected_benefit"] = "No verified improvement is established from the retrieved metadata."
                base["borrowable_change"] = "Defer pending local benchmark; not a verified improvement."
                base["claim_kind"] = "model_inference"
            return base

        # 1. End-to-end negative prose with recommendation=defer settles as assessed defer (not proposed, not invalid_citations).
        runner = FixtureModelRunner(_neg_defer_handler)
        res = run_due(self.store, transport, runner)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["assessment"]["proposed"], 0)
        self.assertGreaterEqual(res["assessment"]["deferred"], 1)
        by_url = {a.canonical_url: a for a in self.store.list_assessments(p.project_id)}
        bench = by_url["https://github.com/eval-org/harness-bench"]
        self.assertEqual(bench.lifecycle_state, "matched")
        self.assertEqual(bench.decision, "defer")
        self.assertEqual(bench.last_completed_step, "assessed_deferred")
        self.assertEqual(bench.assessment_status, "assessed")
        self.assertEqual(get_recommendations(self.store, project_id=p.project_id)["recommendations"], [])

        # 2. Simulate pre-IL-8 persisted store with saved synthesis (IL-7 support) that failed false-positive check,
        #    plus an affirmative-claim failure and an invalid-citation failure that must stay blocked.
        reval_store = LearnerStore(self.root / "il8-reval-store", clock=lambda: self.now)
        try:
            reval_store.register_profile(p)
            reval_store.set_trial_grant(
                unlimited=True,
                grant_id="il8-seed-unlimited",
                authority="Owner: Cancel the limit and continue",
            )

            def _seed_failed_assessment(
                url_suffix: str,
                *,
                expected_benefit: str,
                citation_errors: list[str],
                recommendation: str = "defer",
            ) -> str:
                url = f"https://github.com/eval-org/{url_suffix}"
                art = _build_artifact(
                    raw_url=url,
                    source_class="github_popular",
                    publisher="eval-org",
                    title=f"eval-org/{url_suffix}",
                    summary="Evaluation harness helper",
                    raw_version="v1.0.0",
                    trust_class="public_community",
                    observed_at=self.now,
                    raw_bytes=url_suffix.encode("utf-8"),
                )
                saved_art, _ = reval_store.record_source_artifact(art)
                asmt, _ = reval_store.ensure_assessment_for_artifact(
                    p,
                    saved_art,
                    goal_ref="improve automated evaluation harnesses",
                    capability_ref="deterministic evaluation replay",
                    reason="triage_qualified",
                )
                ev_id = f"ev_{url_suffix}"
                ev_digest = f"sha256:{url_suffix}"
                primary_bundle = {
                    "claim_kind": "primary_evidence_bundle",
                    "evidence_items": [
                        {
                            "evidence_id": ev_id,
                            "kind": "github_readme",
                            "url": url,
                            "digest": ev_digest,
                            "excerpt": "README excerpt",
                        }
                    ],
                    "license_id": "MIT",
                    "maintenance": {"archived": False},
                    "version": "v1.0.0",
                    "limitations": [],
                    "untrusted_directive_flags": [],
                    "observed_at": self.now,
                }
                cite_id = "ev_nonexistent_999" if any(e.startswith("unknown_evidence_id:") for e in citation_errors) else ev_id
                h_payload = {
                    "recommendation": recommendation,
                    "alternatives": ["Keep existing harness"],
                    "borrowable_change": "Defer until benchmarked.",
                    "affected_existing_capability": "deterministic test runner",
                    "expected_benefit": expected_benefit,
                    "effort_and_risk": "Low effort",
                    "test_baseline_rollback": {
                        "test_plan": "Run unittest",
                        "baseline": "Current baseline",
                        "rollback": "Revert",
                    },
                    "citations": [{"evidence_id": cite_id, "url": url, "digest": ev_digest, "claim": "README."}],
                    "unresolved_claims": [],
                    "uncertainty": "Unmeasured locally.",
                    "claim_kind": "model_inference",
                }
                synth_payload = dict(h_payload) | {"dissent": []}
                # Mark deep-slot settled just like a real pre-IL-8 run.
                slot_res, _ = reval_store.reserve_dispatch(
                    idempotency_key=f"deep-slot:{asmt.assessment_id}:{asmt.source_digest}:r{asmt.profile_revision}",
                    project_id="__shared__",
                    scope_type="deep_candidate",
                    budget_bucket="weekly_deep_calls",
                    units=1,
                    cap_override=5,
                )
                reval_store.mark_dispatched(slot_res.reservation_id)
                reval_store.settle_dispatch(slot_res.reservation_id, actual_units=1)
                reval_store.transition_assessment(
                    asmt.assessment_id,
                    lifecycle_state="matched",
                    last_completed_step="citation_verification_failed",
                    decision="deferred_invalid_citations",
                    reason=f"invalid_or_unsupported_claims:{','.join(citation_errors)}",
                    reopen_triggers=("missing_evidence_available", "new_source_version"),
                    source_claims=[primary_bundle],
                    model_inferences=[
                        {"step": "assess_hydrogen", "status": "ok", "role": "hydrogen", "payload": h_payload},
                        {"step": "assess_carbon", "status": "ok", "role": "carbon", "payload": h_payload},
                        {"step": "synthesis", "status": "ok", "role": "hydrogen", "payload": synth_payload},
                        {
                            "step": "verified_proposal",
                            "citations_valid": False,
                            "citation_errors": citation_errors,
                        },
                    ],
                    assessment_status="invalid_citations",
                )
                return asmt.assessment_id

            neg_id = _seed_failed_assessment(
                "neg-false-pos",
                expected_benefit="No verified improvement is established.",
                citation_errors=["model_output_cannot_claim_verified_improvement"],
                recommendation="defer",
            )
            pos_id = _seed_failed_assessment(
                "pos-true-pos",
                expected_benefit="This is a verified improvement of 40%.",
                citation_errors=["model_output_cannot_claim_verified_improvement"],
                recommendation="borrow",
            )
            bad_cite_id = _seed_failed_assessment(
                "bad-cite",
                expected_benefit="No verified improvement is established.",
                citation_errors=["unknown_evidence_id:ev_nonexistent_999"],
                recommendation="defer",
            )

            def _no_model_calls_allowed(role: str, prompt: str) -> dict[str, object]:
                raise AssertionError(f"Unexpected model call during revalidation: {role}")

            zero_transport = FixtureTransport({})
            zero_runner = FixtureModelRunner(_no_model_calls_allowed)

            res_reval = assess_pending_work(reval_store, zero_transport, zero_runner, project_ids=(p.project_id,))
            self.assertEqual(res_reval["status"], "ok")
            self.assertEqual(res_reval["deep_assessed"], 1)
            self.assertEqual(res_reval["proposed"], 0)
            self.assertEqual(res_reval["deferred"], 1)
            self.assertEqual(zero_runner.calls, [])
            self.assertEqual(zero_transport.calls, [])

            by_id = {a.assessment_id: a for a in reval_store.list_assessments(p.project_id)}
            self.assertEqual(by_id[neg_id].decision, "defer")
            self.assertEqual(by_id[neg_id].last_completed_step, "assessed_deferred")
            self.assertEqual(by_id[neg_id].assessment_status, "assessed")
            self.assertEqual(by_id[pos_id].decision, "deferred_invalid_citations")
            self.assertEqual(by_id[pos_id].last_completed_step, "citation_verification_failed")
            self.assertEqual(by_id[bad_cite_id].decision, "deferred_invalid_citations")
            self.assertEqual(by_id[bad_cite_id].last_completed_step, "citation_verification_failed")
            self.assertEqual(get_recommendations(reval_store, project_id=p.project_id)["recommendations"], [])

            # Subsequent pass is a clean no-op (no infinite re-verification loop).
            res_again = assess_pending_work(reval_store, zero_transport, zero_runner, project_ids=(p.project_id,))
            self.assertEqual(res_again["deep_assessed"], 0)
            self.assertEqual(res_again["deferred"], 0)
            self.assertEqual(zero_runner.calls, [])
            self.assertEqual(zero_transport.calls, [])
        finally:
            reval_store.close()

    def test_bug_1064a8854aea_trial_cli_rejects_all_conflicting_action_combinations_without_mutation(self) -> None:
        storage_path = self.root / "bug-1064-cli"
        store = LearnerStore(storage_path, clock=lambda: self.now)
        try:
            store.set_trial_grant(
                max_model_calls=3,
                expires_at=self.now + 3600.0,
                grant_id="baseline-finite",
                authority="Host baseline",
            )
            before_status = store.get_trial_status()
        finally:
            store.close()

        conflicts = [
            ["trial", "--grant", "--unlimited", "--authority", "owner", "--grant-id", "c1"],
            [
                "trial",
                "--grant",
                "--unlimited",
                "--max-calls",
                "5",
                "--expires-at",
                "2099-01-01T00:00:00Z",
                "--authority",
                "owner",
                "--grant-id",
                "c2",
            ],
            [
                "trial",
                "--grant",
                "--revoke",
                "--max-calls",
                "5",
                "--expires-at",
                "2099-01-01T00:00:00Z",
                "--grant-id",
                "c3",
            ],
            ["trial", "--unlimited", "--revoke", "--authority", "owner", "--grant-id", "c4"],
            [
                "trial",
                "--grant",
                "--unlimited",
                "--revoke",
                "--max-calls",
                "5",
                "--expires-at",
                "2099-01-01T00:00:00Z",
                "--authority",
                "owner",
                "--grant-id",
                "c5",
            ],
        ]
        for argv in conflicts:
            with self.subTest(argv=argv):
                err_buf = io.StringIO()
                with mock.patch("sys.stderr", err_buf):
                    rc = cli_main(["--storage-root", str(storage_path), *argv])
                self.assertEqual(rc, 1)
                self.assertIn("conflicting", err_buf.getvalue().lower())
                check_store = LearnerStore(storage_path, clock=lambda: self.now)
                try:
                    self.assertEqual(check_store.get_trial_status(), before_status)
                finally:
                    check_store.close()

        # Ordinary single actions continue to succeed cleanly.
        for argv, expected_state, expected_mode in (
            (
                ["--json", "trial", "--grant", "--max-calls", "5", "--expires-at", "2099-01-01T00:00:00Z", "--grant-id", "single-finite"],
                "active",
                "finite",
            ),
            (
                ["--json", "trial", "--unlimited", "--authority", "owner-auth", "--grant-id", "single-unlim"],
                "active",
                "unlimited",
            ),
            (
                ["--json", "trial", "--revoke"],
                "revoked",
                "unlimited",
            ),
        ):
            out_buf = io.StringIO()
            with redirect_stdout(out_buf):
                rc = cli_main(["--storage-root", str(storage_path), *argv])
            self.assertEqual(rc, 0)
            payload = json.loads(out_buf.getvalue())
            self.assertEqual(payload["state"], expected_state)
            self.assertEqual(payload["mode"], expected_mode)

    def test_bug_ed3fdaff4a18_contracted_double_negation_blocked_while_negative_prose_allowed(self) -> None:
        from learner.assessment import _verify_citations_and_claims
        from learner.sources import PrimaryEvidenceResult

        p = make_profile(execution_paused=True)
        ev_item = {
            "evidence_id": "ev_1",
            "url": "https://github.com/eval-org/harness-bench",
            "digest": "sha256:abc123",
            "excerpt": "Deterministic offline evaluation harness.",
        }
        primary = PrimaryEvidenceResult(
            ok=True,
            source_id="src_1",
            canonical_url=ev_item["url"],
            evidence_items=[ev_item],
            raw_blobs={},
            license_id="MIT",
            maintenance={"archived": False},
            version="v1.0.0",
            limitations=[],
            untrusted_directive_flags=(),
            observed_at=self.now,
            error=None,
        )
        base_eval = {"recommendation": "defer"}

        def _proposal(**overrides: object) -> dict[str, object]:
            base: dict[str, object] = {
                "recommendation": "defer",
                "expected_benefit": "No verified improvement is established.",
                "borrowable_change": "Defer pending local benchmark.",
                "claim_kind": "model_inference",
                "citations": [
                    {
                        "evidence_id": ev_item["evidence_id"],
                        "url": ev_item["url"],
                        "digest": ev_item["digest"],
                        "claim": "Primary metadata reviewed.",
                    }
                ],
                "dissent": [],
                "unresolved_claims": [],
            }
            base.update(overrides)
            return base

        # Contracted double negations (including supported punctuation & fields) must be blocked as unnegated claims.
        for label, kw in (
            ("isnt_without", {"expected_benefit": "It isn't without a verified improvement."}),
            ("wasnt_without_bang", {"expected_benefit": "It wasn't without a verified improvement!"}),
            ("cant_without_q", {"expected_benefit": "It can't without a verified improvement?"}),
            ("isnt_without_comma_semicolon", {"expected_benefit": "Yes, it isn't without a verified gain; defer."}),
            ("wasnt_without_paren", {"expected_benefit": "(It wasn't without a verified speedup)"}),
            ("cant_without_colon", {"borrowable_change": "Note: it can't without verified observation."}),
            ("isnt_curly_without", {"expected_benefit": "It isn\u2019t without a verified improvement."}),
        ):
            with self.subTest(blocked=label):
                v = _verify_citations_and_claims(_proposal(**kw), primary, base_eval, base_eval, p)
                self.assertFalse(v["citations_valid"], v)
                self.assertIn("model_output_cannot_claim_verified_improvement", v["citation_errors"])

        # Genuine contracted negative prose remains accepted with all safety flags False.
        for label, text in (
            ("isnt_a", "It isn't a verified improvement."),
            ("wasnt_a", "It wasn't a verified improvement."),
            ("cant_prove", "It can't prove a verified improvement."),
            ("without_a", "Without a verified improvement, keep deferred."),
        ):
            with self.subTest(allowed=label):
                v = _verify_citations_and_claims(
                    _proposal(expected_benefit=text),
                    primary,
                    base_eval,
                    base_eval,
                    p,
                )
                self.assertTrue(v["citations_valid"], v)
                self.assertEqual(v["citation_errors"], [])
                self.assertFalse(v["auto_promote_eligible"])
                self.assertFalse(v["creates_project_tasks"])
                self.assertFalse(v["adoption_permission_granted"])

    def test_il13_synthesis_oversized_context_defers_before_dispatch_without_rerunning_blind_assessors(self) -> None:
        from learner.assessment import summarize_assessment_queue
        from learner.sources import _build_artifact

        p = make_profile(execution_paused=True)
        self.store.register_profile(p)
        self.store.set_trial_grant(unlimited=True, grant_id="il13-cap-test", authority="Owner test")

        # 1. Oversized completed blind assessor context that cannot fit minimum cited evidence under 24000 bytes:
        #    must defer cleanly BEFORE synthesis dispatch, spend 0 model calls/budget, and NOT rerun blind assessors.
        art_over = _build_artifact(
            raw_url="https://github.com/eval-org/oversized-ctx",
            source_class="github_popular",
            publisher="eval-org",
            title="eval-org/oversized-ctx",
            summary="Oversized context item",
            raw_version="main",
            trust_class="public_community",
            observed_at=self.now,
            raw_bytes=b"oversized-ctx",
        )
        saved_over, _ = self.store.record_source_artifact(art_over)
        asmt_over, _ = self.store.ensure_assessment_for_artifact(
            p,
            saved_over,
            goal_ref="improve deterministic verification",
            capability_ref="bounded review packet",
            reason="triage_qualified",
        )
        ev_items_over = []
        for idx in range(4):
            dig = f"{idx + 10:02x}" * 32
            ev_items_over.append(
                {
                    "evidence_id": f"ev_{dig[:12]}",
                    "kind": "readme" if idx == 0 else "followup_doc",
                    "url": f"https://raw.githubusercontent.com/eval-org/oversized-ctx/main/doc_{idx}.md",
                    "digest": dig,
                    "excerpt": f"Primary detail {idx}: " + "grounded text " * 40,
                    "truncated": False,
                }
            )
        huge_bundle = {
            "claim_kind": "primary_evidence_bundle",
            "extraction_version": 2,
            "evidence_items": ev_items_over,
            "license_id": "MIT",
            "maintenance": {"archived": False},
            "version": "main",
            "limitations": [],
            "untrusted_directive_flags": [],
            "observed_at": self.now,
        }
        huge_eval_base = {
            "alternatives": ["Keep existing gate " + "a" * 2500],
            "borrowable_change": "Defer oversized candidate " + "b" * 3500,
            "affected_existing_capability": "bounded review packet",
            "expected_benefit": "No verified improvement is established. " + "c" * 2500,
            "effort_and_risk": "High overlap risk " + "d" * 2500,
            "test_baseline_rollback": {
                "test_plan": "Offline tests " + "e" * 1500,
                "baseline": "Existing baseline",
                "rollback": "Revert",
            },
            "citations": [
                {
                    "evidence_id": it["evidence_id"],
                    "url": it["url"],
                    "digest": it["digest"],
                    "claim": f"Observation from {it['evidence_id']}",
                }
                for it in ev_items_over
            ],
            "unresolved_claims": ["Unverified efficacy " + "f" * 1500],
            "uncertainty": "High uncertainty " + "g" * 1000,
            "claim_kind": "model_inference",
        }
        h_huge = dict(huge_eval_base) | {"recommendation": "borrow"}
        c_huge = dict(huge_eval_base) | {"recommendation": "reject"}
        slot_res, _ = self.store.reserve_dispatch(
            idempotency_key=f"deep-slot:{asmt_over.assessment_id}:{asmt_over.source_digest}:r{asmt_over.profile_revision}",
            project_id="__shared__",
            scope_type="deep_candidate",
            budget_bucket="weekly_deep_calls",
            units=1,
            cap_override=5,
        )
        self.store.mark_dispatched(slot_res.reservation_id)
        self.store.settle_dispatch(slot_res.reservation_id, actual_units=1)
        self.store.transition_assessment(
            asmt_over.assessment_id,
            lifecycle_state="matched",
            last_completed_step="carbon_assessed",
            decision="qualify",
            reason="triage_qualified",
            source_claims=[*saved_over.source_claims, huge_bundle],
            model_inferences=[
                {"step": "assess_hydrogen", "status": "ok", "role": "hydrogen", "payload": h_huge},
                {"step": "assess_carbon", "status": "ok", "role": "carbon", "payload": c_huge},
            ],
            assessment_status="partial_assessed",
        )

        zero_runner = FixtureModelRunner(lambda role, prompt: (_ for _ in ()).throw(AssertionError(f"Unexpected call {role}")))
        zero_transport = FixtureTransport({})
        res_over = assess_pending_work(self.store, zero_transport, zero_runner, project_ids=(p.project_id,))
        self.assertEqual(res_over["status"], "degraded")
        self.assertEqual(res_over["deferred"], 1)
        self.assertEqual(res_over["deep_assessed"], 0)
        self.assertIn("synthesis_packet_capacity_exceeded", res_over["errors"])
        self.assertEqual(zero_runner.calls, [])
        self.assertEqual(self.store.budget_used("__shared__", "daily_calls"), 0)

        after_over = self.store.get_assessment(asmt_over.assessment_id)
        assert after_over is not None
        self.assertEqual(after_over.lifecycle_state, "matched")
        self.assertEqual(after_over.decision, "defer")
        self.assertEqual(after_over.last_completed_step, "synthesis_packet_deferred")
        self.assertEqual(after_over.assessment_status, "packet_capacity_deferred")
        # Completed blind assessors and dissent remain preserved.
        steps = [inf.get("step") for inf in after_over.model_inferences]
        self.assertEqual(steps, ["assess_hydrogen", "assess_carbon"])
        summary = summarize_assessment_queue(self.store, project_ids=(p.project_id,))
        self.assertEqual(summary["packet_capacity_deferred"], 1)

        # Repeated pass (and --refresh-deferred) spends zero model calls and zero fetches.
        res_over2 = assess_pending_work(self.store, zero_transport, zero_runner, project_ids=(p.project_id,), refresh_deferred=True)
        self.assertEqual(res_over2["deferred"], 0)
        self.assertEqual(zero_runner.calls, [])
        self.assertEqual(zero_transport.calls, [])

        # 2. Large realistic blind assessor payloads (~8.5KB each) citing follow-up items that previously got clipped:
        #    synthesis receives ALL cited IDs without rerunning blind assessors, and invalid IDs are still rejected.
        art_fit = _build_artifact(
            raw_url="https://github.com/eval-org/large-realistic-fit",
            source_class="github_popular",
            publisher="eval-org",
            title="eval-org/large-realistic-fit",
            summary="Large realistic fit",
            raw_version="main",
            trust_class="public_community",
            observed_at=self.now,
            raw_bytes=b"large-realistic-fit",
        )
        saved_fit, _ = self.store.record_source_artifact(art_fit)
        asmt_fit, _ = self.store.ensure_assessment_for_artifact(
            p,
            saved_fit,
            goal_ref="improve deterministic verification",
            capability_ref="bounded review packet",
            reason="triage_qualified",
        )
        fit_items = []
        for idx in range(8):
            dig = f"{idx + 30:02x}" * 32
            fit_items.append(
                {
                    "evidence_id": f"ev_{dig[:12]}" + (f"_s{idx % 3}" if idx % 3 else ""),
                    "kind": "github_repo_metadata" if idx == 0 else ("readme" if idx <= 3 else "followup_doc"),
                    "url": f"https://raw.githubusercontent.com/eval-org/large-realistic-fit/main/doc_{idx}.md",
                    "source_version": "main",
                    "section_title": f"Section {idx}",
                    "line_start": 1 + idx * 10,
                    "line_end": 20 + idx * 10,
                    "char_start": idx * 1000,
                    "char_end": idx * 1000 + 900,
                    "fetched_bytes": 12000,
                    "total_bytes_hint": 12000,
                    "document_truncated": False,
                    "digest": dig,
                    "excerpt": f"[Section {idx}] Concrete verification evidence {idx}: " + "detail " * 140,
                    "truncated": False,
                }
            )
        fit_bundle = {
            "claim_kind": "primary_evidence_bundle",
            "extraction_version": 2,
            "evidence_items": fit_items,
            "license_id": "MIT",
            "maintenance": {"archived": False},
            "version": "main",
            "limitations": [],
            "untrusted_directive_flags": [],
            "observed_at": self.now,
        }

        def _realistic_eval(rec: str, tag: str) -> dict[str, object]:
            return {
                "recommendation": rec,
                "alternatives": [f"{tag} alt: " + "keep existing runner " * 20],
                "borrowable_change": f"{tag} change: " + "structured verification checklist " * 22,
                "affected_existing_capability": "bounded review packet",
                "expected_benefit": f"{tag} model inference only: " + "reduces missed checks " * 18,
                "effort_and_risk": f"{tag} risk: " + "maintenance overhead " * 22,
                "test_baseline_rollback": {
                    "test_plan": f"{tag} plan: " + "offline replay check " * 18,
                    "baseline": "Current baseline",
                    "rollback": "Revert diff",
                },
                "citations": [
                    {
                        "evidence_id": it["evidence_id"],
                        "url": it["url"],
                        "digest": it["digest"],
                        "claim": f"{tag} cited detail from {it['evidence_id']}: " + "evidence " * 14,
                    }
                    for it in fit_items
                ],
                "unresolved_claims": [f"{tag} unresolved " + "u" * 200],
                "uncertainty": f"{tag} uncertainty: unmeasured locally " + "z" * 160,
                "claim_kind": "model_inference",
            }

        slot_fit, _ = self.store.reserve_dispatch(
            idempotency_key=f"deep-slot:{asmt_fit.assessment_id}:{asmt_fit.source_digest}:r{asmt_fit.profile_revision}",
            project_id="__shared__",
            scope_type="deep_candidate",
            budget_bucket="weekly_deep_calls",
            units=1,
            cap_override=5,
        )
        self.store.mark_dispatched(slot_fit.reservation_id)
        self.store.settle_dispatch(slot_fit.reservation_id, actual_units=1)
        self.store.transition_assessment(
            asmt_fit.assessment_id,
            lifecycle_state="matched",
            last_completed_step="carbon_assessed",
            decision="qualify",
            reason="triage_qualified",
            source_claims=[*saved_fit.source_claims, fit_bundle],
            model_inferences=[
                {"step": "assess_hydrogen", "status": "ok", "role": "hydrogen", "payload": _realistic_eval("defer", "H")},
                {"step": "assess_carbon", "status": "ok", "role": "carbon", "payload": _realistic_eval("reject", "C")},
            ],
            assessment_status="partial_assessed",
        )

        last_item = fit_items[-1]

        def _synth_citing_last_item(role: str, prompt: str) -> dict[str, object]:
            req = json.loads(prompt)
            self.assertEqual(req["task"], "bounded_proposal_synthesis")
            self.assertLessEqual(len(prompt.encode("utf-8")), 24_000)
            evs = {e["evidence_id"]: e for e in req["retrieved_evidence"]}
            self.assertEqual(set(evs), {it["evidence_id"] for it in fit_items})
            target = evs[last_item["evidence_id"]]
            self.assertGreaterEqual(len(target["excerpt"]), 120)
            return {
                "recommendation": "defer",
                "alternatives": ["Keep existing flow"],
                "borrowable_change": "Defer pending benchmark.",
                "affected_existing_capability": "bounded review packet",
                "expected_benefit": "No verified improvement is established.",
                "effort_and_risk": "Low",
                "test_baseline_rollback": {"test_plan": "unittest", "baseline": "current", "rollback": "revert"},
                "citations": [
                    {
                        "evidence_id": target["evidence_id"],
                        "url": target["url"],
                        "digest": target["digest"],
                        "claim": "Verified present in synthesis retrieved_evidence.",
                    }
                ],
                "dissent": ["Assessor dissent: hydrogen=defer vs carbon=reject"],
                "unresolved_claims": ["Unmeasured locally"],
                "uncertainty": "Defer without local benchmark.",
                "claim_kind": "model_inference",
            }

        fit_runner = FixtureModelRunner(_synth_citing_last_item, requires_trial_grant=True)
        res_fit = assess_pending_work(self.store, zero_transport, fit_runner, project_ids=(p.project_id,))
        self.assertEqual(res_fit["status"], "ok")
        self.assertEqual(res_fit["deep_assessed"], 1)
        self.assertEqual(res_fit["deferred"], 1)
        # Only synthesis was called; completed blind assessors were NOT rerun.
        self.assertEqual(len(fit_runner.calls), 1)
        after_fit = self.store.get_assessment(asmt_fit.assessment_id)
        assert after_fit is not None
        self.assertEqual(after_fit.decision, "defer")
        self.assertEqual(after_fit.last_completed_step, "assessed_deferred")
        verified = next(i for i in after_fit.model_inferences if i.get("step") == "verified_proposal")
        self.assertTrue(verified["citations_valid"])
        self.assertTrue(any("hydrogen=defer vs carbon=reject" in d for d in verified["dissent"]))

        # 3. End-to-end synthesis citing a genuinely invented ID under large realistic payloads is still rejected.
        art_inv = _build_artifact(
            raw_url="https://github.com/eval-org/large-realistic-invented",
            source_class="github_popular",
            publisher="eval-org",
            title="eval-org/large-realistic-invented",
            summary="Large realistic invented",
            raw_version="main",
            trust_class="public_community",
            observed_at=self.now,
            raw_bytes=b"large-realistic-invented",
        )
        saved_inv, _ = self.store.record_source_artifact(art_inv)
        asmt_inv, _ = self.store.ensure_assessment_for_artifact(
            p,
            saved_inv,
            goal_ref="improve deterministic verification",
            capability_ref="bounded review packet",
            reason="triage_qualified",
        )
        slot_inv, _ = self.store.reserve_dispatch(
            idempotency_key=f"deep-slot:{asmt_inv.assessment_id}:{asmt_inv.source_digest}:r{asmt_inv.profile_revision}",
            project_id="__shared__",
            scope_type="deep_candidate",
            budget_bucket="weekly_deep_calls",
            units=1,
            cap_override=5,
        )
        self.store.mark_dispatched(slot_inv.reservation_id)
        self.store.settle_dispatch(slot_inv.reservation_id, actual_units=1)
        self.store.transition_assessment(
            asmt_inv.assessment_id,
            lifecycle_state="matched",
            last_completed_step="carbon_assessed",
            decision="qualify",
            reason="triage_qualified",
            source_claims=[*saved_inv.source_claims, fit_bundle],
            model_inferences=[
                {"step": "assess_hydrogen", "status": "ok", "role": "hydrogen", "payload": _realistic_eval("defer", "H")},
                {"step": "assess_carbon", "status": "ok", "role": "carbon", "payload": _realistic_eval("reject", "C")},
            ],
            assessment_status="partial_assessed",
        )

        def _synth_invented(role: str, prompt: str) -> dict[str, object]:
            out = _synth_citing_last_item(role, prompt)
            out["citations"] = [
                {
                    "evidence_id": "ev_genuinely_invented_999",
                    "url": last_item["url"],
                    "digest": last_item["digest"],
                    "claim": "Invented evidence citation.",
                }
            ]
            return out

        inv_runner = FixtureModelRunner(_synth_invented, requires_trial_grant=True)
        res_inv = assess_pending_work(self.store, zero_transport, inv_runner, project_ids=(p.project_id,))
        self.assertEqual(res_inv["status"], "ok")
        self.assertEqual(len(inv_runner.calls), 1)
        after_inv = self.store.get_assessment(asmt_inv.assessment_id)
        assert after_inv is not None
        self.assertEqual(after_inv.decision, "deferred_invalid_citations")
        self.assertEqual(after_inv.last_completed_step, "citation_verification_failed")
        self.assertIn("unknown_evidence_id:ev_genuinely_invented_999", after_inv.reason)


if __name__ == "__main__":
    unittest.main()

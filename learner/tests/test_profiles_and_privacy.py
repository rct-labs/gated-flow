"""Offline tests for profile lifecycle, isolation, privacy guards, SSRF, and injection containment."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from learner.contracts import (
    BudgetPolicy,
    ExportPolicy,
    PrivacyViolationError,
    UnsafeUrlError,
    ValidationError,
)
from learner.pipeline import draft_profile_from_docs, run_intake
from learner.privacy import assert_no_private_leak, build_public_queries, validate_public_url
from learner.sources import FixtureTransport, HttpTransport, TransportResponse, fetch_github_popular
from learner.store import LearnerStore
from learner.tests.fixtures import INJECTION_POPULAR_JSON, build_standard_transport, make_profile


class ProfileAndPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1_794_800_000.0
        self.store = LearnerStore(self.root / "store", clock=lambda: self.now)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_no_registered_profile_means_zero_network_or_dispatch(self) -> None:
        transport = build_standard_transport()
        res_opp = run_intake(self.store, "opportunity-led", transport)
        res_proj = run_intake(self.store, "project-led", transport, project_id="missing")
        self.assertEqual(res_opp["status"], "noop")
        self.assertEqual(res_opp["reason"], "no_active_profiles")
        self.assertEqual(res_proj["status"], "blocked")
        self.assertEqual(res_proj["reason"], "not_registered")
        self.assertEqual(transport.calls, [])
        self.assertEqual(self.store.list_dispatches(), [])

    def test_draft_profile_from_bounded_docs_is_explicit_and_never_auto_activates(self) -> None:
        proj_dir = self.root / "SecretProjDir"
        proj_dir.mkdir()
        (proj_dir / "README.md").write_text(
            "# SecretProjDir\n\nBuild offline evaluation harness for SecretProjDir.\n"
            "## Goals\n- Improve bounded task scheduling\n"
            "## Capabilities\n- Deterministic replay runner\n",
            encoding="utf-8",
        )
        other_dir = self.root / "UnrelatedSibling"
        other_dir.mkdir()
        (other_dir / "README.md").write_text("# UnrelatedSibling\n- Should never be discovered\n", encoding="utf-8")

        draft = draft_profile_from_docs("secret-proj", proj_dir, name="SecretProjName")
        self.assertFalse(draft.activated)
        self.assertEqual(draft.profile.status, "draft")
        self.assertEqual(self.store.list_profiles(), [])
        self.assertEqual(draft.source_docs, ("README.md",))
        for concept in draft.profile.public_concepts:
            self.assertNotIn("secretproj", concept.lower())
        with self.assertRaises(ValidationError):
            draft_profile_from_docs("secret-proj", proj_dir, doc_paths=("../UnrelatedSibling/README.md",))

    def test_private_query_sentinel_and_shared_public_evidence_isolation(self) -> None:
        p = make_profile(
            project_id="private-proj-99",
            name="PrivateBusinessProduct",
            mission="Optimize PrivateBusinessProduct margin forecasting using SENTINEL_PRIVATE_TOKEN",
            public_concepts=("evaluation harness", "SENTINEL_PRIVATE_TOKEN"),
            private_terms=("SENTINEL_PRIVATE_TOKEN", "PrivateBusinessProduct"),
        )
        self.store.register_profile(p)
        transport = build_standard_transport()

        with self.assertRaises(PrivacyViolationError):
            build_public_queries(p)
        with self.assertRaises(PrivacyViolationError):
            fetch_github_popular(transport, query="SENTINEL_PRIVATE_TOKEN", profiles=(p,))
        with self.assertRaises(PrivacyViolationError):
            assert_no_private_leak("PrivateBusinessProduct roadmap", (p,))
        self.assertEqual(transport.calls, [])

        with self.assertRaises(PrivacyViolationError):
            self.store.put_evidence(b"contains SENTINEL_PRIVATE_TOKEN", visibility="public")
        with self.assertRaises(PrivacyViolationError):
            self.store.put_evidence(b"private note", visibility="public", project_id="private-proj-99")

        priv_digest = self.store.put_evidence(
            b"contains SENTINEL_PRIVATE_TOKEN",
            visibility="private",
            project_id="private-proj-99",
        )
        self.assertEqual(self.store.get_evidence(priv_digest), b"contains SENTINEL_PRIVATE_TOKEN")

    def test_private_endpoint_and_redirect_ssrf_blocked(self) -> None:
        for bad_url in (
            "http://github.com/trending",
            "https://localhost/admin",
            "https://127.0.0.1/secret",
            "https://10.0.0.5/internal",
            "https://169.254.169.254/latest/meta-data",
            "https://[::1]/local",
            "https://user:pass@github.com/trending",
            "https://metadata.internal/token",
        ):
            with self.assertRaises(UnsafeUrlError, msg=f"Expected UnsafeUrlError for {bad_url}"):
                validate_public_url(bad_url)

        with self.assertRaises(UnsafeUrlError):
            HttpTransport(resolve_dns=False).fetch("https://127.0.0.1/internal")
        with self.assertRaises(UnsafeUrlError):
            validate_public_url("https://rebind.example.com/feed", resolve_dns=True, resolver=lambda _h: ["127.0.0.1"])

        transport = FixtureTransport(
            {
                "https://api.github.com/search/repositories*": TransportResponse(
                    status_code=302,
                    body=b"",
                    headers={"location": "http://127.0.0.1:8080/internal-secret"},
                    final_url="https://api.github.com/search/repositories",
                )
            }
        )
        cov = fetch_github_popular(transport, query="evaluation").coverage[0]
        self.assertEqual(cov.status, "error")
        self.assertIn("HTTPS", cov.error or "")
        self.assertEqual(len(transport.calls), 1)

    def test_untrusted_prompt_injection_contained_as_source_claim(self) -> None:
        p = make_profile(execution_paused=True, learning_enabled=True)
        self.store.register_profile(p)
        transport = FixtureTransport(
            {
                "https://api.github.com/search/repositories*": TransportResponse(
                    status_code=200,
                    body=INJECTION_POPULAR_JSON,
                    headers={"content-type": "application/json"},
                    final_url="https://api.github.com/search/repositories",
                ),
                "https://export.arxiv.org/api/query*": TransportResponse(
                    status_code=200,
                    body=b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>',
                    headers={"content-type": "application/atom+xml"},
                    final_url="https://export.arxiv.org/api/query",
                ),
            }
        )
        run_intake(self.store, "project-led", transport, project_id=p.project_id)
        prof_after = self.store.get_profile(p.project_id)
        assert prof_after is not None
        self.assertTrue(prof_after.execution_paused)
        self.assertTrue(prof_after.learning_enabled)

        arts = self.store.list_source_artifacts()
        self.assertEqual(len(arts), 1)
        self.assertIn("ignore_instructions", arts[0].untrusted_directive_flags)
        self.assertIn("permission_escalation", arts[0].untrusted_directive_flags)
        self.assertIn("shell_execution", arts[0].untrusted_directive_flags)

        asmt = self.store.list_assessments(p.project_id)[0]
        self.assertEqual(asmt.claim_kind, "source_claim")
        self.assertEqual(asmt.model_inferences, ())
        self.assertEqual(asmt.assessment_status, "unconfigured")

    def test_isolated_second_project_and_selective_profile_revision(self) -> None:
        alpha = make_profile(
            project_id="alpha-proj",
            name="Alpha Workspace",
            public_concepts=("evaluation harness",),
            goals=("improve automated evaluation harnesses",),
        )
        beta = make_profile(
            project_id="beta-proj",
            name="Beta Workspace",
            root_path="/tmp/beta-root",
            mission="Optimize Beta Workspace cold storage indexing",
            existing_capabilities=("snapshot compaction",),
            goals=("reduce snapshot latency",),
            public_concepts=("storage indexing",),
            private_terms=("BETA_SECRET_KEY_42",),
        )
        self.store.register_profile(alpha)
        self.store.register_profile(beta)
        transport = build_standard_transport()
        run_intake(self.store, "opportunity-led", transport)

        # Shared fetch ran once for both active projects.
        alpha_asmts = self.store.list_assessments("alpha-proj")
        beta_asmts = self.store.list_assessments("beta-proj")
        self.assertGreaterEqual(len(alpha_asmts), 2)
        self.assertEqual(len(alpha_asmts), len(beta_asmts))

        # Revising only budget does NOT invalidate applicability.
        rev2, reopened_budget = self.store.revise_profile(
            "alpha-proj",
            {"budget": BudgetPolicy(daily_calls=8, weekly_deep_calls=3, daily_fetches=12).to_dict()},
            note="budget bump",
        )
        self.assertEqual(rev2.revision, 2)
        self.assertEqual(reopened_budget, [])

        # Revising alpha's goals invalidates only alpha's affected assessments, never beta's or shared sources.
        shared_before = len(self.store.list_source_artifacts())
        rev3, reopened_goals = self.store.revise_profile(
            "alpha-proj",
            {"goals": ["completely new goal around compiler optimization"], "known_gaps": ["new gap"]},
            note="goal shift",
        )
        self.assertEqual(rev3.revision, 3)
        self.assertEqual(len(reopened_goals), len(alpha_asmts))
        self.assertEqual(len(self.store.list_source_artifacts()), shared_before)

        alpha_new = self.store.list_assessments("alpha-proj")
        self.assertTrue(all(a.profile_revision == 3 and a.predecessor_id is not None for a in alpha_new))
        beta_after = self.store.list_assessments("beta-proj")
        self.assertTrue(all(a.profile_revision == 1 and a.predecessor_id is None for a in beta_after))

    def test_pause_separation_and_unregister_retention(self) -> None:
        alpha = make_profile(
            project_id="alpha-proj",
            export_policy=ExportPolicy(private_retention="purge_private"),
        )
        beta = make_profile(
            project_id="beta-proj",
            name="Beta Workspace",
            root_path="/tmp/beta-root",
            mission="Optimize Beta Workspace",
            private_terms=("BETA_ONLY_SENTINEL",),
        )
        self.store.register_profile(alpha)
        self.store.register_profile(beta)
        transport = build_standard_transport()
        run_intake(self.store, "opportunity-led", transport)

        # Execution pause stays independent of learning_enabled.
        p1 = self.store.set_execution_paused("alpha-proj", True)
        self.assertTrue(p1.execution_paused)
        self.assertTrue(p1.learning_enabled)
        p2 = self.store.set_learning_enabled("alpha-proj", False)
        self.assertTrue(p2.execution_paused)
        self.assertFalse(p2.learning_enabled)
        p3 = self.store.set_learning_enabled("alpha-proj", True)
        self.assertTrue(p3.execution_paused)  # Enabling learning never clears execution_paused.

        priv_digest = self.store.put_evidence(b"alpha private blob", visibility="private", project_id="alpha-proj")
        asmt_id = self.store.list_assessments("alpha-proj")[0].assessment_id
        self.store.record_outcome_reference(
            project_id="alpha-proj",
            assessment_id=asmt_id,
            external_experiment_ref="exp-1",
            status="rejected",
            baseline_summary="b0",
            outcome_summary="o1",
            evaluator="local-test",
            feedback_note="not useful",
        )

        summary = self.store.unregister_profile("alpha-proj", retention="purge_private")
        self.assertGreaterEqual(summary["revoked_assessments"], 1)
        self.assertEqual(summary["purged_private_blobs"], 1)
        self.assertIsNone(self.store.get_profile("alpha-proj"))
        self.assertEqual(self.store.list_outcomes("alpha-proj"), [])
        with self.assertRaises(ValidationError):
            self.store.get_evidence(priv_digest)

        # Shared public artifacts & beta's assessments remain intact.
        self.assertGreaterEqual(len(self.store.list_source_artifacts()), 2)
        self.assertGreaterEqual(len(self.store.list_assessments("beta-proj")), 2)

        # retain_redacted revokes pending work and redacts private records instead of deleting audit rows.
        beta_asmt = self.store.list_assessments("beta-proj")[0].assessment_id
        self.store.record_outcome_reference(
            project_id="beta-proj",
            assessment_id=beta_asmt,
            external_experiment_ref="exp-beta-1",
            status="inconclusive",
            baseline_summary="secret beta baseline",
            outcome_summary="secret beta outcome",
            evaluator="local-test",
            feedback_note="BETA_ONLY_SENTINEL note",
        )
        beta_unreg = self.store.unregister_profile("beta-proj", retention="retain_redacted")
        self.assertGreaterEqual(beta_unreg["revoked_assessments"], 1)
        redacted_outcomes = self.store.list_outcomes("beta-proj")
        self.assertEqual(len(redacted_outcomes), 1)
        self.assertEqual(redacted_outcomes[0].feedback_note, "[redacted]")
        self.assertEqual(redacted_outcomes[0].baseline_summary, "[redacted]")
        revoked_asmts = self.store.list_assessments("beta-proj")
        self.assertTrue(all(a.lifecycle_state == "closed" and a.decision == "revoked" for a in revoked_asmts))

    def test_flow_profile_and_sanitized_summary_preserve_semantics_with_triggering_public_words(self) -> None:
        import json
        from learner.assessment import run_due, run_review
        from learner.contracts import ProjectProfile
        from learner.model_dispatch import FixtureModelRunner
        from learner.privacy import assert_safe_model_packet, build_sanitized_profile_summary
        from learner.tests.test_assessment_and_cli import (
            _primary_routes_for_standard_fixtures,
            _standard_model_handler,
        )

        profile = ProjectProfile.from_dict(
            {
                "adapter": "flow",
                "budget": {"daily_calls": 5, "daily_fetches": 10, "weekly_deep_calls": 2},
                "desired_outcomes": [
                    "Actionable bounded learning recommendations surfaced in flow resume without interrupting authorized execution",
                    "Evaluated low-impact skill improvements with immutable task pins and verified rollback",
                ],
                "exclusions": [
                    "resume blocked CR-1",
                    "bypass gate model policy",
                    "modify installed runtime outside release.py",
                    "automatic business project activation",
                ],
                "execution_paused": False,
                "existing_capabilities": [
                    "task admission, pre-commit gate, and bounded runner loop",
                    "host session continuity resume checkpoint recall",
                    "reversible local release stage activate rollback",
                    "bounded review packet and repair lifecycle",
                    "modular workflow skills: flow, flow-run, model-debate, run-queue",
                    "standalone two-mode directed learner foundation",
                ],
                "export_policy": {
                    "allow_public_concept_queries": True,
                    "allow_shared_public_evidence": True,
                    "private_retention": "purge_private",
                },
                "goals": [
                    "Integrate independent two-mode directed learning with Flow single-entry continuity",
                    "Evaluate and govern bounded low-impact skill guidance with pinned task packets and rollback",
                ],
                "known_gaps": [
                    "bounded learning evidence surfacing in flow-run inspection",
                    "skill release pinning and outcome feedback loop",
                ],
                "learning_enabled": True,
                "mission": "Maintain deterministic task gating, host continuity, reversible local releases, and governed adaptive skills for Flow without violating model routing or pause boundaries.",
                "name": "flow",
                "private_terms": ["CR-1"],
                "project_id": "flow",
                "provenance": {"cr1_paused": True, "eligible_for_host_activation": True},
                "public_concepts": ["evaluation harness", "task scheduling", "workflow skills", "agent continuity"],
                "revision": 1,
                "root_path": str(self.root / "flow-repo"),
                "source_policy": {
                    "allowed_classes": ["github_trending", "github_popular", "github_releases", "arxiv"],
                    "allowed_hosts": [],
                    "max_items_per_source": 20,
                    "max_items_per_sweep": 60,
                },
                "stage": "draft",
                "status": "active",
            }
        )
        self.store.register_profile(profile)

        summary = build_sanitized_profile_summary(profile, alias="P1")
        self.assertTrue(summary["goals"], "goals must preserve useful semantics after redacting 'flow'")
        self.assertTrue(summary["existing_capabilities"], "capabilities must preserve semantics after redacting 'flow'")
        self.assertTrue(summary["desired_outcomes"], "desired_outcomes must not be dropped wholesale")
        self.assertTrue(summary["known_gaps"], "known_gaps must not be dropped wholesale")
        summary_json = json.dumps(summary)
        self.assertNotIn("CR-1", summary_json)
        self.assertNotIn(profile.root_path, summary_json)
        self.assertNotRegex(summary_json.lower(), r"\bflow\b")

        routes = _primary_routes_for_standard_fixtures()
        routes["https://api.github.com/repos/eval-org/harness-bench/releases*"] = TransportResponse(
            status_code=200,
            body=json.dumps(
                [
                    {
                        "tag_name": "v1.2.0",
                        "name": "Flow releases independent change v1.2.0",
                        "html_url": "https://github.com/eval-org/harness-bench/releases/tag/v1.2.0",
                        "published_at": "2026-11-10T00:00:00Z",
                        "body": "Independent workflow change for releases routing and pause checks.",
                        "prerelease": False,
                        "draft": False,
                    }
                ]
            ).encode("utf-8"),
            headers={"content-type": "application/json"},
            final_url="https://api.github.com/repos/eval-org/harness-bench/releases",
        )
        routes["https://raw.githubusercontent.com/eval-org/harness-bench/HEAD/README.md"] = TransportResponse(
            status_code=200,
            body=(
                "# harness-bench\n"
                "Control flow releases with independent change verification and routing pause checks.\n"
            ).encode("utf-8"),
            headers={"content-type": "text/plain"},
            final_url="https://raw.githubusercontent.com/eval-org/harness-bench/HEAD/README.md",
        )
        for empty_repo in ("meme-org/ascii-cat-party", "hyped-org/mega-popular-toy"):
            routes[f"https://api.github.com/repos/{empty_repo}/releases*"] = TransportResponse(
                status_code=200,
                body=b"[]",
                headers={"content-type": "application/json"},
                final_url=f"https://api.github.com/repos/{empty_repo}/releases",
            )
        transport = FixtureTransport(routes)
        runner = FixtureModelRunner(_standard_model_handler())

        due_res = run_due(self.store, transport, runner)
        self.assertEqual(due_res["status"], "ok", due_res)
        self.assertGreaterEqual(due_res["assessment"]["proposed"], 1)

        # Shared triage receipt must be stored as internal evidence, never public source artifact.
        with self.store._lock:
            vis_rows = self.store._conn().execute(
                "SELECT visibility, content_type FROM evidence_blobs"
            ).fetchall()
        vis_set = {r["visibility"] for r in vis_rows}
        self.assertIn("internal", vis_set)
        self.assertIn("public", vis_set)

        # Ensure review(mode="both") on actual flow profile also succeeds with triggering words.
        self.now += 60.0
        rev_res = run_review(self.store, transport, runner, project_id="flow", mode="both")
        self.assertEqual(rev_res["status"], "ok", rev_res)

        # Structured packet check rejects secrets anywhere, and rejects project identifiers in project fields,
        # while permitting untrusted_source_data containing ordinary words ('flow', 'releases', 'independent', 'change').
        safe_packet = json.dumps(
            {
                "task": "independent_blind_assessment",
                "instructions": "Independently assess borrowable_change.",
                "project": summary,
                "untrusted_source_data": {
                    "canonical_url": "https://github.com/eval-org/harness-bench/releases/tag/v1.2.0",
                    "title": "Flow releases independent change",
                },
            }
        )
        assert_safe_model_packet(safe_packet, (profile,))
        with self.assertRaises(PrivacyViolationError):
            assert_safe_model_packet(safe_packet.replace("harness-bench", "SENTINEL_LEAK_99"), (profile,))
        with self.assertRaises(PrivacyViolationError):
            assert_safe_model_packet(
                json.dumps({"task": "batch_triage", "projects": [{"project_alias": "P1", "name": "flow"}], "untrusted_source_data": []}),
                (profile,),
            )


if __name__ == "__main__":
    unittest.main()

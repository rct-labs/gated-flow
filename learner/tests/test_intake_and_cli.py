"""Offline tests for two-mode intake, feed degradation, CLI demo, and packaging smoke."""

from __future__ import annotations

import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

from learner.cli import main as cli_main
from learner.pipeline import run_intake, summarize_intake_fit
from learner.sources import (
    FixtureTransport,
    SourceFetchError,
    TransportResponse,
    fetch_arxiv_feed,
    fetch_github_popular,
    fetch_github_releases,
    fetch_github_trending,
)
from learner.store import LearnerStore
from learner.tests.fixtures import (
    GITHUB_RELEASES_V1,
    GITHUB_RELEASES_V1_3,
    build_standard_transport,
    make_profile,
)


class IntakeAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1_794_800_000.0
        self.store = LearnerStore(self.root / "store", clock=lambda: self.now)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_both_modes_and_adjacent_useful_vs_misleading_popular_intake(self) -> None:
        p = make_profile(
            existing_capabilities=("deterministic test runner",),
            goals=("improve automated evaluation harnesses",),
            public_concepts=("evaluation harness", "task scheduling"),
        )
        self.store.register_profile(p)
        transport = build_standard_transport()

        # Mode 2: opportunity-led discovery without user URLs or stated gaps.
        opp = run_intake(
            self.store,
            "opportunity-led",
            transport,
            release_repos=("eval-org/harness-bench",),
        )
        self.assertEqual(opp["status"], "ok")
        self.assertEqual(opp["assessment_status"], "unconfigured")
        self.assertGreaterEqual(opp["artifacts_found"], 4)

        artifacts = {a.canonical_url: a for a in self.store.list_source_artifacts()}
        adjacent = artifacts["https://github.com/eval-org/harness-bench"]
        misleading = artifacts["https://github.com/hyped-org/mega-popular-toy"]

        fit_adj = summarize_intake_fit(p, adjacent)
        fit_pop = summarize_intake_fit(p, misleading)
        self.assertEqual(fit_adj["fit"], "adjacent_concept_match")
        self.assertIsNotNone(fit_adj["goal_ref"])
        self.assertEqual(fit_pop["fit"], "unmatched_popularity_only")
        self.assertEqual(fit_pop["concept_hits"], [])
        self.assertTrue(misleading.popularity_signal_only)
        self.assertEqual(misleading.metadata["qualification_status"], "intake_only")

        # Mode 1: project-led review uses sanitized concepts only and deduplicates identical items.
        self.now += 10.0
        proj = run_intake(
            self.store,
            "project-led",
            transport,
            project_id=p.project_id,
            release_repos=("eval-org/harness-bench",),
        )
        self.assertEqual(proj["status"], "ok")
        for called_url in transport.calls:
            self.assertNotIn("alpha-proj", called_url.lower())
            self.assertNotIn("SENTINEL", called_url)

    def test_trending_degraded_fallback_is_honest_and_never_labelled_trending(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        transport = build_standard_transport(trending_fails=True)

        res = run_intake(self.store, "opportunity-led", transport)
        self.assertEqual(res["status"], "degraded")
        self.assertTrue(res["incomplete"])

        covs = res["coverage"]
        trending_cov = [c for c in covs if c["source_class"] == "github_trending"][0]
        self.assertEqual(trending_cov["status"], "degraded")
        self.assertTrue(trending_cov["incomplete"])
        self.assertIn("503", trending_cov["error"])

        fallback_items = [
            a for a in self.store.list_source_artifacts() if a.degraded_fallback
        ]
        self.assertGreaterEqual(len(fallback_items), 1)
        for item in fallback_items:
            self.assertFalse(item.is_actual_trending)
            self.assertFalse(item.measured_star_velocity)
            self.assertEqual(item.metadata["source_label"], "github_search_fallback")

    def test_duplicate_versions_skipped_and_new_version_reopens_with_lineage(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        t1 = FixtureTransport(
            {
                "https://api.github.com/repos/eval-org/harness-bench/releases*": TransportResponse(
                    status_code=200,
                    body=GITHUB_RELEASES_V1,
                    headers={"content-type": "application/json"},
                    final_url="https://api.github.com/repos/eval-org/harness-bench/releases",
                )
            }
        )
        c1 = fetch_github_releases(t1, repo="eval-org/harness-bench", clock=lambda: self.now)
        art1, is_new1 = self.store.record_source_artifact(c1.artifacts[0])
        self.assertTrue(is_new1)
        self.assertEqual(art1.canonical_version, "1.2.0")
        asmt1, created1 = self.store.ensure_assessment_for_artifact(p, art1)
        self.assertTrue(created1)

        # Duplicate identical release (same canonical URL + version) -> no duplicate work.
        _, is_new_dup = self.store.record_source_artifact(c1.artifacts[0])
        self.assertFalse(is_new_dup)
        asmt_dup, created_dup = self.store.ensure_assessment_for_artifact(p, art1)
        self.assertFalse(created_dup)
        self.assertEqual(asmt_dup.assessment_id, asmt1.assessment_id)

        # Close v1.2.0, then ingest v1.3.0 -> reopens with predecessor_id lineage.
        self.store.transition_assessment(
            asmt1.assessment_id,
            lifecycle_state="closed",
            last_completed_step="closed_prior",
            decision="defer",
            reason="wait_for_next_release",
            reopen_triggers=("new_source_version",),
        )
        t2 = FixtureTransport(
            {
                "https://api.github.com/repos/eval-org/harness-bench/releases*": TransportResponse(
                    status_code=200,
                    body=GITHUB_RELEASES_V1_3,
                    headers={"content-type": "application/json"},
                    final_url="https://api.github.com/repos/eval-org/harness-bench/releases",
                )
            }
        )
        self.now += 60.0
        c2 = fetch_github_releases(t2, repo="eval-org/harness-bench", clock=lambda: self.now)
        art2, is_new2 = self.store.record_source_artifact(c2.artifacts[0])
        self.assertTrue(is_new2)
        asmt2, created2 = self.store.ensure_assessment_for_artifact(p, art2)
        self.assertTrue(created2)
        self.assertEqual(asmt2.predecessor_id, asmt1.assessment_id)
        self.assertEqual(asmt2.lifecycle_state, "seen")
        self.assertIn("1.2.0->1.3.0", asmt2.reason)

        # arXiv canonical URL stays base /abs/<id> while version v1 -> v2 reopens cleanly.
        arxiv_v1 = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2611.99999v1</id><title>Eval Paper</title><summary>Eval v1</summary></entry></feed>'
        arxiv_v2 = b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>https://arxiv.org/abs/2611.99999v2</id><title>Eval Paper</title><summary>Eval v2</summary></entry></feed>'
        ax1 = fetch_arxiv_feed(
            FixtureTransport({"https://export.arxiv.org/api/query*": TransportResponse(200, arxiv_v1, {}, "https://export.arxiv.org/api/query")}),
            query="evaluation",
            clock=lambda: self.now,
        )
        ax_art1, _ = self.store.record_source_artifact(ax1.artifacts[0])
        ax_asmt1, _ = self.store.ensure_assessment_for_artifact(p, ax_art1)
        ax2 = fetch_arxiv_feed(
            FixtureTransport({"https://export.arxiv.org/api/query*": TransportResponse(200, arxiv_v2, {}, "https://export.arxiv.org/api/query")}),
            query="evaluation",
            clock=lambda: self.now + 10.0,
        )
        ax_art2, is_new_ax2 = self.store.record_source_artifact(ax2.artifacts[0])
        self.assertTrue(is_new_ax2)
        self.assertEqual(ax_art1.canonical_url, ax_art2.canonical_url)
        ax_asmt2, created_ax2 = self.store.ensure_assessment_for_artifact(p, ax_art2)
        self.assertTrue(created_ax2)
        self.assertEqual(ax_asmt2.predecessor_id, ax_asmt1.assessment_id)

    def test_malformed_failing_feeds_and_incomplete_coverage_retention(self) -> None:
        p = make_profile()
        self.store.register_profile(p)

        # Initial successful trending run at T1.
        good_transport = build_standard_transport(trending_fails=False)
        res1 = run_intake(self.store, "opportunity-led", good_transport)
        self.assertEqual(res1["status"], "ok")
        artifacts_after_t1 = len(self.store.list_source_artifacts())
        t1 = self.now

        # Later malformed/failing feeds at T2 must preserve last_success_at=T1 and prior items.
        self.now += 3600.0
        bad_transport = FixtureTransport(
            {
                "https://github.com/trending*": TransportResponse(
                    status_code=200,
                    body=b"<html><body>broken no articles</body></html>",
                    headers={},
                    final_url="https://github.com/trending",
                ),
                "https://api.github.com/search/repositories*": TransportResponse(
                    status_code=200,
                    body=b"{not-valid-json",
                    headers={},
                    final_url="https://api.github.com/search/repositories",
                ),
                "https://export.arxiv.org/api/query*": TransportResponse(
                    status_code=200,
                    body=b'<!DOCTYPE foo [<!ENTITY xxe "bad">]><feed></feed>',
                    headers={},
                    final_url="https://export.arxiv.org/api/query",
                ),
            }
        )
        res2 = run_intake(self.store, "opportunity-led", bad_transport, allow_search_fallback=False)
        self.assertEqual(res2["status"], "degraded")
        self.assertTrue(res2["incomplete"])
        self.assertEqual(len(self.store.list_source_artifacts()), artifacts_after_t1)

        latest_trending = [c for c in res2["coverage"] if c["source_class"] == "github_trending"][-1]
        self.assertEqual(latest_trending["status"], "error")
        self.assertEqual(latest_trending["last_success_at"], t1)

        # Timeout and payload_too_large bounds on reusable collectors.
        timeout_t = FixtureTransport(
            {"https://export.arxiv.org/api/query*": SourceFetchError("timeout", "read timed out")}
        )
        cov_to = fetch_arxiv_feed(timeout_t, query="evaluation").coverage[0]
        self.assertEqual(cov_to.status, "error")
        self.assertIn("timed out", cov_to.error or "")

        oversized_t = FixtureTransport(
            {
                "https://api.github.com/search/repositories*": TransportResponse(
                    status_code=200,
                    body=b"x" * 2048,
                    headers={},
                    final_url="https://api.github.com/search/repositories",
                )
            }
        )
        cov_big = fetch_github_popular(oversized_t, query="evaluation", max_bytes=128).coverage[0]
        self.assertEqual(cov_big.status, "error")
        self.assertIn("max_bytes", cov_big.error or "")
        self.assertEqual(fetch_github_trending(bad_transport, allow_search_fallback=False).coverage[0].status, "error")

    def test_cli_end_to_end_demo_reports_unconfigured_assessment_honestly(self) -> None:
        storage = self.root / "cli-store"
        proj_dir = self.root / "demo-proj"
        proj_dir.mkdir()
        (proj_dir / "README.md").write_text(
            "# Demo\n\nOffline evaluation harness.\n## Goals\n- Improve verification\n",
            encoding="utf-8",
        )
        draft_path = self.root / "draft.json"
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(
                cli_main(["--storage-root", str(storage), "draft", "--project-id", "demo", "--root-path", str(proj_dir), "--out", str(draft_path)]),
                0,
            )
            self.assertEqual(
                cli_main(["--storage-root", str(storage), "register", "--profile", str(draft_path)]),
                0,
            )

            fixture_map_path = self.root / "fixtures.json"
            fixture_map_path.write_text(
                json.dumps(
                    {
                        "https://github.com/trending*": {
                            "status_code": 200,
                            "body": "<html><body><article class='Box-row'><h2><a href='/eval-org/harness-bench'>eval-org / harness-bench</a></h2><p>Evaluation harness</p><span>10 stars today</span></article></body></html>",
                        },
                        "https://api.github.com/search/repositories*": {
                            "status_code": 200,
                            "body": json.dumps({"items": []}),
                        },
                        "https://export.arxiv.org/api/query*": {
                            "status_code": 200,
                            "body": '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>',
                        },
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                cli_main(["--storage-root", str(storage), "intake", "--mode", "opportunity-led", "--fixture-map", str(fixture_map_path)]),
                0,
            )

        cli_store = LearnerStore(storage)
        try:
            asmts = cli_store.list_assessments("demo")
            self.assertEqual(len(asmts), 1)
            self.assertEqual(asmts[0].assessment_status, "unconfigured")
            asmt_id = asmts[0].assessment_id
        finally:
            cli_store.close()

        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli_main(
                    [
                        "--storage-root",
                        str(storage),
                        "feedback",
                        "--project-id",
                        "demo",
                        "--assessment-id",
                        asmt_id,
                        "--experiment-ref",
                        "exp-42",
                        "--status",
                        "accepted",
                        "--baseline",
                        "10 passed",
                        "--outcome",
                        "12 passed",
                        "--evaluator",
                        "unittest",
                        "--note",
                        "more evaluation checks",
                    ]
                ),
                0,
            )
        status_buf = io.StringIO()
        with redirect_stdout(status_buf):
            self.assertEqual(cli_main(["--storage-root", str(storage), "status", "--json"]), 0)
        status_payload = json.loads(status_buf.getvalue())
        self.assertEqual(status_payload["assessment_status"], "unconfigured")
        self.assertEqual(len(status_payload["profiles"]), 1)
        self.assertEqual(len(status_payload["assessments"]), 1)
        self.assertEqual(len(status_payload["outcomes"]), 1)

    def test_packaging_smoke_outside_repo_and_zero_flow_imports(self) -> None:
        learner_src = Path(__file__).resolve().parents[1]
        with (learner_src / "pyproject.toml").open("rb") as fh:
            meta = tomllib.load(fh)
        self.assertEqual(meta["project"]["name"], "project-learner")
        self.assertEqual(meta["project"]["dependencies"], [])

        # Verify no module in learner/ imports flow or gate.
        for py_file in learner_src.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0].lower()
                        self.assertNotIn(top, {"flow", "gate"}, msg=f"Forbidden import in {py_file}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    top = node.module.split(".")[0].lower()
                    self.assertNotIn(top, {"flow", "gate"}, msg=f"Forbidden import in {py_file}")

        # Copy learner package into isolated temp directory outside this repo and run smoke import + CLI.
        ext_root = self.root / "isolated-pkg"
        ext_root.mkdir()
        shutil.copytree(learner_src, ext_root / "learner", ignore=shutil.ignore_patterns("__pycache__", "tests"))

        smoke_code = (
            "import json, learner; "
            "assert hasattr(learner, 'LearnerStore'); "
            "assert hasattr(learner, 'collect_opportunity_led'); "
            "print(json.dumps({'version': learner.__version__, 'file': learner.__file__}))"
        )
        env = {"PATH": "", "PYTHONPATH": str(ext_root)}
        proc = subprocess.run(
            [sys.executable, "-c", smoke_code],
            cwd=str(ext_root),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        info = json.loads(proc.stdout)
        self.assertTrue(Path(info["file"]).resolve().is_relative_to(ext_root.resolve()))

        cli_proc = subprocess.run(
            [sys.executable, "-m", "learner", "--help"],
            cwd=str(ext_root),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("Standalone directed learner CLI", cli_proc.stdout)

    def test_cli_status_and_results_surface_shared_unknown_dispatches_after_crash(self) -> None:
        storage = self.root / "shared-unknown-cli"
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
                        "--public-concepts",
                        "evaluation harness",
                    ]
                ),
                0,
            )

        store = LearnerStore(storage, clock=lambda: self.now)
        try:
            res, _ = store.reserve_dispatch(
                idempotency_key="shared-triage:crashed",
                project_id="__shared__",
                scope_type="shared_triage",
                budget_bucket="daily_calls",
                units=1,
                ttl_s=30.0,
                target_project_ids=("flow",),
                step_keys=("triage:flow:a1",),
            )
            store.mark_dispatched(res.reservation_id)
            store.mark_unknown(res.reservation_id, error="timeout_after_120s")
        finally:
            store.close()

        st_buf = io.StringIO()
        with redirect_stdout(st_buf):
            self.assertEqual(cli_main(["--storage-root", str(storage), "--json", "status", "--project-id", "flow"]), 0)
        st_payload = json.loads(st_buf.getvalue())
        self.assertEqual(st_payload["assessment_status"], "unknown_pending_reconciliation")
        self.assertEqual(len(st_payload["unknown_dispatches"]), 1)
        self.assertEqual(st_payload["unknown_dispatches"][0]["reservation_id"], res.reservation_id)

        res_buf = io.StringIO()
        with redirect_stdout(res_buf):
            self.assertEqual(cli_main(["--storage-root", str(storage), "--json", "results", "--project-id", "flow", "--coverage"]), 0)
        res_payload = json.loads(res_buf.getvalue())
        self.assertEqual(len(res_payload["unknown_dispatches"]), 1)
        self.assertEqual(res_payload["unknown_dispatches"][0]["reservation_id"], res.reservation_id)


if __name__ == "__main__":
    unittest.main()

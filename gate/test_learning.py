"""Offline acceptance tests for Flow learning integration (`flow learn`, `flow resume`, profiles, languages)."""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.request

from test_compat import FLOW, committed_runner_repo, run

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flow import learning as flow_learning  # noqa: E402
from learner import (  # noqa: E402
    FixtureModelRunner,
    FixtureTransport,
    LearnerStore,
    TransportResponse,
    run_review,
)
from learner.tests.test_assessment_and_cli import (  # noqa: E402
    _primary_routes_for_standard_fixtures,
    _standard_model_handler,
)


def _tree_digest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.relative_to(root).parts:
            out[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _run_flow(*args: str, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(FLOW), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        env=env,
    )


class FlowLearningIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.storage = self.root / "learner-store"
        self.env = {"FLOW_LEARNER_ROOT": str(self.storage)}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _setup_flow_like_repo(self, name: str = "flow-sample") -> Path:
        parent = self.root / f"{name}-parent"
        parent.mkdir(parents=True, exist_ok=True)
        tmp_repo = committed_runner_repo(parent, ["codex"])
        repo = self.root / f"{name}-ws"
        shutil.move(str(tmp_repo), str(repo))
        (repo / "CONTEXT.md").write_text(
            "# CONTEXT\n\n## 1. Goal & red lines\n"
            "Maintain deterministic task gating and continuity while keeping CR-1 paused.\n\n"
            "## 2. Now\nActive IL-3 work.\n\n## 3. Last stable checkpoint\nClean.\n\n"
            "## 4. Next step\nVerify IL-3.\n\n## 5. Waiting on a human\nNone.\n",
            encoding="utf-8",
        )
        (repo / "TASK_QUEUE.md").write_text(
            "# TASK_QUEUE\n\n| # | ID | name | status | start baseline | end baseline |\n|---|---|---|---|---|---|\n"
            "| 1 | EAV-2 | fixture task | `TODO` | 1 | 2 |\n"
            "| 2 | CR-1 | Recover verified separately committed task implementations | `BLOCKED` | 247 | pending |\n",
            encoding="utf-8",
        )
        (repo / "flow").mkdir(exist_ok=True)
        (repo / "flow" / "continuity.py").write_text("# continuity\n", encoding="utf-8")
        (repo / "flow" / "release.py").write_text("# release\n", encoding="utf-8")
        (repo / "gate").mkdir(exist_ok=True)
        (repo / "gate" / "gate.py").write_text("# gate\n", encoding="utf-8")
        (repo / "gate" / "review_packet.py").write_text("# review\n", encoding="utf-8")
        (repo / "skills" / "flow-run").mkdir(parents=True)
        (repo / "skills" / "flow-run" / "SKILL.md").write_text("---\nname: flow-run\n---\n", encoding="utf-8")
        return repo

    def test_resume_with_learner_absent_or_empty_is_usable_readonly_and_quiet(self) -> None:
        repo = self._setup_flow_like_repo("absent-repo")
        before = _tree_digest(repo)

        # Learner explicitly disabled -> resume works, writes nothing, emits quiet normal output.
        res_disabled = _run_flow(
            "resume",
            "--repo",
            str(repo),
            env_extra={"FLOW_LEARNER_ROOT": str(self.storage), "FLOW_LEARNER_DISABLED": "1"},
        )
        self.assertEqual(res_disabled.returncode, 0, res_disabled.stderr)
        self.assertNotIn("Learning recommendations", res_disabled.stdout)
        self.assertEqual(_tree_digest(repo), before)
        self.assertFalse(self.storage.exists(), "resume must never initialize learner store")

        # Explicit --learning-status exposes absence without breaking JSON contract.
        res_json = _run_flow(
            "resume",
            "--repo",
            str(repo),
            "--json",
            "--learning-status",
            env_extra={"FLOW_LEARNER_ROOT": str(self.storage), "FLOW_LEARNER_DISABLED": "1"},
        )
        self.assertEqual(res_json.returncode, 0)
        payload = json.loads(res_json.stdout)
        self.assertIn("queue", payload)
        self.assertIn("gate", payload)
        self.assertFalse(payload["learning"]["available"])
        self.assertEqual(payload["learning"]["reason"], "learner_unavailable")

    def test_goal_level_onboarding_preserves_cr1_pause_and_isolates_sibling_dirs(self) -> None:
        repo = self._setup_flow_like_repo("flow-repo")
        sibling = self.root / "other-project-sibling"
        sibling.mkdir()
        (sibling / "CONTEXT.md").write_text("# Sibling\nPrivate trading operations.\n", encoding="utf-8")
        queue_before = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")

        res = _run_flow(
            "learn",
            "onboard",
            "--repo",
            str(repo),
            "--project-id",
            "flow-repo",
            "--goal",
            "Improve evaluation harnesses and governed skills",
            "--json",
            env_extra=self.env,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        onboarded = json.loads(res.stdout)
        self.assertEqual(onboarded["profile"]["project_id"], "flow-repo")
        self.assertTrue(onboarded["profile"]["provenance"]["cr1_paused"])
        self.assertIn("host session continuity resume checkpoint recall", onboarded["profile"]["existing_capabilities"])

        # Sibling directory was never discovered or registered.
        store = LearnerStore(self.storage)
        try:
            self.assertEqual([p.project_id for p in store.list_profiles()], ["flow-repo"])
        finally:
            store.close()
        self.assertEqual((repo / "TASK_QUEUE.md").read_text(encoding="utf-8"), queue_before)

        # Pause/resume learning never clears CR-1 pause or mutates TASK_QUEUE.md.
        p_res = _run_flow("learn", "pause", "--repo", str(repo), "--json", env_extra=self.env)
        self.assertEqual(p_res.returncode, 0, p_res.stderr)
        self.assertFalse(json.loads(p_res.stdout)["learning_enabled"])
        r_res = _run_flow("learn", "resume", "--repo", str(repo), "--json", env_extra=self.env)
        self.assertEqual(r_res.returncode, 0, r_res.stderr)
        self.assertTrue(json.loads(r_res.stdout)["learning_enabled"])
        self.assertIn("`BLOCKED`", (repo / "TASK_QUEUE.md").read_text(encoding="utf-8"))

    def test_offline_profile_drafts_provenance_and_separate_eligibility(self) -> None:
        from learner import draft_profile_from_docs

        flow_repo = self._setup_flow_like_repo("flow-draft")
        flow_draft = flow_learning.derive_flow_profile_draft(flow_learning.import_learner(), flow_repo).to_dict()

        alpha_repo = self.root / "alpha-app"
        alpha_repo.mkdir()
        (alpha_repo / "README.md").write_text("# Alpha App\nLocal trading backtest sandbox.\n", encoding="utf-8")
        alpha_base = draft_profile_from_docs("alpha-app", alpha_repo, name="Alpha App", adapter="generic")
        alpha_data = alpha_base.to_dict()
        alpha_data["profile"]["learning_enabled"] = False
        alpha_data["profile"]["exclusions"] = ["strategy promotion"]
        alpha_data["profile"]["provenance"]["eligible_for_host_activation"] = False

        beta_repo = self.root / "beta-service"
        beta_repo.mkdir()
        (beta_repo / "README.md").write_text("# Beta Service\nStatic portal docs.\n", encoding="utf-8")
        beta_base = draft_profile_from_docs("beta-service", beta_repo, name="Beta Service", adapter="generic")
        beta_data = beta_base.to_dict()
        beta_data["profile"]["learning_enabled"] = False
        beta_data["profile"]["exclusions"] = ["external live website source or DNS changes"]
        beta_data["profile"]["provenance"]["eligible_for_host_activation"] = False

        for draft in (flow_draft, alpha_data, beta_data):
            self.assertFalse(draft["activated"])
            self.assertEqual(draft["profile"]["status"], "draft")

        self.assertTrue(flow_draft["profile"]["provenance"]["eligible_for_host_activation"])
        self.assertTrue(flow_draft["profile"]["provenance"]["cr1_paused"])
        self.assertFalse(alpha_data["profile"]["learning_enabled"])
        self.assertFalse(alpha_data["profile"]["provenance"]["eligible_for_host_activation"])
        self.assertIn("strategy promotion", alpha_data["profile"]["exclusions"])
        self.assertFalse(beta_data["profile"]["learning_enabled"])
        self.assertFalse(beta_data["profile"]["provenance"]["eligible_for_host_activation"])
        self.assertIn("external live website source or DNS changes", beta_data["profile"]["exclusions"])

    def test_resume_surfaces_top3_quiet_no_inference_and_feedback_language_precedence(self) -> None:
        repo = self._setup_flow_like_repo("flow-active")
        res_on = _run_flow(
            "learn",
            "onboard",
            "--repo",
            str(repo),
            "--project-id",
            "flow-active",
            "--json",
            env_extra=self.env,
        )
        self.assertEqual(res_on.returncode, 0, res_on.stderr)

        # Quiet when 0 recommendations, explicit status shows coverage.
        quiet_txt = _run_flow("resume", "--repo", str(repo), env_extra=self.env).stdout
        self.assertNotIn("Learning recommendations", quiet_txt)
        status_txt = _run_flow("resume", "--repo", str(repo), "--learning-status", env_extra=self.env).stdout
        self.assertIn("## Learning status", status_txt)

        # Populate proposals via offline learner review (no live model/network).
        store = LearnerStore(self.storage)
        try:
            transport = FixtureTransport(_primary_routes_for_standard_fixtures())
            runner = FixtureModelRunner(_standard_model_handler())
            rev = run_review(store, transport, runner, project_id="flow-active", mode="project-led")
            self.assertEqual(rev["status"], "ok")
        finally:
            store.close()

        # Verify flow resume surfaces recommendation with required fields and zero network/subprocess calls.
        def _deny(*args, **kwargs):
            raise AssertionError("flow resume must never invoke network or subprocesses for inference")

        with (
            mock.patch.dict(os.environ, self.env),
            mock.patch.object(subprocess, "Popen", side_effect=_deny),
            mock.patch.object(socket, "create_connection", side_effect=_deny),
            mock.patch.object(urllib.request, "urlopen", side_effect=_deny),
        ):
            summary = flow_learning.resume_learning_summary(repo, storage_root=self.storage)
        self.assertTrue(summary["available"])
        self.assertEqual(len(summary["recommendations"]), 1)
        rec = summary["recommendations"][0]
        for key in ("benefit", "fit", "evidence", "uncertainty", "effort_risk", "next_action"):
            self.assertIn(key, rec)

        res_txt = _run_flow("resume", "--repo", str(repo), env_extra=self.env).stdout
        self.assertIn("Learning Recommendations", res_txt)
        self.assertIn("Expected benefit:", res_txt)
        self.assertIn("Recommended next action:", res_txt)

        # Natural-language feedback delegates to learner preferences without broadening mission.
        fb = _run_flow(
            "learn",
            "feedback",
            "--repo",
            str(repo),
            "--json",
            "focus more on evaluation",
            env_extra=self.env,
        )
        self.assertEqual(fb.returncode, 0, fb.stderr)
        fb_data = json.loads(fb.stdout)
        self.assertTrue(fb_data["mission_unchanged"])
        self.assertTrue(fb_data["authority_unchanged"])
        self.assertIn("evaluation", fb_data["preferences"]["focus_more_on"])

        # Language precedence: input fallback -> global default -> project pref -> explicit user language.
        res_fallback = _run_flow(
            "learn",
            "results",
            "--repo",
            str(repo),
            "--input-text",
            "\u8bf7\u67e5\u770b\u6539\u8fdb\u5efa\u8bae",
            "--json",
            env_extra=self.env,
        )
        self.assertEqual(json.loads(res_fallback.stdout)["user_language"], "zh")

        _run_flow(
            "learn",
            "feedback",
            "--repo",
            str(repo),
            "--user-language",
            "es",
            "--deliverable-language",
            "en",
            env_extra=self.env,
        )
        res_proj = _run_flow("learn", "results", "--repo", str(repo), "--json", env_extra=self.env)
        proj_lang_payload = json.loads(res_proj.stdout)
        self.assertEqual(proj_lang_payload["user_language"], "es")
        self.assertEqual(proj_lang_payload["deliverable_language"], "en")
        self.assertEqual(proj_lang_payload["internal_language"], "en")

        res_explicit = _run_flow(
            "learn",
            "results",
            "--repo",
            str(repo),
            "--user-language",
            "zh",
            "--json",
            env_extra=self.env,
        )
        self.assertEqual(json.loads(res_explicit.stdout)["user_language"], "zh")

    def test_onboarded_flow_checkout_cli_check_both_shared_unknown_and_live_trial_gate(self) -> None:
        import stat

        repo = self._setup_flow_like_repo("flow")
        (repo / "CONTEXT.md").write_text(
            "# CONTEXT\n\n## 1. Goal & red lines\n"
            "Implement independent learner and governed workflow change without altering model routing or CR-1 pause.\n",
            encoding="utf-8",
        )
        res_on = _run_flow(
            "learn",
            "onboard",
            "--repo",
            str(repo),
            "--project-id",
            "flow",
            "--goal",
            "Integrate independent learning with Flow continuity",
            "--json",
            env_extra=self.env,
        )
        self.assertEqual(res_on.returncode, 0, res_on.stderr)

        routes = _primary_routes_for_standard_fixtures()
        routes["https://api.github.com/repos/eval-org/harness-bench/releases*"] = TransportResponse(
            status_code=200,
            body=json.dumps(
                [
                    {
                        "tag_name": "v1.2.0",
                        "name": "Flow releases independent change",
                        "html_url": "https://github.com/eval-org/harness-bench/releases/tag/v1.2.0",
                        "published_at": "2026-11-10T00:00:00Z",
                        "body": "Independent flow releases change for routing and pause checks.",
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
            body=b"# harness-bench\nIndependent flow releases change with routing and pause support.\n",
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
        fixture_map = {
            url: {
                "status_code": resp.status_code,
                "body": resp.body.decode("utf-8"),
                "headers": resp.headers,
                "final_url": resp.final_url,
            }
            for url, resp in routes.items()
        }
        fixture_path = self.root / "flow_fixtures.json"
        fixture_path.write_text(json.dumps(fixture_map), encoding="utf-8")

        model_fixture_path = self.root / "flow_model_fixture.json"
        model_fixture_path.write_text(
            json.dumps(
                {
                    "tasks": {
                        "batch_triage": {
                            "qualify_contains": ["harness-bench"],
                            "goal_ref": "Integrate independent learning",
                            "capability_ref": "evaluation harness",
                            "qualify_rationale": "Adjacent offline evaluation harness.",
                            "uncertainty": "Need primary evidence.",
                        },
                        "independent_blind_assessment": {
                            "recommendation": "borrow",
                            "alternatives": ["Keep existing"],
                            "borrowable_change": "Borrow replay verification.",
                            "affected_existing_capability": "task admission",
                            "expected_benefit": "Improves harness reliability.",
                            "effort_and_risk": "Low effort.",
                            "test_baseline_rollback": {
                                "test_plan": "unittest",
                                "baseline": "current tests",
                                "rollback": "git revert",
                            },
                            "unresolved_claims": [],
                            "uncertainty": "Offline only.",
                            "claim_kind": "model_inference",
                        },
                        "bounded_proposal_synthesis": {
                            "recommendation": "borrow",
                            "alternatives": ["Keep existing"],
                            "borrowable_change": "Borrow replay verification.",
                            "affected_existing_capability": "task admission",
                            "expected_benefit": "Improves harness reliability.",
                            "effort_and_risk": "Low effort.",
                            "test_baseline_rollback": {
                                "test_plan": "unittest",
                                "baseline": "current tests",
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

        chk = _run_flow(
            "learn",
            "check",
            "--repo",
            str(repo),
            "--mode",
            "both",
            "--fixture-map",
            str(fixture_path),
            "--model-fixture",
            str(model_fixture_path),
            "--json",
            env_extra=self.env,
        )
        self.assertEqual(chk.returncode, 0, chk.stderr)
        chk_payload = json.loads(chk.stdout)
        self.assertEqual(chk_payload["status"], "ok", chk_payload)
        self.assertGreaterEqual(chk_payload["assessment"]["proposed"], 1)

        # Shared unknown dispatch for project 'flow' must surface in both `flow learn status` and `flow resume --learning-status`.
        store = LearnerStore(self.storage)
        try:
            r_unk, _ = store.reserve_dispatch(
                idempotency_key="shared-triage:flow:unk-1",
                project_id="__shared__",
                scope_type="shared_triage",
                budget_bucket="daily_calls",
                units=1,
                target_project_ids=("flow",),
                step_keys=("triage:flow:x1",),
            )
            store.mark_dispatched(r_unk.reservation_id)
            store.mark_unknown(r_unk.reservation_id, error="timeout")
        finally:
            store.close()

        st_res = _run_flow("learn", "status", "--repo", str(repo), "--json", env_extra=self.env)
        self.assertEqual(st_res.returncode, 0, st_res.stderr)
        st_json = json.loads(st_res.stdout)
        self.assertEqual(len(st_json["unknown_dispatches"]), 1)
        self.assertEqual(st_json["unknown_dispatches"][0]["reservation_id"], r_unk.reservation_id)

        res_st = _run_flow("resume", "--repo", str(repo), "--json", "--learning-status", env_extra=self.env)
        self.assertEqual(res_st.returncode, 0, res_st.stderr)
        res_json = json.loads(res_st.stdout)
        self.assertEqual(len(res_json["learning"]["unknown_dispatches"]), 1)

        # `--live` without active trial grant must NEVER invoke fake CLI subprocess!
        marker = self.root / "live_called.txt"
        fake_cli = self.root / "agy-flow"
        fake_cli.write_text(
            f"#!{sys.executable}\n"
            "import pathlib\n"
            f"pathlib.Path({str(marker)!r}).write_text('CALLED', encoding='utf-8')\n",
            encoding="utf-8",
        )
        os.chmod(fake_cli, os.stat(fake_cli).st_mode | stat.S_IEXEC)
        policy_path = self.root / "policy.json"
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
        live_res = _run_flow(
            "learn",
            "check",
            "--repo",
            str(repo),
            "--mode",
            "project-led",
            "--live",
            "--policy-path",
            str(policy_path),
            "--cli-path",
            str(fake_cli),
            "--json",
            env_extra=self.env,
        )
        # No live network in sandbox; regardless of fetch outcome, live CLI must never be spawned without active trial grant.
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()

"""Offline tests for transactional budget concurrency, crash recovery, and shared-fetch accounting."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from learner.contracts import BudgetPolicy, DispatchBlockedError, LeaseConflictError
from learner.pipeline import run_intake
from learner.store import LearnerStore
from learner.tests.fixtures import build_standard_transport, make_profile


class BudgetAndRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.now = 1_794_800_000.0
        self.store = LearnerStore(self.root / "store", clock=lambda: self.now)

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def test_transactional_budget_concurrency_never_exceeds_cap(self) -> None:
        p = make_profile(budget=BudgetPolicy(daily_calls=3, weekly_deep_calls=2, daily_fetches=5))
        self.store.register_profile(p)

        def attempt(idx: int) -> bool:
            try:
                res, should_dispatch = self.store.reserve_dispatch(
                    idempotency_key=f"concurrent-call-{idx}",
                    project_id=p.project_id,
                    scope_type="project_call",
                    budget_bucket="daily_calls",
                    units=1,
                )
                if should_dispatch:
                    self.store.mark_dispatched(res.reservation_id)
                    self.store.settle_dispatch(res.reservation_id, actual_units=1, receipt={"idx": idx})
                    return True
                return False
            except DispatchBlockedError:
                return False

        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(attempt, range(10)))

        self.assertEqual(sum(1 for ok in outcomes if ok), 3)
        self.assertEqual(self.store.budget_used(p.project_id, "daily_calls"), 3)

        # Deterministic clock seam advances UTC day -> fresh window available.
        self.now += 86_400.0
        self.assertEqual(self.store.budget_used(p.project_id, "daily_calls"), 0)
        res, should_dispatch = self.store.reserve_dispatch(
            idempotency_key="next-day-call",
            project_id=p.project_id,
            scope_type="project_call",
            budget_bucket="daily_calls",
            units=1,
        )
        self.assertTrue(should_dispatch)
        self.store.settle_dispatch(res.reservation_id, actual_units=1)

    def test_crash_unknown_prevents_blind_retry_and_requires_reconciliation(self) -> None:
        p = make_profile(budget=BudgetPolicy(daily_calls=2, weekly_deep_calls=1, daily_fetches=5))
        self.store.register_profile(p)

        res, should_dispatch = self.store.reserve_dispatch(
            idempotency_key="call-abc-1",
            project_id=p.project_id,
            scope_type="project_call",
            budget_bucket="daily_calls",
            units=1,
            ttl_s=60.0,
        )
        self.assertTrue(should_dispatch)
        self.store.mark_dispatched(res.reservation_id)

        # Simulate crash past lease TTL before receipt is recorded.
        self.now += 120.0
        recovered = self.store.recover_stale_dispatches()
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].state, "unknown")

        # Unknown call counts against budget and blocks blind retry on same idempotency_key.
        self.assertEqual(self.store.budget_used(p.project_id, "daily_calls"), 1)
        with self.assertRaises(DispatchBlockedError) as ctx:
            self.store.reserve_dispatch(
                idempotency_key="call-abc-1",
                project_id=p.project_id,
                scope_type="project_call",
                budget_bucket="daily_calls",
                units=1,
            )
        self.assertEqual(ctx.exception.reason, "unknown_pending_reconciliation")

        # Explicit reconciliation resolves unknown state accurately.
        reconciled = self.store.reconcile_dispatch(
            res.reservation_id,
            outcome="failed",
            actual_units=0,
            note="provider_confirmed_not_executed",
        )
        self.assertEqual(reconciled.state, "failed")
        self.assertEqual(reconciled.actual_units, 0)
        self.assertEqual(self.store.budget_used(p.project_id, "daily_calls"), 0)

    def test_no_per_project_double_charging_shared_fetches(self) -> None:
        alpha = make_profile(
            project_id="alpha-proj",
            budget=BudgetPolicy(daily_calls=5, weekly_deep_calls=2, daily_fetches=1),
        )
        beta = make_profile(
            project_id="beta-proj",
            name="Beta Workspace",
            root_path="/tmp/beta-root",
            mission="Optimize Beta Workspace",
            private_terms=("BETA_SENTINEL",),
            budget=BudgetPolicy(daily_calls=5, weekly_deep_calls=2, daily_fetches=1),
        )
        self.store.register_profile(alpha)
        self.store.register_profile(beta)

        transport = build_standard_transport()
        res = run_intake(self.store, "opportunity-led", transport)
        self.assertEqual(res["status"], "ok")
        # 1 Trending + 1 Popular + 1 arXiv = 3 HTTP requests total (not 6 for 2 projects).
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(self.store.budget_used("__shared__", "daily_fetches"), 1)
        self.assertEqual(self.store.budget_used("alpha-proj", "daily_fetches"), 0)
        self.assertEqual(self.store.budget_used("beta-proj", "daily_fetches"), 0)

        # Second sweep on same day hits shared daily_fetches cap=1 and preserves queued items.
        res2 = run_intake(self.store, "opportunity-led", transport)
        self.assertEqual(res2["status"], "blocked")
        self.assertEqual(res2["reason"], "budget_exhausted")
        self.assertGreaterEqual(res2["queued_preserved"], 4)
        self.assertEqual(len(transport.calls), 3)

    def test_disabled_learning_and_overlap_lease_preserve_queued_work(self) -> None:
        p = make_profile()
        self.store.register_profile(p)
        transport = build_standard_transport()
        run_intake(self.store, "opportunity-led", transport)
        queued_before = len(self.store.list_assessments(p.project_id))
        self.assertGreaterEqual(queued_before, 2)

        self.store.set_learning_enabled(p.project_id, False)
        blocked = run_intake(self.store, "project-led", transport, project_id=p.project_id)
        self.assertEqual(blocked["status"], "blocked")
        self.assertEqual(blocked["reason"], "learning_disabled")
        self.assertEqual(blocked["queued_preserved"], queued_before)
        with self.assertRaises(DispatchBlockedError) as ctx:
            self.store.reserve_dispatch(
                idempotency_key="disabled-call",
                project_id=p.project_id,
                scope_type="project_call",
                budget_bucket="daily_calls",
            )
        self.assertEqual(ctx.exception.reason, "learning_disabled")

        # Overlap lease conflict blocks concurrent intake runner.
        self.store.set_learning_enabled(p.project_id, True)
        self.store.acquire_lease("intake:project-led:alpha-proj", "other-worker", ttl_s=120.0)
        with self.assertRaises(LeaseConflictError):
            run_intake(self.store, "project-led", transport, project_id=p.project_id, lease_owner="second-worker")

    def test_crashed_shared_fetch_blocks_across_restarts_and_day_boundaries_until_reconciled(self) -> None:
        from learner.cli import main as cli_main
        import io
        import json
        from contextlib import redirect_stdout

        p = make_profile(project_id="flow", name="flow")
        self.store.register_profile(p)

        # Simulate a process crash after mark_dispatched in shared_fetch.
        res, should = self.store.reserve_dispatch(
            idempotency_key="shared-fetch:opportunity-led:__shared__:a1",
            project_id="__shared__",
            scope_type="shared_fetch",
            budget_bucket="daily_fetches",
            units=1,
            ttl_s=60.0,
            target_project_ids=("flow",),
            step_keys=("shared-fetch:opportunity-led:__shared__",),
        )
        self.assertTrue(should)
        self.store.mark_dispatched(res.reservation_id)
        self.store.close()

        # Advance past lease expiry AND across UTC day boundary, then reopen store.
        self.now += 86_500.0
        reopened = LearnerStore(self.root / "store", clock=lambda: self.now)
        try:
            transport = build_standard_transport()
            calls_before = len(transport.calls)
            # Entrypoint automatically recovers stale dispatched -> unknown and blocks retry!
            blocked = run_intake(
                reopened,
                "opportunity-led",
                transport,
                release_repos=("other-org/other-repo",),
            )
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["reason"], "unknown_pending_reconciliation")
            self.assertEqual(len(transport.calls), calls_before)
        finally:
            reopened.close()

        # Project-scoped CLI status must surface relevant __shared__ unknown dispatches.
        st_buf = io.StringIO()
        with redirect_stdout(st_buf):
            rc = cli_main(["--storage-root", str(self.root / "store"), "--json", "status", "--project-id", "flow"])
        self.assertEqual(rc, 0)
        st_payload = json.loads(st_buf.getvalue())
        self.assertEqual(st_payload["assessment_status"], "unknown_pending_reconciliation")
        self.assertEqual(len(st_payload["unknown_dispatches"]), 1)
        self.assertEqual(st_payload["unknown_dispatches"][0]["reservation_id"], res.reservation_id)

        # Explicit CLI reconcile unblocks subsequent intake cleanly.
        with redirect_stdout(io.StringIO()):
            rc_rec = cli_main(
                [
                    "--storage-root",
                    str(self.root / "store"),
                    "--json",
                    "reconcile",
                    "--reservation-id",
                    res.reservation_id,
                    "--outcome",
                    "failed",
                    "--actual-units",
                    "0",
                    "--note",
                    "crashed_before_fetch",
                ]
            )
        self.assertEqual(rc_rec, 0)

        reopened2 = LearnerStore(self.root / "store", clock=lambda: self.now)
        try:
            res_ok = run_intake(reopened2, "opportunity-led", build_standard_transport())
            self.assertEqual(res_ok["status"], "ok")
        finally:
            reopened2.close()

    def test_bug_bb0e0f1bb3c1_trial_check_precedes_bucket_exhaustion_without_mutation(self) -> None:
        p = make_profile(budget=BudgetPolicy(daily_calls=1, weekly_deep_calls=1, daily_fetches=1))
        self.store.register_profile(p)
        self.store.set_trial_grant(
            max_model_calls=10,
            expires_at=self.now + 3600.0,
            grant_id="bug-bb0e-finite",
        )

        # Exhaust both daily_calls=1 and daily_fetches=1 on the current UTC day.
        call_res, should_call = self.store.reserve_dispatch(
            idempotency_key="exhaust-daily-call",
            project_id=p.project_id,
            scope_type="deep_hydrogen",
            budget_bucket="daily_calls",
            units=1,
            require_trial_grant=True,
        )
        self.assertTrue(should_call)
        self.store.mark_dispatched(call_res.reservation_id)
        self.store.settle_dispatch(call_res.reservation_id, actual_units=1)

        fetch_res, should_fetch = self.store.reserve_dispatch(
            idempotency_key="exhaust-daily-fetch",
            project_id="__shared__",
            scope_type="shared_fetch",
            budget_bucket="daily_fetches",
            units=1,
            cap_override=1,
        )
        self.assertTrue(should_fetch)
        self.store.mark_dispatched(fetch_res.reservation_id)
        self.store.settle_dispatch(fetch_res.reservation_id, actual_units=1)

        # Unknown and inflight reservations to verify preserved behavior.
        unk_res, _ = self.store.reserve_dispatch(
            idempotency_key="unk-key",
            project_id="__shared__",
            scope_type="shared_fetch",
            budget_bucket="daily_fetches",
            units=1,
            cap_override=5,
        )
        self.store.mark_unknown(unk_res.reservation_id, error="timed_out")
        inf_res, _ = self.store.reserve_dispatch(
            idempotency_key="inf-key",
            project_id="__shared__",
            scope_type="shared_fetch",
            budget_bucket="daily_fetches",
            units=1,
            cap_override=5,
        )

        def _snapshot() -> tuple[int, int, int, int]:
            return (
                len(self.store.list_dispatches()),
                self.store.budget_used(p.project_id, "daily_calls"),
                self.store.budget_used("__shared__", "daily_fetches"),
                self.store.get_trial_status()["cumulative_model_calls"],
            )

        # 1. Revoked finite grant must raise trial_revoked BEFORE daily_calls bucket exhaustion.
        self.store.revoke_trial_grant()
        snap_before = _snapshot()
        with self.assertRaises(DispatchBlockedError) as rev_fin_ctx:
            self.store.reserve_dispatch(
                idempotency_key="after-revoked-finite-exhausted-bucket",
                project_id=p.project_id,
                scope_type="deep_hydrogen",
                budget_bucket="daily_calls",
                units=1,
                require_trial_grant=True,
            )
        self.assertEqual(rev_fin_ctx.exception.reason, "trial_revoked")
        self.assertEqual(_snapshot(), snap_before)

        # 2. Revoked unlimited grant must raise trial_revoked BEFORE bucket exhaustion.
        self.store.set_trial_grant(
            unlimited=True,
            grant_id="bug-bb0e-unlim",
            authority="Owner test",
        )
        self.store.revoke_trial_grant()
        snap_before = _snapshot()
        with self.assertRaises(DispatchBlockedError) as rev_unlim_ctx:
            self.store.reserve_dispatch(
                idempotency_key="after-revoked-unlim-exhausted-bucket",
                project_id=p.project_id,
                scope_type="deep_hydrogen",
                budget_bucket="daily_calls",
                units=1,
                require_trial_grant=True,
            )
        self.assertEqual(rev_unlim_ctx.exception.reason, "trial_revoked")
        self.assertEqual(_snapshot(), snap_before)

        # 3. Expired finite grant must raise trial_expired BEFORE bucket exhaustion.
        self.store.set_trial_grant(
            max_model_calls=10,
            expires_at=self.now + 10.0,
            grant_id="bug-bb0e-expiring",
        )
        self.now += 20.0
        snap_before = _snapshot()
        with self.assertRaises(DispatchBlockedError) as exp_ctx:
            self.store.reserve_dispatch(
                idempotency_key="after-expired-exhausted-bucket",
                project_id=p.project_id,
                scope_type="deep_hydrogen",
                budget_bucket="daily_calls",
                units=1,
                require_trial_grant=True,
            )
        self.assertEqual(exp_ctx.exception.reason, "trial_expired")
        self.assertEqual(_snapshot(), snap_before)

        # 4. Non-model fetch semantics remain governed by daily_fetches (not trial grant).
        snap_before = _snapshot()
        with self.assertRaises(DispatchBlockedError) as fetch_blk:
            self.store.reserve_dispatch(
                idempotency_key="non-model-fetch-over-cap",
                project_id="__shared__",
                scope_type="shared_fetch",
                budget_bucket="daily_fetches",
                units=1,
                cap_override=1,
                require_trial_grant=True,
            )
        self.assertEqual(fetch_blk.exception.reason, "budget_exhausted")
        self.assertEqual(_snapshot(), snap_before)

        # Non-model fetch succeeds when cap allows even while trial grant is expired.
        ok_fetch, ok_should = self.store.reserve_dispatch(
            idempotency_key="non-model-fetch-allowed",
            project_id="__shared__",
            scope_type="shared_fetch",
            budget_bucket="daily_fetches",
            units=1,
            cap_override=10,
            require_trial_grant=True,
        )
        self.assertTrue(ok_should)
        self.store.settle_dispatch(ok_fetch.reservation_id, actual_units=1)

        # 5. Settled idempotent reuse, unknown, and inflight refusal are preserved even with expired grant.
        reused, should_reuse = self.store.reserve_dispatch(
            idempotency_key="exhaust-daily-call",
            project_id=p.project_id,
            scope_type="deep_hydrogen",
            budget_bucket="daily_calls",
            units=1,
            require_trial_grant=True,
        )
        self.assertFalse(should_reuse)
        self.assertEqual(reused.reservation_id, call_res.reservation_id)
        with self.assertRaises(DispatchBlockedError) as unk_ctx:
            self.store.reserve_dispatch(
                idempotency_key="unk-key",
                project_id="__shared__",
                scope_type="shared_fetch",
                budget_bucket="daily_fetches",
                units=1,
            )
        self.assertEqual(unk_ctx.exception.reason, "unknown_pending_reconciliation")
        with self.assertRaises(DispatchBlockedError) as inf_ctx:
            self.store.reserve_dispatch(
                idempotency_key="inf-key",
                project_id="__shared__",
                scope_type="shared_fetch",
                budget_bucket="daily_fetches",
                units=1,
            )
        self.assertEqual(inf_ctx.exception.reason, "duplicate_inflight_dispatch")
        self.assertEqual(inf_res.state, "reserved")

        # 6. Inactive store with exhausted bucket and require_trial_grant=True raises trial_inactive.
        inactive_store = LearnerStore(self.root / "inactive-store", clock=lambda: self.now)
        try:
            inactive_store.register_profile(p)
            r0, _ = inactive_store.reserve_dispatch(
                idempotency_key="no-trial-fill-bucket",
                project_id=p.project_id,
                scope_type="deep_hydrogen",
                budget_bucket="daily_calls",
                units=1,
                require_trial_grant=False,
            )
            inactive_store.settle_dispatch(r0.reservation_id, actual_units=1)
            with self.assertRaises(DispatchBlockedError) as inact_ctx:
                inactive_store.reserve_dispatch(
                    idempotency_key="no-trial-require-grant",
                    project_id=p.project_id,
                    scope_type="deep_hydrogen",
                    budget_bucket="daily_calls",
                    units=1,
                    require_trial_grant=True,
                )
            self.assertEqual(inact_ctx.exception.reason, "trial_inactive")
            self.assertEqual(len(inactive_store.list_dispatches()), 1)
        finally:
            inactive_store.close()


if __name__ == "__main__":
    unittest.main()

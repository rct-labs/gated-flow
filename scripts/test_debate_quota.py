"""Offline behavior tests for the reusable debate allowance/recovery boundary."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/model-debate/scripts"
sys.path.insert(0, str(SCRIPTS))
import quota_guard as g
import quota_probe as p

NOW = datetime(2026, 9, 9, tzinfo=timezone.utc)


def plan():
    pools = {}
    models = {}
    for key in ("a", "b", "c", "d"):
        pools[key] = {"observer": {"adapter": "grok", "executable": key + ".exe", "model": key},
                      "per_call": {"percent": 1}, "reserve": {"percent": 20},
                      "estimate_basis": "declared_bootstrap", "estimate_evidence": "fixture declaration"}
        models[key] = {"model": key, "family": key, "pool": key, "identity_evidence": "fixture.json"}
    return {"version": 1, "min_models": 3, "project_min_models": 3, "max_calls": 18, "used_calls": 0,
            "max_age_seconds": 60, "safety_factor": 1.5, "excluded_models": [], "excluded_pools": [],
            "pools": pools, "models": models,
            "seats": [{"id": key, "candidates": [key], "remaining_calls": 3} for key in ("a", "b", "c")]}


def samples(doc, remaining=80):
    return {key: {"pool": key, "status": "KNOWN", "observer_digest": g.digest(pool["observer"]),
                  "observed_at": NOW.isoformat(), "windows": [
                      {"name": "weekly", "remaining": remaining, "unit": "percent",
                       "reset_at": (NOW + timedelta(days=3)).isoformat()}]}
            for key, pool in doc["pools"].items()}


def failure():
    return {"kind": "QUOTA_EXHAUSTED", "model_ref": "c", "stage": "round-2",
            "assignments": {"a": "a", "b": "b", "c": "c"}, "process_reconciled": True,
            "read_only_verified": True, "context_unchanged": True, "independence_verified": True,
            "replacement_authorized": True, "no_web": False}


class BudgetTests(unittest.TestCase):
    def test_complete_roster_fits(self):
        doc = plan()
        result = g.evaluate(doc, samples(doc), NOW)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["assignments"], {"a": "a", "b": "b", "c": "c"})
        self.assertEqual(result["pool_checks"][0]["windows"][0]["required"], 24.5)

    def test_98_99_used_never_starts(self):
        for left in (2, 1, 0):
            with self.subTest(left=left):
                doc = plan(); snap = samples(doc); snap["a"]["windows"][0]["remaining"] = left
                self.assertEqual(g.evaluate(doc, snap, NOW)["status"], "BLOCKED")

    def test_unknown_stale_future_wrong_identity_and_route_refuse(self):
        mutators = [lambda s: s.update(status="UNKNOWN"),
                    lambda s: s.update(observed_at=(NOW-timedelta(seconds=61)).isoformat()),
                    lambda s: s.update(observed_at=(NOW+timedelta(seconds=1)).isoformat()),
                    lambda s: s.update(pool="foreign"), lambda s: s.update(observer_digest="changed")]
        for change in mutators:
            doc = plan(); snap = samples(doc); change(snap["a"])
            self.assertEqual(g.evaluate(doc, snap, NOW)["status"], "BLOCKED")

    def test_invalid_values_refuse(self):
        for value in (True, None, -1, 101, float("nan"), float("inf"), "bad"):
            with self.subTest(value=value):
                doc = plan(); snap = samples(doc); snap["a"]["windows"][0]["remaining"] = value
                self.assertEqual(g.evaluate(doc, snap, NOW)["status"], "BLOCKED")

    def test_missing_duplicate_expired_and_wrong_unit_refuse(self):
        for mode in ("empty", "duplicate", "expired", "unit"):
            doc = plan(); snap = samples(doc); windows = snap["a"]["windows"]
            if mode == "empty": windows.clear()
            if mode == "duplicate": windows.append(deepcopy(windows[0]))
            if mode == "expired": windows[0]["reset_at"] = NOW.isoformat()
            if mode == "unit": windows[0]["unit"] = "CNY"
            self.assertEqual(g.evaluate(doc, snap, NOW)["status"], "BLOCKED")

    def test_shared_account_budget_is_aggregated(self):
        doc = plan(); doc["models"]["b"]["pool"] = "a"
        snap = samples(doc, remaining=26)
        result = g.evaluate(doc, snap, NOW)
        self.assertEqual(result["status"], "BLOCKED")
        check = next(c for c in result["pool_checks"] if c["pool"] == "a")
        self.assertEqual(check["windows"][0]["required"], 29)

    def test_inflight_calls_count(self):
        doc = plan(); doc["pools"]["a"]["inflight_calls"] = 2
        self.assertEqual(g.evaluate(doc, samples(doc, 26), NOW)["status"], "BLOCKED")

    def test_probes_retries_and_spent_calls_count_in_cap(self):
        doc = plan(); doc["used_calls"] = 10
        with self.assertRaisesRegex(g.GuardError, "CALL_BUDGET"):
            g.validate(doc)

    def test_exclusion_prevents_any_probe(self):
        doc = plan(); doc["excluded_models"] = ["a"]
        doc["excluded_pools"] = ["b"]
        seen = []
        def reader(key, pool):
            seen.append(key)
            return samples(doc)[key]
        g.observe(doc, reader)
        self.assertEqual(seen, ["c"])

    def test_exclusion_applies_to_alias_and_family(self):
        for blocked in ("actual-a", "family-a", "a"):
            doc = plan(); doc["models"]["a"].update(model="actual-a", family="family-a")
            doc["excluded_models"] = [blocked]
            self.assertTrue(g.excluded(doc, "a"))

    def test_excluding_one_known_alias_excludes_the_underlying_model(self):
        doc = plan(); doc["models"]["d"]["family"] = "a"
        doc["seats"][0]["candidates"] = ["d"]
        doc["excluded_models"] = ["a"]
        self.assertTrue(g.excluded(doc, "d"))
        seen = []
        g.observe(doc, lambda key, pool: seen.append(key) or samples(doc)[key])
        self.assertNotIn("d", seen)

    def test_completed_seat_needs_no_remaining_allowance(self):
        doc = plan(); doc["seats"][0]["remaining_calls"] = 0
        snap = samples(doc); snap.pop("a")
        self.assertEqual(g.evaluate(doc, snap, NOW)["status"], "READY")

    def test_last_call_can_transfer_to_inflight_without_false_rejection(self):
        doc = plan(); doc["seats"][0]["remaining_calls"] = 0
        doc["pools"]["a"]["inflight_calls"] = 1
        result = g.evaluate(doc, samples(doc), NOW)
        self.assertEqual(result["status"], "READY")
        self.assertEqual(result["pool_checks"][0]["windows"][0]["calls"], 1)

    def test_project_quorum_cannot_be_lowered(self):
        doc = plan(); doc["min_models"] = 2
        with self.assertRaisesRegex(g.GuardError, "PROJECT_QUORUM"):
            g.validate(doc)

    def test_aliases_do_not_count_as_distinct_models(self):
        doc = plan(); doc["models"]["b"]["family"] = "a"
        self.assertEqual(g.evaluate(doc, samples(doc), NOW)["status"], "BLOCKED")

    def test_whitespace_unknown_identity_cannot_supply_quorum(self):
        doc = plan(); doc["models"]["c"]["family"] = " UNKNOWN "
        with self.assertRaisesRegex(g.GuardError, "MODEL_IDENTITY_UNVERIFIED"):
            g.evaluate(doc, samples(doc), NOW)

    def test_backup_priority_and_fresh_quota(self):
        doc = plan(); doc["seats"][2]["candidates"] += ["d"]
        snap = samples(doc); snap["c"]["status"] = "UNKNOWN"
        self.assertEqual(g.evaluate(doc, snap, NOW)["assignments"]["c"], "d")
        snap["d"]["observed_at"] = (NOW-timedelta(minutes=2)).isoformat()
        self.assertEqual(g.evaluate(doc, snap, NOW)["status"], "BLOCKED")

    def test_frozen_roster_never_silently_selects_backup(self):
        doc = plan(); doc["seats"][2]["candidates"] += ["d"]
        doc["frozen_assignments"] = {"a": "a", "b": "b", "c": "c"}
        snap = samples(doc); snap["c"]["status"] = "UNKNOWN"
        self.assertEqual(g.evaluate(doc, snap, NOW)["status"], "BLOCKED")
        seen = []
        g.observe(doc, lambda key, pool: seen.append(key) or snap[key])
        self.assertNotIn("d", seen)

    def test_money_preserves_currency_and_margin(self):
        doc = plan(); pool = doc["pools"]["c"]
        pool.update(observer={"adapter": "deepseek", "config_path": "settings.toml"},
                    per_call={"CNY": 2}, reserve={"CNY": 20})
        snap = samples(doc); snap["c"]["windows"] = [{"name": "balance", "unit": "CNY", "remaining": "28", "reset_at": None}]
        self.assertEqual(g.evaluate(doc, snap, NOW)["status"], "BLOCKED")
        snap["c"]["windows"][0]["remaining"] = "29"
        self.assertEqual(g.evaluate(doc, snap, NOW)["status"], "READY")

    def test_duplicate_route_cannot_multiply_account_budget(self):
        doc = plan(); doc["pools"]["b"]["observer"]["executable"] = "a.exe"
        with self.assertRaisesRegex(g.GuardError, "DUPLICATE_POOL_ROUTE"):
            g.validate(doc)

    def test_bootstrap_estimate_must_be_explicit(self):
        doc = plan(); doc["pools"]["a"]["estimate_evidence"] = ""
        with self.assertRaises(g.GuardError): g.validate(doc)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.doc = plan(); self.doc["seats"][2]["candidates"] += ["d"]

    def test_replacement_quarantines_pool_and_rebuilds_all_second_round(self):
        action, revised = g.recovery_plan(self.doc, failure())
        self.assertEqual(action["status"], "PREFLIGHT_REQUIRED")
        self.assertIn("c", revised["excluded_pools"])
        self.assertIn("all round2", action["invalidate"])
        result = g.evaluate(revised, samples(revised), NOW)
        self.assertEqual(result["assignments"]["c"], "d")

    def test_original_plan_and_failure_not_mutated(self):
        old = deepcopy(self.doc); record = failure(); before = deepcopy(record)
        g.recovery_plan(self.doc, record)
        self.assertEqual(old, self.doc); self.assertEqual(before, record)

    def test_no_authorization_no_probe(self):
        record = failure(); record["replacement_authorized"] = False
        result = g.execute(self.doc, record, reader=lambda *a: self.fail("probe called"))
        self.assertEqual(result["reason"], "ROSTER_CHANGE_AUTHORIZATION_MISSING")

    def test_unknown_process_or_write_boundary_blocks(self):
        for flag in ("process_reconciled", "read_only_verified"):
            record = failure(); record[flag] = False
            action, revised = g.recovery_plan(self.doc, record)
            self.assertEqual(action["reason"], "RECONCILIATION_REQUIRED"); self.assertIsNone(revised)

    def test_changed_input_or_peer_exposure_requires_rebase(self):
        for flag in ("context_unchanged", "independence_verified"):
            record = failure(); record[flag] = False
            self.assertEqual(g.recovery_plan(self.doc, record)[0]["reason"], "REBASE_INPUTS_BEFORE_RECOVERY")

    def test_short_rate_limit_waits_within_budget(self):
        record = failure(); record.update(kind="RATE_LIMIT", retry_after_seconds=20,
                                         max_wait_seconds=30, retry_budget_available=True)
        self.assertEqual(g.recovery_plan(self.doc, record)[0]["status"], "WAIT")
        record["retry_after_seconds"] = 45
        self.assertEqual(g.recovery_plan(self.doc, record)[0]["status"], "BLOCKED")

    def test_unknown_429_does_not_switch(self):
        record = failure(); record["kind"] = "UNKNOWN_FAILURE"
        self.assertEqual(g.recovery_plan(self.doc, record)[0]["reason"], "REPAIR_REQUIRED")

    def test_replacement_fresh_work_must_fit_budget(self):
        self.doc["seats"][2]["remaining_calls"] = 1
        with self.assertRaisesRegex(g.GuardError, "REPLACEMENT_WORK_NOT_BUDGETED"):
            g.recovery_plan(self.doc, failure())

    def test_replacement_must_keep_three_models(self):
        self.doc["models"]["d"]["family"] = "a"
        _, revised = g.recovery_plan(self.doc, failure())
        self.assertEqual(g.evaluate(revised, samples(revised), NOW)["status"], "BLOCKED")

    def test_other_seat_never_changes_implicitly(self):
        self.doc["seats"][0]["candidates"] += ["d"]
        _, revised = g.recovery_plan(self.doc, failure())
        self.assertEqual(revised["seats"][0]["candidates"], ["a"])

    def test_frozen_roster_changes_only_on_authorized_recovery(self):
        self.doc["frozen_assignments"] = failure()["assignments"]
        action, revised = g.recovery_plan(self.doc, failure())
        self.assertNotIn("frozen_assignments", revised)
        self.assertEqual(g.evaluate(revised, samples(revised), NOW)["assignments"]["c"], "d")
        self.assertIn("c", action["required_plan_changes"]["excluded_pools"])

    def test_successful_second_round_must_budget_its_repeat(self):
        self.doc["seats"][0]["remaining_calls"] = 0
        with self.assertRaisesRegex(g.GuardError, "REPEATED_ROUND2_NOT_BUDGETED"):
            g.recovery_plan(self.doc, failure())


class NativeNormalizationTests(unittest.TestCase):
    def test_codex_all_windows_and_bucket_selection(self):
        window = {"usedPercent": 12, "resetsAt": 2000000000}
        snap = {"primary": window, "secondary": {**window, "usedPercent": 98},
                "individualLimit": {"remainingPercent": 70, "resetsAt": 2000000000}}
        result = p.codex_windows({"rateLimitsByLimitId": {"codex": snap}}, "codex")
        self.assertEqual([w["remaining"] for w in result], [88, 2, 70])
        with self.assertRaises(KeyError): p.codex_windows({"rateLimitsByLimitId": {"other": snap}}, "codex")

    def test_codex_reached_cap_refuses(self):
        with self.assertRaisesRegex(p.ProbeError, "QUOTA_EXHAUSTED"):
            p.codex_windows({"rateLimitsByLimitId": {"codex": {"spendControlReached": True}}}, "codex")

    def test_codex_explicit_single_window_response_is_supported(self):
        snap = {"primary": {"usedPercent": 71, "resetsAt": 2000000000}, "secondary": None}
        result = p.codex_windows({"rateLimitsByLimitId": {"codex": snap}}, "codex")
        self.assertEqual(len(result), 1); self.assertEqual(result[0]["remaining"], 29)
        del snap["secondary"]
        with self.assertRaises(KeyError):
            p.codex_windows({"rateLimitsByLimitId": {"codex": snap}}, "codex")

    def test_wrong_native_shape_is_typed_unknown(self):
        with patch.object(p, "rpc", return_value=[{}, []]):
            doc = {"observer": {"adapter": "codex", "executable": "codex.exe", "limit_id": "codex"}}
            self.assertEqual(p.probe("x", doc)["status"], "UNKNOWN")

    def test_grok_percent_means_used(self):
        result = p.grok_windows({"config": {"creditUsagePercent": 15, "currentPeriod": {
            "type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2000-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z"}}})
        self.assertEqual(result[0]["remaining"], 85)

    def test_native_failure_never_echoes_secret(self):
        with patch.object(p, "rpc", side_effect=OSError("PRIVATE-KEY-DO-NOT-ECHO")):
            result = p.probe("a", plan()["pools"]["a"])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_deepseek_requires_existing_route_authority_before_key_read(self):
        with self.assertRaisesRegex(p.ProbeError, "NOT_AUTHORIZED"):
            p.deepseek_balance({})

    def test_deadline_failure_is_unknown(self):
        pool = {"observer": {"adapter": "deepseek"}}
        with patch.object(p, "deepseek_balance", side_effect=subprocess.TimeoutExpired("hidden", 18)):
            self.assertEqual(p.probe("x", pool)["status"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()

"""Decide allowance admission and read-only debate recovery; never runs a model.

CLI: quota_guard.py check PLAN [--out NEW_FILE]
     quota_guard.py recover PLAN FAILURE [--out NEW_FILE]
Fresh native reads are automatic; decisions are observations, not reservations.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
import sys

from quota_probe import probe


class GuardError(ValueError):
    pass


def number(value, positive=False):
    if isinstance(value, bool):
        raise GuardError("INVALID_NUMBER")
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise GuardError("INVALID_NUMBER") from exc
    if not math.isfinite(result) or result < 0 or (positive and result == 0):
        raise GuardError("INVALID_NUMBER")
    return result


def integer(value, minimum=0):
    if type(value) is not int or value < minimum:
        raise GuardError("INVALID_INTEGER")
    return value


def instant(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.utcoffset() is None:
        raise GuardError("NAIVE_TIME")
    return dt


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def excluded(plan, model_ref):
    model = plan["models"][model_ref]
    tokens = set(plan["excluded_models"])
    families = {m["family"] for ref, m in plan["models"].items()
                if {ref, m["model"], m["family"]} & tokens}
    return (model["pool"] in plan["excluded_pools"] or
            model["family"] in families)


def candidates(plan, seat):
    frozen = plan.get("frozen_assignments")
    return [frozen[seat["id"]]] if frozen is not None else seat["candidates"]


def validate(plan):
    if type(plan.get("version")) is not int or plan["version"] != 1:
        raise GuardError("UNSUPPORTED_PLAN")
    integer(plan["min_models"], 2)
    integer(plan["project_min_models"], 1)
    if plan["min_models"] < plan["project_min_models"]:
        raise GuardError("PROJECT_QUORUM_NOT_MET")
    integer(plan["max_calls"], 1)
    integer(plan["used_calls"])
    if not 0 < number(plan["max_age_seconds"]) <= 60:
        raise GuardError("INVALID_FRESHNESS")
    if number(plan["safety_factor"]) < 1:
        raise GuardError("INVALID_MARGIN")
    for key in ("excluded_models", "excluded_pools"):
        if not isinstance(plan[key], list) or any(not isinstance(x, str) for x in plan[key]):
            raise GuardError("INVALID_EXCLUSIONS")
    seats = plan["seats"]
    if not isinstance(seats, list) or not 1 <= len(seats) <= 12:
        raise GuardError("INVALID_SEATS")
    if len({s["id"] for s in seats}) != len(seats):
        raise GuardError("DUPLICATE_SEAT")
    for seat in seats:
        if not isinstance(seat["id"], str) or not seat["id"]:
            raise GuardError("INVALID_SEAT_ID")
        integer(seat["remaining_calls"])
        if not isinstance(seat["candidates"], list) or not 1 <= len(seat["candidates"]) <= 6:
            raise GuardError("INVALID_CANDIDATES")
        if len(set(seat["candidates"])) != len(seat["candidates"]):
            raise GuardError("DUPLICATE_CANDIDATE")
        for ref in seat["candidates"]:
            model = plan["models"][ref]
            if any(not isinstance(model[k], str) or not model[k].strip()
                   for k in ("model", "family", "pool", "identity_evidence")):
                raise GuardError("MODEL_IDENTITY_UNVERIFIED")
            if model["family"].strip().lower() == "unknown":
                raise GuardError("MODEL_IDENTITY_UNVERIFIED")
            if model["pool"] not in plan["pools"]:
                raise GuardError("POOL_MISSING")
    frozen = plan.get("frozen_assignments")
    if frozen is not None:
        if not isinstance(frozen, dict) or set(frozen) != {s["id"] for s in seats}:
            raise GuardError("INCOMPLETE_FROZEN_ROSTER")
        if any(frozen[s["id"]] not in s["candidates"] for s in seats):
            raise GuardError("FROZEN_MODEL_NOT_ALLOWED")
    sources = set()
    for key, pool in plan["pools"].items():
        observer = pool["observer"]
        # The same configured account route cannot acquire extra allowance by
        # being named twice. Different models on the route share a pool.
        source = (observer["adapter"], str(Path(observer.get("executable", ".")).resolve()),
                  str(Path(observer.get("config_path", ".")).resolve()), observer.get("limit_id"))
        if source in sources:
            raise GuardError("DUPLICATE_POOL_ROUTE")
        sources.add(source)
        if pool["estimate_basis"] not in ("measured_history", "declared_bootstrap"):
            raise GuardError("ESTIMATE_BASIS_REQUIRED")
        if not isinstance(pool["estimate_evidence"], str) or not pool["estimate_evidence"].strip():
            raise GuardError("ESTIMATE_EVIDENCE_REQUIRED")
        for unit, estimate in pool["per_call"].items():
            if unit not in ("percent", "CNY", "USD", "tokens", "requests"):
                raise GuardError("UNKNOWN_UNIT")
            number(estimate, positive=True)
            number(pool["reserve"][unit])
            if unit == "percent" and not 2 <= number(pool["reserve"][unit]) <= 100:
                raise GuardError("INVALID_PERCENT_RESERVE")
        integer(pool.get("inflight_calls", 0))
    calls = plan["used_calls"] + sum(s["remaining_calls"] for s in seats)
    calls += sum(p.get("inflight_calls", 0) for p in plan["pools"].values())
    if calls > plan["max_calls"]:
        raise GuardError("CALL_BUDGET_EXCEEDED")


def observe(plan, reader=probe):
    # Exclusions apply before even read-only native discovery/probe processes.
    refs = {ref for seat in plan["seats"] for ref in candidates(plan, seat) if not excluded(plan, ref)
            and (seat["remaining_calls"] > 0 or
                 plan["pools"][plan["models"][ref]["pool"]].get("inflight_calls", 0) > 0)}
    pool_ids = sorted({plan["models"][ref]["pool"] for ref in refs})
    with ThreadPoolExecutor(max_workers=3) as executor:
        rows = list(executor.map(lambda key: reader(key, plan["pools"][key]), pool_ids))
    return dict(zip(pool_ids, rows))


def headroom(pool_id, pool, sample, calls, plan, now):
    result = {"pool": pool_id, "eligible": False, "reason": "QUOTA_UNKNOWN"}
    try:
        if sample.get("status") != "KNOWN" or sample["pool"] != pool_id:
            return result
        if sample["observer_digest"] != digest(pool["observer"]):
            return {**result, "reason": "ACCOUNT_ROUTE_CHANGED"}
        age = (now - instant(sample["observed_at"])).total_seconds()
        if not 0 <= age <= plan["max_age_seconds"]:
            return {**result, "reason": "STALE_QUOTA"}
        windows = sample["windows"]
        names = [w["name"] for w in windows]
        required = {"codex": {"primary"}, "grok": {"weekly"},
                    "deepseek": {"balance"}}.get(pool["observer"]["adapter"])
        if not windows or len(names) != len(set(names)) or required is None or not required.issubset(names):
            raise GuardError("WINDOWS_UNVERIFIED")
        details = []
        for window in windows:
            unit = window["unit"]
            expected = "CNY" if pool["observer"]["adapter"] == "deepseek" else "percent"
            if unit != expected:
                raise GuardError("UNIT_MISMATCH")
            left = number(window["remaining"])
            if unit == "percent":
                if left > 100 or instant(window["reset_at"]) <= now:
                    raise GuardError("INVALID_WINDOW")
                if left <= 2:
                    return {**result, "reason": "QUOTA_NEAR_EXHAUSTION"}
            elif window.get("reset_at") is not None and instant(window["reset_at"]) <= now:
                raise GuardError("INVALID_WINDOW")
            total_calls = calls + pool.get("inflight_calls", 0)
            needed = (number(pool["reserve"][unit]) +
                      total_calls * number(pool["per_call"][unit], positive=True) * plan["safety_factor"])
            details.append({"name": window["name"], "unit": unit, "remaining": left,
                            "required": needed, "calls": total_calls})
        enough = all(w["remaining"] >= w["required"] for w in details)
        return {**result, "eligible": enough, "reason": "HEADROOM_PASSED" if enough else "ROUND_BUDGET_INSUFFICIENT",
                "windows": details, "estimate_basis": pool["estimate_basis"]}
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        return {**result, "reason": "QUOTA_SCHEMA_UNKNOWN"}


def evaluate(plan, observations, now=None):
    validate(plan)
    now = now or datetime.now(timezone.utc)
    result = {"status": "BLOCKED", "reason": "NO_ELIGIBLE_ROSTER", "plan_digest": digest(plan),
              "observed_at": now.isoformat(), "min_models": plan["min_models"],
              "assignments": {}, "pool_checks": [], "observations": observations,
              "limitation": "Allowance observations and declared estimates are not reservations or completion guarantees."}
    choices = [[ref for ref in candidates(plan, s) if not excluded(plan, ref)] for s in plan["seats"]]
    for index, refs in enumerate(itertools.product(*choices)):
        if index >= 10000:
            return {**result, "reason": "SELECTION_SEARCH_LIMIT"}
        families = {plan["models"][ref]["family"] for ref in refs}
        if len(families) < plan["min_models"]:
            continue
        costs = {}
        for seat, ref in zip(plan["seats"], refs):
            key = plan["models"][ref]["pool"]
            costs[key] = costs.get(key, 0) + seat["remaining_calls"]
        checks = [headroom(key, plan["pools"][key], observations.get(key, {}), calls, plan, now)
                  for key, calls in costs.items() if calls > 0 or plan["pools"][key].get("inflight_calls", 0) > 0]
        result["pool_checks"] = checks
        if all(c["eligible"] for c in checks):
            return {**result, "status": "READY", "reason": "WHOLE_REMAINING_RUN_FITS_ESTIMATED_BUDGET",
                    "assignments": {s["id"]: ref for s, ref in zip(plan["seats"], refs)},
                    "distinct_models": len(families)}
    return result


def recovery_plan(plan, failure):
    validate(plan)
    base = {"status": "BLOCKED", "reason": "REPAIR_REQUIRED", "preserve_originals": True}
    if failure.get("process_reconciled") is not True or failure.get("read_only_verified") is not True:
        return {**base, "reason": "RECONCILIATION_REQUIRED"}, None
    if failure.get("kind") == "RATE_LIMIT":
        delay = failure.get("retry_after_seconds")
        if delay is not None and number(delay) <= number(failure.get("max_wait_seconds", 0)) and failure.get("retry_budget_available") is True:
            return {**base, "status": "WAIT", "reason": "COOLDOWN_THEN_FRESH_PREFLIGHT",
                    "wait_seconds": number(delay)}, None
        return {**base, "reason": "RATE_LIMIT_KIND_OR_WAIT_BUDGET_UNRESOLVED"}, None
    if failure.get("kind") != "QUOTA_EXHAUSTED":
        return base, None
    if failure.get("replacement_authorized") is not True:
        return {**base, "reason": "ROSTER_CHANGE_AUTHORIZATION_MISSING"}, None
    if failure.get("context_unchanged") is not True or failure.get("independence_verified") is not True:
        return {**base, "reason": "REBASE_INPUTS_BEFORE_RECOVERY"}, None
    failed_ref = failure["model_ref"]
    failed_pool = plan["models"][failed_ref]["pool"]
    assignments = failure["assignments"]
    if plan.get("frozen_assignments") is not None and plan["frozen_assignments"] != assignments:
        raise GuardError("FROZEN_ROSTER_MISMATCH")
    if set(assignments) != {s["id"] for s in plan["seats"]}:
        raise GuardError("INCOMPLETE_FROZEN_ROSTER")
    if failed_ref not in assignments.values():
        raise GuardError("FAILED_MODEL_NOT_IN_ROSTER")
    stage = failure["stage"]
    if stage not in ("research", "round-1", "round-2"):
        raise GuardError("INVALID_STAGE")
    revised = deepcopy(plan)
    revised.pop("frozen_assignments", None)
    revised["excluded_pools"] = list(set(plan["excluded_pools"]) | {failed_pool})
    affected = []
    for seat in revised["seats"]:
        old_ref = assignments[seat["id"]]
        if old_ref not in seat["candidates"]:
            raise GuardError("FROZEN_MODEL_NOT_ALLOWED")
        if plan["models"][old_ref]["pool"] == failed_pool:
            affected.append(seat["id"])
            # Supplied remaining-call counts must include a replacement's fresh
            # research/round1/round2, plus its probes and authorized retries.
            minimum = 2 if failure.get("no_web") is True else 3
            if seat["remaining_calls"] < minimum:
                raise GuardError("REPLACEMENT_WORK_NOT_BUDGETED")
        else:
            seat["candidates"] = [old_ref]
            if seat["remaining_calls"] < 1:
                raise GuardError("REPEATED_ROUND2_NOT_BUDGETED")
    return {**base, "status": "PREFLIGHT_REQUIRED", "reason": "AUTHORIZED_POOL_REPLACEMENT",
            "affected_seats": affected, "failed_pool": failed_pool,
            "required_plan_changes": {"excluded_pools": revised["excluded_pools"],
                                      "freeze_assignments_from_ready_result": True},
            "preserve": "Other seats' successful research/round1 only when input, role, identity and isolation fingerprints match.",
            "invalidate": "Replaced seats' active research/round1; all round2 results and the merged research/objection index.",
            "restart": "Fresh isolated replacement research/round1; rebuild the barrier/index; rerun round2 for every seat."}, revised


def execute(plan, failure=None, reader=probe):
    validate(plan)
    action = None
    if failure is not None:
        action, plan = recovery_plan(plan, failure)
        if plan is None:
            return action
    observations = observe(plan, reader)
    result = evaluate(plan, observations)
    if action:
        result["recovery"] = action
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "recover"))
    parser.add_argument("plan", type=Path)
    parser.add_argument("failure", type=Path, nargs="?")
    parser.add_argument("--out", type=Path, help="New receipt file; existing evidence is never overwritten")
    args = parser.parse_args()
    try:
        if (args.mode == "recover") != (args.failure is not None):
            raise GuardError("RECOVERY_REQUIRES_FAILURE_RECORD")
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        failure = json.loads(args.failure.read_text(encoding="utf-8")) if args.failure else None
        result = execute(plan, failure)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        result = {"status": "BLOCKED", "reason": str(exc) if isinstance(exc, GuardError) else "INVALID_PLAN_OR_PROBE"}
    output = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.out:
        try:
            with args.out.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(output)
        except OSError:
            print('{"status":"BLOCKED","reason":"RECEIPT_NOT_WRITTEN"}')
            return 2
    print(output)
    return 0 if result["status"] == "READY" else 75


if __name__ == "__main__":
    sys.exit(main())

"""Explicit queue execution profiles. Never reads a user's CLI model defaults."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re

ROLES = ("workers", "judges", "revisions")
JUDGE_TOOLS = {"fable": "claude", "opus": "claude", "codex": "codex"}
EFFORTS = {"codex": {"none", "minimal", "low", "medium", "high", "xhigh"},
           "claude": {"low", "medium", "high", "xhigh", "max"}}


class PolicyError(ValueError):
    pass


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def overrides(text: str) -> dict:
    result = {}
    for task, raw in re.findall(r"<!--\s*task:(\S+)\s+execution:\s*(.*?)\s*-->", text, re.S):
        if task in result:
            raise PolicyError(f"duplicate execution declaration: {task}")
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise PolicyError(f"invalid execution JSON for {task}: {exc}") from exc
        if not isinstance(value, dict) or set(value) - set(ROLES):
            raise PolicyError(f"invalid execution roles for {task}")
        result[task] = value
    return result


def profile(value: object, tool: str) -> dict:
    if not isinstance(value, dict) or set(value) - {"model", "reasoning_effort", "reasoning_note"}:
        raise PolicyError(f"invalid execution profile for {tool}")
    model, effort = value.get("model"), value.get("reasoning_effort")
    if (not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", model)
            or model.lower() in {"unknown", "default", "inherit", "auto", "latest", "opus", "sonnet", "fable"}):
        raise PolicyError(f"{tool}: an explicit model ID is required (no moving alias)")
    if not isinstance(effort, str):
        raise PolicyError(f"{tool}: reasoning_effort must be explicit text")
    if tool in EFFORTS:
        if effort not in EFFORTS[tool]:
            raise PolicyError(f"{tool}: explicit supported reasoning_effort is required")
    elif tool in {"grok", "kimi"}:
        if effort != "not_applicable" or not str(value.get("reasoning_note", "")).strip():
            raise PolicyError(f"{tool}: no effort adapter; require not_applicable and reasoning_note, "
                              "or add a verified adapter before dispatch")
    else:
        raise PolicyError(f"no execution adapter for {tool}")
    return deepcopy(value)


def resolve(cfg: dict, text: str, tasks: dict, pins: dict) -> dict:
    declared = overrides(text)
    if set(declared) - set(tasks):
        raise PolicyError("execution declaration references an unknown task")
    section = cfg.get("execution")
    if not isinstance(section, dict):
        raise PolicyError("execution.defaults must be configured before queue dispatch")
    defaults = section.get("defaults", {})
    if not isinstance(defaults, dict) or set(defaults) - set(ROLES):
        raise PolicyError("execution.defaults must contain workers, judges, and/or revisions")
    resolved = {}
    for task, row in tasks.items():
        if row["status"] not in cfg["todo_markers"] + cfg["in_progress_markers"]:
            continue
        maps = {}
        for role in ROLES:
            baseline = defaults.get(role, defaults.get("workers", {}) if role == "revisions" else {})
            if not isinstance(baseline, dict):
                raise PolicyError(f"execution.defaults.{role} must be an object")
            maps[role] = deepcopy(baseline)
            additions = declared.get(task, {}).get(role, {})
            if not isinstance(additions, dict):
                raise PolicyError(f"{task}: {role} override must be an object")
            # A task worker override also applies to its revisions unless a
            # distinct revision profile was explicitly declared.
            if role == "revisions" and "revisions" not in defaults:
                maps[role] = deepcopy(maps["workers"])
            for member, value in additions.items():
                if not isinstance(value, dict):
                    raise PolicyError(f"{task}: invalid {role}/{member} override")
                maps[role][member] = {**maps[role].get(member, {}), **value}
        workers = list(dict.fromkeys(([pins[task]] if task in pins else []) + cfg["workers"]))
        judges = cfg.get("judge", {})
        chain = judges.get("chain", ["fable", "opus", "codex"]) if judges.get("enabled") else []
        for role, members in (("workers", workers), ("revisions", workers), ("judges", chain)):
            maps[role] = {member: profile(maps[role].get(member),
                           JUDGE_TOOLS.get(member, member) if role == "judges" else member)
                          for member in members}
        resolved[task] = {"workers": workers, "judge_chain": chain, "profiles": maps,
                          "pin": pins.get(task)}
    return resolved


def lock_path(repo: Path) -> Path:
    return repo / ".gate" / "execution-lock.json"


def read_lock(repo: Path) -> dict:
    try:
        value = json.loads(lock_path(repo).read_text(encoding="utf-8"))
        if value.get("schema_version") != 1 or digest(value["tasks"]) != value.get("sha256"):
            raise PolicyError("invalid execution lock receipt")
        return value
    except (OSError, KeyError, ValueError, TypeError, AttributeError) as exc:
        raise PolicyError(f"execution lock missing or invalid: {exc}") from exc


def freeze(repo: Path, policies: dict, reason: str | None = None, pending: set | None = None) -> dict:
    path = lock_path(repo)
    old = read_lock(repo) if path.exists() else None
    combined = deepcopy(old["tasks"]) if old else {}
    for task, value in policies.items():
        if old and task in combined and combined[task] != value:
            if not reason or not reason.strip():
                raise PolicyError("execution changed; lock-execution --reason must record the explicit instruction")
            if pending is None or task not in pending:
                raise PolicyError(f"cannot change execution of an already started task: {task}")
        combined[task] = value
    receipt = digest(combined)
    if old and old["sha256"] == receipt:
        return old
    result = {"schema_version": 1, "sha256": receipt, "tasks": combined,
              "reason": reason or "initial queue execution freeze",
              "previous_sha256": old["sha256"] if old else None}
    path.parent.mkdir(parents=True, exist_ok=True)
    history = path.parent / "execution-locks"
    history.mkdir(exist_ok=True)
    payload = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    # Keep the first receipt for an identical profile set; never replace history.
    historical = history / f"{receipt}.json"
    if not historical.exists():
        historical.write_text(payload, encoding="utf-8")
    temp = path.with_name(f"execution-lock.{os.getpid()}.tmp")
    temp.write_text(payload, encoding="utf-8")
    os.replace(temp, path)
    with (path.parent / "execution-changes.ndjson").open("a", encoding="utf-8") as history_log:
        history_log.write(json.dumps(result, ensure_ascii=False) + "\n")
    return result


def selected(cfg: dict, worker: str) -> dict | None:
    policy = cfg.get("_execution_policy")
    if policy is None:
        return None
    role = cfg.get("_execution_role", "workers")
    member = cfg.get("_execution_member", worker)
    try:
        return policy["profiles"][role][member]
    except KeyError as exc:
        raise PolicyError(f"missing frozen profile: {role}/{member}") from exc


def bind(cfg: dict, policy: dict, task: str) -> dict:
    result = deepcopy(cfg)
    result.update(_execution_policy=deepcopy(policy), _execution_task=task,
                  _execution_role="workers")
    result["workers"] = list(policy["workers"])
    result["judge"] = {**result.get("judge", {}), "chain": list(policy["judge_chain"])}
    return result


def arguments(argv: list[str], tool: str, requested: dict) -> list[str]:
    """Replace selection flags, preserving sandbox/schema/prompt and resume args."""
    requested = profile(requested, tool)
    kept = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in {"--model", "-m", "--effort", "--reasoning-effort"}:
            if i + 1 >= len(argv):
                raise PolicyError(f"selection flag lacks value: {arg}")
            i += 2
            continue
        if arg.startswith(("--model=", "--effort=", "--reasoning-effort=")):
            i += 1
            continue
        if arg.startswith("-m") and not arg.startswith("--") and len(arg) > 2:
            i += 1
            continue
        if tool == "codex" and (arg.startswith("--config=") or arg.startswith("-c") and len(arg) > 2):
            setting = arg[len("--config="):] if arg.startswith("--config=") else arg[2:]
            if setting.split("=", 1)[0].strip() in {"model", "model_reasoning_effort"}:
                i += 1
                continue
        if tool == "codex" and arg in {"-c", "--config"} and i + 1 < len(argv):
            key = argv[i + 1].split("=", 1)[0].strip()
            if key in {"model", "model_reasoning_effort"}:
                i += 2
                continue
        if arg.startswith(("--fallback-model", "--profile")) or arg == "-p" and tool == "codex":
            raise PolicyError("profile or hidden fallback flags conflict with frozen execution")
        kept.append(arg)
        i += 1
    if tool == "codex":
        if len(kept) < 2 or kept[1] != "exec":
            raise PolicyError("Codex execution adapter requires codex exec")
        flags = ["--model", requested["model"], "-c",
                 'model_reasoning_effort=' + json.dumps(requested["reasoning_effort"])]
        return kept[:2] + flags + kept[2:]
    flags = ["--model" if tool != "kimi" else "-m", requested["model"]]
    if tool == "claude":
        flags += ["--effort", requested["reasoning_effort"]]
    return kept[:1] + flags + kept[1:]


def observed(output: str, requested: dict) -> dict:
    values = {"model": "unknown", "reasoning_effort": "unknown"}
    # Only the CLI startup banner is evidence; task text can repeat these words.
    banner = output.split("\nuser", 1)[0][:2000]
    for field, label in (("model", "model"), ("reasoning_effort", "reasoning effort")):
        match = re.search(r"^" + label + r":\s*(\S+)\s*$", banner, re.M)
        if match:
            values[field] = match[1]
    values["mismatch"] = any(v != "unknown" and v != requested[k] for k, v in values.items())
    return values

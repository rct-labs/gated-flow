"""Tools-off native CLI model dispatch (Hydrogen/Carbon via agy-flow/agy TLS route) and offline fixture runner."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import time
from typing import Any, Callable, Protocol, Sequence

from learner.contracts import ValidationError, sha256_bytes, sha256_json

DEFAULT_POLICY_PATH = Path.home() / ".config/mini-model-routing/policy.json"
DEFAULT_POLICY_DOC_PATH = DEFAULT_POLICY_PATH.with_name("POLICY.md")
DEFAULT_AGY_FLOW_CANDIDATES = (
    Path.home() / ".local/bin/agy-flow",
    Path.home() / ".local/lib/agy-tls-bridge/bin/agy",
)
ALLOWED_MODEL_ROLES = ("hydrogen", "carbon")
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ModelPolicyPin:
    role: str
    model: str
    effort: str
    policy_path: str
    subscription_only: bool = True
    automatic_paid_fallback: bool = False


@dataclass(frozen=True)
class ModelCallReceipt:
    role: str
    route: str
    requested_model: str
    requested_effort: str
    returned_model: str
    status: str  # ok | error | timeout | invalid_response | unconfigured
    parsed_json: dict[str, Any] | None
    raw_terminal: dict[str, Any] | None
    raw_output: str
    raw_error: str | None
    usage: dict[str, Any]
    exit_code: int | None
    timed_out: bool
    duration_seconds: float | None
    tool_use_count: int
    advertised_tools_count: int
    prompt_digest: str
    dispatched: bool
    policy_context: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "role": self.role,
            "route": self.route,
            "requested_model": self.requested_model,
            "requested_effort": self.requested_effort,
            "returned_model": self.returned_model,
            "status": self.status,
            "raw_terminal": self.raw_terminal,
            "raw_error": self.raw_error,
            "usage": dict(self.usage),
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "duration_seconds": self.duration_seconds,
            "tool_use_count": self.tool_use_count,
            "advertised_tools_count": self.advertised_tools_count,
            "prompt_digest": self.prompt_digest,
            "dispatched": self.dispatched,
        }
        if self.policy_context is not None:
            data["policy_context"] = self.policy_context
        return data


class ModelRunner(Protocol):
    def check_available(self, role: str) -> tuple[bool, str | None, ModelPolicyPin | None]: ...

    def invoke(
        self,
        role: str,
        prompt: str,
        *,
        timeout_s: float = 120.0,
        validator: Callable[[dict[str, Any]], list[str]] | None = None,
    ) -> ModelCallReceipt: ...


def resolve_policy_pin(
    role: str,
    *,
    policy_path: str | Path | None = None,
) -> tuple[ModelPolicyPin | None, str | None]:
    """Resolve exact model ID and effort from central policy.json without paid fallback."""
    if role not in ALLOWED_MODEL_ROLES:
        raise ValidationError(f"Unsupported learner model role: {role!r}")

    path = Path(policy_path) if policy_path is not None else DEFAULT_POLICY_PATH
    if not path.is_file():
        return None, f"policy_missing:{path}"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"policy_invalid_json:{exc}"

    if not isinstance(data, dict):
        return None, "policy_not_object"
    if data.get("subscription_only") is not True:
        return None, "policy_subscription_only_required"
    if data.get("automatic_paid_fallback") is not False:
        return None, "policy_paid_fallback_forbidden"

    section = "coding" if role == "hydrogen" else "design"
    sec_obj = data.get(section)
    if not isinstance(sec_obj, dict):
        return None, f"policy_missing_section:{section}"
    gem = sec_obj.get("gemini")
    if not isinstance(gem, dict):
        return None, f"policy_missing_gemini:{section}"
    if gem.get("enabled") is False or gem.get("disabled") is True:
        return None, f"model_disabled:{role}"

    model = gem.get("model")
    effort = gem.get("effort")
    if not isinstance(model, str) or not model.strip():
        return None, f"policy_missing_model:{role}"
    if not isinstance(effort, str) or not effort.strip():
        return None, f"policy_missing_effort:{role}"

    model_clean = model.strip()
    effort_clean = effort.strip()
    if role == "hydrogen" and ("hydrogen" not in model_clean.lower() or effort_clean != "high"):
        return None, f"policy_hydrogen_mismatch:{model_clean}:{effort_clean}"
    if role == "carbon" and ("carbon" not in model_clean.lower() or effort_clean != "medium"):
        return None, f"policy_carbon_mismatch:{model_clean}:{effort_clean}"

    return (
        ModelPolicyPin(
            role=role,
            model=model_clean,
            effort=effort_clean,
            policy_path=str(path),
            subscription_only=True,
            automatic_paid_fallback=False,
        ),
        None,
    )


def _read_routing_document(
    path: Path,
    *,
    required: bool,
    missing_code: str,
    unreadable_code: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Read a routing policy document and return its verbatim content plus SHA-256 provenance."""
    if not path.is_file():
        if required:
            return None, f"{missing_code}:{path}"
        return {
            "path": str(path),
            "resolved_path": str(path.resolve(strict=False)),
            "present": False,
            "sha256": None,
            "size_bytes": 0,
            "content": "",
        }, None
    try:
        raw_bytes = path.read_bytes()
        text = raw_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{unreadable_code}:{path}:{exc}"
    return {
        "path": str(path),
        "resolved_path": str(path.resolve(strict=False)),
        "present": True,
        "sha256": sha256_bytes(raw_bytes),
        "size_bytes": len(raw_bytes),
        "content": text,
    }, None


def resolve_routing_context(
    role: str,
    *,
    policy_path: str | Path | None = None,
    policy_doc_path: str | Path | None = None,
) -> tuple[ModelPolicyPin | None, dict[str, Any] | None, str | None]:
    """Resolve current authoritative routing pin, policy.json, and POLICY.md with SHA-256 digests."""
    pin, policy_err = resolve_policy_pin(role, policy_path=policy_path)
    if pin is None:
        return None, None, policy_err

    json_path = Path(pin.policy_path)
    json_doc, json_err = _read_routing_document(
        json_path,
        required=True,
        missing_code="policy_missing",
        unreadable_code="policy_unreadable",
    )
    if json_doc is None:
        return pin, None, json_err

    is_default_policy = json_path.resolve(strict=False) == DEFAULT_POLICY_PATH.resolve(strict=False)
    if policy_doc_path is not None:
        md_path = Path(policy_doc_path)
        md_required = True
    else:
        md_path = json_path.with_name("POLICY.md")
        md_required = is_default_policy

    md_doc, md_err = _read_routing_document(
        md_path,
        required=md_required,
        missing_code="policy_doc_missing",
        unreadable_code="policy_doc_unreadable",
    )
    if md_doc is None:
        return pin, None, md_err

    context = {
        "policy_json": json_doc,
        "policy_md": md_doc,
        "provenance": {
            "policy_json": {
                "path": json_doc["path"],
                "resolved_path": json_doc["resolved_path"],
                "present": json_doc["present"],
                "sha256": json_doc["sha256"],
                "size_bytes": json_doc["size_bytes"],
                "subscription_only": pin.subscription_only,
                "automatic_paid_fallback": pin.automatic_paid_fallback,
            },
            "policy_md": {
                "path": md_doc["path"],
                "resolved_path": md_doc["resolved_path"],
                "present": md_doc["present"],
                "sha256": md_doc["sha256"],
                "size_bytes": md_doc["size_bytes"],
            },
            "tools_off": True,
        },
    }
    return pin, context, None


def build_tools_off_prompt(prompt: str, routing_context: dict[str, Any]) -> str:
    """Assemble read-only tools-off CLI prompt with supplied routing documents and digests."""
    p_json = routing_context["policy_json"]
    p_md = routing_context["policy_md"]
    prov = routing_context["provenance"]

    lines = [
        "[AUTHORITATIVE PRE-SUPPLIED ROUTING CONTEXT — TOOLS OFF / READ-ONLY]",
        "Inherited owner machine-wide model routing read requirement:",
        "- This invocation runs in tools-off read-only mode (`--mode plan`, all file/shell/network/MCP tools denied).",
        "- No tool access is permitted. Do NOT call `read_file` or any other tool; any tool attempt will be denied and will reject the response.",
        "- The current authoritative routing documents are already read and supplied verbatim below with their provenance and SHA-256 digests.",
        "- These supplied documents satisfy the required policy read; do not attempt to re-read them via tools.",
        "- Proceed directly to the task below and output ONLY the requested single valid JSON object.",
        "",
        "Provenance & Digests:",
        (
            f"- policy.json: path={p_json['path']} resolved_path={p_json['resolved_path']} "
            f"present={str(p_json['present']).lower()} sha256={p_json['sha256']} "
            f"size_bytes={p_json['size_bytes']} "
            f"subscription_only={str(prov['policy_json']['subscription_only']).lower()} "
            f"automatic_paid_fallback={str(prov['policy_json']['automatic_paid_fallback']).lower()}"
        ),
        (
            f"- POLICY.md: path={p_md['path']} resolved_path={p_md['resolved_path']} "
            f"present={str(p_md['present']).lower()} sha256={p_md['sha256'] or 'none'} "
            f"size_bytes={p_md['size_bytes']}"
        ),
        "",
    ]
    if p_md["present"]:
        lines.extend(
            [
                f"=== BEGIN AUTHORITATIVE DOCUMENT: {p_md['path']} (sha256={p_md['sha256']}) ===",
                p_md["content"].rstrip("\n"),
                f"=== END AUTHORITATIVE DOCUMENT: {p_md['path']} ===",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"=== NOTICE: POLICY.md not present at {p_md['path']} (no tool read permitted) ===",
                "",
            ]
        )
    lines.extend(
        [
            f"=== BEGIN AUTHORITATIVE DOCUMENT: {p_json['path']} (sha256={p_json['sha256']}) ===",
            p_json["content"].rstrip("\n"),
            f"=== END AUTHORITATIVE DOCUMENT: {p_json['path']} ===",
            "",
            "[TASK INPUT — RETURN ONLY VALID JSON, NO TOOL CALLS]",
            prompt,
        ]
    )
    return "\n".join(lines)


def resolve_agy_tls_executable(explicit_path: str | Path | None = None) -> tuple[str | None, str, str | None]:
    """Resolve ONLY the existing agy-flow/agy TLS route; never fall back to paid/other CLIs."""
    if explicit_path is not None:
        cand = Path(explicit_path)
        if cand.is_file() and os.access(cand, os.X_OK):
            route = "agy-flow" if "agy-flow" in cand.name else "agy"
            return str(cand), route, None
        return None, "unconfigured", f"cli_not_executable:{cand}"

    for cmd_name in ("agy-flow", "agy"):
        found = shutil.which(cmd_name)
        if found:
            return found, cmd_name, None

    for cand in DEFAULT_AGY_FLOW_CANDIDATES:
        if cand.is_file() and os.access(cand, os.X_OK):
            route = "agy-flow" if "agy-flow" in cand.name else "agy"
            return str(cand), route, None

    return None, "unconfigured", "agy_tls_cli_not_found"


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("Empty model response text")
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass

    fences = _JSON_FENCE_RE.findall(raw)
    if len(fences) == 1:
        parsed = json.loads(fences[0].strip())
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Model response did not contain a valid single JSON object")


def _normalize_usage(raw_usage: Any) -> dict[str, Any]:
    if not isinstance(raw_usage, dict):
        return {
            "status": "unknown",
            "input_tokens": None,
            "output_tokens": None,
            "thinking_tokens": None,
            "cache_read_tokens": None,
            "total_tokens": None,
        }

    def _int_or_none(key: str) -> int | None:
        val = raw_usage.get(key)
        return val if type(val) is int and val >= 0 else None

    out = {
        "input_tokens": _int_or_none("input_tokens"),
        "output_tokens": _int_or_none("output_tokens"),
        "thinking_tokens": _int_or_none("thinking_tokens"),
        "cache_read_tokens": _int_or_none("cache_read_tokens"),
        "total_tokens": _int_or_none("total_tokens"),
    }
    has_any = any(v is not None for v in out.values())
    out["status"] = "observed" if has_any else "unknown"
    return out


def parse_agy_cli_output(stdout_text: str) -> dict[str, Any]:
    """Parse agy/agy-flow stream-json NDJSON or single JSON output without fabricating missing metadata."""
    returned_model = "unknown"
    advertised_tools_count = 0
    tool_use_count = 0
    terminal_event: dict[str, Any] | None = None
    terminal_result: dict[str, Any] | None = None
    response_text = ""
    usage_raw: Any = None
    duration_seconds: float | None = None
    terminal_status: str | None = None

    lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue

        ev_name = event.get("event")
        if ev_name == "init" and isinstance(event.get("init"), dict):
            init_obj = event["init"]
            if isinstance(init_obj.get("model"), str) and init_obj["model"].strip():
                returned_model = init_obj["model"].strip()
            tools = init_obj.get("tools")
            if isinstance(tools, list):
                advertised_tools_count = len(tools)
        elif ev_name == "step_update" and isinstance(event.get("step_update"), dict):
            su = event["step_update"]
            stype = str(su.get("step_type") or "").lower()
            if stype and stype not in ("user_input", "agent_response", "thinking"):
                tool_use_count += 1
            if su.get("tool_calls") or su.get("tool_use"):
                tool_use_count += 1
            if su.get("usage") is not None:
                usage_raw = su.get("usage")
        elif ev_name == "result" and isinstance(event.get("result"), dict):
            terminal_event = event
            terminal_result = event["result"]
            terminal_status = str(terminal_result.get("status") or "")
            response_text = str(terminal_result.get("response") or "")
            if terminal_result.get("usage") is not None:
                usage_raw = terminal_result.get("usage")
            if isinstance(terminal_result.get("duration_seconds"), (int, float)):
                duration_seconds = float(terminal_result["duration_seconds"])
            if isinstance(terminal_result.get("model"), str) and terminal_result["model"].strip():
                returned_model = terminal_result["model"].strip()
        elif "response" in event and "status" in event and terminal_result is None:
            terminal_event = event
            terminal_result = event
            terminal_status = str(event.get("status") or "")
            response_text = str(event.get("response") or "")
            if event.get("usage") is not None:
                usage_raw = event.get("usage")
            if isinstance(event.get("duration_seconds"), (int, float)):
                duration_seconds = float(event["duration_seconds"])
            if isinstance(event.get("model"), str) and event["model"].strip():
                returned_model = event["model"].strip()

    return {
        "returned_model": returned_model,
        "advertised_tools_count": advertised_tools_count,
        "tool_use_count": tool_use_count,
        "terminal_event": terminal_event,
        "terminal_status": terminal_status,
        "response_text": response_text,
        "usage": _normalize_usage(usage_raw),
        "duration_seconds": duration_seconds,
    }


def _coerce_subprocess_text(value: str | bytes | bytearray | None) -> str:
    """Normalize subprocess stdout/stderr (bytes in Python 3.14 TimeoutExpired, str in communicate) to str."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    return str(value)


def _merge_timeout_output(
    initial: str | bytes | bytearray | None,
    remainder: str | bytes | bytearray | None,
) -> str:
    """Combine TimeoutExpired buffered output with post-kill communicate() output without duplication."""
    initial_text = _coerce_subprocess_text(initial)
    rem_text = _coerce_subprocess_text(remainder)
    if not initial_text:
        return rem_text
    if not rem_text:
        return initial_text
    if rem_text.startswith(initial_text):
        return rem_text
    if (
        isinstance(initial, (bytes, bytearray))
        and initial_text.endswith("\ufffd")
        and len(initial_text) > 1
        and rem_text.startswith(initial_text[:-1])
        and len(rem_text) >= len(initial_text)
    ):
        return rem_text
    if initial_text.startswith(rem_text):
        return initial_text
    return initial_text + rem_text


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


class AgyCliRunner:
    """Tools-off native CLI runner for Hydrogen/Carbon via agy-flow/agy TLS route only."""

    requires_trial_grant: bool = True

    def __init__(
        self,
        *,
        policy_path: str | Path | None = None,
        policy_doc_path: str | Path | None = None,
        cli_path: str | Path | None = None,
    ) -> None:
        self.policy_path = Path(policy_path) if policy_path is not None else DEFAULT_POLICY_PATH
        self.policy_doc_path = Path(policy_doc_path) if policy_doc_path is not None else None
        self.cli_path = Path(cli_path) if cli_path is not None else None

    def check_available(self, role: str) -> tuple[bool, str | None, ModelPolicyPin | None]:
        pin, _, policy_err = resolve_routing_context(
            role,
            policy_path=self.policy_path,
            policy_doc_path=self.policy_doc_path,
        )
        if pin is None or policy_err is not None:
            return False, policy_err, pin
        exe, _, cli_err = resolve_agy_tls_executable(self.cli_path)
        if exe is None:
            return False, cli_err, pin
        return True, None, pin

    def invoke(
        self,
        role: str,
        prompt: str,
        *,
        timeout_s: float = 120.0,
        validator: Callable[[dict[str, Any]], list[str]] | None = None,
    ) -> ModelCallReceipt:
        raw_prompt_digest = sha256_json({"role": role, "prompt": prompt})
        pin, routing_context, policy_err = resolve_routing_context(
            role,
            policy_path=self.policy_path,
            policy_doc_path=self.policy_doc_path,
        )
        if pin is None or routing_context is None or policy_err is not None:
            return ModelCallReceipt(
                role=role,
                route="unconfigured",
                requested_model=pin.model if pin is not None else "unknown",
                requested_effort=pin.effort if pin is not None else "unknown",
                returned_model="unknown",
                status="unconfigured",
                parsed_json=None,
                raw_terminal=None,
                raw_output="",
                raw_error=policy_err,
                usage=_normalize_usage(None),
                exit_code=None,
                timed_out=False,
                duration_seconds=None,
                tool_use_count=0,
                advertised_tools_count=0,
                prompt_digest=raw_prompt_digest,
                dispatched=False,
                policy_context=None,
            )

        policy_provenance = routing_context["provenance"]
        rendered_prompt = build_tools_off_prompt(prompt, routing_context)
        prompt_digest = sha256_json({"role": role, "prompt": rendered_prompt})

        def _receipt(
            *,
            route_name: str,
            status: str,
            returned_model: str = "unknown",
            parsed_json: dict[str, Any] | None = None,
            raw_terminal: dict[str, Any] | None = None,
            raw_output: str = "",
            raw_error: str | None = None,
            usage: dict[str, Any] | None = None,
            exit_code: int | None = None,
            timed_out: bool = False,
            duration_seconds: float | None = None,
            tool_use_count: int = 0,
            advertised_tools_count: int = 0,
            dispatched: bool = True,
        ) -> ModelCallReceipt:
            return ModelCallReceipt(
                role=role,
                route=route_name,
                requested_model=pin.model,
                requested_effort=pin.effort,
                returned_model=returned_model,
                status=status,
                parsed_json=parsed_json,
                raw_terminal=raw_terminal,
                raw_output=raw_output,
                raw_error=raw_error,
                usage=usage if usage is not None else _normalize_usage(None),
                exit_code=exit_code,
                timed_out=timed_out,
                duration_seconds=duration_seconds,
                tool_use_count=tool_use_count,
                advertised_tools_count=advertised_tools_count,
                prompt_digest=prompt_digest,
                dispatched=dispatched,
                policy_context=policy_provenance,
            )

        exe, route, cli_err = resolve_agy_tls_executable(self.cli_path)
        if exe is None:
            return _receipt(
                route_name="unconfigured",
                status="unconfigured",
                raw_error=cli_err,
                dispatched=False,
            )

        deny_settings = json.dumps(
            {
                "permissions": {
                    "deny": [
                        "read_file(*)",
                        "write_file(*)",
                        "command(*)",
                        "unsandboxed(*)",
                        "read_url(*)",
                        "execute_url(*)",
                        "mcp(*)",
                    ]
                }
            },
            separators=(",", ":"),
        )
        print_timeout = f"{max(1, int(round(timeout_s)))}s"
        argv = [
            exe,
            "--model",
            pin.model,
            "--effort",
            pin.effort,
            "--mode",
            "plan",
            "--disable-slash-commands",
            "--settings",
            deny_settings,
            "--output-format",
            "stream-json",
            "--print-timeout",
            print_timeout,
            "-p",
            rendered_prompt,
        ]

        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="learner-agy-") as empty_cwd:
            try:
                proc = subprocess.Popen(
                    argv,
                    cwd=empty_cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    start_new_session=True,
                )
            except OSError as exc:
                return _receipt(
                    route_name=route,
                    status="error",
                    raw_error=f"spawn_failed:{exc}",
                    duration_seconds=round(time.monotonic() - started, 3),
                    dispatched=False,
                )

            try:
                stdout_text, stderr_text = proc.communicate(timeout=timeout_s)
                elapsed = round(time.monotonic() - started, 3)
            except subprocess.TimeoutExpired as exc:
                _kill_process_group(proc)
                try:
                    rem_out, rem_err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired as rem_exc:
                    rem_out, rem_err = rem_exc.stdout, rem_exc.stderr
                elapsed = round(time.monotonic() - started, 3)
                partial_out = _merge_timeout_output(exc.stdout, rem_out)
                partial_err = _merge_timeout_output(exc.stderr, rem_err)
                parsed = parse_agy_cli_output(partial_out)
                base_err = partial_err.strip() or f"timeout_after_{timeout_s}s"
                if parsed["tool_use_count"] > 0:
                    base_err = f"unexpected_tool_use_in_tools_off_call:{parsed['tool_use_count']};{base_err}"
                return _receipt(
                    route_name=route,
                    status="timeout",
                    returned_model=parsed["returned_model"],
                    raw_terminal=parsed["terminal_event"],
                    raw_output=partial_out[:16_000],
                    raw_error=base_err[:2000],
                    usage=parsed["usage"],
                    exit_code=-1,
                    timed_out=True,
                    duration_seconds=elapsed,
                    tool_use_count=parsed["tool_use_count"],
                    advertised_tools_count=parsed["advertised_tools_count"],
                )

        parsed = parse_agy_cli_output(stdout_text)
        rc = proc.returncode if proc.returncode is not None else -1
        duration = parsed["duration_seconds"] if parsed["duration_seconds"] is not None else elapsed
        stderr_clean = stderr_text.strip()

        if parsed["tool_use_count"] > 0:
            return _receipt(
                route_name=route,
                status="invalid_response",
                returned_model=parsed["returned_model"],
                raw_terminal=parsed["terminal_event"],
                raw_output=stdout_text[:16_000],
                raw_error=f"unexpected_tool_use_in_tools_off_call:{parsed['tool_use_count']}",
                usage=parsed["usage"],
                exit_code=rc,
                duration_seconds=duration,
                tool_use_count=parsed["tool_use_count"],
                advertised_tools_count=parsed["advertised_tools_count"],
            )

        if rc != 0 or parsed["terminal_status"] not in (None, "SUCCESS"):
            err_msg = stderr_clean or f"cli_failed:rc={rc}:status={parsed['terminal_status']}"
            return _receipt(
                route_name=route,
                status="error",
                returned_model=parsed["returned_model"],
                raw_terminal=parsed["terminal_event"],
                raw_output=stdout_text[:16_000],
                raw_error=err_msg[:2000],
                usage=parsed["usage"],
                exit_code=rc,
                duration_seconds=duration,
                tool_use_count=parsed["tool_use_count"],
                advertised_tools_count=parsed["advertised_tools_count"],
            )

        try:
            payload = _extract_json_object(parsed["response_text"])
            if validator is not None:
                errors = validator(payload)
                if errors:
                    raise ValueError("; ".join(errors))
        except ValueError as exc:
            return _receipt(
                route_name=route,
                status="invalid_response",
                returned_model=parsed["returned_model"],
                raw_terminal=parsed["terminal_event"],
                raw_output=stdout_text[:16_000],
                raw_error=f"json_validation_error:{exc}"[:2000],
                usage=parsed["usage"],
                exit_code=rc,
                duration_seconds=duration,
                tool_use_count=parsed["tool_use_count"],
                advertised_tools_count=parsed["advertised_tools_count"],
            )

        return _receipt(
            route_name=route,
            status="ok",
            returned_model=parsed["returned_model"],
            parsed_json=payload,
            raw_terminal=parsed["terminal_event"],
            raw_output=stdout_text[:16_000],
            raw_error=stderr_clean[:1000] if stderr_clean else None,
            usage=parsed["usage"],
            exit_code=rc,
            duration_seconds=duration,
            tool_use_count=parsed["tool_use_count"],
            advertised_tools_count=parsed["advertised_tools_count"],
        )


@dataclass
class FixtureModelCall:
    role: str
    prompt: str
    prompt_digest: str
    timeout_s: float
    requested_model: str
    requested_effort: str


class FixtureModelRunner:
    """Deterministic offline model runner for tests; records all prompts and simulates receipts/failures."""

    def __init__(
        self,
        handler: Callable[[str, str], dict[str, Any] | str | Exception] | None = None,
        *,
        policy_path: str | Path | None = None,
        hydrogen_pin: tuple[str, str] = ("gemini-4-hydrogen-high", "high"),
        carbon_pin: tuple[str, str] = ("gemini-4-carbon-medium", "medium"),
        available: bool = True,
        unavailable_reason: str = "unconfigured_fixture",
        return_unknown_model: bool = False,
        requires_trial_grant: bool = False,
    ) -> None:
        self.handler = handler
        self.policy_path = Path(policy_path) if policy_path is not None else None
        self.hydrogen_pin = hydrogen_pin
        self.carbon_pin = carbon_pin
        self.available = available
        self.unavailable_reason = unavailable_reason
        self.return_unknown_model = return_unknown_model
        self.requires_trial_grant = requires_trial_grant
        self.calls: list[FixtureModelCall] = []

    def check_available(self, role: str) -> tuple[bool, str | None, ModelPolicyPin | None]:
        if role not in ALLOWED_MODEL_ROLES:
            raise ValidationError(f"Unsupported learner model role: {role!r}")
        if not self.available:
            return False, self.unavailable_reason, None
        if self.policy_path is not None:
            pin, err = resolve_policy_pin(role, policy_path=self.policy_path)
            return (pin is not None), err, pin
        model, effort = self.hydrogen_pin if role == "hydrogen" else self.carbon_pin
        return (
            True,
            None,
            ModelPolicyPin(
                role=role,
                model=model,
                effort=effort,
                policy_path="fixture",
                subscription_only=True,
                automatic_paid_fallback=False,
            ),
        )

    def invoke(
        self,
        role: str,
        prompt: str,
        *,
        timeout_s: float = 120.0,
        validator: Callable[[dict[str, Any]], list[str]] | None = None,
    ) -> ModelCallReceipt:
        prompt_digest = sha256_json({"role": role, "prompt": prompt})
        ok, err, pin = self.check_available(role)
        if not ok or pin is None:
            return ModelCallReceipt(
                role=role,
                route="unconfigured",
                requested_model="unknown",
                requested_effort="unknown",
                returned_model="unknown",
                status="unconfigured",
                parsed_json=None,
                raw_terminal=None,
                raw_output="",
                raw_error=err,
                usage=_normalize_usage(None),
                exit_code=None,
                timed_out=False,
                duration_seconds=None,
                tool_use_count=0,
                advertised_tools_count=0,
                prompt_digest=prompt_digest,
                dispatched=False,
            )

        self.calls.append(
            FixtureModelCall(
                role=role,
                prompt=prompt,
                prompt_digest=prompt_digest,
                timeout_s=timeout_s,
                requested_model=pin.model,
                requested_effort=pin.effort,
            )
        )
        returned_model = "unknown" if self.return_unknown_model else pin.model
        usage = _normalize_usage({"input_tokens": 120, "output_tokens": 80, "total_tokens": 200})

        if self.handler is None:
            return ModelCallReceipt(
                role=role,
                route="fixture",
                requested_model=pin.model,
                requested_effort=pin.effort,
                returned_model=returned_model,
                status="error",
                parsed_json=None,
                raw_terminal=None,
                raw_output="",
                raw_error="no_fixture_handler",
                usage=_normalize_usage(None),
                exit_code=1,
                timed_out=False,
                duration_seconds=0.01,
                tool_use_count=0,
                advertised_tools_count=0,
                prompt_digest=prompt_digest,
                dispatched=True,
            )

        try:
            outcome = self.handler(role, prompt)
        except TimeoutError as exc:
            return ModelCallReceipt(
                role=role,
                route="fixture",
                requested_model=pin.model,
                requested_effort=pin.effort,
                returned_model="unknown",
                status="timeout",
                parsed_json=None,
                raw_terminal=None,
                raw_output="",
                raw_error=str(exc) or "fixture_timeout",
                usage=_normalize_usage(None),
                exit_code=-1,
                timed_out=True,
                duration_seconds=timeout_s,
                tool_use_count=0,
                advertised_tools_count=0,
                prompt_digest=prompt_digest,
                dispatched=True,
            )
        except Exception as exc:
            return ModelCallReceipt(
                role=role,
                route="fixture",
                requested_model=pin.model,
                requested_effort=pin.effort,
                returned_model="unknown",
                status="error",
                parsed_json=None,
                raw_terminal=None,
                raw_output="",
                raw_error=str(exc) or exc.__class__.__name__,
                usage=_normalize_usage(None),
                exit_code=1,
                timed_out=False,
                duration_seconds=0.01,
                tool_use_count=0,
                advertised_tools_count=0,
                prompt_digest=prompt_digest,
                dispatched=True,
            )

        if isinstance(outcome, TimeoutError):
            return ModelCallReceipt(
                role=role,
                route="fixture",
                requested_model=pin.model,
                requested_effort=pin.effort,
                returned_model="unknown",
                status="timeout",
                parsed_json=None,
                raw_terminal=None,
                raw_output="",
                raw_error=str(outcome) or "fixture_timeout",
                usage=_normalize_usage(None),
                exit_code=-1,
                timed_out=True,
                duration_seconds=timeout_s,
                tool_use_count=0,
                advertised_tools_count=0,
                prompt_digest=prompt_digest,
                dispatched=True,
            )
        if isinstance(outcome, Exception):
            return ModelCallReceipt(
                role=role,
                route="fixture",
                requested_model=pin.model,
                requested_effort=pin.effort,
                returned_model="unknown",
                status="error",
                parsed_json=None,
                raw_terminal=None,
                raw_output="",
                raw_error=str(outcome) or outcome.__class__.__name__,
                usage=_normalize_usage(None),
                exit_code=1,
                timed_out=False,
                duration_seconds=0.01,
                tool_use_count=0,
                advertised_tools_count=0,
                prompt_digest=prompt_digest,
                dispatched=True,
            )

        raw_text = outcome if isinstance(outcome, str) else json.dumps(outcome, sort_keys=True)
        try:
            payload = outcome if isinstance(outcome, dict) else _extract_json_object(raw_text)
            if validator is not None:
                errors = validator(payload)
                if errors:
                    raise ValueError("; ".join(errors))
        except ValueError as exc:
            return ModelCallReceipt(
                role=role,
                route="fixture",
                requested_model=pin.model,
                requested_effort=pin.effort,
                returned_model=returned_model,
                status="invalid_response",
                parsed_json=None,
                raw_terminal={"status": "SUCCESS", "response": raw_text},
                raw_output=raw_text,
                raw_error=f"json_validation_error:{exc}",
                usage=usage,
                exit_code=0,
                timed_out=False,
                duration_seconds=0.01,
                tool_use_count=0,
                advertised_tools_count=0,
                prompt_digest=prompt_digest,
                dispatched=True,
            )

        return ModelCallReceipt(
            role=role,
            route="fixture",
            requested_model=pin.model,
            requested_effort=pin.effort,
            returned_model=returned_model,
            status="ok",
            parsed_json=payload,
            raw_terminal={"status": "SUCCESS", "model": returned_model, "usage": usage},
            raw_output=raw_text,
            raw_error=None,
            usage=usage,
            exit_code=0,
            timed_out=False,
            duration_seconds=0.01,
            tool_use_count=0,
            advertised_tools_count=0,
            prompt_digest=prompt_digest,
            dispatched=True,
        )

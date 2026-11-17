"""Fail-closed implementation CLI guard; owner policy JSON owns model pins.

This validates explicit argv contracts, not CLI installation or ambient state.
The engine alone decides whether a call is an implementation call.
"""
from datetime import datetime
import errno
import json
import math
from pathlib import Path
import re
import time

POLICY_PATH = Path.home() / '.config/mini-model-routing/policy.json'
CANONICAL_REPO = Path.home() / 'AI/maintenance/flow'
EXCEPTION_PACKAGE = 'ai-evolution-ideas'
_EXCEPTION_KEYS = {'enabled', 'repository', 'package', 'tasks', 'authorization', 'expires_at'}
_AUTH_KEYS = {'approved_by', 'record'}
_FORBIDDEN_EXCEPTION_TASKS = {'CR-1'}
_MAX_EXCEPTION_TASKS = 64
_TASK_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')

# Consume values atomically, particularly prompts which may look like flags.
_VALUE_OPTIONS = {
    '--prompt', '--print', '-p', '--prompt-file', '-C', '--cd', '--cwd',
    '--sandbox', '-s', '--ask-for-approval', '-a', '--permission-mode',
    '--allowedTools', '--disallowedTools', '--allowed-tools', '--disallowed-tools',
    '--output-format', '--output-schema', '--json-schema', '-o', '--output-last-message',
    '--max-turns', '--max-budget-usd', '--add-dir', '--session-id',
}
_BOOL_OPTIONS = {
    '--skip-git-repo-check', '--full-auto',
    '--dangerously-bypass-approvals-and-sandbox', '--dangerously-skip-permissions',
    '--allow-dangerously-skip-permissions', '--no-session-persistence',
    '--no-session', '--verbose', '--json', '--ephemeral', '--disable-slash-commands',
}
_SAFE_CONFIG = {'sandbox_mode', 'approval_policy', 'sandbox_workspace_write',
                'sandbox_workspace_write.network_access'}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate JSON key {key}')
        result[key] = value
    return result


def _json(text):
    return json.loads(text, object_pairs_hook=_unique_object)


def _settings(value, worker):
    obj = _json(value)  # Files cannot be inspected safely as scoped settings.
    allowed = {'permissions'} if worker != 'claude' else {'permissions', 'enabledPlugins'}
    if not isinstance(obj, dict) or not obj or not set(obj) <= allowed:
        raise ValueError('--settings must be inline permission-only JSON')
    def check(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if re.search(r'model|effort|provider|profile|agent|fallback|config', key, re.I):
                    raise ValueError(f'route override in --settings: {key}')
                check(child)
        elif isinstance(node, list):
            for child in node:
                check(child)
    check(obj)
    if 'permissions' in obj and not isinstance(obj['permissions'], dict):
        raise ValueError('--settings permissions must be an object')
    if 'enabledPlugins' in obj and (not isinstance(obj['enabledPlugins'], dict) or
            any(type(v) is not bool for v in obj['enabledPlugins'].values())):
        raise ValueError('--settings enabledPlugins must contain booleans')


def _require_pin_pair(mapping, section, key):
    pin = mapping[key]
    if not isinstance(pin, dict):
        raise ValueError(f'{section}.{key} must be an object')
    for field in ('model', 'effort'):
        if not isinstance(pin[field], str) or not pin[field].strip():
            raise ValueError(f'{section}.{key}.{field} must be a nonempty string')
    return pin


def _parse_expiry_epoch(value):
    if type(value) in (int, float):
        if not math.isfinite(value) or value <= 0:
            raise ValueError('exception expires_at must be a finite positive timestamp')
        return float(value)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith('Z'):
            raw = raw[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f'malformed exception expires_at: {exc}') from exc
        if dt.tzinfo is None:
            raise ValueError('exception expires_at string must include a timezone')
        return dt.timestamp()
    raise ValueError('exception expires_at must be a finite epoch number or ISO-8601 timestamp')


def _is_safe_relative_package_path(raw, package):
    if not isinstance(raw, str) or not raw.strip() or raw.strip() != raw:
        return False
    if raw.startswith('/') or '\\' in raw or raw.startswith(':'):
        return False
    parts = Path(raw).parts
    if not parts or '..' in parts:
        return False
    return len(parts) >= 4 and parts[0] == 'docs' and parts[1] == 'work' and parts[2] == package


def _resolve_path(path):
    """Resolve path, normalizing Python <3.13 symlink-loop RuntimeError to OSError."""
    try:
        return Path(path).resolve()
    except RuntimeError as exc:
        if 'Symlink loop' in str(exc) or (
            isinstance(exc.__context__, OSError) and exc.__context__.errno == errno.ELOOP
        ):
            raise OSError(errno.ELOOP, str(exc)) from exc
        raise


def _validate_exception(cfg, context):
    """Fail-closed validation of the scoped difficult-coding package exception."""
    if not isinstance(context, dict) or not context:
        raise ValueError('difficult-coding exception requires engine-derived repository and task context')
    if context.get('project_prefix', '') != '':
        raise ValueError('difficult-coding exception does not apply to subprojects')

    ctx_repo = context.get('repository')
    if not isinstance(ctx_repo, str) or not ctx_repo or not Path(ctx_repo).is_absolute():
        raise ValueError('missing or invalid engine repository context')
    canonical_resolved = _resolve_path(CANONICAL_REPO)
    actual_repo_resolved = _resolve_path(ctx_repo)
    if actual_repo_resolved != canonical_resolved:
        raise ValueError(f'wrong repository for package exception: {actual_repo_resolved}')

    ctx_package = context.get('package')
    if ctx_package != EXCEPTION_PACKAGE:
        raise ValueError(f'wrong package for exception: expected {EXCEPTION_PACKAGE!r}, got {ctx_package!r}')
    ctx_spec = context.get('spec')
    if not _is_safe_relative_package_path(ctx_spec, EXCEPTION_PACKAGE):
        raise ValueError('missing or invalid engine task spec context')
    package_root = _resolve_path(actual_repo_resolved / 'docs' / 'work' / EXCEPTION_PACKAGE)
    spec_resolved = _resolve_path(actual_repo_resolved / ctx_spec)
    if not spec_resolved.is_file() or not spec_resolved.is_relative_to(package_root):
        raise ValueError('task spec does not resolve inside authorized package directory')

    ctx_task = context.get('task')
    if not isinstance(ctx_task, str) or not _TASK_ID_RE.fullmatch(ctx_task):
        raise ValueError('missing or malformed engine task context')
    if ctx_task in _FORBIDDEN_EXCEPTION_TASKS:
        raise ValueError(f'task {ctx_task!r} is forbidden from difficult-coding exception')
    if context.get('dispatchable') is not True:
        raise ValueError(f'task {ctx_task!r} is not in a dispatchable queue state')

    if not isinstance(cfg, dict):
        raise ValueError('missing config for difficult-coding exception')
    grant = cfg.get('model_policy_exception')
    if not isinstance(grant, dict) or set(grant) != _EXCEPTION_KEYS:
        raise ValueError('missing or malformed model_policy_exception grant')
    if grant['enabled'] is not True:
        raise ValueError('model_policy_exception is not enabled')

    grant_repo = grant['repository']
    if not isinstance(grant_repo, str) or not grant_repo or not Path(grant_repo).is_absolute():
        raise ValueError('malformed model_policy_exception repository')
    if _resolve_path(grant_repo) != canonical_resolved:
        raise ValueError(f'wrong repository in model_policy_exception: {grant_repo!r}')
    if grant['package'] != EXCEPTION_PACKAGE:
        raise ValueError(f'wrong package in model_policy_exception: {grant["package"]!r}')

    tasks = grant['tasks']
    if (not isinstance(tasks, list) or not tasks or len(tasks) > _MAX_EXCEPTION_TASKS
            or any(not isinstance(t, str) or not _TASK_ID_RE.fullmatch(t) for t in tasks)
            or len(set(tasks)) != len(tasks)):
        raise ValueError('model_policy_exception tasks must be a finite list of unique task IDs')
    if any(t in _FORBIDDEN_EXCEPTION_TASKS for t in tasks):
        raise ValueError('model_policy_exception tasks cannot include forbidden tasks')
    if ctx_task not in tasks:
        raise ValueError(f'task {ctx_task!r} is not in model_policy_exception allowlist')

    auth = grant['authorization']
    if not isinstance(auth, dict) or set(auth) != _AUTH_KEYS:
        raise ValueError('malformed model_policy_exception authorization record')
    if not isinstance(auth['approved_by'], str) or not auth['approved_by'].strip():
        raise ValueError('model_policy_exception authorization.approved_by must be nonempty')
    record_rel = auth['record']
    if not _is_safe_relative_package_path(record_rel, EXCEPTION_PACKAGE):
        raise ValueError('model_policy_exception authorization.record must stay inside package docs')
    record_resolved = _resolve_path(actual_repo_resolved / record_rel)
    if not record_resolved.is_file() or not record_resolved.is_relative_to(package_root):
        raise ValueError('model_policy_exception authorization.record file does not exist in package')

    expiry_epoch = _parse_expiry_epoch(grant['expires_at'])
    if time.time() >= expiry_epoch:
        raise ValueError('model_policy_exception has expired')


def validate_worker(worker, template, model, cfg, policy_path=None, context=None):
    """Return None if allowed, else an actionable model_policy: refusal.

    `template` is an argv list, never a shell command. Only {model} is resolved
    here; prompt and path placeholders remain opaque option values. `context`
    must be derived by the engine, never taken from config assertions.
    """
    def refuse(reason):
        return f'model_policy:{reason}; host must reconcile worker_cmds/worker_models with owner policy'
    if worker not in {'codex', 'agy', 'agy-flow', 'claude'}:
        return refuse(f'unsupported implementation worker {worker!r}; use codex, agy/agy-flow, or authorized Claude fallback')
    try:
        policy = _json(Path(policy_path if policy_path is not None else POLICY_PATH).read_text(encoding='utf-8'))
        if not isinstance(policy, dict) or type(policy.get('version')) is not int or policy['version'] != 1:
            raise ValueError('expected policy version 1')
        coding = policy['coding']
        if not isinstance(coding, dict):
            raise ValueError('policy coding must be an object')
        for key in ('codex', 'gemini', 'claude_fallback'):
            _require_pin_pair(coding, 'coding', key)
        if coding['claude_fallback']['only_when'] != 'primary_coding_unavailable':
            raise ValueError('invalid Claude fallback condition')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return refuse(f'cannot load valid owner policy: {exc}')
    expected = coding[{'codex': 'codex', 'agy': 'gemini', 'agy-flow': 'gemini', 'claude': 'claude_fallback'}[worker]]
    try:
        if not isinstance(template, (list, tuple)) or not template or any(not isinstance(p, str) for p in template):
            raise ValueError('worker contract must be a nonempty argv list')
        if Path(template[0]).name != worker:
            raise ValueError('helper/wrapper contracts are unsupported')
        if any('{model}' in p for p in template) and (not isinstance(model, str) or not model):
            raise ValueError(f'worker_models.{worker} must explicitly resolve {{model}}')
        argv = [p.replace('{model}', model) for p in template[1:]]
        pins = {'model': [], 'effort': []}
        i = 0
        while i < len(argv):
            token = argv[i]
            i += 1
            if token == '--':
                break
            if not token.startswith('-') or token == '-':
                continue
            option, sep, inline = token.partition('=')
            def value():
                nonlocal i
                if sep:
                    result = inline
                else:
                    if i >= len(argv):
                        raise ValueError(f'{option} needs a value')
                    result = argv[i]
                    i += 1
                if not result:
                    raise ValueError(f'{option} needs a nonempty value')
                return result
            if option in {'--model', '-m'}:
                pins['model'].append(value())
            elif option == '--effort' and worker != 'codex':
                pins['effort'].append(value())
            elif option in {'-c', '--config'} and worker == 'codex':
                setting = value()
                key, equals, raw = setting.partition('=')
                key = key.strip()
                if not equals:
                    raise ValueError('Codex config must be key=value')
                if key in {'model', 'model_reasoning_effort'}:
                    raw = raw.strip()
                    # CLI config strings are TOML basic/literal strings, or bare values.
                    if raw.startswith('"'):
                        raw = json.loads(raw)
                    elif raw.startswith("'") and raw.endswith("'"):
                        raw = raw[1:-1]
                    pins['model' if key == 'model' else 'effort'].append(raw)
                elif key not in _SAFE_CONFIG:
                    raise ValueError(f'unsupported Codex config override {key!r}')
            elif option == '--settings' and worker != 'codex':
                _settings(value(), worker)
            elif option == '--sandbox' and worker in {'agy', 'agy-flow'} and not sep:
                pass
            elif option in {'--mode', '--print-timeout'} and worker in {'agy', 'agy-flow'}:
                value()
            elif option in _VALUE_OPTIONS and not (worker == 'codex' and option in {'-p', '--print', '--prompt'}):
                if option == '--sandbox' and worker in {'agy', 'agy-flow'}:
                    raise ValueError('agy --sandbox is a boolean option')
                value()
            elif option in _BOOL_OPTIONS and not sep:
                pass
            else:
                raise ValueError(f'unsupported option {option!r}; remove route/profile/provider/agent overrides')
        for key in ('model', 'effort'):
            if len(pins[key]) != 1:
                raise ValueError(f'require exactly one explicit {key} pin (found {len(pins[key])})')
        actual_model, actual_effort = pins['model'][0], pins['effort'][0]
        if actual_model == expected['model']:
            if actual_effort != expected['effort']:
                raise ValueError(f"{worker} coding requires effort={expected['effort']!r}, got {actual_effort!r}")
            if worker == 'claude' and (not isinstance(cfg, dict) or cfg.get('model_policy_fallback_reason') != expected['only_when']):
                return refuse('Claude coding requires model_policy_fallback_reason=primary_coding_unavailable')
            return None

        design_key = {'agy': 'gemini', 'agy-flow': 'gemini', 'claude': 'claude_fallback'}.get(worker)
        if design_key is not None:
            try:
                design = policy['design']
                if not isinstance(design, dict):
                    raise ValueError('policy design must be an object')
                exception_expected = _require_pin_pair(design, 'design', design_key)
            except (KeyError, TypeError, ValueError) as exc:
                return refuse(f'cannot load valid owner policy: {exc}')
            if actual_model == exception_expected['model']:
                if actual_effort != exception_expected['effort']:
                    raise ValueError(
                        f"{worker} difficult-coding exception requires effort={exception_expected['effort']!r}, got {actual_effort!r}"
                    )
                _validate_exception(cfg, context)
                return None

        raise ValueError(f"{worker} coding requires model={expected['model']!r}, got {actual_model!r}")
    except (OSError, ValueError, TypeError) as exc:
        return refuse(str(exc))
    return None

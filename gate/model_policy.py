"""Fail-closed implementation CLI guard; owner policy JSON owns model pins.

This validates explicit argv contracts, not CLI installation or ambient state.
The engine alone decides whether a call is an implementation call.
"""
from pathlib import Path
import json
import re

POLICY_PATH = Path.home() / '.config/mini-model-routing/policy.json'

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


def validate_worker(worker, template, model, cfg, policy_path=None):
    """Return None if allowed, else an actionable model_policy: refusal.

    `template` is an argv list, never a shell command. Only {model} is resolved
    here; prompt and path placeholders remain opaque option values.
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
        for key in ('codex', 'gemini', 'claude_fallback'):
            pin = coding[key]
            for field in ('model', 'effort'):
                if not isinstance(pin[field], str) or not pin[field].strip():
                    raise ValueError(f'coding.{key}.{field} must be a nonempty string')
        if coding['claude_fallback']['only_when'] != 'primary_coding_unavailable':
            raise ValueError('invalid Claude fallback condition')
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return refuse(f'cannot load valid owner policy: {exc}')
    expected = coding[{'codex': 'codex', 'agy': 'gemini', 'agy-flow': 'gemini', 'claude': 'claude_fallback'}[worker]]
    if worker == 'claude' and cfg.get('model_policy_fallback_reason') != expected['only_when']:
        return refuse('Claude coding requires model_policy_fallback_reason=primary_coding_unavailable')
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
            if pins[key][0] != expected[key]:
                raise ValueError(f'{worker} coding requires {key}={expected[key]!r}, got {pins[key][0]!r}')
    except (ValueError, TypeError) as exc:
        return refuse(str(exc))
    return None

"""Bounded, local-only eligibility. Policy owns pins; inventory owns observations.

Catalog visibility is advisory, never entitlement or a quality score. No discovery,
network, subprocess, sleeps or journal scans belong in this module.
"""
import json
from datetime import datetime
import math
import os
import hashlib
import re
import shlex
from pathlib import Path
import time

MAX_BYTES = 1_048_576
MAX_OBSERVATIONS = 512
ALIASES = {'agy-flow': 'agy', 'gemini': 'agy', 'astra': 'codex', 'carbon': 'agy',
           'fable': 'claude', 'opus': 'claude'}


def policy_path():
    return Path.home() / '.config/mini-model-routing/policy.json'


def inventory_path():
    return Path.home() / '.local/share/flow/model-inventory.json'


def account_key(route, model, scope=None):
    scope = account_scope(route) if scope is None else scope
    suffix = '' if scope == 'default-login' else ':' + scope
    return f'{ALIASES.get(route, route)}:{model}{suffix}'


def native_argv(template):
    """Inspect the known env wrapper without changing its launch transport."""
    if not template or Path(template[0]).name != 'env':
        return template
    i = 1
    while i < len(template):
        token = template[i]
        if token == '--':
            return template[i + 1:]
        if token in ('-C', '--chdir', '-u', '--unset'):
            i += 2
        elif token.startswith(('--chdir=', '--unset=')) or token in ('-i', '--ignore-environment'):
            i += 1
        elif re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', token):
            i += 1
        elif token.startswith('-'):
            raise ValueError('unsupported env review wrapper')
        else:
            return template[i:]
    raise ValueError('env wrapper has no executable')


def native_route(route, template=()):
    inner = native_argv(template) if template else ()
    native = ALIASES.get(Path(inner[0]).name, Path(inner[0]).name) if inner else None
    return native if native in ('claude', 'codex', 'agy') else ALIASES.get(route, route)


def account_scope(route, template=()):
    """Explicit opaque pool label, otherwise a disclosed single-login assumption.

    Known CLI profile selectors split scopes without reading credentials. Paths
    are hashed locally and are never written to the shared inventory.
    """
    route = native_route(route, template)
    label = os.environ.get('FLOW_ACCOUNT_SCOPE_' + route.upper())
    if label:
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', label):
            raise ValueError('invalid account scope label')
        return label
    names = {'codex': 'CODEX_HOME', 'claude': 'CLAUDE_CONFIG_DIR', 'agy': 'CLOUDSDK_CONFIG'}
    name = names.get(route)
    profile = os.environ.get(name, '') if name else ''
    inner = native_argv(template) if template else ()
    if template and inner != template:
        if '-i' in template[:len(template)-len(inner)] or '--ignore-environment' in template[:len(template)-len(inner)]:
            profile = ''
        for token in template[:len(template)-len(inner)]:
            if name and token.startswith(name + '='):
                profile = token.split('=', 1)[1]
    for i, token in enumerate(inner):
        if token == '--profile' and i + 1 < len(inner):
            profile += '|profile=' + inner[i+1]
        elif token.startswith('--profile='):
            profile += '|' + token
    return 'profile-' + hashlib.sha256(profile.encode()).hexdigest()[:24] if profile else 'default-login'


def read_json(path, default=None):
    """Bound memory and parse work even with a damaged local file."""
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('local routing state exceeds byte limit')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError('local routing state must be an object')
        return value
    except FileNotFoundError:
        return {} if default is None else default


def load_policy():
    return read_json(policy_path()) if policy_path().exists() else None


def coding_pin(route, policy=None):
    policy = load_policy() if policy is None else policy
    role = {'agy': 'gemini', 'agy-flow': 'gemini', 'gemini': 'gemini',
            'codex': 'codex', 'claude': 'claude_fallback'}.get(route)
    if policy is None or role is None:
        return None
    return _pin(policy, 'coding', role)


def _pin(policy, section, role):
    try:
        pin = policy[section][role]
        if not isinstance(pin, dict) or any(not isinstance(pin[k], str) or not pin[k].strip()
                                            for k in ('model', 'effort')):
            raise ValueError('missing exact pin')
        return pin
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f'model_routes: invalid policy {section}.{role}: {exc}') from exc


def _epoch(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('observation time must be a nonnegative finite number')
    return value


LEGACY_CREDIT_REASON = 'Actual subscription CLI requires usage credits; no paid fallback.'


def entitlement_observation(row):
    return (row.get('entitlement') is True or row.get('status') == 'credit_denied' or
            (row.get('authority', 'account') == 'account' and
             row.get('status') == 'cooldown' and row.get('reason') == LEGACY_CREDIT_REASON))


def local_model_blocked(info, route, model, state, *, scope, now):
    """Only identified credit holds may yield to an explicit entitlement recheck."""
    credit = info.get('reason') in ('credit_denied', LEGACY_CREDIT_REASON)
    if credit:
        stamp = info.get('observed_at')
        if stamp is None:
            try:
                stamp = datetime.fromisoformat(info.get('seen', '').replace('Z', '+00:00')).timestamp()
            except ValueError:
                stamp = info.get('disabled_until', float('inf'))
        released = any(entitlement_observation(r) and r.get('status') == 'recheck' and
                       r.get('route') == ALIASES.get(route, route) and r.get('model') == model and
                       r.get('scope', 'default-login') == scope and
                       stamp < r.get('observed_at', 0) <= now
                       for r in state.get('observations', []))
        return not released
    return info.get('disabled_until', 0) > now


def local_account_blocked(tool_state, route, model, state, *, scope, now):
    key = account_key(route, model, scope)
    return any(local_model_blocked(tool_state.get(k, {}), route, model, state,
                                   scope=scope, now=now) for k in (key, key + ':subscription-credit'))


def unavailable(route, model, *, state=None, now=None, scope=None):
    """An owner exclusion can only be released by a newer owner observation.

    Account evidence is ordered by event time, never collection/array order.
    Shared pools apply only with an explicit route/model membership record.
    """
    now = time.time() if now is None else now
    route = ALIASES.get(route, route)
    state = read_json(inventory_path()) if state is None else state
    scope = account_scope(route) if scope is None else scope
    rows = state.get('observations', [])
    if not isinstance(rows, list) or len(rows) > MAX_OBSERVATIONS:
        raise ValueError('model_routes: invalid observations')
    pools = state.get('pools', {})
    if not isinstance(pools, dict) or len(pools) > 64:
        raise ValueError('model_routes: invalid pools')
    latest = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('model_routes: invalid observation')
        if row.get('authority', 'account') != 'owner' and row.get('scope', 'default-login') != scope:
            continue
        pool = row.get('pool')
        matches = ([route, model] in pools.get(pool, []) if pool else
                   (ALIASES.get(row.get('route'), row.get('route')) == route and
                    row.get('model') in (model, '*')))
        if not matches:
            continue
        authority = row.get('authority', 'account')
        if authority not in ('owner', 'account'):
            continue  # discovery hints never change eligibility
        stamp = _epoch(row.get('observed_at'))
        if stamp > now:
            continue
        # Keep owner/account and exact/wildcard/pool scopes independent.
        key = (authority, pool, ALIASES.get(row.get('route'), row.get('route')), row.get('model'), entitlement_observation(row))
        prior = latest.get(key)
        restrictive = row.get('status') in ('excluded', 'cooldown', 'credit_denied')
        if prior is None or (stamp, restrictive) > (prior['observed_at'], prior.get('status') in ('excluded','cooldown','credit_denied')):
            latest[key] = row
    for row in latest.values():
        status = row.get('status')
        if status == 'excluded':
            return 'owner_excluded' if row.get('authority') == 'owner' else 'account_excluded'
        if entitlement_observation(row) and status != 'recheck':
            return 'subscription_credit_denied'
        if status == 'cooldown' and _epoch(row.get('until', 0)) > now:
            return 'account_cooldown'
    return None


def credit_hold_hint(route, model, *, state=None, scope=None, now=None, local=None, cache=None):
    """Human diagnostics only; reuse eligibility and never expose raw refusal metadata."""
    route = ALIASES.get(route, route)
    scope = account_scope(route) if scope is None else scope
    now = time.time() if now is None else now
    state = read_json(inventory_path()) if state is None else state
    held = unavailable(route, model, state=state, scope=scope, now=now) == 'subscription_credit_denied'
    if local:
        key = account_key(route, model, scope)
        held = held or any(info.get('reason') in ('credit_denied', LEGACY_CREDIT_REASON) and
            local_model_blocked(info, route, model, state, scope=scope, now=now)
            for info in (local.get(key, {}), local.get(key + ':subscription-credit', {})))
    if not held:
        return ''
    # Quote display fields as well as shell arguments; never interpolate raw reasons.
    display = lambda value: json.dumps(str(value), ensure_ascii=True)
    message = (f'Model {display(model)} skipped: subscription requires usage credits; '
               f'no paid fallback. Route {display(route)}, account/profile {display(scope)}.')
    accounts = state.get('subscription_accounts') or {}
    account = accounts.get(route + ':' + scope, {}) if isinstance(accounts, dict) else {}
    account = account if isinstance(account, dict) else {}
    signals = account.get('signals') or {}
    tier = signals.get('planTier') if isinstance(signals, dict) else None
    stamp = account.get('observed_at')
    # A different login in the same profile must not lend its tier to an old hold.
    mismatched = any(isinstance(row.get('baseline'), dict) and
        row['baseline'].get('identity') != account.get('identity')
        for row in state.get('observations', []) if row.get('route') == route and
        row.get('model') == model and row.get('scope', 'default-login') == scope and
        entitlement_observation(row) and row.get('status') != 'recheck')
    if (not mismatched and isinstance(tier, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', tier) and
            type(stamp) in (int, float) and math.isfinite(stamp) and 0 <= stamp <= now):
        message += f' Last verified tier: {tier}.'
    if scope == account_scope(route):
        argv = ['flow', 'inventory', 'recheck', '--model', model, '--route', route]
        if cache is not None:
            argv += ['--cache', str(cache)]
        message += ' After confirming subscription access, explicitly recheck: ' + shlex.join(argv)
    else:
        message += ' Switch to this account/profile and run flow inventory status for its recheck command.'
    return message


def contract_pin(template, model='', *, validated_worker=False):
    """Extract explicit CLI pins without interpreting prompt contents as flags."""
    if not isinstance(template, (list, tuple)) or not template or not all(isinstance(x,str) for x in template):
        raise ValueError('review contract must be argv')
    template = native_argv(template)
    pins = {'model': [], 'effort': []}
    values = {'-p', '--prompt', '--print', '-C', '--cd', '--cwd', '--output-format',
              '--output-schema', '--json-schema', '-o', '--output-last-message',
              '--allowedTools', '--disallowedTools', '--allowed-tools', '--disallowed-tools',
              '--max-budget-usd', '--max-turns', '--settings', '--print-timeout', '--sandbox', '--mode',
              '--ask-for-approval', '-a', '--add-dir'}
    argv = [v.replace('{model}', model) for v in template[1:]]
    i = 0
    while i < len(argv):
        token = argv[i]; i += 1
        if token == '--': break
        option, sep, inline = token.partition('=')
        def value():
            nonlocal i
            if sep: return inline
            if i >= len(argv): raise ValueError(f'review option {option} requires a value')
            v = argv[i]; i += 1; return v
        if option in ('--model','-m','--effort'):
            pins['effort' if option == '--effort' else 'model'].append(value())
        elif option in ('-c','--config') and Path(template[0]).name == 'codex':
            key, _, val = value().partition('=')
            key = key.strip()
            if key in ('model','model_reasoning_effort'):
                pins['model' if key == 'model' else 'effort'].append(val.strip('"\''))
            elif not validated_worker:
                raise ValueError('review contract has unsupported Codex config')
        elif option == '--sandbox' and Path(template[0]).name in ('agy', 'agy-flow'):
            pass
        elif option in values or option == '--permission-mode':
            value()
        elif not validated_worker and option.startswith('-') and option not in (
                '-', '--skip-git-repo-check', '--ephemeral', '--json', '--no-session-persistence'):
            raise ValueError(f'unsupported review option {option}')
    if any(len(pins[k]) != 1 or not pins[k][0] for k in pins):
        raise ValueError('review requires one exact model and effort pin')
    return pins['model'][0], pins['effort'][0]


def carbon_read_settings():
    """Single scoped headless read contract; no persistent settings mutation."""
    return {'permissions': {
        'allow': ['read_file({projdir}/**)',
                  'read_file({projdir}/.gate/final-review-evidence/**)',
                  'read_file(' + str(policy_path().with_name('POLICY.md')) + ')',
                  'read_file(' + str(policy_path()) + ')'],
        'deny': ['write_file(*)', 'command(*)', 'unsandboxed(*)',
                 'read_url(*)', 'execute_url(*)', 'mcp(*)']}}


def carbon_read_contract(template):
    """Upgrade legacy plan argv, or validate the exact scoped session settings."""
    result = list(template)
    settings = []
    values = {'--model', '-m', '--effort', '--mode', '-p', '--prompt', '--print',
              '--output-format', '--json-schema', '--print-timeout', '--add-dir'}
    inner = native_argv(result)
    # Parse native options only; prompt text cannot introduce settings flags.
    i = 1
    while i < len(inner):
        option, sep, inline = inner[i].partition('='); i += 1
        if option == '--':
            break
        if option == '--settings' or option in values:
            value = inline if sep else inner[i] if i < len(inner) else ''
            if not sep:
                i += 1
            if option == '--settings':
                settings.append(json.loads(value))
    expected = carbon_read_settings()
    if settings and settings != [expected]:
        raise ValueError('review contract requires scoped read-only settings')
    if not settings:
        # Insert before the native arguments, preserving env/TLS wrappers.
        at = len(result) - len(inner) + 1
        result[at:at] = ['--settings', json.dumps(expected, sort_keys=True)]
    return result


def _readonly(template, tool):
    """Known central adapters have one enforced mode and no alternate agent route."""
    template = native_argv(template)
    flag, expected = (('--sandbox','read-only') if tool == 'codex' else
                      ('--permission-mode','plan') if tool == 'claude' else ('--mode','plan'))
    modes = []
    value_options = {'--model','-m','--effort','-c','--config','-p','--prompt','--print',
                     '-C','--cd','--cwd','--output-schema','--output-format','--json-schema',
                     '-o','--output-last-message','--allowedTools','--allowed-tools',
                     '--disallowedTools','--disallowed-tools','--max-budget-usd',
                     '--max-turns','--print-timeout'}
    if tool == 'agy':
        value_options.add('--settings')
    i = 1
    while i < len(template):
        option, sep, inline = template[i].partition('='); i += 1
        if option == '--': break
        if option == flag or option in value_options:
            val = inline if sep else template[i] if i < len(template) else ''
            if not sep: i += 1
            if option == flag: modes.append(val)
        elif option in ('--sandbox','-s','--mode','--permission-mode','--settings',
                        '--full-auto','--dangerously-skip-permissions',
                        '--dangerously-bypass-approvals-and-sandbox','--profile',
                        '--agent','--agents','--fallback-model'):
            raise ValueError('review contract cannot override read-only mode or route')
    if modes != [expected]:
        raise ValueError('review contract requires explicit read-only mode')


def review_contracts(cfg, chain, defaults, actual_workers=(), *, diagnostic=None):
    """Validate actual contracts, never disqualify a role by its display alias.

    Historical pins and wrappers are preserved; native Carbon plan commands
    receive scoped read settings. Only the implicit built-in chain is replaced
    with centrally pinned independent adapters.
    Non-owner installations retain their existing custom contracts.
    """
    policy = load_policy()
    overrides = cfg.get('judge_cmds') or {}
    cmds = dict(defaults); cmds.update(overrides)
    if policy is None:
        chain = chain or ['fable', 'opus', 'codex']
        workers = {native_route(w, (cfg.get('worker_cmds') or {}).get(w, ()))
                   for w in actual_workers}
        state = read_json(inventory_path())
        result = []
        for member in chain:
            if member not in cmds:
                continue
            template = cmds[member]
            tool = native_route(member, template)
            if tool in workers:
                continue
            # Legacy unpinned contracts retain unknown identity. Explicit pins
            # share the same local availability evidence as policy installations.
            try:
                model = contract_pin(template)[0]
            except ValueError:
                model = ''
            scope = account_scope(tool, template)
            if model and unavailable(tool, model, state=state, scope=scope):
                hint = credit_hold_hint(tool, model, state=state, scope=scope)
                if hint and diagnostic:
                    diagnostic(hint)
                continue
            result.append((member, tool, template))
        if not result:
            raise ValueError('review model_routes: no independent eligible reviewer')
        return result
    implicit = not (cfg.get('judge') or {}).get('chain')
    if implicit:
        # Preserve expressly configured contracts, while replacing only the
        # unconfigured legacy defaults. Explicit historical chains stay exact.
        chain = list(dict.fromkeys([m for m in chain if m in overrides] + ['carbon', 'astra']))
    workers = set()
    for worker in actual_workers:
        template = (cfg.get('worker_cmds') or {}).get(worker)
        native = Path(native_argv(template)[0]).name if template else worker
        workers.add(ALIASES.get(native, native))
    result = []
    hints = []
    state = read_json(inventory_path())
    for member in chain:
        tool = ALIASES.get(member, member)
        template = overrides.get(member)
        # Custom aliases for a known native CLI must satisfy the same policy.
        if template is not None:
            if not isinstance(template, (list, tuple)) or not template:
                raise ValueError('review contract must be nonempty argv')
            executable = Path(native_argv(template)[0]).name
            native = ALIASES.get(executable, executable)
            if native in ('agy', 'codex', 'claude'):
                tool = native
            elif tool in ('agy', 'codex', 'claude'):
                raise ValueError('review model_policy: native alias cannot hide a wrapper')
        if tool in ('agy','codex','claude'):
            role = {'agy':'gemini', 'codex':'codex', 'claude':'claude'}[tool]
            pin = _pin(policy, 'design', role)
            if template is None and (implicit or member in ('carbon','astra','agy','gemini')):
                template = (['codex','exec','--model',pin['model'],'-c',f'model_reasoning_effort="{pin["effort"]}"',
                             '--sandbox','read-only','--skip-git-repo-check','-C','{projdir}',
                             '--output-schema','{schema_file}','-o','{out_file}','-'] if tool == 'codex' else
                            ['agy','--model',pin['model'],'--effort',pin['effort'],'--mode','plan',
                             '--output-format','text','-p','{prompt} Return plain JSON conforming to {schema}.'])
            elif template is None:
                template = cmds.get(member)
            if template is None or contract_pin(template) != (pin['model'],pin['effort']):
                raise ValueError(f'review model_policy: stale/ineligible {member} contract')
            if tool == 'agy':
                template = carbon_read_contract(template)
            _readonly(template, tool)
            model = pin['model']
            blocked = unavailable(tool,model,state=state,scope=account_scope(tool, template))
            if blocked and (not implicit or member in overrides):
                hint = credit_hold_hint(tool, model, state=state, scope=account_scope(tool, template))
                if hint and hint not in hints:
                    hints.append(hint)
            if tool not in workers and not blocked:
                result.append((member,tool,template))
        elif member in cmds and tool not in workers:
            result.append((member,tool,cmds[member]))
        else:
            raise ValueError(f'review model_policy: no eligible contract for {member}')
    if not result:
        raise ValueError('review model_policy: no independent eligible reviewer' +
                         ('\n' + '\n'.join(hints) if hints else ''))
    if diagnostic:
        for hint in hints:
            diagnostic(hint)
    return result


def consultation_contracts(cfg, chain, defaults):
    """Consultation uses the existing guarded review contracts in fresh sessions.

    No implicit provider calls on portable hosts; their project must explicitly
    supply the chain and argv. Owner hosts resolve exact design pins locally.
    """
    policy = load_policy()
    if policy is None:
        if not ((cfg.get('judge') or {}).get('chain') and cfg.get('judge_cmds')):
            return []
        return review_contracts(cfg, chain, defaults)
    pin = _pin(policy, 'design', 'claude')
    overrides = dict(cfg.get('judge_cmds') or {})
    overrides.setdefault('fable', [
        'claude', '-p', '{prompt}', '--model', pin['model'], '--effort', pin['effort'],
        '--permission-mode', 'plan', '--allowedTools', 'Read,Grep,Glob',
        '--output-format', 'json', '--json-schema', '{schema}',
        '--max-budget-usd', '{budget_usd}'])
    consult_cfg = dict(cfg, judge={**(cfg.get('judge') or {}),
                                   'chain': ['carbon', 'astra', 'fable']},
                       judge_cmds=overrides)
    return review_contracts(consult_cfg, ['carbon', 'astra', 'fable'], defaults)


def worker_model(template, model=''):
    """Extract native pins after the worker policy guard; preserve custom CLIs."""
    if not template or ALIASES.get(Path(template[0]).name, Path(template[0]).name) not in ('codex', 'agy', 'claude'):
        return model
    return contract_pin(template, model, validated_worker=True)[0]

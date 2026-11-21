"""Weekly out-of-band discovery and ordered machine account observations.

No inference or policy edits. Readers never wait for this writer. A failed refresh
retains the complete last-good snapshot, including independently owned limits.
"""
from contextlib import contextmanager
from datetime import date
import json
import hashlib
import importlib.util
import math
import os
import re
import selectors
import subprocess
from pathlib import Path
import tempfile
import time

_spec = importlib.util.spec_from_file_location(
    '_inventory_model_routes', Path(__file__).resolve().parents[1] / 'gate/model_routes.py')
if _spec is None or _spec.loader is None:
    raise ImportError('Cannot load shared model route eligibility')
routes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(routes)
MAX_BYTES, MAX_OBSERVATIONS = routes.MAX_BYTES, routes.MAX_OBSERVATIONS
inventory_path, load_policy, read_json, _epoch = routes.inventory_path, routes.load_policy, routes.read_json, routes._epoch

WEEK = 7 * 86_400
AGY_TIMEOUT_S = 10
# Release/capability hints only; never authorization to adopt new model pins.
PRIMARY_SOURCES = (
    {'provider': 'google', 'authority': 'primary',
     'url': 'https://antigravity.google/docs/models.md'},
    {'provider': 'anthropic', 'authority': 'primary',
     'url': 'https://platform.claude.com/docs/en/models/overview.md'},
    {'provider': 'openai', 'authority': 'primary',
     'url': 'https://developers.openai.com/api/docs/models.md'},
    {'provider': 'openai', 'authority': 'primary',
     'url': 'https://learn.chatgpt.com/docs/models.md'},
)


def process_identity(pid=None):
    pid = os.getpid() if pid is None else pid
    try:
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        stat = Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        return f'{boot}:{stat[19]}'
    except (OSError, IndexError):
        return None


@contextmanager
def refresh_lock(path):
    """Kernel ownership survives PID reuse and releases on process death."""
    path = Path(path); path.parent.mkdir(parents=True,exist_ok=True)
    lock = path.with_suffix('.lock')
    with lock.open('a+b') as stream:
        try:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0); stream.write(b' '); stream.flush(); stream.seek(0)
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            yield False
            return
        try:
            stream.seek(0); stream.truncate()
            stream.write(json.dumps({'pid':os.getpid(),'identity':process_identity()}).encode());stream.flush()
            yield True
        finally:
            if os.name == 'nt':
                stream.seek(0); msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def _atomic(path, state):
    raw = json.dumps(state,sort_keys=True,allow_nan=False).encode()
    if len(raw) > MAX_BYTES: raise ValueError('inventory exceeds byte limit')
    fd, name = tempfile.mkstemp(prefix='.inventory-',dir=Path(path).parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(raw);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)


def _native_json_output(argv):
    """Bounded non-inference CLI output, shared by weekly discovery adapters."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    raw = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + AGY_TIMEOUT_S
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError('native discovery timed out')
                if not selector.select(remaining):
                    raise ValueError('native discovery timed out')
                chunk = os.read(proc.stdout.fileno(), min(65536, MAX_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > MAX_BYTES:
                    raise ValueError('native discovery exceeds byte limit')
            if proc.wait(timeout=max(.001, deadline-time.monotonic())) != 0:
                raise ValueError('native discovery unavailable')
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
    return raw.decode('utf-8')


def collect_claude_account(*, now=None):
    """Weekly trusted auth status only; persist no identifiers or credentials."""
    data = json.loads(_native_json_output(['claude', 'auth', 'status', '--json']))
    if not isinstance(data, dict):
        raise ValueError('invalid subscription account status')
    if (data.get('loggedIn') is not True or data.get('authMethod') not in ('claude.ai', 'oauth')
            or data.get('apiProvider') != 'firstParty'):
        raise ValueError('subscription account status unavailable')
    signals = {key: data.get(key) for key in ('subscriptionType', 'planTier')}
    if any(not isinstance(v, str) or not v.strip() or len(v) > 100 for v in signals.values()):
        raise ValueError('subscription entitlement signals unavailable')
    # A profile alone cannot distinguish a different login in that profile.
    # Without a stable identity the manual path is deliberately required.
    identity = {k: data[k] for k in ('email', 'accountId', 'orgId', 'organizationId')
                if isinstance(data.get(k), str) and data[k]}
    if not any(identity.get(k) for k in ('email', 'accountId')):
        raise ValueError('subscription identity unavailable; use inventory recheck')
    return dict(route='claude', scope=routes.account_scope('claude'),
                identity=hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest(),
                signals=signals, observed_at=time.time() if now is None else now)


def collect_agy(*, now=None):
    """Native subscription catalog; called only by weekly refresh."""
    now = time.time() if now is None else now
    text = _native_json_output(['agy', 'models'])
    try:
        data = json.loads(text)
        models = data.get('models', []) if isinstance(data, dict) else data
        version = str(data.get('version', 'unknown'))[:200] if isinstance(data, dict) else 'unknown'
        if not isinstance(models, list) or any(not isinstance(m, (str, dict)) for m in models):
            raise ValueError('invalid agy catalog')
        ids = [m if isinstance(m,str) else m.get('id') or m.get('slug') or m.get('model') for m in models]
    except json.JSONDecodeError:
        text = re.sub(r'\x1b\[[0-9;]*m', '', text)
        ids = re.findall(r'\b(?:gemini|gpt|claude)-[A-Za-z0-9_.-]+', text)
        version = 'unknown'
    if not ids or len(ids) > 2048 or any(not isinstance(m,str) or not m.strip() for m in ids):
        raise ValueError('invalid agy catalog')
    return [dict(route='agy', model=m, version=version, source='agy models',
                 observed_at=now, kind='catalog_visibility') for m in sorted(set(ids))]


def collect_local(*, catalogs=None, now=None, native_agy=False):
    """Read existing CLI catalogs; optionally include the native no-inference listing.

    Additional CLI routes supply their existing local catalog path explicitly;
    absence is unknown, never evidence of quota or entitlement.
    """
    now = time.time() if now is None else now
    catalogs = {'codex':Path.home()/'.codex/models_cache.json'} if catalogs is None else catalogs
    result = []
    for route, path in catalogs.items():
        path = Path(path)
        if not path.exists(): continue
        data = read_json(path)
        models = data.get('models',[])
        if not isinstance(models,list) or len(models) > 2048:
            raise ValueError('invalid local catalog')
        version = str(data.get('client_version') or data.get('version') or 'unknown')[:200]
        stamp = min(now,path.stat().st_mtime)
        for model in models:
            if not isinstance(model, (str, dict)):
                raise ValueError('invalid catalog model')
            model_id = model if isinstance(model,str) else (model.get('slug') or model.get('id') or model.get('model'))
            if not isinstance(model_id,str) or not model_id.strip():
                raise ValueError('catalog model ID missing')
            result.append(dict(route=route,model=model_id,version=version,
                               source=str(path),observed_at=stamp,kind='catalog_visibility'))
    if native_agy:
        result.extend(collect_agy(now=now))
    if not result: raise ValueError('no local catalogs available; entitlement unknown')
    return result


def collect_hints(store, transport, *, sources=PRIMARY_SOURCES, now=None):
    """Use existing bounded HTTPS transport and immutable learner evidence storage.

    Caller-supplied secondary sources require a date and stay advisory. Failures
    are reported separately so local discovery can retain its useful snapshot.
    """
    from urllib.parse import urlparse
    now = store.now() if now is None else now
    hints, errors = [], []
    if len(sources) > 16:
        raise ValueError('too many hint sources')
    for source in sources:
        spec = {'url':source,'authority':'primary'} if isinstance(source,str) else source
        try:
            url = spec['url']; parsed = urlparse(url)
            if parsed.scheme != 'https' or not parsed.hostname:
                raise ValueError('inventory hints require HTTPS')
            authority = spec.get('authority','secondary')
            if authority not in ('primary', 'secondary'):
                raise ValueError('invalid hint authority')
            if authority == 'secondary':
                date.fromisoformat(spec.get('as_of', ''))
            model = spec.get('model')
            if model:
                if any(not isinstance(spec.get(k), str) or not spec[k].strip()
                       for k in ('model', 'provider', 'reviewed_at', 'reviewed_by')):
                    raise ValueError('version-specific hint requires explicit review provenance')
                date.fromisoformat(spec['reviewed_at'])
            response = transport.fetch(url,timeout_s=10,max_bytes=65536,allowed_hosts=(parsed.hostname,))
            if response.status_code != 200 or response.truncated or len(response.body) > 65536:
                raise ValueError('hint source unavailable or truncated')
            if model and not re.search(r'(?<![A-Za-z0-9_.-])' + re.escape(model) + r'(?![A-Za-z0-9_.-])',
                                       response.body.decode('utf-8', errors='replace')):
                raise ValueError('reviewed model version missing from source evidence')
            text = response.body.decode('utf-8', errors='replace')
            # Advisory evidence names only models/families literally present in
            # the source. These names never become policy or eligibility pins.
            names = list(dict.fromkeys(re.findall(
                r'(?i)\b(?:gpt(?:-[a-z0-9.]+)*|gemini(?:[- ][0-9][a-z0-9.-]*)?|claude(?:-[a-z0-9.]+)*|codex|opus|sonnet|haiku|o[134])\b', text)))[:32]
            lines = [line.strip() for line in text.splitlines()
                     if re.search(r'(?i)\b(?:gpt|gemini|claude|codex|opus|sonnet|haiku|o[134])\b', line)]
            summary = '\n'.join(line[:500] for line in lines[:8])[:3000]
            digest = store.put_evidence(response.body,visibility='public',content_type='text/plain')
            hints.append(dict(source=url,authority=authority,as_of=spec.get('as_of') or time.strftime('%Y-%m-%d', time.gmtime(now)),
                              model=model, provider=spec.get('provider'), reviewed_at=spec.get('reviewed_at'),
                              reviewed_by=spec.get('reviewed_by'),
                              observed_at=now,models=names,summary=summary,evidence_digest=digest,kind='advisory_release_capability'))
        except Exception as exc:
            errors.append(str(exc)[:300])
    return hints, errors


def refresh_due(*, path=None, now=None, collector=None, hint_collector=None, account_collector=None):
    path = Path(path) if path is not None else inventory_path()
    now = time.time() if now is None else _epoch(now)
    try:
        state = read_json(path)
        refreshed = state.get('refreshed_at')
        if refreshed is not None and 0 <= now - _epoch(refreshed) < WEEK:
            return {'status':'fresh','refreshed_at':refreshed}
        with refresh_lock(path) as acquired:
            if not acquired: return {'status':'busy'}
            # A second writer may have completed between the initial read and lock.
            state = read_json(path)
            refreshed = state.get('refreshed_at')
            if refreshed is not None and 0 <= now - _epoch(refreshed) < WEEK:
                return {'status':'fresh','refreshed_at':refreshed}
            # Persist the attempt separately: failures retain the exact last-good
            # snapshot and cannot retry external discovery on every daily tick.
            attempt_path = path.with_suffix('.attempt.json')
            attempt = read_json(attempt_path).get('attempted_at')
            if attempt is not None and 0 <= now - _epoch(attempt) < WEEK:
                return {'status': 'deferred', 'attempted_at': attempt}
            _atomic(attempt_path, {'attempted_at': now})
        # Discovery is outside the shared account writer lease. The persisted
        # weekly attempt reserves this refresh while account events stay writable.
        rows = (collector or collect_local)()
        if not isinstance(rows,list) or not rows or len(rows)>2048:
            raise ValueError('inventory requires bounded nonempty catalog records')
        for row in rows:
            if not isinstance(row,dict) or any(not isinstance(row.get(k),str) or not row[k].strip()
                  for k in ('route','model','source','version')):
                raise ValueError('invalid inventory record')
            _epoch(row.get('observed_at'))
        account = None
        account_errors = []
        if account_collector is not None:
            try:
                account = account_collector()
            except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                account_errors.append('subscription account status unavailable; holds retained')
        hints, hint_errors = hint_collector() if hint_collector is not None else ([], [])
        with refresh_lock(path) as acquired:
            if not acquired:
                return {'status': 'busy'}
            # Re-read the authoritative state, preserving events written while
            # discovery was in flight, including newer owner exclusions.
            updated = dict(read_json(path), version=1, refreshed_at=now, models=rows)
            _merge_account(updated, dict(account, observed_at=now) if account else None, now)
            if hints:
                sources = {h.get('source') for h in hints}
                updated['hints'] = [h for h in updated.get('hints', [])
                                    if h.get('source') not in sources] + hints
            _atomic(path,updated)
        return {'status':'refreshed','refreshed_at':now,'models':len(rows),'hint_errors':hint_errors, 'account_errors':account_errors}
    except (OSError,ValueError,TypeError,KeyError,subprocess.SubprocessError) as exc:
        return {'status':'stale_good','reason':str(exc)[:300]}


def record_observation(route, model, status, *, authority='account', observed_at=None,
                       until=None, reason='', pool=None, members=None, path=None, scope=None):
    """Record explicit evidence; quota never enters learned quality tables."""
    if status not in ('excluded','cooldown','success','unknown','released','credit_denied') or authority not in ('owner','account'):
        raise ValueError('invalid account observation')
    if status == 'excluded' and authority != 'owner':
        raise ValueError('permanent exclusion requires owner authority')
    if status == 'released' and authority != 'owner':
        raise ValueError('exclusion release requires owner authority')
    if not all(isinstance(v,str) and v.strip() for v in (route,model)):
        raise ValueError('route and exact model are required')
    route = routes.ALIASES.get(route, route)
    observed_at = time.time() if observed_at is None else _epoch(observed_at)
    if status == 'cooldown' and (until is None or _epoch(until) <= observed_at):
        raise ValueError('cooldown requires a future expiry')
    path = Path(path) if path is not None else inventory_path()
    scope = routes.account_scope(route) if scope is None else scope
    if not isinstance(scope, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', scope):
        raise ValueError('invalid account scope')
    row = dict(route=route,model=model,status=status,authority=authority,observed_at=observed_at,reason=reason[:1000],scope=scope)
    if until is not None: row['until'] = _epoch(until)
    if pool: row['pool'] = pool
    with refresh_lock(path) as acquired:
        if not acquired: raise ValueError('inventory writer busy; observation not recorded')
        state = read_json(path)
        _merge_account(state, None, observed_at)
        rows = state.get('observations',[])
        if not isinstance(rows,list): raise ValueError('invalid observations')
        if pool:
            if not isinstance(members,list) or not members or len(members)>64 or any(
                    not isinstance(m,list) or len(m)!=2 or not all(isinstance(v,str) and v for v in m) for m in members):
                raise ValueError('shared pool needs explicit evidenced route/model members')
            pools = dict(state.get('pools',{}));pools[pool]=members;state['pools']=pools
        identity = lambda r: (routes.ALIASES.get(r.get('route'), r.get('route')),
                              r.get('model'),r.get('authority', 'account'),r.get('pool'),routes.entitlement_observation(r),
                              r.get('scope', 'default-login') if r.get('authority') != 'owner' else None)
        if status == 'credit_denied':
            row['entitlement'] = True
            baseline = state.get('subscription_accounts', {}).get(route + ':' + scope)
            if baseline and baseline.get('observed_at', 0) <= observed_at:
                row['baseline'] = baseline
        previous = [r for r in rows if identity(r)==identity(row)]
        # Older account events must never overwrite newer restrictions.
        if previous:
            latest = max(r['observed_at'] for r in previous)
            if latest > observed_at or (latest == observed_at and
                    (status != 'credit_denied' or any(r.get('status') == 'credit_denied' for r in previous))):
                return {'status':'ignored_stale'}
        rows = [r for r in rows if identity(r)!=identity(row)] + [row]
        if len(rows)>MAX_OBSERVATIONS: raise ValueError('observation limit reached')
        state.update(version=1,observations=rows);_atomic(path,state)
    return {'status':'recorded','observation':row}


def exclude_current_fable(reason, *, path=None, now=None, release=False):
    policy = load_policy()
    try: model = policy['design']['claude']['model']
    except (KeyError,TypeError) as exc: raise ValueError('current central Fable pin unavailable') from exc
    if not reason.strip(): raise ValueError('owner report requires a reason')
    return record_observation('claude',model,'released' if release else 'excluded',authority='owner',
                              observed_at=now,reason=reason,path=path)


def _merge_account(state, account, now):
    """Called under the existing writer lock; merge against the latest denials."""
    rows = state.get('observations', [])
    for row in rows:
        if routes.entitlement_observation(row):
            row['entitlement'] = True
            if row.get('status') == 'cooldown':
                row.update(status='credit_denied')
                row.pop('until', None)
    if account is None:
        return
    if (account.get('route') != 'claude' or not isinstance(account.get('scope'), str) or
            not isinstance(account.get('identity'), str) or not account.get('signals') or
            _epoch(account.get('observed_at')) > now):
        raise ValueError('invalid subscription evidence')
    key = account['route'] + ':' + account['scope']
    accounts = state.setdefault('subscription_accounts', {})
    previous = accounts.get(key)
    if previous and previous['observed_at'] >= account['observed_at']:
        return
    for row in rows:
        if (row.get('status') != 'credit_denied' or row.get('route') != account['route'] or
                row.get('scope', 'default-login') != account['scope'] or
                row['observed_at'] >= account['observed_at']):
            continue
        baseline = row.get('baseline')
        if (baseline and baseline['identity'] == account['identity'] and
                baseline['signals'] != account['signals']):
            row.update(status='recheck', observed_at=account['observed_at'],
                       reason='Subscription entitlement changed; next authorized call may revalidate.')
        # First trusted weekly evidence establishes the baseline, never access.
        if baseline is None or baseline['identity'] == account['identity']:
            row['baseline'] = account
    accounts[key] = account


def recheck_subscription(model=None, *, route='claude', reason='', path=None, now=None):
    """Release only an exact current-profile subscription credit hold."""
    route = routes.ALIASES.get(route, route)
    if not model:
        try:
            model = load_policy()['design']['claude']['model']
        except (KeyError, TypeError) as exc:
            raise ValueError('specify the exact subscription model') from exc
        if route != 'claude':
            raise ValueError('specify the exact subscription model')
    path = Path(path) if path is not None else inventory_path()
    now = time.time() if now is None else _epoch(now)
    scope = routes.account_scope(route)
    with refresh_lock(path) as acquired:
        if not acquired:
            raise ValueError('inventory writer busy; recheck not recorded')
        state = read_json(path)
        _merge_account(state, None, now)
        count = 0
        for row in state.get('observations', []):
            if (row.get('authority', 'account') == 'account' and row.get('route') == route and
                    row.get('model') == model and row.get('scope', 'default-login') == scope and
                    row.get('status') == 'credit_denied' and row['observed_at'] < now):
                row.update(status='recheck', observed_at=now,
                           reason=('Manual subscription recheck: ' + reason)[:1000])
                count += 1
        if not count:
            # Also permits recovery of a local hold whose shared write failed.
            # The separate entitlement channel cannot release a quota/exclusion.
            rows = state.setdefault('observations', [])
            rows[:] = [r for r in rows if not (
                r.get('route') == route and r.get('model') == model and
                r.get('scope', 'default-login') == scope and
                r.get('status') == 'recheck' and r.get('observed_at', 0) <= now)]
            if len(rows) >= MAX_OBSERVATIONS:
                raise ValueError('observation limit reached')
            rows.append(dict(route=route, model=model, scope=scope, authority='account',
                             entitlement=True, status='recheck', observed_at=now,
                             reason=('Manual subscription recheck: ' + reason)[:1000]))
        _atomic(path, state)
    return {'status': 'recheck_allowed', 'route': route, 'model': model,
            'scope': scope, 'cleared': count}

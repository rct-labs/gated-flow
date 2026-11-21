"""Durable, finite consultation episodes; the gate owns invocation and acceptance.

Reservations are spent before dispatch, including outages and uncertain crashes.
No automatic retries of a reserved seat. Advice never becomes a review receipt.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {k: {'type': 'string'} for k in
                   ('diagnosis', 'evidence', 'next_change', 'test')},
    'required': ['diagnosis', 'evidence', 'next_change', 'test'],
}


def parse(output, out_file, last_json):
    candidates = []
    if out_file.exists():
        try:
            candidates.append(json.loads(out_file.read_text()))
        except (OSError, ValueError):
            pass
    outer = last_json(output)
    if outer:
        candidates.extend([outer.get('structured_output'), last_json(outer.get('result', ''))
                           if isinstance(outer.get('result'), str) else None, outer])
    for value in candidates:
        if (isinstance(value, dict) and all(isinstance(value.get(k), str)
                and value[k].strip() for k in SCHEMA['required'])
                and len(json.dumps(value).encode()) <= 12000):
            return {k: value[k] for k in SCHEMA['required']}
    return None


@contextlib.contextmanager
def locked(repo, try_lock):
    directory = Path(repo) / '.gate/consult'
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'owner.lock').open('a+b') as owner:
        if not try_lock(owner):
            raise ValueError('consultation/review already owned by another process')
        yield directory


def load(directory):
    path = directory / 'state.json'
    if not path.exists():
        return {'version': 1, 'lineages': {}}
    with path.open('rb') as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('consultation state exceeds limit; host reconciliation required')
    value = json.loads(raw)
    if value.get('version') != 1 or not isinstance(value.get('lineages'), dict):
        raise ValueError('invalid consultation state')
    return value


def save(directory, state):
    path = directory / 'state.json'
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(state, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def lineage(packet, digest):
    # Contract changes do not buy a fresh cumulative task budget. Package/spec
    # paths distinguish independently scoped queues reusing the same task IDs.
    return digest([packet['original_tasks'], packet.get('queue_path'),
                   sorted(packet.get('acceptance', {})), packet.get('package_paths', [])])


def entry(state, key):
    return state['lineages'].setdefault(key, {'spent': 0, 'rejections': {}, 'episodes': []})


def related(state, task_keys):
    return [item for item in state['lineages'].values()
            if set(item.get('task_keys', [])) & set(task_keys)]


def rejection(item, candidate, contract, verdict, blockers, repairs, digest):
    token = digest([candidate, contract])
    previous = item.get('last')
    required = (verdict.get('escalation_kind') not in ('human', 'infrastructure') and
                (verdict['verdict'] == 'escalate' or verdict.get('disputed') or
                (bool(repairs) and previous and previous.get('blockers')
                 and set(previous['blockers']) <= set(blockers))))
    result = dict(candidate=candidate, contract=contract, verdict=verdict,
                  blockers=blockers, consultation=bool(required), token=token)
    item['rejections'][token] = result
    item['last'] = result
    return result


def coordinate(directory, state, item, rejected, packet, seats, invoke, digest,
               available_calls, emit, reserve=lambda episode: None):
    """Hold the caller's OS lease through reservation, both calls and saves."""
    episode = next((e for e in item['episodes'] if e['token'] == rejected['token']), None)
    if episode and (episode.get('seats') or episode.get('reserved')):
        return episode  # includes reserved/uncertain calls; never replay them
    existing = episode
    episode = episode or dict(id=digest([rejected['token'], len(item['episodes'])])[:24],
                   token=rejected['token'], status='pending', reason='', seats=[],
                   candidate=rejected['candidate'], contract=rejected['contract'],
                   tasks=packet.get('original_tasks', []),
                   prior_episode=item['episodes'][-1]['id'] if item['episodes'] else None)
    if not existing:
        item['episodes'].append(episode)
    if len(seats) != 2:
        episode['reason'] = 'two distinct eligible senior families unavailable'
    elif (item['spent'] + 2 > 4 or available_calls < 2 or
          any(sum(other['spent'] for other in state['lineages'].values()
                  if task in other.get('task_keys', [])) + 2 > 4
              for task in item.get('task_keys', []))):
        episode['reason'] = 'finite consultation/task or remaining run budget unavailable'
    elif len(json.dumps(packet).encode()) > 24000:
        episode['reason'] = 'bounded consultation evidence packet exceeds 24000 bytes'
    else:
        episode['seats'] = [dict(member=s[0], tool=s[1], contract=s[2], status='reserved') for s in seats]
        item['spent'] += 2
        episode['reserved'] = True
        episode['reason'] = 'awaiting host evidence disposition'
    save(directory, state)  # reserve the entire round before the first call
    if episode['seats']:
        try:
            reserve(episode['id'])
        except (ValueError, OSError) as exc:
            episode.update(reason=str(exc), seats=[])
            save(directory, state)
    for index, seat in enumerate(episode['seats']):
        try:
            reply, why = invoke(seats[index], packet, episode['id'], index)
        except (OSError, ValueError, RuntimeError) as exc:
            reply, why = None, str(exc)
        seat.update(status='replied' if reply else 'unavailable', reply=reply, reason=why)
        save(directory, state)
    if len(episode['seats']) == 2 and all(s.get('reply') for s in episode['seats']):
        episode['status'] = 'needs_host'
        # Similar wording is not evidence of agreement. Host records any dissent.
        episode['reason'] = 'two independent replies retained; host must check evidence and dissent'
    save(directory, state)
    emit(episode)
    return episode


def hold(item):
    if not item:
        return None
    if not item.get('episodes'):
        return None
    episode = item['episodes'][-1]
    return None if episode['status'] in ('ready', 'reviewed') else episode


def settle(item, episode_id, disposition, candidate, contract):
    episode = item['episodes'][-1] if item.get('episodes') else None
    if not episode or episode['id'] != episode_id:
        raise ValueError('not the current consultation episode')
    if episode['candidate'] != candidate or episode['contract'] != contract:
        raise ValueError('candidate or contract changed; reconcile original evidence first')
    if episode['status'] not in ('needs_host', 'blocked'):
        raise ValueError('two successful replies and an unsettled episode are required')
    if (len(json.dumps(disposition).encode()) > 8000 or
            disposition.get('status') not in ('ready', 'blocked') or
            any(not isinstance(disposition.get(k), str) or not disposition[k].strip()
                for k in ('evidence_checked', 'dissent', 'repair', 'test'))):
        raise ValueError('host disposition requires evidence_checked, dissent, repair and test')
    episode.update(status=disposition['status'], disposition=disposition)
    return episode

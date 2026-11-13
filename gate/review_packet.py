"""Bounded review projections. Originals and receipts live in existing run/journal files."""
from __future__ import annotations

import fnmatch
import hashlib
import json
import subprocess
from pathlib import Path


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args])


def events(repo):
    path = Path(repo) / '.gate/journal.ndjson'
    for line in path.read_text().splitlines() if path.exists() else []:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def manifest(repo, base, head):
    # No rename folding: both the removed and added path remain visible, even
    # for binary files, deletions, spaces and newlines in filenames.
    fields = git(repo, 'diff', '--no-renames', '--numstat', '-z', base, head).split(b'\0')
    result = []
    for field in fields:
        if not field:
            continue
        added, deleted, path = field.split(b'\t', 2)
        name = path.decode('utf-8', 'surrogateescape')
        exists = subprocess.run(['git', '-C', str(repo), 'cat-file', '-e', f'{head}:{name}'],
                                capture_output=True).returncode == 0
        result.append(dict(path=name, added=added.decode(), deleted=deleted.decode(),
                           binary=added == b'-', removed=not exists))
    return result


def ancestor(repo, base, head):
    return subprocess.run(['git', '-C', str(repo), 'merge-base', '--is-ancestor', base, head],
                          capture_output=True).returncode == 0


def parsed(value):
    return (isinstance(value, dict) and value.get('verdict') in ('pass', 'revise', 'escalate')
            and type(value.get('score')) is int and 0 <= value['score'] <= 100
            and isinstance(value.get('revision_brief'), str)
            and isinstance(value.get('findings'), list)
            and all(isinstance(f, dict) and all(isinstance(f.get(k), str)
                    for k in ('issue', 'file', 'fix', 'id')) and bool(f['id'])
                    and f.get('severity') in ('low', 'medium', 'high', 'critical')
                    and f.get('action') in ('required', 'optional', 'none')
                    for f in value['findings']))


def build(repo, base, head, stage, tasks, declared, acceptance, evidence, defects,
          contract, repairs, full_globs=(), review_of=None, navigation_paths=(), semantic_config_paths=()):
    base = git(repo, 'rev-parse', '--verify', base + '^{commit}').decode().strip()
    head = git(repo, 'rev-parse', '--verify', head + '^{commit}').decode().strip()
    review_of = review_of or {}
    originals = sorted({o for t in tasks for o in review_of.get(t, [t])})
    mapped = (bool(repairs) and all(review_of.get(t) for t in repairs)
              and set(tasks) - set(repairs) <= {o for t in repairs for o in review_of.get(t, [])})
    # The latest relevant receipt owns the baseline. Never fall back past a
    # damaged or incomplete newer receipt to manufacture compatibility.
    prior = next((e for e in reversed(list(events(repo)))
                  if e.get('event') == 'review_verdict'
                  and set(e.get('original_tasks', e.get('tasks', []))) == set(originals)), None)
    valid_prior = (prior and artifact_valid(prior) and parsed(prior.get('parsed_review'))
                   and prior.get('base') and prior.get('head')
                   and ancestor(repo, prior['base'], prior['head'])
                   and ancestor(repo, prior['head'], head))
    if mapped and valid_prior:
        base = prior['base']
    whole = manifest(repo, base, head)
    contract_hash = digest(contract)
    mode, anchor = 'full', base
    reason = 'no compatible parsed repair baseline'
    if (mapped and valid_prior and prior.get('contract_hash') == contract_hash
            and prior.get('stage') == stage and prior.get('base') == base
            and stage != 'delivery'):
        delta = manifest(repo, prior['head'], head)
        repair_paths = [p for t in repairs for p in declared.get(t, [])]
        original_paths = [p for t in originals for p in declared.get(t, [])]
        covered_paths = {e['path'] for e in manifest(repo, base, prior['head'])}
        prior_packet = json.loads(Path(prior['packet']).read_text())
        prior_paths = [p for t in set(prior_packet['tasks']) | set(originals)
                       for p in prior_packet['declared'].get(t, [])]
        risky = ['**/spec.md', '*spec.md', '**/config*', '.gate/**', '**/policy*',
                 '*policy*', 'AGENTS.md', 'gate/**', '**/auth/**', 'auth/**',
                 '**/security/**', 'security/**', '**/deploy*', 'deploy*',
                 '**/gate/**', '**/AGENTS.md']
        def covered(path, patterns):
            return (path in navigation_paths or path in semantic_config_paths
                    or any(fnmatch.fnmatch(path, p) for p in patterns))
        if (all(covered(e['path'], repair_paths) for e in delta)
                and all(covered(e['path'], prior_paths + original_paths + repair_paths) for e in whole)
                and all(covered(p, prior_paths) for p in covered_paths)
                and not any(fnmatch.fnmatch(e['path'], p)
                            and not (e['path'] in semantic_config_paths
                                     and p in ('.gate/**', '**/config*'))
                            for e in whole for p in risky)
                and not any(fnmatch.fnmatch(e['path'], p) for e in whole for p in full_globs)):
            mode, anchor, reason = 'incremental', prior['head'], 'validated mapped repair baseline'
    payload = dict(base=base, head=head, stage=stage, tasks=tasks, original_tasks=originals,
                   review_of={t: review_of[t] for t in tasks if t in review_of},
                   contract_hash=contract_hash, mode=mode, anchor=anchor, reason=reason,
                   manifest=whole, delta=manifest(repo, anchor, head), declared=declared,
                   acceptance=acceptance, evidence=evidence, defects=defects,
                   prior_blockers=(prior or {}).get('blocker_ids', []),
                   prior_findings=((prior or {}).get('parsed_review') or {}).get('findings', []) if parsed((prior or {}).get('parsed_review')) else [])
    payload['packet_hash'] = digest(payload)
    return payload


def render(packet, artifact, cap=24000):
    instructions = (f'\n[REVIEW EVIDENCE] Full packet: {artifact}\n'
                    f"Mode: {packet['mode']}; base: {packet['base']}; head: {packet['head']}; "
                    f"anchor: {packet['anchor']}.\n"
                    'Independent read-only review: inspect beyond this packet as needed. '
                    f"Read the requested diff FIRST with git diff --no-ext-diff --binary {packet['anchor']} {packet['head']}; "
                    + ('the full-range manifest is navigation, not a request to rereview unchanged code. '
                       'Inspect original blockers and directly affected callers for repair regressions. '
                       if packet['mode'] == 'incremental' else '') +
                    'read full source with git show HEAD:path and deleted source with git show BASE:path '
                    '(replace HEAD/BASE with the full hashes above). Binary changes need their own inspection. '
                    'Omitted evidence is NOT reviewed. Read omitted sections from the full packet and originals. '
                    'Prior blocker IDs stay stable; status labels alone never resolve defects.\n')
    # The original JSON is never truncated. Each projection is either included
    # whole or explicitly omitted; the cap never silently slices evidence.
    for key in ('tasks', 'acceptance', 'manifest', 'delta', 'evidence', 'defects',
                'prior_blockers', 'prior_findings'):
        section = key + ': ' + json.dumps(packet[key], ensure_ascii=True) + '\n'
        if len((instructions + section).encode()) <= cap - 1200:
            instructions += section
        else:
            instructions += f'{key}: OMITTED from prompt; read {artifact} ({len(section.encode())} bytes).\n'
    return instructions


def artifact_valid(receipt):
    try:
        packet = json.loads(Path(receipt['packet']).read_text())
        packet_hash = packet.pop('packet_hash')
        if (digest(packet) != packet_hash or packet_hash != receipt['packet_hash']
                or any(packet.get(k) != receipt.get(k) for k in
                       ('base', 'head', 'stage', 'tasks', 'original_tasks', 'contract_hash'))):
            return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return True


def receipt_valid(repo, cfg, receipt, tasks, contract_hash=None):
    if not (receipt.get('passed') and parsed(receipt.get('parsed_review'))
            and receipt.get('packet_hash') and receipt.get('contract_hash')
            and set(tasks) <= set(receipt.get('tasks', []))
            and receipt.get('stage') == (cfg.get('delivery') or {}).get('stage', 'delivery')):
        return False
    if contract_hash and receipt['contract_hash'] != contract_hash:
        return False
    if not artifact_valid(receipt):
        return False
    if subprocess.run(['git', '-C', str(repo), 'diff', '--quiet', 'HEAD'], capture_output=True).returncode:
        return False
    untracked = git(repo, 'ls-files', '--others', '--exclude-standard', '-z').split(b'\0')
    if any(path and not path.startswith(b'.gate/') for path in untracked):
        return False
    current = git(repo, 'rev-parse', 'HEAD').decode().strip()
    reviewed = receipt.get('head', '')
    if current == reviewed:
        return True
    # The gate's own notes commit is evidence, not a change to reviewed work.
    return (bool(reviewed) and current == receipt.get('recorded_head')
            and ancestor(repo, reviewed, current)
            and all(e['path'] == receipt.get('notes_path') for e in manifest(repo, reviewed, current)))

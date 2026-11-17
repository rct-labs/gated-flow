#!/usr/bin/env python3
"""Verified local Linux releases. No installer, inference, hooks or project writes.

The maintenance checkout owns source. Each immutable runtime is a deployment
copy; manifest.json and backup/ bind its inputs and recovery bytes. state.json
only tracks the reversible transaction. Use the CLI from the checkout, including
for recovery, so a broken active runtime cannot prevent rollback.
"""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

SKILLS = ('flow', 'flow-run', 'run-queue', 'model-debate')
PAYLOAD = ('flow', 'gate', 'learner', 'skills', 'docs', 'README.md', 'LICENSE')
IGNORED = {'__pycache__', '.git', '.gate', '.pytest_cache', '.learner-data', '.venv', 'build', 'dist'}
IGNORED_SUFFIXES = {'.pyc', '.pyo', '.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.db'}
CANONICAL_REPO = Path.home() / 'AI/maintenance/flow'
TRANSITION_PACKAGE = 'ai-evolution-ideas'
TRANSITION_KEYS = frozenset({
    'package',
    'repository',
    'reviewed_revision',
    'target_version',
    'prior_release',
    'prior_policy_sha256',
    'candidate_policy_sha256',
    'review_ref',
    'evaluation_ref',
})


def require(ok, message):
    if not ok:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def safe(path):
    """Refuse symlink traversal, including existing parents, before any write."""
    path = Path(os.path.abspath(path))
    for item in (path, *path.parents):
        require(not item.is_symlink(), f'symlink destination/source: {item}')
    return path


def _is_ignored_item(item, rel_posix=None, untracked=None):
    if item.name in IGNORED or item.name.endswith('.egg-info') or item.suffix in IGNORED_SUFFIXES:
        return True
    return bool(untracked and rel_posix is not None and rel_posix in untracked)


def _git_untracked(source):
    """Return untracked non-ignored relative posix paths when source is a git checkout."""
    if not (source / '.git').exists():
        return frozenset()
    res = subprocess.run(
        ['git', '-C', str(source), 'ls-files', '--others', '--exclude-standard', '-z'],
        capture_output=True, check=False,
    )
    require(res.returncode == 0, 'failed to inspect git untracked files')
    return frozenset(p for p in os.fsdecode(res.stdout).split('\0') if p)


def _git_head_and_dirty(source):
    """Return (HEAD_sha, is_dirty) for a git checkout, or (None, None) otherwise."""
    if not (source / '.git').exists():
        return None, None
    head = subprocess.run(
        ['git', '-C', str(source), 'rev-parse', '--verify', 'HEAD'],
        capture_output=True, text=True, check=False,
    )
    require(head.returncode == 0, 'failed to resolve git HEAD')
    status = subprocess.run(
        ['git', '-C', str(source), 'status', '--porcelain', '--untracked-files=all'],
        capture_output=True, text=True, check=False,
    )
    require(status.returncode == 0, 'failed to inspect git status')
    return head.stdout.strip(), bool(status.stdout.strip())


def inventory(path, _prefix='', untracked=None):
    path = safe(path)
    require(path.is_dir(), f'missing directory: {path}')
    result = {'.': {'mode': path.stat().st_mode & 0o7777}}
    for item in sorted(path.iterdir()):
        rel = item.name if not _prefix else f'{_prefix}/{item.name}'
        if _is_ignored_item(item, rel, untracked):
            continue
        require(not item.is_symlink(), f'symlink in tree: {item}')
        if item.is_dir():
            for name, data in inventory(item, _prefix=rel, untracked=untracked).items():
                key = item.name if name == '.' else f'{item.name}/{name}'
                result[key] = data
        else:
            require(item.is_file(), f'nonregular file: {item}')
            result[item.name] = {'sha256': hashlib.sha256(item.read_bytes()).hexdigest(),
                                 'mode': item.stat().st_mode & 0o7777}
    return result


def payload_inventory(source, *, check_untracked=False):
    untracked = _git_untracked(source) if check_untracked else frozenset()
    result = {}
    for name in PAYLOAD:
        path = safe(source / name)
        if path.is_dir():
            result[name] = inventory(path, _prefix=name, untracked=untracked)
        elif path.is_file() and name not in untracked:
            result[name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                            'mode': path.stat().st_mode & 0o7777}
    for name in ('flow', 'gate', 'skills'):
        require(name in result, f'missing runtime component: {name}')
    for name in ('flow/flow.py', 'gate/gate.py', 'gate/model_policy.py'):
        require((source / name).is_file(), f'missing entrypoint: {name}')
    if 'learner' in result or (source / 'flow/learning.py').is_file():
        for name in ('learner/__init__.py', 'learner/__main__.py', 'learner/cli.py'):
            require((source / name).is_file() and name not in untracked, f'missing entrypoint: {name}')
    for name in SKILLS:
        skill_identity(source / 'skills' / name, name)
    return result


def skill_identity(path, name):
    inventory(path)
    text = (path / 'SKILL.md').read_text()
    require(re.search(r'^name:\s*' + re.escape(name) + r'\s*$', text, re.M),
            f'unowned canonical skill: {path}')


def shim_bytes(runtime):
    # Quote only paths that can be safely embedded in the fixed shell template.
    require(not any(c in str(runtime) for c in '\n\r"`$\\'), 'unsafe runtime path')
    return f'#!/bin/sh\nexec python3 "{runtime}/flow/flow.py" "$@"\n'.encode()


def paths(home, version=None):
    home = safe(home)
    base = safe(home / '.local/share/flow')
    if version is None:
        return base
    require(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', version or ''), 'invalid version')
    return safe(base / version)


def read_shim(home):
    path = safe(home / '.local/bin/flow')
    data = path.read_bytes()
    match = re.fullmatch(rb'#!/bin/sh\nexec python3 "([^\n]+?)/flow/flow.py" "\$@"\n', data)
    require(match is not None, 'unowned flow shim')
    target = safe(Path(os.fsdecode(match[1])))
    base = paths(home)
    require(target.is_relative_to(base) and target != base, 'shim outside release root')
    require(data == shim_bytes(target), 'unowned flow shim')
    return {'hex': data.hex(), 'mode': path.stat().st_mode & 0o7777}, target


def aliases(home):
    result = {}
    for agent in ('.codex', '.claude'):
        for name in SKILLS:
            path = home / agent / 'skills' / name
            safe(path.parent)
            canonical = home / '.agents/skills' / name
            require(path.is_symlink() and path.resolve() == canonical,
                    f'unowned/missing skill alias: {path}')
            result[f'{agent}/skills/{name}'] = os.readlink(path)
    return result


def inspect_install(home):
    home = safe(home)
    shim, target = read_shim(home)
    result = {'shim': shim, 'target': str(target), 'aliases': aliases(home), 'skills': {}}
    result['runtime'] = payload_inventory(target)
    for name in SKILLS:
        path = home / '.agents/skills' / name
        skill_identity(path, name)
        result['skills'][name] = inventory(path)
    return result


def copy_tree(source, target, _prefix='', untracked=None):
    target.mkdir(parents=True, exist_ok=True)
    shutil.copystat(source, target)
    for item in sorted(source.iterdir()):
        rel = item.name if not _prefix else f'{_prefix}/{item.name}'
        if _is_ignored_item(item, rel, untracked):
            continue
        dst = target / item.name
        if item.is_dir():
            copy_tree(item, dst, _prefix=rel, untracked=untracked)
        else:
            shutil.copy2(item, dst)


def atomic_bytes(path, data, mode=0o600):
    safe(path)
    fd, tmp = tempfile.mkstemp(prefix='.flow-release-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_state(release, phase):
    atomic_bytes(release / 'state.json', json.dumps({'phase': phase}).encode())


@contextmanager
def locked(home):
    base = paths(home)
    require(base.is_dir(), 'existing versioned installation required')
    lock = safe(base / '.release.lock')
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def prior_release_id(home, target):
    rel = safe(target).relative_to(paths(home))
    require(len(rel.parts) >= 1, 'invalid prior release target')
    return rel.parts[0]


def _require_nonplaceholder_ref(value, field):
    require(isinstance(value, str) and bool(value.strip()), f'missing {field}')
    cleaned = value.strip()
    low = cleaned.lower()
    require(low not in ('pending', 'todo', 'tbd', 'none', 'model_opinion_only')
            and 'model_opinion_only' not in low, f'invalid {field}: {cleaned!r}')
    return cleaned


def _validate_policy_transition(source, home, version, before, source_hashes, transition):
    """Fail-closed validation for an explicit reviewed model_policy.py transition."""
    require(isinstance(transition, dict) and set(transition) == TRANSITION_KEYS,
            'policy_transition must contain exact reviewed transition keys')
    require(transition['package'] == TRANSITION_PACKAGE,
            f'wrong transition package: {transition["package"]!r}')
    pkg_dir = safe(source / 'docs/work' / TRANSITION_PACKAGE)
    require((pkg_dir / 'release.md').is_file() and (pkg_dir / 'spec.md').is_file(),
            'missing canonical ai-evolution package docs in source')

    repo_raw = transition['repository']
    require(isinstance(repo_raw, str) and os.path.isabs(repo_raw),
            'transition repository must be an absolute path')
    canonical = safe(CANONICAL_REPO)
    require(source == canonical and safe(Path(repo_raw)) == canonical,
            f'unrelated repository for policy transition: {source}')

    require('learner' in source_hashes, 'policy_transition requires learner package in runtime')
    require(transition['target_version'] == version,
            f'transition target_version mismatch: {transition["target_version"]!r} != {version!r}')
    actual_prior = prior_release_id(home, Path(before['target']))
    require(transition['prior_release'] == actual_prior,
            f'transition prior_release mismatch: {transition["prior_release"]!r} != {actual_prior!r}')

    prior_sha = before['runtime']['gate']['model_policy.py']['sha256']
    cand_sha = source_hashes['gate']['model_policy.py']['sha256']
    require(prior_sha != cand_sha, 'policy_transition requires changed model_policy.py')
    require(transition['prior_policy_sha256'] == prior_sha,
            'transition prior_policy_sha256 does not match installed policy')
    require(transition['candidate_policy_sha256'] == cand_sha,
            'transition candidate_policy_sha256 does not match candidate policy')

    rev = transition['reviewed_revision']
    require(isinstance(rev, str) and re.fullmatch(r'[0-9a-f]{40}', rev),
            'reviewed_revision must be a full 40-char git commit sha')
    head, dirty = _git_head_and_dirty(source)
    require(head is not None, 'policy_transition requires a git source checkout')
    require(not dirty, 'dirty source checkout refused for policy_transition')
    require(head == rev, f'stale or mismatched reviewed_revision: {rev} != HEAD {head}')

    review_ref = _require_nonplaceholder_ref(transition['review_ref'], 'review_ref')
    eval_ref = _require_nonplaceholder_ref(transition['evaluation_ref'], 'evaluation_ref')
    return {
        'package': TRANSITION_PACKAGE,
        'repository': str(canonical),
        'reviewed_revision': rev,
        'target_version': version,
        'prior_release': actual_prior,
        'prior_policy_sha256': prior_sha,
        'candidate_policy_sha256': cand_sha,
        'review_ref': review_ref,
        'evaluation_ref': eval_ref,
    }


def _compatibility_metadata(source, source_hashes):
    compat = {
        'python_min': '3.10',
        'skills': list(SKILLS),
        'learner_included': 'learner' in source_hashes,
    }
    pyproject = source / 'learner/pyproject.toml'
    if pyproject.is_file():
        match = re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject.read_text(encoding='utf-8'))
        if match:
            compat['learner_requires_python'] = match.group(1)
    return compat


def stage(source, home, version, expected_install, *, policy_transition=None, references=None):
    source, home = safe(source), safe(home)
    with locked(home):
        target = paths(home, version)
        require(not target.exists(), 'release already exists; choose a new version')
        require(not source.is_relative_to(paths(home)) and not paths(home).is_relative_to(source),
                'maintenance source must be separate from runtime root')
        before = inspect_install(home)
        require(digest(before) == expected_install, 'installation drift / unapproved baseline')
        untracked = _git_untracked(source)
        source_hashes = payload_inventory(source, check_untracked=True)
        validated_transition = None
        if policy_transition is not None:
            validated_transition = _validate_policy_transition(
                source, home, version, before, source_hashes, policy_transition)
        else:
            require(source_hashes['gate']['model_policy.py'] == before['runtime']['gate']['model_policy.py'],
                    'model_policy.py must remain byte-identical to installed policy')
        prior_release = prior_release_id(home, Path(before['target']))
        head, dirty = _git_head_and_dirty(source)
        refs = dict(references or {})
        if validated_transition is not None:
            refs.setdefault('review_ref', validated_transition['review_ref'])
            refs.setdefault('evaluation_ref', validated_transition['evaluation_ref'])

        tmp = Path(tempfile.mkdtemp(prefix='.stage-', dir=paths(home)))
        try:
            runtime = tmp / 'runtime'
            runtime.mkdir()
            for name in source_hashes:
                src, dst = source / name, runtime / name
                copy_tree(src, dst, _prefix=name, untracked=untracked) if src.is_dir() else shutil.copy2(src, dst)
            backup = tmp / 'backup'
            (backup / 'skills').mkdir(parents=True)
            for name in SKILLS:
                copy_tree(home / '.agents/skills' / name, backup / 'skills' / name)
            (backup / 'shim').write_bytes(bytes.fromhex(before['shim']['hex']))
            require(payload_inventory(runtime) == source_hashes, 'source changed during staging')
            require(payload_inventory(source, check_untracked=True) == source_hashes, 'source drift during staging')
            require(inspect_install(home) == before, 'installation changed during staging')
            manifest = {
                'format': 1,
                'source': str(source),
                'home': str(home),
                'version': version,
                'prior_release': prior_release,
                'source_revision': None if head is None else {'head': head, 'clean': not dirty},
                'compatibility': _compatibility_metadata(source, source_hashes),
                'references': refs,
                'payload': source_hashes,
                'before': before,
            }
            if validated_transition is not None:
                manifest['policy_transition'] = validated_transition
            (tmp / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
            write_state(tmp, 'prepared')
            os.rename(tmp, target)
        except BaseException:
            shutil.rmtree(tmp)
            raise
        return target


def load_release(home, version):
    release = paths(home, version)
    safe(release / 'manifest.json')
    safe(release / 'state.json')
    manifest = json.loads((release / 'manifest.json').read_text())
    require(manifest.get('format') == 1 and manifest['home'] == str(home)
            and manifest['version'] == version, 'release identity mismatch')
    before = manifest['before']
    if 'prior_release' in manifest:
        require(manifest['prior_release'] == prior_release_id(home, Path(before['target'])),
                'prior release mismatch')
    if 'policy_transition' in manifest:
        require(isinstance(manifest['policy_transition'], dict)
                and set(manifest['policy_transition']) == TRANSITION_KEYS,
                'malformed policy_transition in manifest')
    require(payload_inventory(safe(Path(before['target']))) == before['runtime'], 'prior runtime drift')
    require(before['shim']['hex'] == shim_bytes(Path(before['target'])).hex(), 'invalid prior target')
    for name in SKILLS:
        require(inventory(release / 'backup/skills' / name) == before['skills'][name], 'backup drift')
    require(safe(release / 'backup/shim').read_bytes().hex() == before['shim']['hex'], 'shim backup drift')
    return release, manifest, json.loads((release / 'state.json').read_text())['phase']


def _smoke_learner(runtime, tmp, env):
    store = str(Path(tmp) / 'learner-smoke-store')
    code = (
        'import json, pathlib, sys; '
        f'rt = pathlib.Path({str(runtime)!r}).resolve(); '
        'sys.path.insert(0, str(rt)); '
        'import learner, learner.cli; '
        'assert "flow" not in sys.modules and "gate" not in sys.modules; '
        'assert pathlib.Path(learner.__file__).resolve().is_relative_to(rt / "learner"); '
        f'raise SystemExit(learner.cli.main(["--storage-root", {store!r}, "status", "--json"]))'
    )
    res = subprocess.run([sys.executable, '-I', '-B', '-c', code],
                         cwd=tmp, env=env, capture_output=True, text=True, timeout=30)
    require(res.returncode == 0, f'isolated learner import smoke failed: {res.stderr}')
    require('profiles' in json.loads(res.stdout), 'isolated learner status output invalid')

    store2 = str(Path(tmp) / 'learner-cwd-store')
    res_cwd = subprocess.run([sys.executable, '-B', '-m', 'learner', '--storage-root', store2, 'status', '--json'],
                             cwd=str(runtime), env=env, capture_output=True, text=True, timeout=30)
    require(res_cwd.returncode == 0, f'runtime-cwd learner smoke failed: {res_cwd.stderr}')
    require('profiles' in json.loads(res_cwd.stdout), 'runtime-cwd learner status output invalid')

    if (runtime / 'flow/learning.py').is_file():
        store3 = str(Path(tmp) / 'flow-learn-store')
        res_flow = subprocess.run(
            [sys.executable, '-I', '-B', str(runtime / 'flow/flow.py'), 'learn', '--storage-root', store3, 'status', '--json'],
            cwd=tmp, env=env, capture_output=True, text=True, timeout=30,
        )
        require(res_flow.returncode == 0, f'isolated flow learn smoke failed: {res_flow.stderr}')
        require('profiles' in json.loads(res_flow.stdout), 'isolated flow learn status output invalid')


def smoke(runtime):
    """Isolated, read-only entrypoint smoke: never run business work or inference."""
    runtime = safe(runtime)
    with tempfile.TemporaryDirectory(prefix='flow-smoke-') as tmp:
        env = dict(os.environ, HOME=tmp, XDG_CONFIG_HOME=tmp, XDG_DATA_HOME=tmp,
                   PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONPATH', None)
        for script, arg in (('flow/flow.py', 'home'), ('gate/gate.py', '--help')):
            result = subprocess.run([sys.executable, '-I', '-B', str(runtime / script), arg],
                                    cwd=tmp, env=env, capture_output=True, text=True, timeout=30)
            require(result.returncode == 0, f'isolated smoke failed: {script}: {result.stderr}')
            if arg == 'home':
                require(result.stdout.strip() == str(runtime), 'smoke resolved unexpected runtime')
        if (runtime / 'learner').is_dir():
            _smoke_learner(runtime, tmp, env)


def replace_skill(release, home, name, source, bucket):
    """Keep displaced trees for crash recovery; aliases always retain their text."""
    destination = safe(home / '.agents/skills' / name)
    work = safe(release / bucket)
    work.mkdir(exist_ok=True)
    pending = work / (name + '.pending')
    displaced = work / (name + '.displaced')
    if pending.exists():
        shutil.rmtree(pending)
    copy_tree(source, pending)
    if destination.exists():
        require(not displaced.exists(), f'already displaced: {displaced}')
        os.rename(destination, displaced)
    os.rename(pending, destination)


def recovery_preflight(release, home, manifest):
    """Allow only old/new/mid-rename bytes, never overwrite unrelated drift."""
    before = manifest['before']
    require(aliases(home) == before['aliases'], 'alias drift')
    shim, _ = read_shim(home)
    new_shim = {'hex': shim_bytes(release / 'runtime').hex(), 'mode': 0o755}
    require(shim in (before['shim'], new_shim), 'shim drift')
    for name in SKILLS:
        path = safe(home / '.agents/skills' / name)
        old = before['skills'][name]
        # Compare against the trusted staged inventory, not potentially damaged
        # runtime bytes. These skill trees are the only abandoned payload needed
        # to recognize our own installed old/new states during recovery.
        new = {'.' if key == name else key[len(name) + 1:]: value
               for key, value in manifest['payload']['skills'].items()
               if key == name or key.startswith(name + '/')}
        require(inventory(release / 'runtime/skills' / name) == new, 'runtime skill drift')
        if path.exists():
            require(inventory(path) in (old, new), f'canonical skill drift: {name}')
        else:
            # A missing destination is ours only when its rename is evidenced.
            candidates = [release / b / (name + '.displaced') for b in ('activation', 'recovery')]
            require(any(p.exists() and inventory(p) in (old, new) for p in candidates),
                    f'unowned missing canonical skill: {name}')


def restore(release, home, manifest):
    recovery_preflight(release, home, manifest)
    write_state(release, 'rolling_back')
    for name in SKILLS:
        destination = home / '.agents/skills' / name
        if destination.exists() and inventory(destination) == manifest['before']['skills'][name]:
            continue
        replace_skill(release, home, name, release / 'backup/skills' / name, 'recovery')
    atomic_bytes(home / '.local/bin/flow', bytes.fromhex(manifest['before']['shim']['hex']),
                 manifest['before']['shim']['mode'])
    require(inspect_install(home) == manifest['before'], 'rollback verification failed')
    write_state(release, 'rolled_back')


def _verify_activation_inputs(home, version, manifest):
    source = safe(Path(manifest['source']))
    require(payload_inventory(release_runtime := paths(home, version) / 'runtime') == manifest['payload'],
            'staged runtime drift')
    require(payload_inventory(source, check_untracked=True) == manifest['payload'], 'source drift')
    require(inspect_install(home) == manifest['before'], 'installation drift')
    if 'policy_transition' in manifest:
        _validate_policy_transition(
            source, home, version, manifest['before'], manifest['payload'], manifest['policy_transition'])
    else:
        require(manifest['payload']['gate']['model_policy.py'] == manifest['before']['runtime']['gate']['model_policy.py'],
                'model_policy.py must remain byte-identical to installed policy')


def activate(home, version):
    home = safe(home)
    with locked(home):
        release, manifest, phase = load_release(home, version)
        require(phase == 'prepared', f'cannot activate phase {phase}; recover or stage a new version')
        _verify_activation_inputs(home, version, manifest)
        smoke(release / 'runtime')
        # Recheck after the subprocesses and before the first live mutation.
        _verify_activation_inputs(home, version, manifest)
        write_state(release, 'activating')
        try:
            for name in SKILLS:
                replace_skill(release, home, name, release / 'runtime/skills' / name, 'activation')
            atomic_bytes(home / '.local/bin/flow', shim_bytes(release / 'runtime'), 0o755)
            after = inspect_install(home)
            require(after['aliases'] == manifest['before']['aliases'] and
                    all(after['skills'][name] == inventory(release / 'runtime/skills' / name)
                        for name in SKILLS) and after['target'] == str(release / 'runtime'),
                    'activation verification failed')
            write_state(release, 'active')
        except Exception:
            restore(release, home, manifest)
            raise


def rollback(home, version):
    home = safe(home)
    with locked(home):
        release, manifest, phase = load_release(home, version)
        require(phase in ('active', 'activating', 'rolling_back'), f'cannot rollback phase {phase}')
        restore(release, home, manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('inspect', 'stage', 'activate', 'rollback'))
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--source', type=Path)
    parser.add_argument('--version')
    parser.add_argument('--expected-install', help='reviewed inspect fingerprint; required to stage')
    parser.add_argument('--policy-transition', type=Path,
                        help='JSON file with explicit reviewed model_policy.py transition evidence')
    parser.add_argument('--review-ref', help='optional review reference recorded in manifest')
    parser.add_argument('--evaluation-ref', help='optional evaluation reference recorded in manifest')
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            snapshot = inspect_install(args.home)
            print(json.dumps({'fingerprint': digest(snapshot), 'installation': snapshot}, indent=2))
        elif args.command == 'stage':
            require(args.source is not None and args.expected_install, 'stage needs --source and --expected-install')
            transition = None
            if args.policy_transition is not None:
                transition = json.loads(safe(args.policy_transition).read_text(encoding='utf-8'))
            refs = {}
            if args.review_ref:
                refs['review_ref'] = _require_nonplaceholder_ref(args.review_ref, 'review_ref')
            if args.evaluation_ref:
                refs['evaluation_ref'] = _require_nonplaceholder_ref(args.evaluation_ref, 'evaluation_ref')
            print(stage(args.source, args.home, args.version, args.expected_install,
                        policy_transition=transition, references=refs or None))
        else:
            {'activate': activate, 'rollback': rollback}[args.command](args.home, args.version)
            print(f'{args.command}: {args.version}')
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, f'release refused/failed: {exc}\n')


if __name__ == '__main__':
    main()

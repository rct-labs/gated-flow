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
PAYLOAD = ('flow', 'gate', 'skills', 'docs', 'README.md', 'LICENSE')
IGNORED = {'__pycache__', '.git', '.gate', '.pytest_cache'}


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


def inventory(path):
    path = safe(path)
    require(path.is_dir(), f'missing directory: {path}')
    result = {'.': {'mode': path.stat().st_mode & 0o7777}}
    for item in sorted(path.iterdir()):
        if item.name in IGNORED or item.suffix == '.pyc':
            continue
        require(not item.is_symlink(), f'symlink in tree: {item}')
        if item.is_dir():
            for name, data in inventory(item).items():
                key = item.name if name == '.' else f'{item.name}/{name}'
                result[key] = data
        else:
            require(item.is_file(), f'nonregular file: {item}')
            result[item.name] = {'sha256': hashlib.sha256(item.read_bytes()).hexdigest(),
                                 'mode': item.stat().st_mode & 0o7777}
    return result


def payload_inventory(source):
    result = {}
    for name in PAYLOAD:
        path = safe(source / name)
        if path.is_dir():
            result[name] = inventory(path)
        elif path.is_file():
            result[name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                            'mode': path.stat().st_mode & 0o7777}
    for name in ('flow', 'gate', 'skills'):
        require(name in result, f'missing runtime component: {name}')
    for name in ('flow/flow.py', 'gate/gate.py', 'gate/model_policy.py'):
        require((source / name).is_file(), f'missing entrypoint: {name}')
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


def copy_tree(source, target):
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*IGNORED, '*.pyc'))


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


def stage(source, home, version, expected_install):
    source, home = safe(source), safe(home)
    with locked(home):
        target = paths(home, version)
        require(not target.exists(), 'release already exists; choose a new version')
        require(not source.is_relative_to(paths(home)) and not paths(home).is_relative_to(source),
                'maintenance source must be separate from runtime root')
        before = inspect_install(home)
        require(digest(before) == expected_install, 'installation drift / unapproved baseline')
        source_hashes = payload_inventory(source)
        require(source_hashes['gate']['model_policy.py'] == before['runtime']['gate']['model_policy.py'],
                'model_policy.py must remain byte-identical to installed policy')
        tmp = Path(tempfile.mkdtemp(prefix='.stage-', dir=paths(home)))
        try:
            runtime = tmp / 'runtime'
            runtime.mkdir()
            for name in source_hashes:
                src, dst = source / name, runtime / name
                copy_tree(src, dst) if src.is_dir() else shutil.copy2(src, dst)
            backup = tmp / 'backup'
            (backup / 'skills').mkdir(parents=True)
            for name in SKILLS:
                copy_tree(home / '.agents/skills' / name, backup / 'skills' / name)
            (backup / 'shim').write_bytes(bytes.fromhex(before['shim']['hex']))
            require(payload_inventory(runtime) == source_hashes, 'source changed during staging')
            require(payload_inventory(source) == source_hashes, 'source drift during staging')
            require(inspect_install(home) == before, 'installation changed during staging')
            manifest = {'format': 1, 'source': str(source), 'home': str(home), 'version': version,
                        'payload': source_hashes, 'before': before}
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
    require(payload_inventory(safe(Path(before['target']))) == before['runtime'], 'prior runtime drift')
    require(before['shim']['hex'] == shim_bytes(Path(before['target'])).hex(), 'invalid prior target')
    for name in SKILLS:
        require(inventory(release / 'backup/skills' / name) == before['skills'][name], 'backup drift')
    require(safe(release / 'backup/shim').read_bytes().hex() == before['shim']['hex'], 'shim backup drift')
    return release, manifest, json.loads((release / 'state.json').read_text())['phase']


def smoke(runtime):
    """Isolated, read-only entrypoint smoke: never run business work or inference."""
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


def activate(home, version):
    home = safe(home)
    with locked(home):
        release, manifest, phase = load_release(home, version)
        require(phase == 'prepared', f'cannot activate phase {phase}; recover or stage a new version')
        require(payload_inventory(release / 'runtime') == manifest['payload'], 'staged runtime drift')
        require(payload_inventory(safe(Path(manifest['source']))) == manifest['payload'], 'source drift')
        require(inspect_install(home) == manifest['before'], 'installation drift')
        smoke(release / 'runtime')
        # Recheck after the subprocesses and before the first live mutation.
        require(inspect_install(home) == manifest['before'], 'installation drift after smoke')
        require(payload_inventory(release / 'runtime') == manifest['payload'], 'runtime drift after smoke')
        require(payload_inventory(safe(Path(manifest['source']))) == manifest['payload'], 'source drift after smoke')
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
    args = parser.parse_args()
    try:
        if args.command == 'inspect':
            snapshot = inspect_install(args.home)
            print(json.dumps({'fingerprint': digest(snapshot), 'installation': snapshot}, indent=2))
        elif args.command == 'stage':
            require(args.source is not None and args.expected_install, 'stage needs --source and --expected-install')
            print(stage(args.source, args.home, args.version, args.expected_install))
        else:
            {'activate': activate, 'rollback': rollback}[args.command](args.home, args.version)
            print(f'{args.command}: {args.version}')
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(2, f'release refused/failed: {exc}\n')


if __name__ == '__main__':
    main()

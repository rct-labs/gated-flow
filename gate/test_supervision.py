"""Offline supervision contracts: isolated repositories, synthetic CLIs and explicit IPC.

The integration engine uses a synthetic owner-policy file, with the production
policy validator intact. No model, network, installed runtime or active project
is invoked. Pins come from the public policy fixture.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(path, name='supervision_test'):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def command(*args, env=None):
    result = subprocess.run(args, text=True, capture_output=True, env=env)
    if result.returncode:
        raise AssertionError(result.stdout[-2000:] + result.stderr[-2000:])
    return result.stdout


class SupervisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.engine = self.root / 'engine'
        (self.engine / 'flow').mkdir(parents=True)
        (self.engine / 'gate').mkdir()
        for path in (ROOT / 'gate').glob('*.py'):
            if not path.name.startswith('test_'):
                shutil.copy(path, self.engine / 'gate' / path.name)
        for name in ('supervision.py', 'flow.py'):
            shutil.copy(ROOT / 'flow' / name, self.engine / 'flow' / name)
        self.home = self.root / 'home'
        policy_path = self.home / '.config/mini-model-routing/policy.json'
        policy_path.parent.mkdir(parents=True)
        policy_path.write_text((ROOT / 'gate/test_policy.json').read_text())
        self.mod = load(self.engine / 'flow/supervision.py')
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        # Every fixture resolves a synthetic backend, including refusal tests.
        backend = self.bin / 'codex'
        backend.write_text('#!' + sys.executable + '\nraise SystemExit("unexpected fixture dispatch")\n')
        backend.chmod(0o755)
        self.env = {**os.environ, 'HOME': str(self.home),
                    'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'GATE_MACHINE_DIR': str(self.root / 'machine')}
        self.env.pop('FLOW_SUPERVISION_TOKEN', None)
        self.patcher = mock.patch.dict(os.environ, self.env, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        command('git', 'init', '-q', str(self.repo))
        command('git', '-C', str(self.repo), 'config', 'user.name', 'Fixture')
        command('git', '-C', str(self.repo), 'config', 'user.email', 'fixture@example.invalid')
        (self.repo / '.gate').mkdir()
        (self.repo / '.gitignore').write_text('.gate/*\n!.gate/config.json\n')
        self.config = {'workers': ['codex'], 'worker_cmds': {'codex': [
            'codex', '--model', 'gpt-6.1-sol', '-c', 'model_reasoning_effort="medium"']},
            'judge': {'enabled': False}, 'probe': {'enabled': False},
            'max_tasks': 1, 'max_attempts_per_task': 1, 'task_timeout_s': 20,
            'run_timeout_s': 90, 'max_model_calls': 3,
            'verify_cmd': f'"{sys.executable}" -c "print(\'1 passed\')"'}
        self.write_config()
        (self.repo / 'spec.md').write_text('Fixture package: implement A only.\n')
        (self.repo / 'TASK_QUEUE.md').write_text(
            '| # | ID | name | status | start baseline | end baseline |\n'
            '|---|---|---|---|---|---|\n'
            '| 1 | A | Implement fixture | `TODO` | 1 passed | pending |\n'
            '<!-- task:A files: result.txt -->\n')
        command('git', '-C', str(self.repo), 'add', '.')
        command('git', '-C', str(self.repo), 'commit', '-qm', 'fixture')
        command(sys.executable, str(self.engine / 'gate/gate.py'), 'install-hook', '--repo', str(self.repo))

    def write_config(self):
        (self.repo / '.gate/config.json').write_text(json.dumps(self.config))

    def bind(self):
        def fake_launch(repo, state):
            state.update(phase='running', runner=[99999999, 'old-boot', '0'])
            self.mod.save(repo, state)
        with mock.patch.object(self.mod, 'launch_runner', side_effect=fake_launch):
            self.mod.bind(self.repo, 'origin-thread', 'spec.md', ['A'], 3, 90)
        return self.mod.load(self.repo)

    def write_state(self, **values):
        state = self.mod.load(self.repo)
        state.update(values)
        self.mod.save(self.repo, state)
        return state

    def event(self, **event):
        with (self.repo / '.gate/journal.ndjson').open('a') as stream:
            stream.write(json.dumps(event) + '\n')

    def test_binding_freezes_repository_package_scope_and_budget(self):
        state = self.bind()
        self.assertEqual(state['tasks'], ['A'])
        self.assertEqual(state['session'], 'origin-thread')
        self.assertEqual(state['max_calls'], 3)
        self.assertLess(state['deadline'], time.time() + 91)
        self.mod.validate(self.repo, state, 'A')
        with self.assertRaisesRegex(ValueError, 'outside_authorized'):
            self.mod.validate(self.repo, state, 'B')
        (self.repo / 'spec.md').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'package_changed'):
            self.mod.validate(self.repo, state)

    def test_status_changes_allowed_but_scope_and_oracle_changes_refused(self):
        state = self.bind()
        queue = self.repo / 'TASK_QUEUE.md'
        queue.write_text(queue.read_text().replace('`TODO`', '`IN_PROGRESS`'))
        self.mod.validate(self.repo, state, 'A')
        queue.write_text(queue.read_text() + '<!-- task:A verify: {"cmd":"false"} -->\n')
        with self.assertRaisesRegex(ValueError, 'resume_scope_changed'):
            self.mod.validate(self.repo, state, 'A')

    def test_multiline_metadata_and_task_identity_are_immutable(self):
        state = self.bind()
        path = self.repo / 'TASK_QUEUE.md'
        original = path.read_text()
        path.write_text(original.replace('Implement fixture', 'Different work'))
        with self.assertRaisesRegex(ValueError, 'resume_scope_changed'):
            self.mod.validate(self.repo, state)
        path.write_text(original + '<!-- task:A handoff: {\n"goal":"original"\n} -->\n')
        original_contract = self.mod.contracts(path.read_text(), ['A'])
        path.write_text(path.read_text().replace('"original"', '"expanded"'))
        self.assertNotEqual(original_contract, self.mod.contracts(path.read_text(), ['A']))

    def test_configuration_changes_require_host(self):
        state = self.bind()
        self.config['max_model_calls'] = 999
        self.write_config()
        with self.assertRaisesRegex(ValueError, 'configuration_changed'):
            self.mod.validate(self.repo, state)

    def test_stop_survives_reconnect_and_blocks_every_role(self):
        state = self.bind()
        self.mod.stop(self.repo)
        reloaded = load(self.engine / 'flow/supervision.py', 'reconnected')
        self.assertTrue(reloaded.status(self.repo)['stopped'])
        for task in ('A', None):
            with self.assertRaisesRegex(ValueError, 'user_stop'):
                with reloaded.spawn(self.repo, state['token'], task):
                    self.fail('dispatch after stop')

    def test_stop_and_spawn_are_serialized(self):
        state = self.bind()
        stopped = threading.Event()
        with self.mod.spawn(self.repo, state['token'], 'A'):
            thread = threading.Thread(target=lambda: (self.mod.stop(self.repo), stopped.set()))
            thread.start()
            self.assertFalse(stopped.wait(.05))
        thread.join(2)
        self.assertTrue(stopped.is_set())
        with self.assertRaisesRegex(ValueError, 'user_stop'):
            with self.mod.spawn(self.repo, state['token'], 'A'):
                pass

    def test_cumulative_calls_are_not_reset_by_runner_restart(self):
        state = self.bind()
        for _ in range(3):
            with self.mod.spawn(self.repo, state['token'], 'A'):
                pass
            self.mod.call_finished(self.repo, state['token'])
        with self.mod.runner(self.repo, state['token']):
            with self.assertRaisesRegex(ValueError, 'cumulative_call_budget'):
                with self.mod.spawn(self.repo, state['token']):
                    pass
        self.assertEqual(self.mod.load(self.repo)['calls'], 3)

    def test_consultation_reserves_two_calls_and_never_refunds_after_restart(self):
        state = self.bind()
        self.mod.reserve_consultation(self.repo, state['token'], 'episode')
        self.assertEqual(self.mod.load(self.repo)['calls'], 2)
        reloaded = load(self.engine / 'flow/supervision.py', 'consult_restart')
        with self.assertRaisesRegex(ValueError, 'already reserved'):
            reloaded.reserve_consultation(self.repo, state['token'], 'episode')
        for _ in range(2):
            with reloaded.spawn(self.repo, state['token'], consultation='episode'):
                pass
            reloaded.call_finished(self.repo, state['token'])
        self.assertEqual(reloaded.load(self.repo)['calls'], 2)
        with self.assertRaisesRegex(ValueError, 'invalid consultation'):
            with reloaded.spawn(self.repo, state['token'], consultation='episode'):
                pass
        with self.assertRaisesRegex(ValueError, 'cumulative_call_budget'):
            reloaded.reserve_consultation(self.repo, state['token'], 'second')

    def test_stop_blocks_even_reserved_consultation_seats(self):
        state = self.bind()
        self.mod.reserve_consultation(self.repo, state['token'], 'episode')
        self.mod.stop(self.repo)
        with self.assertRaisesRegex(ValueError, 'user_stop'):
            with self.mod.spawn(self.repo, state['token'], consultation='episode'):
                pass

    def test_absolute_deadline_enforced_before_dispatch(self):
        state = self.bind()
        self.write_state(deadline=time.time() - 1)
        with self.assertRaisesRegex(ValueError, 'cumulative_deadline'):
            with self.mod.spawn(self.repo, state['token']):
                pass

    def test_competing_runner_and_wrong_binding_refused(self):
        state = self.bind()
        with self.mod.runner(self.repo, state['token']):
            with self.assertRaisesRegex(ValueError, 'owner busy'):
                with self.mod.runner(self.repo, state['token']):
                    pass
        with self.assertRaisesRegex(ValueError, 'incorrect_supervision'):
            with self.mod.runner(self.repo, 'wrong'):
                pass
        with self.assertRaisesRegex(ValueError, 'requires supervised'):
            with self.mod.runner(self.repo):
                pass

    def test_idle_tick_is_incremental_and_deduplicated_without_remote_work(self):
        self.bind()
        self.write_state(runner=self.mod.process_identity(os.getpid()))
        self.event(event='heartbeat', at='now')
        with (mock.patch.object(self.mod.subprocess, 'Popen', side_effect=AssertionError('no process')),
              mock.patch.object(self.mod.socket, 'socket', side_effect=AssertionError('no IPC or network'))):
            first = self.mod.tick(self.repo)
            second = self.mod.tick(self.repo)
            third = self.mod.tick(self.repo, now=time.time() + 1801)
        self.assertGreater(first['bytes_read'], 0)
        self.assertEqual(second['bytes_read'], 0)
        self.assertFalse(second['due'])
        self.assertEqual(third['bytes_read'], 0)
        self.assertIsNone(third['pending'])

    def test_cursor_and_pending_delivery_commit_together_on_crash(self):
        self.bind()
        self.write_state(runner=self.mod.process_identity(os.getpid()))
        self.event(event='task_done', task='A', worker='codex')
        before = self.mod.load(self.repo)['cursor']
        with mock.patch.object(self.mod, 'save', side_effect=OSError('simulated crash before commit')):
            with self.assertRaisesRegex(OSError, 'simulated crash'):
                self.mod.tick(self.repo)
        self.assertEqual(self.mod.load(self.repo)['cursor'], before)
        result = self.mod.tick(self.repo)
        self.assertEqual(result['pending']['message']['events'][0]['event'], 'task_done')

    def test_backlog_pages_are_bounded_and_immediately_due(self):
        self.bind()
        self.write_state(runner=self.mod.process_identity(os.getpid()))
        for _ in range(120):
            self.event(event='task_done', task='A', worker='codex')
        first = self.mod.tick(self.repo)
        self.assertTrue(first['due'])
        self.assertLessEqual(len(first['pending']['message']['events']), 12)
        self.assertLessEqual(self.mod.load(self.repo)['next_tick'], time.time())
        self.assertTrue(self.mod.tick(self.repo)['due'])
        self.assertFalse(self.mod.tick(self.repo)['due'])

    def test_journal_integrity_warning_is_a_durable_host_hold(self):
        self.bind()
        with (self.repo / '.gate/journal.ndjson').open('a') as stream:
            stream.write('invalid record\n')
        with mock.patch.object(self.mod, 'launch_runner', side_effect=AssertionError('unsafe replay')):
            result = self.mod.tick(self.repo)
        self.assertEqual(result['phase'], 'needs_host')
        self.assertIn('journal_integrity', result['reason'])

    def test_dirty_interruption_preserves_edits_without_replay(self):
        self.bind()
        (self.repo / 'result.txt').write_text('unfinished')
        with mock.patch.object(self.mod, 'launch_runner', side_effect=AssertionError('unsafe replay')):
            result = self.mod.tick(self.repo)
        self.assertEqual(result['phase'], 'needs_host')
        self.assertEqual((self.repo / 'result.txt').read_text(), 'unfinished')

    def test_unresolved_call_never_auto_replayed(self):
        self.bind()
        self.write_state(inflight=True, calls=1)
        result = self.mod.tick(self.repo)
        self.assertEqual(result['phase'], 'needs_host')
        self.assertIn('partial_work', result['reason'])

    def test_clean_interruption_resumes_once_then_surfaces_unchanged_failure(self):
        self.bind()
        def resume(repo, state):
            state.update(phase='running')
        with mock.patch.object(self.mod, 'launch_runner', side_effect=resume) as launch:
            self.mod.tick(self.repo)
            result = self.mod.tick(self.repo, now=time.time() + 1801)
        self.assertEqual(launch.call_count, 1)
        self.assertEqual(result['phase'], 'needs_host')
        self.assertIn('unchanged_failure', result['reason'])

    def test_runner_end_is_not_package_completion(self):
        state = self.bind()
        self.mod.record_end(self.repo, state['token'], 'queue_empty')
        result = self.mod.tick(self.repo)
        self.assertEqual(result['phase'], 'needs_host')
        self.assertEqual(result['accepted'], [])

    def test_unknown_launch_identity_requires_host(self):
        self.bind()
        self.write_state(runner=None, phase='launching')
        result = self.mod.tick(self.repo)
        self.assertEqual(result['phase'], 'needs_host')
        self.assertIn('ownership', result['reason'])

    def test_pid_reuse_is_not_a_live_owner(self):
        self.assertIsNotNone(self.mod.process_identity(os.getpid()))
        pid = os.getpid()
        old = self.mod.process_identity(pid)
        old[2] = 'stale-start'
        self.assertFalse(self.mod.alive(old))
        watch = self.mod.watcher()
        cursor = self.repo / '.gate/pid.cursor'
        journal = self.repo / '.gate/journal.ndjson'
        watch.initialize(journal, cursor)
        self.event(event='run_start', runner_pid=pid, runner_identity=old)
        events = watch.read_events(journal, cursor)['events']
        self.assertIn('run_interrupted', [e['event'] for e in events])
        self.assertNotIn('run_interrupted', [e['event'] for e in watch.read_events(journal, cursor)['events']])

    def test_transport_unavailable_pending_survives_reconnect(self):
        self.bind()
        self.write_state(transport=str(self.root / 'absent.sock'), inflight=True)
        result = self.mod.tick(self.repo)
        self.assertIsNotNone(result['pending'])
        self.assertEqual(result['session'], 'origin-thread')
        pending_id = result['pending']['id']
        self.assertEqual(self.mod.tick(self.repo, now=time.time() + 1801)['pending']['id'], pending_id)

    def test_no_user_turn_wake_over_exact_session_ipc(self):
        self.bind()
        path = str(self.root / 'host.sock')
        got = []
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(path)
            server.listen(1)
            server.settimeout(3)
            def receive():
                conn, _ = server.accept()
                with conn:
                    packet = json.loads(conn.recv(4096))
                    got.append(packet)
                    conn.sendall(json.dumps({'session': packet['session'], 'id': packet['id'], 'ack': True}).encode())
            thread = threading.Thread(target=receive)
            thread.start()
            self.write_state(transport=path, inflight=True)
            # An independent watcher process wakes the receiver without a user turn.
            proc = subprocess.Popen([sys.executable, str(self.engine / 'flow/supervision.py'),
                                     'watch', '--repo', str(self.repo)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=self.env)
            try:
                thread.join(4)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and self.mod.load(self.repo)['pending']:
                    time.sleep(.01)
                result = self.mod.status(self.repo)
            finally:
                proc.terminate()
                proc.communicate(timeout=3)
        self.assertEqual(got[0]['session'], 'origin-thread')
        self.assertEqual(got[0]['repo'], str(self.repo))
        self.assertIsNone(result['pending'])
        self.assertNotIn('prompt', got[0])

    def test_wrong_session_ack_cannot_complete_delivery(self):
        state = self.bind()
        state.update(pending={'id': 'event-1', 'message': {'phase': 'needs_host'}}, transport='fixture')
        with mock.patch.object(self.mod.socket, 'socket') as factory:
            conn = factory.return_value.__enter__.return_value
            conn.recv.return_value = b'{"session":"other","id":"event-1","ack":true}'
            conn.getsockopt.side_effect = OSError('unavailable')
            self.mod.notify(state)
        self.assertIsNotNone(state['pending'])

    def test_settlement_and_end_recover_from_journal_after_projection_crash(self):
        self.bind()
        self.write_state(run_id='bound-run', runner=self.mod.process_identity(os.getpid()))
        head = command('git', '-C', str(self.repo), 'rev-parse', 'HEAD').strip()
        self.event(event='package_settled', run='other-run', tasks=['A'], head=head)
        self.assertEqual(self.mod.tick(self.repo)['accepted'], [])
        # Now simulate the original runner exiting before projecting receipts.
        self.write_state(next_tick=0, runner=[99999999, 'old-boot', '0'])
        self.event(event='package_settled', run='bound-run', tasks=['A'], head=head)
        self.event(event='run_end', run='bound-run', stop='queue_empty')
        result = self.mod.tick(self.repo)
        self.assertEqual(result['phase'], 'accepted')
        self.assertEqual(result['accepted'], ['A'])

    def test_settlement_requires_fresh_clean_evidence(self):
        state = self.bind()
        self.write_state(end='queue_empty')
        with mock.patch.dict(os.environ, {'FLOW_SUPERVISION_TOKEN': state['token']}):
            self.mod.record_settlement(self.repo, ['A'])
        self.assertEqual(self.mod.tick(self.repo)['phase'], 'accepted')
        (self.repo / 'result.txt').write_text('later edits')
        self.write_state(next_tick=0)
        self.assertEqual(self.mod.tick(self.repo)['phase'], 'needs_host')

    def test_host_hold_never_clears_just_because_tree_becomes_clean(self):
        self.bind()
        partial = self.repo / 'result.txt'
        partial.write_text('partial')
        self.assertEqual(self.mod.tick(self.repo)['phase'], 'needs_host')
        partial.unlink()
        with mock.patch.object(self.mod, 'launch_runner', side_effect=AssertionError('hold replay')):
            self.assertEqual(self.mod.tick(self.repo, now=time.time() + 1801)['phase'], 'needs_host')

    def test_gate_stopped_dispatch_has_no_model_call_receipt(self):
        state = self.bind()
        self.mod.stop(self.repo)
        gate = load(self.engine / 'gate/gate.py', 'stopped_gate')
        executable = self.bin / 'codex'
        executable.write_text('#!' + sys.executable + '\nraise SystemExit("must not execute")\n')
        executable.chmod(0o755)
        cfg = gate.load_config(self.repo)
        with mock.patch.dict(os.environ, {'FLOW_SUPERVISION_TOKEN': state['token']}):
            result, error = gate.spawn_worker(self.repo, cfg, 'codex', 'Implement A',
                                              self.repo / '.gate/prompt', task='A')
        self.assertIsNone(result)
        self.assertIn('user_stop', error)
        self.assertEqual(gate.RUN_BUDGET['calls'], 0)
        journal = self.repo / '.gate/journal.ndjson'
        self.assertFalse(journal.exists() and 'model_call' in journal.read_text())

    def test_reconnect_reads_pending_without_mutating_state_and_handles_corruption(self):
        self.bind()
        self.write_state(phase='needs_host', reason='partial_work_requires_host',
                         pending={'id': 'pending-1', 'message': {'phase': 'needs_host'}})
        path = self.mod.directory(self.repo) / 'state.json'
        before = path.read_bytes()
        packet = json.loads(command(sys.executable, str(ROOT / 'flow/flow.py'), 'resume',
                                    '--repo', str(self.repo), '--json', env=self.env))
        self.assertEqual(packet['supervision']['pending']['id'], 'pending-1')
        self.assertEqual(path.read_bytes(), before)
        path.write_text('[]')
        packet = json.loads(command(sys.executable, str(ROOT / 'flow/flow.py'), 'resume',
                                    '--repo', str(self.repo), '--json', env=self.env))
        self.assertEqual(packet['supervision']['phase'], 'needs_host')

    def test_stopped_binding_cannot_be_bypassed_by_standalone_review(self):
        self.bind()
        self.mod.stop(self.repo)
        gate = load(self.engine / 'gate/gate.py', 'standalone_review')
        executable = self.bin / 'codex'
        executable.write_text('#!' + sys.executable + '\nraise SystemExit("must not execute")\n')
        executable.chmod(0o755)
        cfg = gate.load_config(self.repo)
        result, error = gate.spawn_worker(self.repo, cfg, 'codex', 'Review only',
            self.repo / '.gate/prompt', template=cfg['worker_cmds']['codex'])
        self.assertIsNone(result)
        self.assertIn('supervision', error)
        self.assertEqual(gate.RUN_BUDGET['calls'], 0)

    def test_portable_gate_without_adapter_refuses_existing_bound_state(self):
        self.bind()
        self.mod.stop(self.repo)
        (self.engine / 'flow/supervision.py').unlink()
        result = subprocess.run([sys.executable, str(self.engine / 'gate/gate.py'),
            'run', '--repo', str(self.repo)], capture_output=True, text=True, env=self.env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('bound batch requires supervision adapter', result.stderr + result.stdout)

    def test_startup_no_workers_is_durable_only_when_supervised(self):
        self.config['workers'] = ['missing-worker']
        self.config['worker_cmds'] = {}
        self.write_config()
        command('git', '-C', str(self.repo), 'add', '.gate/config.json')
        command('git', '-C', str(self.repo), 'commit', '-qm', 'fixture unavailable route')
        plain = subprocess.run([sys.executable, str(self.engine / 'gate/gate.py'),
            'run', '--repo', str(self.repo)], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(plain.returncode, 0)
        self.assertIn('no usable worker CLI', plain.stdout + plain.stderr)
        self.bind()
        state = self.mod.load(self.repo)
        env = dict(self.env, FLOW_SUPERVISION_TOKEN=state['token'])
        result = subprocess.run([sys.executable, str(self.engine / 'gate/gate.py'),
            'run', '--repo', str(self.repo)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.mod.load(self.repo)['end'], 'no_workers:A')
        self.assertEqual(self.mod.tick(self.repo)['phase'], 'waiting_routes')
        def resumed(repo, current):
            current.update(phase='running', runner=self.mod.process_identity(os.getpid()))
        with mock.patch.object(self.mod.gate_engine(), 'available_workers', return_value=['codex']), \
                mock.patch.object(self.mod, 'launch_runner', side_effect=resumed) as launch:
            self.assertEqual(self.mod.tick(self.repo, now=time.time() + 1801)['phase'], 'running')
            launch.assert_called_once()

    def test_resume_refuses_other_session_and_defaults_to_trusted_origin(self):
        self.bind()
        self.mod.stop(self.repo)
        with self.assertRaisesRegex(ValueError, 'same bound'):
            self.mod.reconcile(self.repo, session='other-thread')
        with mock.patch.dict(os.environ, CODEX_THREAD_ID='other-thread'):
            with self.assertRaisesRegex(ValueError, 'same bound'):
                self.mod.reconcile(self.repo)
        with mock.patch.dict(os.environ, CODEX_THREAD_ID='origin-thread'), \
                mock.patch.object(self.mod, 'launch_runner') as launch:
            self.mod.reconcile(self.repo)
            launch.assert_called_once()

    def test_stopped_watcher_exits_with_pending_undeliverable_message(self):
        self.bind()
        self.mod.stop(self.repo)
        result = subprocess.run([sys.executable, str(self.engine / 'flow/supervision.py'),
            'watch', '--repo', str(self.repo)], env=self.env, capture_output=True,
            text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNotNone(self.mod.load(self.repo)['pending'])

    def test_accepted_watcher_exits_with_pending_final_message(self):
        with mock.patch.object(self.mod, 'tick', return_value={
                'phase': 'accepted', 'due': True, 'pending': {'id': 'final'}}), \
                mock.patch.object(self.mod.time, 'sleep', side_effect=AssertionError('terminal loop')):
            self.assertEqual(self.mod.main(['watch', '--repo', str(self.repo)]), 0)

    def test_codex_nested_repo_requires_explicit_origin_binding(self):
        session = '11111111-2222-4333-8444-555555555555'
        sessions = self.home / '.codex/sessions'
        sessions.mkdir(parents=True)
        path = sessions / ('rollout-' + session + '.jsonl')
        path.write_text(json.dumps({'type': 'session_meta', 'payload':
            {'id': session, 'cwd': str(self.root)}}) + '\n')
        state = {'session': session, 'repo': str(self.repo)}
        with mock.patch.dict(os.environ, HOME=str(self.home), CODEX_THREAD_ID=session):
            self.assertEqual(self.mod.codex_session(state, binding=True), str(path))
        with mock.patch.dict(os.environ, HOME=str(self.home), CODEX_THREAD_ID='other'):
            with self.assertRaisesRegex(ValueError, 'originating'):
                self.mod.codex_session(state, binding=True)
        path.write_text(json.dumps({'type': 'session_meta', 'payload':
            {'id': session, 'cwd': str(self.root / 'unrelated')}}) + '\n')
        with mock.patch.dict(os.environ, HOME=str(self.home)):
            with self.assertRaisesRegex(ValueError, 'repository/UUID mismatch'):
                self.mod.codex_session(state)

    def test_no_workers_waits_locally_then_recovers_with_same_limits(self):
        state = self.bind()
        self.write_state(end='no_workers:A')
        gate = self.mod.gate_engine()
        with mock.patch.object(gate, 'available_workers', return_value=[]), mock.patch.object(self.mod, 'launch_runner') as launch:
            result = self.mod.tick(self.repo)
            self.assertEqual(result['phase'], 'waiting_routes')
            self.mod.tick(self.repo, now=time.time() + 1801)
            launch.assert_not_called()
        def resume(repo, current):
            current.update(phase='running', runner=self.mod.process_identity(os.getpid()))
        with mock.patch.object(gate, 'available_workers', return_value=['codex']), mock.patch.object(self.mod, 'launch_runner', side_effect=resume) as launch:
            self.mod.tick(self.repo, now=time.time() + 3602)
            launch.assert_called_once()
        recovered = self.mod.load(self.repo)
        self.assertEqual(recovered['deadline'], state['deadline'])
        self.assertEqual(recovered['max_calls'], state['max_calls'])
        self.mod.stop(self.repo)
        with mock.patch.object(self.mod, 'launch_runner', side_effect=AssertionError('stopped')):
            self.assertEqual(self.mod.tick(self.repo, now=time.time() + 5403)['phase'], 'stopped')

    def test_owner_reconcile_keeps_budget_and_refuses_partial_work(self):
        state = self.bind()
        self.write_state(phase='needs_host', calls=1)
        with mock.patch.object(self.mod, 'launch_runner') as launch:
            self.mod.reconcile(self.repo, session='origin-thread')
            self.assertEqual(launch.call_args.args[1]['calls'], 1)
            self.assertEqual(launch.call_args.args[1]['token'], state['token'])
        self.write_state(inflight=True)
        with self.assertRaisesRegex(ValueError, 'effects and ownership'):
            self.mod.reconcile(self.repo, session='origin-thread')

    def test_separately_authorized_batch_archives_settled_binding(self):
        state = self.bind()
        self.write_state(phase='stopped', stopped=True)
        with mock.patch.object(self.mod, 'launch_runner', side_effect=lambda repo, current: self.mod.save(repo, current)):
            self.mod.bind(self.repo, 'new-origin', 'spec.md', ['A'], 2, 60)
        self.assertTrue((self.mod.directory(self.repo) / ('settled-' + state['token'] + '.json')).exists())
        self.assertNotEqual(self.mod.load(self.repo)['token'], state['token'])

    def test_exact_codex_adapter_checks_uuid_output_and_never_replays_uncertainty(self):
        state = self.bind()
        session = '11111111-2222-4333-8444-555555555555'
        sessions = self.home / '.codex/sessions/2026/11/21'
        sessions.mkdir(parents=True)
        path = sessions / ('rollout-' + session + '.jsonl')
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': session, 'cwd': str(self.repo)}}) + '\n')
        state.update(session=session, transport='codex', session_path=str(path),
                     pending={'id': 'notice', 'message': {'events': ['untrusted text']}})
        output = '\n'.join(json.dumps(e) for e in [
            {'type': 'thread.started', 'thread_id': session}, {'type': 'turn.completed'}])
        self.mod.save(self.repo, state)
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (output, '')
        with mock.patch.object(self.mod.subprocess, 'Popen', return_value=process) as run:
            self.mod.notify(state)
        self.assertIsNone(state['pending'])
        args = run.call_args.args[0]
        self.assertIn(session, args)
        self.assertIn(state['pending']['id'] if state['pending'] else 'event_id', args[-1])
        self.assertIn('deployment is not established', args[-1])
        self.assertNotIn('untrusted text', args[-1])
        self.assertEqual(state['delivery_evidence'], 'exact_session_turn_completed')
        state.update(pending={'id': 'wrong', 'message': {}}, calls=0)
        self.mod.save(self.repo, state)
        process.communicate.return_value = (output.replace(session, 'other'), '')
        with mock.patch.object(self.mod.subprocess, 'Popen', return_value=process) as run:
            self.mod.notify(state)
            self.assertIsNotNone(state['pending'])
            self.mod.notify(state)
            self.assertEqual(run.call_count, 1)
        self.assertEqual(state['delivery_evidence'], 'uncertain_requires_host')

    def test_verified_notification_tick_allows_same_owner_resume(self):
        self.bind()
        self.write_state(transport='codex', session_path='fixture-session', inflight=True)
        session = 'origin-thread'
        proc = mock.Mock(returncode=0)
        proc.communicate.return_value = ('\n'.join(json.dumps(e) for e in [
            {'type': 'thread.started', 'thread_id': session}, {'type': 'turn.completed'}]), '')
        with mock.patch.object(self.mod, 'codex_session', return_value='fixture-session'), \
                mock.patch.object(self.mod.subprocess, 'Popen', return_value=proc):
            self.mod.tick(self.repo)
        state = self.mod.load(self.repo)
        self.assertIsNone(state['pending'])
        self.assertNotIn('delivery_attempt', state)
        self.assertEqual(state['calls'], 1)
        self.write_state(inflight=False)
        with mock.patch.object(self.mod, 'launch_runner') as launch:
            self.mod.reconcile(self.repo, session=session)
        self.assertEqual(launch.call_args.args[1]['calls'], 1)
        self.assertEqual(launch.call_args.args[1]['token'], state['token'])

    def test_notification_prestart_refunds_but_uncertain_poststart_stays_reserved(self):
        self.bind()
        gate = self.mod.gate_engine()
        failures = ('prestart', 'missing-policy', 'poststart-oserror', 'timeout', 'malformed')
        for failure in failures:
            with self.subTest(failure=failure):
                state = self.write_state(transport='codex', session_path='fixture-session', calls=0,
                    pending={'id': failure, 'message': {}})
                state.pop('delivery_attempt', None)
                self.mod.save(self.repo, state)
                proc = mock.Mock(returncode=0)
                if failure == 'poststart-oserror':
                    proc.communicate.side_effect = OSError('read failed after launch')
                elif failure == 'timeout':
                    proc.communicate.side_effect = [subprocess.TimeoutExpired('codex', 45), ('', '')]
                else:
                    proc.communicate.return_value = ('invalid-json', '')
                policy = gate._model_routes.load_policy()
                with mock.patch.object(self.mod, 'codex_session', return_value='fixture-session'), \
                        mock.patch.object(gate._model_routes, 'load_policy',
                            return_value=None if failure == 'missing-policy' else policy), \
                        mock.patch.object(self.mod.subprocess, 'Popen',
                            side_effect=OSError('not installed') if failure == 'prestart' else None,
                            return_value=proc) as launch:
                    self.mod.notify(state)
                    current = self.mod.load(self.repo)
                    self.assertIsNotNone(state['pending'])
                    if failure in ('prestart', 'missing-policy'):
                        self.assertEqual(current['calls'], 0)
                        self.assertEqual(state['calls'], 0)
                        self.assertNotIn('delivery_attempt', current)
                        self.assertNotIn('delivery_attempt', state)
                    else:
                        self.assertEqual(current['calls'], 1)
                        self.assertEqual(state['delivery_attempt'], failure)
                        self.mod.notify(state)
                        self.assertEqual(launch.call_count, 1)
                        with self.assertRaisesRegex(ValueError, 'effects and ownership'):
                            self.mod.reconcile(self.repo, session='origin-thread')

    def test_codex_delivery_busy_offline_and_limit_remain_pending(self):
        state = self.bind()
        state.update(transport='codex', pending={'id': 'notice', 'message': {}},
                     runner=self.mod.process_identity(os.getpid()))
        with mock.patch.object(self.mod.subprocess, 'Popen', side_effect=AssertionError('busy session')):
            self.mod.notify(state)
        self.assertIsNotNone(state['pending'])
        self.assertEqual(state['delivery_evidence'], 'pending_runner_busy')

    def test_stop_during_native_delivery_persists_without_waiting_for_turn(self):
        state = self.bind()
        session = '11111111-2222-4333-8444-555555555555'
        sessions = self.home / '.codex/sessions'
        sessions.mkdir(parents=True)
        path = sessions / ('rollout-' + session + '.jsonl')
        path.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': session, 'cwd': str(self.repo)}}) + '\n')
        state.update(session=session, session_path=str(path), transport='codex',
                     pending={'id': 'notice', 'message': {}})
        self.mod.save(self.repo, state)
        proc = mock.Mock(returncode=0)
        def communicate(**kwargs):
            result = self.mod.stop(self.repo)
            self.assertTrue(result['stopped'])
            return (json.dumps({'type':'thread.started','thread_id':session}) + '\n' + json.dumps({'type':'turn.completed'}), '')
        proc.communicate.side_effect = communicate
        with mock.patch.object(self.mod.subprocess, 'Popen', return_value=proc) as launch:
            self.mod.notify(state)
            self.assertTrue(self.mod.load(self.repo)['stopped'])
            self.mod.notify(self.mod.load(self.repo))
            self.assertEqual(launch.call_count, 1)

    def test_normal_cli_launch_runs_gate_hook_and_actual_acceptance_offline(self):
        self.normal_launch()

    def test_standalone_cli_launch_without_owner_policy(self):
        (self.home / '.config/mini-model-routing/policy.json').unlink()
        self.normal_launch()

    def test_standalone_codex_notification_uses_explicit_project_pin(self):
        (self.home / '.config/mini-model-routing/policy.json').unlink()
        self.config['supervision'] = {'notification_pin': {'model': 'fixture-design', 'effort': 'medium'}}
        self.write_config()
        command('git', '-C', str(self.repo), 'add', '.gate/config.json')
        command('git', '-C', str(self.repo), 'commit', '-qm', 'explicit notification contract')
        self.bind()
        state = self.write_state(transport='codex', session_path='fixture-session',
                                pending={'id': 'explicit', 'message': {}})
        proc = mock.Mock(returncode=0)
        proc.communicate.return_value = ('\n'.join(json.dumps(e) for e in [
            {'type': 'thread.started', 'thread_id': state['session']}, {'type': 'turn.completed'}]), '')
        with mock.patch.object(self.mod, 'codex_session', return_value='fixture-session'), \
                mock.patch.object(self.mod.subprocess, 'Popen', return_value=proc) as launch:
            self.mod.notify(state)
        self.assertIn('fixture-design', launch.call_args.args[0])
        self.assertEqual(state['delivery_evidence'], 'exact_session_turn_completed')
        self.assertEqual(state['calls'], 1)

    def test_clean_authorized_fallback_keeps_cumulative_accounting(self):
        self.normal_launch(fallback=True)

    def normal_launch(self, fallback=False):
        worker = self.bin / 'codex'
        worker.write_text('#!' + sys.executable + '\n' + '''import subprocess
from pathlib import Path

def run(*args):
    subprocess.run(args, check=True, stdout=subprocess.DEVNULL)
q = Path('TASK_QUEUE.md')
q.write_text(q.read_text().replace('`TODO`', '`IN_PROGRESS`'))
run('git', 'add', 'TASK_QUEUE.md')
run('git', 'commit', '-qm', 'claim')
Path('result.txt').write_text('implemented offline')
run(''' + repr(sys.executable) + ''', ''' + repr(str(self.engine / 'gate/gate.py')) + ''', 'verify', '--repo', '.', '--task', 'A')
q.write_text(q.read_text().replace('`IN_PROGRESS`', '`DONE`'))
run('git', 'add', 'TASK_QUEUE.md', 'result.txt')
run('git', 'commit', '-qm', 'complete')
''')
        worker.chmod(0o755)
        if fallback:
            shutil.copy(worker, self.bin / 'agy')
            worker.write_text('#!' + sys.executable + '\nprint("rate limit exceeded")\nraise SystemExit(1)\n')
            self.config['workers'] = ['codex', 'agy']
            self.config['worker_cmds']['agy'] = ['agy', '--model', 'gemini-4-hydrogen-high', '--effort', 'high']
            self.write_config()
            command('git', '-C', str(self.repo), 'add', '.gate/config.json')
            command('git', '-C', str(self.repo), 'commit', '-qm', 'authorized fallback fixture')
        output = command(sys.executable, str(self.engine / 'flow/flow.py'), 'supervise', 'launch',
            '--repo', str(self.repo), '--session', 'origin-thread', '--package', 'spec.md',
            '--tasks', 'A', '--max-calls', '3', '--seconds', '90', env=self.env)
        self.assertEqual(json.loads(output)['session'], 'origin-thread')
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = self.mod.load(self.repo)
            if self.mod.alive(state.get('runner')) is False:
                break
            time.sleep(.05)
        self.write_state(next_tick=0)
        result = self.mod.tick(self.repo)
        log = (self.mod.directory(self.repo) / 'runner.log').read_text()
        self.assertEqual(result['phase'], 'accepted', log[-3000:])
        self.assertEqual(result['calls'], 2 if fallback else 1)
        self.assertEqual((self.repo / 'result.txt').read_text(), 'implemented offline')
        self.assertIn('`DONE`', (self.repo / 'TASK_QUEUE.md').read_text())
        self.assertEqual(command('git', '-C', str(self.repo), 'status', '--porcelain'), '')
        self.assertIn('package_settled', (self.repo / '.gate/journal.ndjson').read_text())
        self.assertEqual(self.mod.tick(self.repo)['bytes_read'], 0)
        if fallback:
            events = [json.loads(line) for line in (self.repo / '.gate/journal.ndjson').read_text().splitlines()]
            self.assertTrue(any(e['event'] == 'tool_disabled' and e['worker'] == 'codex' for e in events))
            self.assertTrue(any(e['event'] == 'task_done' and e['worker'] == 'agy' for e in events))


if __name__ == '__main__':
    unittest.main()

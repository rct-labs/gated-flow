"""Behavioral checks: no history replay, no lost terminal event, bounded output."""
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

spec = importlib.util.spec_from_file_location('watch_journal', Path(__file__).with_name('watch-journal.py'))
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name) / 'journal.ndjson'
        self.cursor = Path(self.temp.name) / 'cursor.json'
        self.journal.write_bytes(b'')

    def append(self, event):
        with self.journal.open('ab') as stream:
            stream.write(json.dumps(event).encode() + b'\n')

    def read(self, **kwargs):
        return watch.read_events(self.journal, self.cursor, **kwargs)

    def test_history_is_not_replayed_and_idle_reads_zero_bytes(self):
        for _ in range(200):
            self.append({'event': 'task_done', 'task': 'old'})
        watch.initialize(self.journal, self.cursor)
        self.append({'event': 'task_done', 'task': 'new'})
        result = self.read()
        self.assertEqual([e['task'] for e in result['events']], ['new'])
        self.assertEqual(result['records_scanned'], 1)
        for expected in (120, 300, 300):
            idle = self.read()
            self.assertEqual(idle['bytes_read'], 0)
            self.assertEqual(idle['next_poll_s'], expected)

    def test_partial_record_is_delivered_once_when_complete(self):
        watch.initialize(self.journal, self.cursor)
        self.journal.write_bytes(b'{"event":"run_end",')
        pending = self.read()
        self.assertEqual(pending['events'], [])
        self.assertFalse(pending['more'])
        with self.journal.open('ab') as stream:
            stream.write(b'"stop":"queue_empty"}\n')
        self.assertEqual(self.read()['events'][0]['stop'], 'queue_empty')
        self.assertEqual(self.read()['events'], [])

    def test_limit_retains_terminal_event_for_next_read(self):
        watch.initialize(self.journal, self.cursor)
        for name in ('attempt_start', 'task_done', 'run_end'):
            self.append({'event': name})
        result = self.read(limit=2)
        self.assertTrue(result['more'])
        self.assertEqual(result['next_poll_s'], 0)
        self.assertEqual(self.read()['events'], [{'event': 'run_end'}])

    def test_invalid_line_and_heartbeat_cannot_hide_failure(self):
        watch.initialize(self.journal, self.cursor)
        self.journal.write_bytes(b'not json\n')
        self.append({'event': 'heartbeat', 'last_line': 'private data'})
        self.append({'event': 'run_end', 'stop': 'scope_request:T1', 'prompt': 'secret' * 1000})
        result = self.read()
        self.assertEqual([e['event'] for e in result['events']], ['journal_warning', 'run_end'])
        self.assertNotIn('secret', json.dumps(result))
        self.assertLess(len(json.dumps(result)), 500)

    def test_rotation_and_truncation_reset_cursor(self):
        self.append({'event': 'task_done', 'task': 'old' * 100})
        watch.initialize(self.journal, self.cursor)
        self.journal.write_bytes(b'{"event":"run_end"}\n')
        self.assertEqual(self.read()['events'][-1]['event'], 'run_end')
        replacement = self.journal.with_suffix('.new')
        replacement.write_bytes(b'{"event":"attempt_start"}\n')
        replacement.replace(self.journal)
        self.assertEqual(self.read()['events'][-1]['event'], 'attempt_start')

    def test_review_verdict_retains_pass_and_score(self):
        watch.initialize(self.journal, self.cursor)
        self.append({'at': '2026-09-23T08:00:00Z', 'event': 'review_verdict',
                     'passed': False, 'score': 3, 'findings': ['private content']})
        result = self.read()
        self.assertIs(result['events'][0]['passed'], False)
        self.assertEqual(result['events'][0]['score'], 3)
        self.assertEqual(result['last_activity_at'], '2026-09-23T08:00:00Z')
        self.assertNotIn('private content', json.dumps(result))

    def test_oversized_record_does_not_hide_following_stop(self):
        watch.initialize(self.journal, self.cursor)
        self.journal.write_bytes(b'x' * (watch.MAX_LINE + 100) + b'\n')
        self.append({'event': 'run_end'})
        self.assertEqual(self.read()['events'][0]['reason'], 'oversized_record')
        self.assertEqual(self.read()['events'], [{'event': 'run_end'}])

    def test_cursor_cannot_be_reused_for_another_repo(self):
        watch.initialize(self.journal, self.cursor)
        with self.assertRaisesRegex(ValueError, 'different journal'):
            watch.read_events(self.journal.with_suffix('.other'), self.cursor)

    def test_arming_inside_record_skips_only_its_remainder(self):
        self.journal.write_bytes(b'{"event":"task_done",')
        watch.initialize(self.journal, self.cursor)
        with self.journal.open('ab') as stream:
            stream.write(b'"task":"old"}\n')
        self.append({'event': 'run_end'})
        self.assertEqual(self.read()['events'], [{'event': 'run_end'}])

    def test_follow_cli_flushes_terminal_event_and_exits(self):
        repo = Path(self.temp.name) / 'repo'
        (repo / '.gate').mkdir(parents=True)
        journal = repo / '.gate/journal.ndjson'
        cursor = repo / '.gate/watch-journal.cursor.json'
        watch.initialize(journal, cursor)
        journal.write_text('{"event":"run_end","stop":"queue_empty"}\n', encoding='utf-8')
        result = subprocess.run([sys.executable, str(Path(watch.__file__)), str(repo), '--follow'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['events'][0]['stop'], 'queue_empty')

    def test_cli_refuses_unarmed_cursor_and_excessive_wait(self):
        command = [sys.executable, str(Path(watch.__file__)), self.temp.name]
        for extra in (['--once'], ['--wait', '61']):
            result = subprocess.run(command + extra, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()

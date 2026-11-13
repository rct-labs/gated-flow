"""Provider accounting regressions using captured-shaped envelopes only."""
import json
import io
import tempfile
from pathlib import Path
from unittest import mock
import unittest

from test_compat import load_gate_module


def codex(**changes):
    usage = dict(input_tokens=100, cached_input_tokens=40, output_tokens=12)
    usage.update(changes)
    return json.dumps(dict(type='turn.completed', usage=usage)) + '\n'


def claude(**changes):
    usage = dict(input_tokens=10, cache_read_input_tokens=30,
                 cache_creation_input_tokens=20, output_tokens=5)
    usage.update(changes)
    return json.dumps(dict(type='result', session_id='s', usage=usage,
                           modelUsage={'model': usage}, total_cost_usd=99)) + '\n'


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate_module()
        self.extract = self.gate._usage.extract_usage

    def test_codex_cache_is_subset_and_claude_cache_is_additive(self):
        a, b = self.extract('codex', codex()), self.extract('claude', claude())
        self.assertEqual((a['uncached_input_tokens'], a['total_tokens']), (60, 112))
        self.assertEqual((b['uncached_input_tokens'], b['total_tokens']), (10, 65))
        self.assertNotEqual(a['input_semantics'], b['input_semantics'])
        self.assertNotIn('cost', b)

    def test_cumulative_result_and_model_breakdown_are_not_summed(self):
        result = self.extract('claude', claude() + claude(input_tokens=15))
        self.assertEqual(result['total_tokens'], 70)
        self.assertEqual(self.extract('claude', claude() * 2)['total_tokens'], 65)

    def test_codex_turn_ids_deduplicate_but_distinct_turns_sum(self):
        a = json.loads(codex()); a['turn_id'] = 'a'
        b = dict(a, turn_id='b')
        result = self.extract('codex', '\n'.join(map(json.dumps, [a, a, b])))
        self.assertEqual(result['total_tokens'], 224)
        # Repeated unidentifiable turns cannot prove a complete total.
        self.assertIsNone(self.extract('codex', codex() * 2)['total_tokens'])

    def test_missing_partial_and_invalid_fields_stay_unknown(self):
        for value in (-1, True, '12', None, 1.5):
            for worker, output in [('codex', codex(output_tokens=value)),
                                   ('claude', claude(output_tokens=value))]:
                with self.subTest(worker=worker, value=value):
                    self.assertIsNone(self.extract(worker, output)['total_tokens'])
        for output in ('', 'OK', '{bad', '[]', '{"type":"turn.completed"}',
                       '{"type":"turn.completed","usage":{"input_tokens":3}}'):
            self.assertIsNone(self.extract('codex', output)['total_tokens'])
        self.assertIsNone(self.extract('codex', codex(cached_input_tokens=101))['total_tokens'])
        self.assertIsNone(self.extract('other', codex())['total_tokens'])
        self.assertIsNone(self.extract('claude', '{"type":"result","usage":{"input_tokens":1,"output_tokens":2}}')['total_tokens'])

    def test_malformed_terminal_record_does_not_leave_false_complete_total(self):
        self.assertIsNone(self.extract('codex', codex() + '{"type":"turn.completed","usage":null}\n')['total_tokens'])
        self.assertIsNone(self.extract('claude', claude() + '{"type":"result","usage":null}\n')['total_tokens'])

    def test_conflicting_receipts_and_partial_turns_cannot_claim_coverage(self):
        a = json.loads(codex()); a['turn_id'] = 'a'
        bad = dict(a, usage={'input_tokens': 3})
        self.assertIsNone(self.extract('codex', '\n'.join(map(json.dumps, [a, bad])))['total_tokens'])
        bad['turn_id'] = 'b'
        self.assertIsNone(self.extract('codex', '\n'.join(map(json.dumps, [a, bad])))['total_tokens'])
        other = json.loads(claude()); other['session_id'] = 'other'
        self.assertIsNone(self.extract('claude', claude() + json.dumps(other))['total_tokens'])
        start = dict(event='model_call', call='a', role='worker')
        end = dict(event='model_call_end', call='a', role='worker', usage=self.extract('codex', codex()))
        data = self.gate.run_metrics([start, end, dict(end, usage={})])['usage']
        self.assertIsNone(data['totals']['total_tokens'])
        self.assertEqual(data['complete_calls'], 0)
        data = self.gate.run_metrics([start, dict(end, usage=self.extract('codex', '{"type":"turn.completed","usage":{}}'))])['usage']
        self.assertEqual(data['measured_calls'], 0)

    def test_spawn_records_usage_and_preserves_full_log_and_unicode_bytes(self):
        from test_model_policy import CODEX, POLICY
        from test_compat import committed_runner_repo
        with tempfile.TemporaryDirectory() as td:
            repo = committed_runner_repo(Path(td), ['codex'])
            cfg = self.gate.load_config(repo)
            cfg['worker_cmds'] = {'codex': CODEX}
            output = 'progress: \u6e2c\u91cf\n' + codex()
            proc = mock.Mock(returncode=0, stdout=io.StringIO(output), stdin=io.StringIO())
            proc.poll.return_value = 0
            log = repo / '.gate/raw.log'
            self.gate.RUN_BUDGET.update(max=1, calls=0)
            with mock.patch.object(self.gate._model_policy, 'POLICY_PATH', POLICY), \
                 mock.patch.object(self.gate, 'resolve_tool', return_value='/native/codex'), \
                 mock.patch.object(self.gate.subprocess, 'Popen', return_value=proc):
                result, error = self.gate.spawn_worker(repo, cfg, 'codex', '\u6e2c\u91cf', repo / 'prompt', log_path=log)
            self.assertIsNone(error)
            self.assertEqual(result.stdout, output)
            self.assertEqual(log.read_text(), output)
            end = list(self.gate.iter_journal(repo))[-1]
            self.assertEqual(end['prompt_bytes'], len('\u6e2c\u91cf'.encode('utf-8')))
            self.assertEqual(end['output_bytes'], len(output.encode('utf-8')))
            self.assertEqual(end['usage']['total_tokens'], 112)

    def test_all_counter_fields_reject_non_integer_or_missing_values(self):
        for worker, factory, fields in [
                ('codex', codex, ('input_tokens', 'cached_input_tokens', 'output_tokens')),
                ('claude', claude, ('input_tokens', 'cache_read_input_tokens',
                                    'cache_creation_input_tokens', 'output_tokens'))]:
            for field in fields:
                for value in (False, -1, None, '4', 1.5):
                    with self.subTest(worker=worker, field=field, value=value):
                        self.assertIsNone(self.extract(worker, factory(**{field: value}))['total_tokens'])

    def test_role_coverage_and_unknown_aggregate_with_partial_calls(self):
        events = [{'event': 'run_start', 'metrics_version': 1}]
        for call, role, usage in [('a', 'worker', self.extract('codex', codex())),
                                  ('b', 'review', self.extract('claude', 'OK'))]:
            events += [dict(event='model_call', call=call, role=role),
                       dict(event='model_call_end', call=call, role=role, usage=usage,
                            prompt_bytes=7, output_bytes=9)]
        data = self.gate.run_metrics(events)['usage']
        self.assertIsNone(data['totals']['total_tokens'])
        self.assertEqual(data['observed']['total_tokens'], 112)
        self.assertEqual(data['roles']['worker']['totals']['total_tokens'], 112)
        self.assertEqual(data['complete_calls'], 1)
        self.assertEqual(data['calls'], 2)
        self.assertEqual(data['prompt_bytes'], 14)
        report = '\n'.join(self.gate._usage.render_usage(data))
        self.assertIn('| all | 1 / 2 | 14 | 18 | unknown | 112 |', report)
        self.assertIn('Missing usage is unknown', report)
        events.append(events[2].copy())
        self.assertEqual(self.gate.run_metrics(events)['usage']['observed']['total_tokens'], 112)
        events.append(dict(event='model_call', call='c', role='worker'))
        data = self.gate.run_metrics(events)['usage']
        self.assertIsNone(data['roles']['worker']['totals']['total_tokens'])
        self.assertIsNone(data['prompt_bytes'])
        self.assertIsNone(self.gate.run_metrics([])['usage']['totals']['total_tokens'])


if __name__ == '__main__':
    unittest.main()

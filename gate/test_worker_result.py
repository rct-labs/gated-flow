"""Pure offline worker final-output contract regressions."""
import json
import unittest

import worker_result as result


def marker(**changes):
    body = dict(version=1, task='PS-1', status='scope_request',
                files=['gate/test_efficiency.py'], reason='Required regression outside admitted scope')
    body.update(changes)
    return 'FLOW_RESULT: ' + json.dumps(body)


def codex(text, kind='agent_message'):
    return json.dumps({'type': 'item.completed', 'item': {'type': kind, 'text': text,
                       'aggregated_output': text}})


class WorkerResultTests(unittest.TestCase):
    def test_supported_final_envelopes_and_plain_text(self):
        for output in (marker(), codex(marker()) + '\n{"type":"turn.completed"}',
                       json.dumps({'type': 'result', 'result': marker()}),
                       json.dumps({'type': 'assistant', 'message': {'content': [
                           {'type': 'text', 'text': marker()}]}})):
            with self.subTest(output=output):
                self.assertEqual(result.scope_request(output, 'PS-1')['files'],
                                 ['gate/test_efficiency.py'])

    def test_latest_final_supersedes_intermediate_requests(self):
        for output in (codex(marker()) + '\n' + codex('Implemented and committed.'),
                       codex(marker()) + '\n' + json.dumps({'type': 'result', 'result': 'Done'})):
            self.assertIsNone(result.scope_request(output, 'PS-1'))

    def test_tools_source_dumps_and_quoted_examples_are_not_decisions(self):
        for output in (codex(marker(), 'command_execution'),
                       json.dumps({'type': 'user', 'message': {'content': [
                           {'type': 'tool_result', 'content': marker()}]}}),
                       json.dumps({'type': 'unknown', 'nested': {'text': marker()}}),
                       'Example:\n```text\n' + marker() + '\n```\nDone.',
                       '> ' + marker() + '\nDone.'):
            self.assertIsNone(result.scope_request(output, 'PS-1'))

    def test_ordinary_success_can_describe_scope_request_implementation(self):
        for text in ('Implemented the scope request contract and committed.',
                     'Added regression for required scope expansion. Verification passed.',
                     'Added the phrase "please add it to EV-8 scope" in a regression.'):
            self.assertIsNone(result.scope_request(text, 'PS-1'))

    def test_invalid_explicit_contracts_stop(self):
        for changes in ({'version': True}, {'version': 2}, {'task': 'OTHER'}, {'task': 1},
                        {'status': 'done'}, {'files': []}, {'files': 'gate/a.py'},
                        {'reason': ''}, {'reason': ' '}, {'reason': 2}, {'reason': 'x' * 2001}):
            with self.subTest(changes=changes), self.assertRaises(result.ProtocolError):
                result.scope_request(marker(**changes), 'PS-1')
        for path in ('/tmp/a', '../a', 'gate/../a', './a', 'a//b', 'a/', 'C:' + '/a',
                     'C:\\a', 'a\\b', 'a\x00b', '.', '..', ''):
            with self.subTest(path=path), self.assertRaises(result.ProtocolError):
                result.scope_request(marker(files=[path]), 'PS-1')
        for output in ('FLOW_RESULT: {bad', marker() + '\n' + marker(task='OTHER')):
            with self.assertRaises(result.ProtocolError):
                result.scope_request(output, 'PS-1')

    def test_observed_english_and_historical_prose(self):
        for text in (
            'EV-8 is blocked by a required scope expansion for gate/test_autonomy.py. '
            'Please add it to EV-8’s scope. Existing changes remain intact',
            'Scope request: please add gate/test_efficiency.py to PS-1 scope.',
            'Blocked: this needs your decision on the declared files; I would also touch src/extra.py',
            '| \u8981\u4f60\u51b3\u5b9a | \u6269\u56f4 tests/test_new.py |',
        ):
            with self.subTest(text=text):
                request = result.scope_request(codex(text), 'PS-1')
                self.assertTrue(request['files'])
                self.assertEqual(request['reason'], text)
                self.assertIsNone(result.scope_request(codex(text, 'command_execution'), 'PS-1'))
        self.assertIsNone(result.scope_request('| \u8981\u4f60\u51b3\u5b9a | \u65e0 |', 'PS-1'))

    def test_explicit_only_phase_defers_legacy_but_validates_markers(self):
        prose = 'Scope request: please add gate/test_efficiency.py to PS-1 scope.'
        self.assertIsNone(result.scope_request(codex(prose), 'PS-1', include_legacy=False))
        self.assertEqual(result.scope_request(codex(prose), 'PS-1')['files'],
                         ['gate/test_efficiency.py'])
        self.assertEqual(result.scope_request(codex(marker()), 'PS-1', include_legacy=False)['files'],
                         ['gate/test_efficiency.py'])
        with self.assertRaises(result.ProtocolError):
            result.scope_request('FLOW_RESULT: {bad', 'PS-1', include_legacy=False)

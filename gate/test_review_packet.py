"""Offline Git evidence and receipt regressions for EV-2."""
import json
import tempfile
import unittest
from pathlib import Path

import review_packet as packet
from test_compat import run, load_gate_module, committed_runner_repo


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = committed_runner_repo(Path(self.tmp.name), ['codex'])
        self.base = self.head()
        self.change('src.txt', b'before\n')
        self.prior_head = self.head()
        self.contract = {'stage': 'module', 'scope': ['src.txt']}

    def head(self):
        return run('git', '-C', str(self.repo), 'rev-parse', 'HEAD').stdout.strip()

    def change(self, name, content):
        (self.repo / name).write_bytes(content)
        run('git', '-C', str(self.repo), 'add', '--', name)
        result = run('git', '-C', str(self.repo), 'commit', '-qm', name)
        self.assertEqual(result.returncode, 0, result.stderr)

    def build(self, **kw):
        args = dict(repo=self.repo, base=self.base, head=self.head(), stage='module',
                    tasks=['FIX'], declared={'FIX': ['src.txt'], 'ORIGINAL': ['src.txt']}, acceptance={'FIX': 'real acceptance'},
                    evidence={'log': '.gate/check.log'}, defects={}, contract=self.contract,
                    repairs=['FIX'], review_of={'FIX': ['ORIGINAL']})
        args.update(kw)
        return packet.build(**args)

    def prior(self, **kw):
        p = self.build(tasks=['ORIGINAL'], repairs=[], review_of={})
        artifact = self.repo / '.gate' / ('prior-' + str(len(list(packet.events(self.repo)))) + '.json')
        artifact.write_text(json.dumps(p))
        prior = dict(p, packet=str(artifact), event='review_verdict', passed=False,
                     parsed_review={'score': 40, 'verdict': 'revise',
                                    'revision_brief': 'repair the bug',
                                    'findings': [{'id': 'BUG-000000000001', 'issue': 'bug',
                                                  'severity': 'high', 'file': 'src.txt',
                                                  'fix': 'repair', 'action': 'required'}]},
                     blocker_ids=['BUG-000000000001'])
        prior.update(kw)
        with (self.repo / '.gate/journal.ndjson').open('a') as f:
            f.write(json.dumps(prior) + '\n')
        return prior

    def test_manifest_contains_undeclared_deleted_binary_and_odd_names(self):
        self.change('odd\nname.bin', b'\x00binary')
        (self.repo / 'src.txt').unlink()
        run('git', '-C', str(self.repo), 'commit', '-qam', 'delete')
        p = self.build(base=self.prior_head)
        files = {e['path']: e for e in p['manifest']}
        self.assertTrue(files['odd\nname.bin']['binary'])
        self.assertTrue(files['src.txt']['removed'])
        self.assertNotIn('odd\nname.bin', p['declared']['FIX'])
        self.assertEqual(len(p['head']), 40)

    def test_bounded_projection_keeps_original_and_honest_omissions(self):
        p = self.build(acceptance={'FIX': 'acceptance' * 10000})
        artifact = self.repo / '.gate/full.json'
        artifact.write_text(json.dumps(p))
        prompt = packet.render(p, artifact, cap=4000)
        self.assertLessEqual(len(prompt.encode()), 4000)
        self.assertIn('acceptance: OMITTED', prompt)
        self.assertIn('Omitted evidence is NOT reviewed', prompt)
        self.assertEqual(json.loads(artifact.read_text()), p)
        self.assertIn(self.base, prompt)
        self.assertIn('--binary', prompt)

    def test_valid_delta_retains_manifest_findings_and_blocker_ids(self):
        self.prior()
        self.change('src.txt', b'fixed\n')
        p = self.build()
        self.assertEqual(p['mode'], 'incremental')
        self.assertEqual(p['anchor'], self.prior_head)
        self.assertEqual(p['prior_blockers'], ['BUG-000000000001'])
        self.assertEqual(p['prior_findings'][0]['id'], 'BUG-000000000001')
        self.assertEqual(p['manifest'], packet.manifest(self.repo, self.base, self.head()))

    def test_missing_invalid_nonancestor_or_changed_contract_stage_scope_are_full(self):
        self.assertEqual(self.build()['mode'], 'full')
        self.prior(parsed_review={'invalid': True})
        self.assertEqual(self.build()['mode'], 'full')
        self.prior()
        self.change('src.txt', b'fixed\n')
        for change in ({'stage': 'delivery'}, {'stage': 'integration'},
                       {'contract': {'policy': 'changed'}}, {'repairs': []}, {'review_of': {}},
                       {'review_of': {'FIX': ['OTHER']}},
                       {'declared': {'FIX': ['elsewhere']}}):
            with self.subTest(change=change):
                self.assertEqual(self.build(**change)['mode'], 'full')
        run('git', '-C', str(self.repo), 'checkout', '--orphan', 'other')
        run('git', '-C', str(self.repo), 'commit', '-qam', 'unrelated')
        self.assertEqual(self.build()['mode'], 'full')

    def test_high_risk_and_unmapped_changes_force_full_review(self):
        self.prior()
        self.change('outside.txt', b'new scope')
        self.assertEqual(self.build()['mode'], 'full')
        for name in ('docs/spec.md', '.gate/config.json', 'gate/safety.py'):
            (self.repo / name).parent.mkdir(parents=True, exist_ok=True)
            self.change(name, b'changed policy')
            self.assertEqual(self.build(declared={'FIX': ['*']})['mode'], 'full')

    def test_full_and_irreversible_patterns_override_valid_repair(self):
        self.prior()
        self.change('src.txt', b'fixed')
        self.assertEqual(self.build(full_globs=['src.*'])['mode'], 'full')
        self.assertEqual(self.build(full_globs=['**'])['mode'], 'full')

    def test_config_operational_exception_never_overrides_explicit_safety_globs(self):
        self.prior()
        config = self.repo / '.gate/config.json'
        cfg = json.loads(config.read_text())
        cfg['max_model_calls'] = 5
        self.change('.gate/config.json', json.dumps(cfg).encode())
        options = {'semantic_config_paths': ['.gate/config.json']}
        self.assertEqual(self.build(**options)['mode'], 'incremental')
        self.assertEqual(self.build(**options, full_globs=['.gate/**'])['mode'], 'full')
        self.assertEqual(self.build(**options, full_globs=['**/config*'])['mode'], 'full')

    def test_incomplete_or_tampered_latest_receipt_never_reuses_older_baseline(self):
        self.prior()
        self.prior(parsed_review={'score': 40, 'verdict': 'revise', 'findings': []})
        self.change('src.txt', b'fixed')
        self.assertEqual(self.build()['mode'], 'full')
        prior = self.prior()
        prior['original_tasks'] = ['OTHER']
        self.assertFalse(packet.artifact_valid(prior))

    def test_contract_uses_original_semantics_not_queue_accounting(self):
        import copy
        gate = load_gate_module()
        queue = self.repo / 'TASK_QUEUE.md'
        queue.write_text(queue.read_text() + '<!-- task:EAV-2 spec: original-spec.md -->\n')
        (self.repo / 'original-spec.md').write_text('## Acceptance\nOriginal acceptance.\n')
        unrelated = self.repo / 'docs/work/old/spec.md'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text('Unrelated old spec')
        cfg = gate.load_config(self.repo)
        original = gate.review_contract(self.repo, cfg, ['EAV-2'])
        queue.write_text(queue.read_text().replace('`TODO`', '`DONE`') +
                         '<!-- task:EAV-3 origin: review -->\n'
                         '<!-- task:EAV-3 review_of: EAV-2 -->\n'
                         '<!-- task:EAV-3 verify: {"cmd": "repair check", "timeout_s": 12} -->\n')
        unrelated.write_text('Unrelated historical update')
        cfg['repair_continuation'] = {'enabled': True, 'approved_by': 'existing goal', 'tasks': ['EAV-3']}
        cfg['max_model_calls'] = 4
        cfg['max_tasks'] = 1
        self.assertEqual(original, gate.review_contract(self.repo, cfg, ['EAV-3']))
        self.assertEqual(original, gate.review_contract(self.repo, cfg, ['EAV-2', 'EAV-3']))
        for field, value in (('verify_cmd', 'changed check'), ('irreversible_globs', ['*']),
                             ('delivery', {'stage': 'module', 'full_globs': ['*']}),
                             ('judge_prompt', 'Review {tasks} since {base} differently')):
            changed = copy.deepcopy(cfg)
            changed[field] = value
            self.assertNotEqual(original, gate.review_contract(self.repo, changed, ['EAV-3']))
        queue.write_text(queue.read_text().replace('files: eav2.txt', 'files: new.txt'))
        self.assertNotEqual(original, gate.review_contract(self.repo, cfg, ['EAV-3']))
        queue.write_text(queue.read_text().replace('files: new.txt', 'files: eav2.txt'))
        queue.write_text(queue.read_text() + '<!-- task:EAV-2 impact: shared -->\n')
        self.assertNotEqual(original, gate.review_contract(self.repo, cfg, ['EAV-3']))
        (self.repo / 'original-spec.md').write_text('## Acceptance\nChanged acceptance.\n')
        self.assertNotEqual(original, gate.review_contract(self.repo, cfg, ['EAV-3']))

    def test_review_mapping_rejects_missing_unknown_self_duplicate_and_repair_targets(self):
        gate = load_gate_module()
        rows = (self.repo / 'TASK_QUEUE.md').read_text()
        valid = rows + '<!-- task:EAV-3 origin: review -->\n<!-- task:EAV-3 review_of: EAV-2 -->\n'
        self.assertEqual(gate.parse_review_of(valid), {'EAV-3': ['EAV-2']})
        for text in (rows, valid.replace('review_of: EAV-2', 'review_of: UNKNOWN'),
                     valid.replace('review_of: EAV-2', 'review_of: EAV-3'),
                     valid + '<!-- task:EAV-3 review_of: EAV-2 -->',
                     valid + '<!-- task:EAV-2 origin: review -->'):
            self.assertEqual(gate.parse_review_of(text), {})

    def test_receipt_rejects_changed_work_stage_contract_or_packet(self):
        prior = self.prior(passed=True)
        cfg = {'delivery': {'stage': 'module'}}
        self.assertTrue(packet.receipt_valid(self.repo, cfg, prior, ['ORIGINAL'], packet.digest(self.contract)))
        self.assertFalse(packet.receipt_valid(self.repo, {'delivery': {'stage': 'delivery'}}, prior, ['ORIGINAL']))
        self.assertFalse(packet.receipt_valid(self.repo, cfg, prior, ['OTHER']))
        self.assertFalse(packet.receipt_valid(self.repo, cfg, prior, ['ORIGINAL'], 'different'))
        (self.repo / 'untracked.py').write_text('new work')
        self.assertFalse(packet.receipt_valid(self.repo, cfg, prior, ['ORIGINAL']))
        (self.repo / 'untracked.py').unlink()
        (self.repo / 'src.txt').write_text('dirty')
        self.assertFalse(packet.receipt_valid(self.repo, cfg, prior, ['ORIGINAL']))
        self.change('src.txt', b'committed new work')
        self.assertFalse(packet.receipt_valid(self.repo, cfg, prior, ['ORIGINAL']))
        Path(prior['packet']).write_text('{}')
        self.assertEqual(self.build()['mode'], 'full')
        self.assertFalse(packet.receipt_valid(self.repo, cfg, prior, ['ORIGINAL']))

    def test_mixed_legacy_closure_and_new_skipped_review_survive_restart(self):
        gate = load_gate_module()
        for event in (
            {'event': 'task_done', 'task': 'OLD', 'commit': self.head()},
            {'event': 'full_acceptance', 'result': 'PASS'},
            {'event': 'task_done', 'task': 'OLD-SCOPED', 'commit': self.head()},
            {'event': 'stage_acceptance', 'tasks': ['OLD-SCOPED'], 'result': 'PASS'},
            {'event': 'run_start', 'settlement_version': 1},
            {'event': 'task_done', 'task': 'EAV-2', 'commit': self.head(),
             'settlement_version': 1, 'review_required': True},
            {'event': 'review_skipped', 'tasks': ['EAV-2'], 'reason': 'budget'},
            {'event': 'full_acceptance', 'result': 'PASS'},
            {'event': 'stage_acceptance', 'tasks': ['EAV-2'], 'result': 'PASS'},
        ):
            gate.journal(self.repo, event)
        restarted = load_gate_module()
        self.assertEqual([e['task'] for e in restarted.pending_closeout(self.repo)[0]], ['EAV-2'])
        cfg = restarted.load_config(self.repo)
        cfg['judge']['enabled'] = False
        result = restarted.run_stage_acceptance(self.repo, cfg, ['EAV-2'], self.base)
        self.assertEqual(result['result'], 'FAIL')
        self.assertIn('pending independent review', result['tail'])

    def test_run_version_applies_to_task_done_and_legacy_scoped_closure_is_named(self):
        gate = load_gate_module()
        for event in (
            {'event': 'task_done', 'task': 'OLD-A', 'commit': self.head()},
            {'event': 'task_done', 'task': 'OLD-B', 'commit': self.head()},
            {'event': 'stage_acceptance', 'tasks': ['OLD-A'], 'result': 'PASS'},
            {'event': 'run_start', 'settlement_version': 1},
            {'event': 'task_done', 'task': 'NEW', 'commit': self.head()},
            {'event': 'full_acceptance', 'result': 'PASS'},
        ):
            gate.journal(self.repo, event)
        self.assertEqual([e['task'] for e in gate.pending_closeout(self.repo)[0]], ['NEW'])
        gate.journal(self.repo, {'event': 'package_settled', 'tasks': ['NEW']})
        self.assertFalse(gate.pending_closeout(self.repo)[0])

    def test_legacy_implicit_failed_review_closes_only_after_acceptance_pass(self):
        gate = load_gate_module()
        for event in ({'event': 'task_done', 'task': 'EAV-2', 'commit': self.head()},
                      {'event': 'review_verdict', 'tasks': ['EAV-2'], 'passed': False}):
            gate.journal(self.repo, event)
        pending, reviewed = gate.pending_closeout(self.repo)
        self.assertEqual([e['task'] for e in pending], ['EAV-2'])
        self.assertFalse(reviewed)
        gate.journal(self.repo, {'event': 'full_acceptance', 'result': 'PASS'})
        self.assertEqual(gate.pending_closeout(self.repo)[0], [])

    def test_modern_and_explicit_failed_reviews_survive_green_acceptance(self):
        gate = load_gate_module()
        for task, version, explicit in (('MODERN', 1, False), ('EXPLICIT', 0, True)):
            with self.subTest(task=task):
                for event in (
                    {'event': 'task_done', 'task': task, 'commit': self.head(),
                     'settlement_version': version, 'review_required': explicit},
                    {'event': 'review_verdict', 'tasks': [task], 'passed': False},
                    {'event': 'full_acceptance', 'result': 'PASS'},
                    {'event': 'stage_acceptance', 'tasks': [task], 'result': 'PASS'},
                ):
                    gate.journal(self.repo, event)
                pending, reviewed = gate.pending_closeout(self.repo)
                self.assertEqual([e['task'] for e in pending], [task])
                self.assertFalse(reviewed)
                gate.journal(self.repo, {'event': 'package_settled', 'tasks': [task]})
                self.assertEqual(gate.pending_closeout(self.repo)[0], [])


class ReviewEvidenceTests(unittest.TestCase):
    def test_unverified_source_evidence_survives_frozen_finding_schema(self):
        gate = load_gate_module()
        issue = ('Trigger: a scope_request accompanies a DONE claim. '
                 'Source proof: worker decision must precede settlement. '
                 'Repro unavailable; explicitly unverified. '
                 'Expected: stop. Observed source path: settlement occurs first. '
                 'Violated invariant: no worker declaration may settle a task.')
        finding = {'id': 'BUG-000000000001', 'severity': 'high', 'file': 'gate/gate.py',
                   'issue': issue, 'fix': 'interpret the decision before DONE', 'action': 'required'}
        normalized = gate.normalize_judge_verdict({'score': 40, 'verdict': 'revise',
                    'findings': [finding], 'revision_brief': 'repair the ordering'})
        self.assertEqual(normalized['findings'][0]['issue'], issue)
        self.assertEqual(normalized['findings'][0]['severity'], 'high')
        self.assertEqual(normalized['findings'][0]['action'], 'required')
        self.assertIn(issue, gate.format_findings(normalized['findings']))

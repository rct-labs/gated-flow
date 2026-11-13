"""Offline regressions for the conditional skill-reference navigation contract.

Run with: python3 -m unittest discover -s skills/flow/tests -p 'test_*.py'
These checks do not load skills, start services or invoke any provider.
"""
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[3]
ENTRYPOINTS = [ROOT / 'skills' / name / 'SKILL.md'
               for name in ('flow', 'flow-run', 'run-queue')]
AUTHORITY = ROOT / 'docs/autonomy.md'


def links(path):
    """Local Markdown navigation only; CLI examples are not navigation."""
    text = path.read_text(encoding='utf-8')
    for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', text):
        if '://' in target:
            continue
        name, _, fragment = target.partition('#')
        yield (path.parent / unquote(name)).resolve() if name else path, fragment


def reachable(start):
    seen, pending = set(), [start]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        if path.is_file():
            pending.extend(target for target, _ in links(path))
    return seen


class WorkflowReferences(unittest.TestCase):
    def test_all_conditional_links_and_heading_anchors_resolve(self):
        for path in set().union(*(reachable(entry) for entry in ENTRYPOINTS)):
            self.assertTrue(path.is_file(), f'missing conditional reference: {path}')
            for target, fragment in links(path):
                with self.subTest(source=path, target=target, fragment=fragment):
                    self.assertTrue(target.is_file())
                    if fragment:
                        headings = re.findall(r'^#+\s+(.+)$', target.read_text(), re.M)
                        anchors = {re.sub(r'[^\w\- ]', '', h.lower()).replace(' ', '-')
                                   for h in headings}
                        self.assertIn(fragment, anchors)

    def test_each_router_directly_loads_the_same_rule_authority(self):
        for entry in ENTRYPOINTS:
            with self.subTest(entry=entry):
                self.assertIn(AUTHORITY, {path for path, _ in links(entry)})

    def test_host_recovery_links_to_owned_rules(self):
        # These consumers must navigate to the actual rule, not copy it into
        # a router or leave a template user without its governing procedure.
        consumers = {
            'skills/flow-run/references/inspect.md': {'scope-request-recovery',
                                                    'critical-safety-assumptions'},
            'skills/flow-run/references/outcome.md': {'scope-request-recovery',
                                                    'evidence-learning-and-bounded-experiments'},
        }
        for name, anchors in consumers.items():
            with self.subTest(consumer=name):
                owned = {fragment for target, fragment in links(ROOT / name)
                         if target == AUTHORITY}
                self.assertLessEqual(anchors, owned)

    def test_copied_templates_find_installed_rules_without_project_docs(self):
        # Exercise the lookup from a business package with no flow docs, using
        # the candidate CLI as the installed `flow` on PATH. Spaces catch unsafe
        # quoting; no global installation or provider call is needed.
        with tempfile.TemporaryDirectory(prefix='flow consumer ') as directory:
            root = Path(directory)
            package = root / 'business repo/docs/work/example'
            package.mkdir(parents=True)
            installed = root / 'installed flow'
            (installed / 'flow').mkdir(parents=True)
            (installed / 'docs').mkdir()
            shutil.copyfile(ROOT / 'flow/flow.py', installed / 'flow/flow.py')
            shutil.copyfile(AUTHORITY, installed / 'docs/autonomy.md')
            binary = root / 'bin'
            binary.mkdir()
            shim = binary / 'flow'
            shim.write_text('#!/bin/sh\nexec ' + shlex.join(
                [sys.executable, str(installed / 'flow/flow.py')]) + ' "$@"\n')
            shim.chmod(0o755)
            env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ['PATH'])
            for name in ('spec.md', 'evidence.md'):
                copied = package / name
                shutil.copyfile(ROOT / 'flow/templates' / name, copied)
                text = copied.read_text()
                lookups = re.findall(r'Lookup: `([^`]+)`', text)
                self.assertTrue(lookups, f'{name}: no executable rule lookup')
                sections = re.findall(r'section `([^`]+)`', text)
                self.assertTrue(sections, f'{name}: no canonical rule section')
                for command in lookups:
                    with self.subTest(template=name, lookup=command):
                        result = subprocess.run(command, shell=True, cwd=package,
                                                env=env, capture_output=True, text=True,
                                                timeout=10)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, AUTHORITY.read_text())
                        headings = re.findall(r'^#+\s+(.+)$', result.stdout, re.M)
                        for section in sections:
                            self.assertIn(section, headings)
                # Also catch ordinary relative navigation accidentally reintroduced
                # into comments: the copied consumer must resolve every local link.
                for target, _ in links(copied):
                    self.assertTrue(target.is_file(), f'broken copied link: {target}')
            self.assertFalse((root / 'business repo/docs/autonomy.md').exists())

    def test_extracted_procedures_remain_reachable(self):
        graph = set().union(*(reachable(entry) for entry in ENTRYPOINTS))
        procedures = set()
        for entry in ENTRYPOINTS:
            procedures.update((entry.parent / 'references').glob('*.md'))
        self.assertTrue(procedures)
        self.assertLessEqual(procedures, graph, 'orphaned procedure after extraction')
        for entry in ENTRYPOINTS:
            # Every entry must retain access to both continuity and evidence
            # boundaries, including a launch resumed without flow-run.
            with self.subTest(entry=entry):
                self.assertIn(ROOT / 'skills/flow/references/continuity.md', reachable(entry))
                self.assertIn(ROOT / 'flow/templates/evidence.md', reachable(entry))


if __name__ == '__main__':
    unittest.main()

"""Offline acceptance tests for IL-14: declarative scoped handoff extraction, provenance, review inline projection, skill reasons, and IL-12/IL-13 replay."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import review_packet as rpacket
from test_autonomy import verdict
from test_compat import FLOW, GATE, committed_runner_repo, load_gate_module, run

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flow import skills as flow_skills  # noqa: E402


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_historical_prompt_fixture(
    *,
    worker_header: str,
    queue_row: str,
    declared_files: str,
    local_check: str,
    task: str,
    spec_rel: str,
    acceptance_text: str,
    pin_line: str,
) -> str:
    scope_json = json.dumps(
        {
            "version": 1,
            "task": task,
            "status": "scope_request",
            "files": ["path/to/required_file.py"],
            "reason": "Explain why the admitted scope is insufficient",
        }
    )
    parts = [
        worker_header,
        "",
        "[TASK PACKET]",
        f"queue row: {queue_row}",
        f"declared files: {declared_files}",
        f"local check: {local_check}",
        "closing: stage only your own changes, flip the row to DONE, commit through "
        "the installed hook. Use the local check above as final validation; do not "
        "run its underlying command a second time immediately before or after it. "
        "The runner handles stage and full delivery acceptance.",
        "If required scope is missing, preserve partial changes and stop immediately. "
        f"Emit exactly one unquoted line in your final assistant answer: FLOW_RESULT: {scope_json}"
        ". Replace files with 1-100 normalized repository-relative paths (no absolute "
        "paths, backslashes or traversal), and reason with nonempty text of at most "
        "2000 characters. Do not claim DONE or retry. For normal completion omit "
        "FLOW_RESULT and follow the gate closing instructions above.",
        f"spec: {spec_rel} (acceptance embedded below; reread only if changed)",
        "acceptance:\n" + acceptance_text,
        pin_line,
        "Follow the installed agent-skills disciplines (incremental "
        "implementation, test-driven development, debugging and error "
        "recovery) when they are available to you.",
    ]
    return "\n".join(parts) + "\n"


def _parse_packet_components(packet_text: str) -> dict[str, str]:
    marker = "[TASK PACKET]\n"
    idx = packet_text.index(marker)
    body = packet_text[idx + len(marker) :]
    lines = body.splitlines()
    out: dict[str, str] = {}
    acc_lines: list[str] = []
    handoff_lines: list[str] = []
    mode = "header"
    for ln in lines:
        if mode == "header":
            if ln.startswith("queue row: "):
                out["queue_row"] = ln
            elif ln.startswith("declared files: "):
                out["declared_files"] = ln
            elif ln.startswith("local check: "):
                out["local_check"] = ln
            elif ln.startswith("closing: "):
                out["closing"] = ln
            elif "FLOW_RESULT:" in ln and ln.startswith("If required scope is missing"):
                out["scope_protocol"] = ln
            elif ln.startswith("spec: "):
                out["spec"] = ln
            elif ln == "acceptance:":
                mode = "acceptance"
        elif mode == "acceptance":
            if ln.startswith("handoff ("):
                mode = "handoff"
                handoff_lines.append(ln)
            elif ln.startswith("pins: "):
                out["pins"] = ln
                mode = "footer"
            else:
                acc_lines.append(ln)
        elif mode == "handoff":
            if ln.startswith("pins: "):
                out["pins"] = ln
                mode = "footer"
            else:
                handoff_lines.append(ln)
        elif mode == "footer":
            if ln.startswith("Follow the installed agent-skills disciplines"):
                out["disciplines"] = ln
    out["acceptance"] = "\n".join(acc_lines)
    if handoff_lines:
        out["handoff"] = "\n".join(handoff_lines)
    return out


class ScopedHandoffExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gate = load_gate_module()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _make_repo(self, name: str = "sub") -> tuple[Path, str]:
        sub = self.root / name
        sub.mkdir(parents=True, exist_ok=True)
        repo = committed_runner_repo(sub, ["codex"])
        shutil.copytree(ROOT / "skills", repo / "skills")
        (repo / "eav2.txt").write_text("baseline implementation\n", encoding="utf-8")
        qpath = repo / "TASK_QUEUE.md"
        qpath.write_text(
            qpath.read_text(encoding="utf-8") + "<!-- task:EAV-2 spec: docs/work/pkg/spec.md -->\n",
            encoding="utf-8",
        )
        res = run("git", "-C", str(repo), "add", "eav2.txt", "skills", "TASK_QUEUE.md")
        self.assertEqual(res.returncode, 0, res.stderr)
        res = run("git", "-C", str(repo), "commit", "-qm", "seed eav2 and skills")
        self.assertEqual(res.returncode, 0, res.stderr)
        base = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        return repo, base

    def _write_spec(
        self,
        repo: Path,
        *,
        acceptance: str = "Keep acceptance intact with citation `ev_1234567890ab` and BUG-000000000001.",
        metadata: dict | str | None = None,
        handoff_body: str = "",
        include_handoff: bool = True,
    ) -> Path:
        spec_path = repo / "docs" / "work" / "pkg" / "spec.md"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        content = f"# Spec\n\n## Acceptance\n{acceptance}\n"
        if include_handoff:
            meta_line = ""
            if isinstance(metadata, str):
                meta_line = metadata + "\n"
            elif metadata is not None:
                meta_line = f"<!-- handoff: {json.dumps(metadata)} -->\n"
            content += f"\n## Handoff EAV-2\n{meta_line}{handoff_body}\n"
            content += "\n## Handoff EAV-3\n### Secret\nOther task handoff must stay isolated.\n"
        spec_path.write_text(content, encoding="utf-8")
        return spec_path

    def test_legacy_absence_and_legacy_unscoped_handoff_preserved_verbatim(self) -> None:
        repo, base = self._make_repo("legacy")
        cfg = self.gate.load_config(repo)
        qtext = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")

        # 1. Spec with no Handoff section.
        self._write_spec(repo, include_handoff=False)
        pkt_no_handoff = self.gate.task_packet(repo, cfg, qtext, "EAV-2", ["eav2.txt"])
        self.assertNotIn("handoff (", pkt_no_handoff)
        self.assertNotIn("handoff_provenance:", pkt_no_handoff)
        self.assertIn("Keep acceptance intact with citation `ev_1234567890ab`", pkt_no_handoff)

        # 2. Legacy handoff with only base/files (no role_sections or required_sections).
        legacy_body = (
            "Prelude citation [evidence](docs/work/pkg/evidence.md#L7).\n\n"
            "### Worker Steps\nLegacy worker prose.\n\n"
            "### Review Notes\nLegacy review prose."
        )
        self._write_spec(repo, metadata={"base": base, "files": ["eav2.txt"]}, handoff_body=legacy_body)
        h_worker = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role="worker")
        h_review = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role="review")
        self.assertEqual(h_worker["mode"], "legacy")
        self.assertFalse(h_worker["opted_in"])
        self.assertFalse(h_worker["filtered"])
        self.assertEqual(h_worker["text"], h_review["text"])
        self.assertNotIn("handoff_provenance:", h_worker["text"])
        self.assertIn("CURRENT: related files unchanged against base (prose still requires judgment)", h_worker["text"])
        self.assertIn("Legacy worker prose.", h_worker["text"])
        self.assertIn("Legacy review prose.", h_worker["text"])

    def test_selected_required_prelude_citations_code_fences_and_provenance_hashes(self) -> None:
        repo, base = self._make_repo("scoped")
        cfg = self.gate.load_config(repo)
        flow_skills.pin_task_start(repo, "EAV-2", skill_name="flow-run")
        qtext = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")

        prelude = (
            "Shared prelude: preserve [citation](docs/work/pkg/evidence.md#L12) "
            "and primary ID `ev_6dfe590e09f1`."
        )
        sec_impl = (
            "### Implementation\n"
            "Update `eav2.txt` via established owner [gate.py](gate/gate.py#L760).\n"
            "```markdown\n"
            "### Fake Heading Inside Code Fence\n"
            "Keep fenced example intact.\n"
            "```"
        )
        sec_safety = (
            "### Safety & Stop Conditions\n"
            "Preserve all stop conditions, pre-commit hook, and citation `BUG-f54b34a0ebac`."
        )
        sec_review = (
            "### Reviewer Focus\n"
            "Verify exact diff against base and confirm [review_packet.py](gate/review_packet.py#L64)."
        )
        sec_unused = (
            "### Appendix Notes\n"
            "Unselected background prose that must not duplicate into scoped prompts."
        )
        body = "\n\n".join((prelude, sec_impl, sec_safety, sec_review, sec_unused))
        meta = {
            "base": base,
            "files": ["eav2.txt"],
            "role_sections": {
                "worker": ["Implementation", "Safety & Stop Conditions"],
                "review": ["Reviewer Focus"],
            },
            "required_sections": ["Safety & Stop Conditions"],
        }
        self._write_spec(repo, metadata=meta, handoff_body=body)

        # Admission passes cleanly.
        adm = self.gate.admit_verdicts(
            cfg,
            repo,
            self.gate.parse_queue(qtext),
            self.gate.parse_task_files(qtext),
            queue_text=qtext,
        )
        self.assertEqual(adm["EAV-2"]["verdict"], "admit", adm)

        # Worker extraction: retains prelude + Implementation + Safety (once, deduplicated!), omits Reviewer Focus & Appendix Notes.
        w_ext = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role="worker")
        self.assertTrue(w_ext["current"])
        self.assertTrue(w_ext["filtered"])
        self.assertEqual(w_ext["mode"], "scoped")
        self.assertEqual(w_ext["omitted_sections"], ["Reviewer Focus", "Appendix Notes"])
        self.assertEqual([it["name"] for it in w_ext["included"]], ["prelude", "Implementation", "Safety & Stop Conditions"])
        self.assertEqual([it["kind"] for it in w_ext["included"]], ["prelude", "selected", "required"])

        # Exact provenance hashes and byte counts match extracted substrings without duplicating bodies.
        expected_blocks = {
            "prelude": prelude,
            "Implementation": sec_impl,
            "Safety & Stop Conditions": sec_safety,
        }
        for item in w_ext["included"]:
            raw_expected = expected_blocks[item["name"]]
            self.assertEqual(item["bytes"], len(raw_expected.encode("utf-8")))
            self.assertEqual(item["sha256"], _sha256_text(raw_expected))
            self.assertIn(f"{item['sha256'][:12]}:{item['bytes']}B", w_ext["text"])

        self.assertEqual(w_ext["selected_bytes"], len(w_ext["body"].encode("utf-8")))
        self.assertEqual(w_ext["selected_sha256"], _sha256_text(w_ext["body"]))
        self.assertLess(w_ext["selected_bytes"], w_ext["full_bytes"])
        self.assertEqual(w_ext["text"].count("### Safety & Stop Conditions"), 1)
        self.assertIn("### Fake Heading Inside Code Fence", w_ext["text"])
        self.assertIn("[citation](docs/work/pkg/evidence.md#L12)", w_ext["text"])
        self.assertIn("`ev_6dfe590e09f1`", w_ext["text"])
        self.assertIn("omitted=[Reviewer Focus, Appendix Notes]", w_ext["text"])
        self.assertNotIn("Verify exact diff against base", w_ext["text"])
        self.assertNotIn("Unselected background prose", w_ext["text"])
        self.assertNotIn("Other task handoff", w_ext["text"])

        # Mandatory packet components survive worker extraction untouched.
        pkt = self.gate.task_packet(repo, cfg, qtext, "EAV-2", ["eav2.txt"])
        self.assertIn("Keep acceptance intact with citation `ev_1234567890ab` and BUG-000000000001.", pkt)
        self.assertIn('FLOW_RESULT: {"version": 1, "task": "EAV-2", "status": "scope_request"', pkt)
        self.assertIn("pins: skill=flow-run@", pkt)
        self.assertIn("(pinned)", pkt)
        self.assertIn("prose still requires judgment", pkt)

        # Review extraction via the SAME extractor: retains prelude + Safety + Reviewer Focus, omits Implementation & Appendix.
        r_ext = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role="review")
        self.assertTrue(r_ext["filtered"])
        self.assertEqual(r_ext["mode"], "scoped")
        self.assertEqual([it["name"] for it in r_ext["included"]], ["prelude", "Safety & Stop Conditions", "Reviewer Focus"])
        self.assertEqual(r_ext["omitted_sections"], ["Implementation", "Appendix Notes"])
        self.assertIn("Verify exact diff against base", r_ext["text"])
        self.assertIn("`BUG-f54b34a0ebac`", r_ext["text"])
        self.assertIn("`ev_6dfe590e09f1`", r_ext["text"])
        self.assertNotIn("Keep fenced example intact.", r_ext["text"])
        self.assertNotIn("Unselected background prose", r_ext["text"])

    def test_role_without_selection_receives_full_handoff(self) -> None:
        repo, base = self._make_repo("role-fallback")
        cfg = self.gate.load_config(repo)
        qtext = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
        body = (
            "Prelude text.\n\n"
            "### Worker Focus\nWorker implementation detail.\n\n"
            "### Shared Invariant\nMandatory safety note.\n\n"
            "### Extra Context\nAdditional context for unselected roles."
        )
        # Only worker has a selection; review is omitted from role_sections.
        meta = {
            "base": base,
            "files": ["eav2.txt"],
            "role_sections": {"worker": ["Worker Focus"]},
            "required_sections": ["Shared Invariant"],
        }
        self._write_spec(repo, metadata=meta, handoff_body=body)

        w_ext = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role="worker")
        r_ext = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role="review")
        self.assertTrue(w_ext["filtered"])
        self.assertEqual(w_ext["omitted_sections"], ["Extra Context"])
        self.assertFalse(r_ext["filtered"])
        self.assertEqual(r_ext["mode"], "full_role")
        self.assertEqual(r_ext["omitted_sections"], [])
        self.assertEqual(r_ext["selected_bytes"], r_ext["full_bytes"])
        self.assertIn("Worker implementation detail.", r_ext["text"])
        self.assertIn("Mandatory safety note.", r_ext["text"])
        self.assertIn("Additional context for unselected roles.", r_ext["text"])

        # required_sections alone without role_sections validates required headings and gives both roles full handoff.
        self._write_spec(
            repo,
            metadata={"base": base, "files": ["eav2.txt"], "required_sections": ["Shared Invariant"]},
            handoff_body=body,
        )
        for role in ("worker", "review"):
            ext = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role=role)
            self.assertEqual(ext["mode"], "full_role")
            self.assertFalse(ext["filtered"])
            self.assertEqual(ext["omitted_sections"], [])
            self.assertIn("Additional context for unselected roles.", ext["text"])

    def test_invalid_duplicate_missing_and_empty_role_schemas_fail_admission_before_dispatch(self) -> None:
        repo, base = self._make_repo("invalid-schemas")
        cfg_path = repo / ".gate" / "config.json"
        cfg_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        # Disable strict_admit to prove invalid opted-in schemas STILL block dispatch in advisory mode!
        cfg_data["strict_admit"] = False
        cfg_data["probe"] = {"enabled": True}
        cfg_path.write_text(json.dumps(cfg_data), encoding="utf-8")

        valid_body = "Prelude.\n\n### Impl\nNon-empty worker body.\n\n### Audit\nNon-empty review body.\n\n### Safety\nNon-empty safety body."
        cases: list[tuple[str, dict | str, str, str]] = [
            (
                "malformed_opted_in_json",
                '<!-- handoff: {"role_sections": {"worker": ["Impl"]} -->',
                valid_body,
                "invalid JSON",
            ),
            (
                "malformed_opted_in_unquoted_key",
                '<!-- handoff: {required_sections: ["Safety"]} -->',
                valid_body,
                "invalid JSON",
            ),
            ("unclosed_comment", '<!-- handoff: {"role_sections": {"worker": ["Impl"]}}', valid_body, "unclosed handoff"),
            (
                "duplicate_comments",
                f'<!-- handoff: {{"base": "{base}", "files": ["eav2.txt"]}} -->\n<!-- handoff: {{"role_sections": {{"worker": ["Impl"]}}}} -->',
                valid_body,
                "duplicate handoff metadata",
            ),
            (
                "opted_in_non_object_json",
                '<!-- handoff: [{"role_sections": {"worker": ["Impl"]}}] -->',
                valid_body,
                "JSON object",
            ),
            (
                "opted_in_unknown_meta_key",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": ["Impl"]}, "extra": "x"},
                valid_body,
                "unknown handoff metadata key",
            ),
            (
                "empty_role_sections_object",
                {"base": base, "files": ["eav2.txt"], "role_sections": {}},
                valid_body,
                "non-empty JSON object",
            ),
            (
                "unknown_role_name",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"planner": ["Impl"]}},
                valid_body,
                "unknown handoff role",
            ),
            (
                "empty_worker_section_list",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": []}},
                valid_body,
                "role_sections.worker must be a non-empty list",
            ),
            (
                "blank_heading_name",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": ["  "]}},
                valid_body,
                "invalid heading entry",
            ),
            (
                "padded_heading_name",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": [" Impl "]}},
                valid_body,
                "invalid heading entry",
            ),
            (
                "duplicate_worker_selection",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": ["Impl", "Impl"]}},
                valid_body,
                "duplicate heading",
            ),
            (
                "duplicate_required_selection",
                {"base": base, "files": ["eav2.txt"], "required_sections": ["Safety", "Safety"]},
                valid_body,
                "duplicate heading",
            ),
            (
                "empty_required_without_roles",
                {"base": base, "files": ["eav2.txt"], "required_sections": []},
                valid_body,
                "required_sections cannot be empty",
            ),
            (
                "non_list_required",
                {"base": base, "files": ["eav2.txt"], "required_sections": "Safety"},
                valid_body,
                "required_sections must be a list",
            ),
            (
                "unsafe_files_traversal",
                {"base": base, "files": ["../secret.txt"], "role_sections": {"worker": ["Impl"]}},
                valid_body,
                "invalid path in handoff files",
            ),
            (
                "missing_worker_heading",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": ["MissingSec"]}},
                valid_body,
                "missing selected ### heading 'MissingSec' for role worker",
            ),
            (
                "missing_review_heading_caught_at_admission",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": ["Impl"], "review": ["MissingReview"]}},
                valid_body,
                "missing selected ### heading 'MissingReview' for role review",
            ),
            (
                "missing_required_heading",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": ["Impl"]}, "required_sections": ["NoSuchSafety"]},
                valid_body,
                "missing required ### heading in handoff: 'NoSuchSafety'",
            ),
            (
                "duplicate_markdown_h3",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": ["Impl"]}},
                "### Impl\nFirst.\n\n### Impl\nDuplicate.",
                "duplicate ### heading in handoff: 'Impl'",
            ),
            (
                "empty_markdown_h3_title",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": ["Impl"]}},
                "###   \nBody.\n\n### Impl\nOk.",
                "empty ### heading in handoff",
            ),
            (
                "empty_selected_body",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": ["Impl"]}},
                "### Impl\n<!-- comment only, no content -->\n\n### Safety\nReal content.",
                "empty selected ### section 'Impl' for role worker",
            ),
            (
                "empty_required_body",
                {"base": base, "files": ["eav2.txt"], "role_sections": {"worker": ["Impl"]}, "required_sections": ["Safety"]},
                "### Impl\nReal content.\n\n### Safety\n   \n",
                "empty required ### section in handoff: 'Safety'",
            ),
        ]

        qtext = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
        cfg = self.gate.load_config(repo)
        for label, meta, body, expected_substr in cases:
            with self.subTest(case=label):
                self._write_spec(repo, metadata=meta, handoff_body=body)
                verdicts = self.gate.admit_verdicts(
                    cfg,
                    repo,
                    self.gate.parse_queue(qtext),
                    self.gate.parse_task_files(qtext),
                    queue_text=qtext,
                )
                self.assertEqual(verdicts["EAV-2"]["verdict"], "refuse", label)
                self.assertTrue(verdicts["EAV-2"]["invalid_handoff"], label)
                self.assertIn(expected_substr, verdicts["EAV-2"]["reason"])

                notes = self.gate.planning_diagnostics(repo, cfg, qtext, ["EAV-2"])
                self.assertTrue(
                    any(n["level"] == "error" and expected_substr in n["message"] for n in notes),
                    notes,
                )

        # Verify CLI `flow admit --plan` exits 2 and `cmd_run` stops before probe/pin/spawn even with strict_admit=False!
        plan_res = run(sys.executable, str(FLOW), "admit", "--plan", "--repo", str(repo))
        self.assertEqual(plan_res.returncode, 2)
        self.assertIn("REFUSE", plan_res.stdout)

        forbidden_spawn = mock.Mock(side_effect=AssertionError("must never spawn or probe on invalid handoff"))
        with (
            mock.patch.dict(os.environ, {"GATE_CONFIG": ""}),
            mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n),
            mock.patch.object(self.gate, "spawn_worker", side_effect=forbidden_spawn),
        ):
            with self.assertRaisesRegex(SystemExit, "3"):
                self.gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=False, force=True))
        forbidden_spawn.assert_not_called()
        self.assertFalse((repo / ".gate" / "pins" / "EAV-2.json").exists())
        report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
        self.assertIn("admit_refused:EAV-2", report)

    def test_stale_fallback_disables_filtering_and_oversized_packet_rejected_without_truncation(self) -> None:
        repo, base = self._make_repo("stale-and-oversized")
        qtext = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")

        short_worker = "### Worker Slice\n" + ("concise worker guidance " * 4).strip()
        large_review = "### Review Deep Context\n" + ("detailed reviewer context " * 80).strip()
        safety_req = "### Safety\nKeep all stop conditions and citations."
        handoff_body = f"Prelude line.\n\n{short_worker}\n\n{safety_req}\n\n{large_review}"
        meta = {
            "base": base,
            "files": ["eav2.txt"],
            "role_sections": {"worker": ["Worker Slice"], "review": ["Review Deep Context"]},
            "required_sections": ["Safety"],
        }
        self._write_spec(repo, metadata=meta, handoff_body=handoff_body)

        # Measure scoped worker prompt vs full stale fallback prompt.
        cfg = self.gate.load_config(repo)
        scoped_prompt = cfg["worker_prompt"].format(skill="flow-run", task="EAV-2") + self.gate.task_packet(
            repo, cfg, qtext, "EAV-2", ["eav2.txt"]
        )
        scoped_bytes = len(scoped_prompt.encode("utf-8"))

        # Cap allows scoped worker packet, rejects unfiltered packet.
        cap = scoped_bytes + 400
        cfg_path = repo / ".gate" / "config.json"
        cfg_json = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg_json["worker_packet_max_bytes"] = cap
        cfg_path.write_text(json.dumps(cfg_json), encoding="utf-8")
        cfg = self.gate.load_config(repo)

        notes_fresh = self.gate.planning_diagnostics(repo, cfg, qtext, ["EAV-2"])
        self.assertFalse(any(n["level"] == "error" for n in notes_fresh), notes_fresh)

        # Drift eav2.txt -> handoff becomes STALE/UNVERIFIED, disabling role filtering and retaining full handoff!
        (repo / "eav2.txt").write_text("drifted source content\n", encoding="utf-8")
        w_stale = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role="worker")
        r_stale = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role="review")
        self.assertFalse(w_stale["current"])
        self.assertFalse(w_stale["filtered"])
        self.assertEqual(w_stale["mode"], "stale_fallback")
        self.assertEqual(r_stale["mode"], "stale_fallback")
        self.assertIn("STALE/UNVERIFIED", w_stale["text"])
        self.assertIn("reread the related source/callers and replan", w_stale["text"])
        self.assertIn("detailed reviewer context", w_stale["text"])
        self.assertIn("Keep all stop conditions and citations.", w_stale["text"])

        # Because stale fallback retains the full handoff without truncation, packet now exceeds worker_packet_max_bytes!
        notes_stale = self.gate.planning_diagnostics(repo, cfg, qtext, ["EAV-2"])
        self.assertTrue(
            any(n["level"] == "error" and "exceeds worker_packet_max_bytes" in n["message"] for n in notes_stale),
            notes_stale,
        )
        forbidden_spawn = mock.Mock(side_effect=AssertionError("oversized packet must never dispatch"))
        with (
            mock.patch.dict(os.environ, {"GATE_CONFIG": ""}),
            mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n),
            mock.patch.object(self.gate, "spawn_worker", side_effect=forbidden_spawn),
        ):
            with self.assertRaisesRegex(SystemExit, "3"):
                self.gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=True))
        forbidden_spawn.assert_not_called()
        report = (repo / ".gate" / "RUN-REPORT.md").read_text(encoding="utf-8")
        self.assertIn("prompt_too_large:EAV-2", report)

    def test_review_inline_integration_and_tools_off_evidence_retention(self) -> None:
        repo, base = self._make_repo("review-inline")
        qpath = repo / "TASK_QUEUE.md"
        # Single-task queue so closing EAV-2 triggers package review.
        q_lines = [ln for ln in qpath.read_text(encoding="utf-8").splitlines(keepends=True) if "EAV-3" not in ln]
        qpath.write_text("".join(q_lines), encoding="utf-8")
        cfg_path = repo / ".gate" / "config.json"
        cfg_json = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg_json["judge"] = {"enabled": True, "chain": ["fable"], "timeout_s": 60}
        cfg_json["serialize_full_acceptance"] = False
        cfg_path.write_text(json.dumps(cfg_json), encoding="utf-8")
        run("git", "-C", str(repo), "add", "TASK_QUEUE.md", ".gate/config.json")
        run("git", "-C", str(repo), "commit", "-qm", "configure package review")
        base_for_handoff = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()

        handoff_body = (
            "Prelude with citation `ev_review_inline_01`.\n\n"
            "### Worker Only\nWorker-specific step that reviewer does not select.\n\n"
            "### Shared Safety\nRequired invariant: preserve `check-commit` and [autonomy](docs/autonomy.md).\n\n"
            "### Review Checklist\nInline review guidance: verify exact boundary and citation `ev_review_inline_01`."
        )
        self._write_spec(
            repo,
            acceptance="Full embedded acceptance for EAV-2 must remain inline.",
            metadata={
                "base": base_for_handoff,
                "files": ["eav2.txt"],
                "role_sections": {"worker": ["Worker Only"], "review": ["Review Checklist"]},
                "required_sections": ["Shared Safety"],
            },
            handoff_body=handoff_body,
        )
        run("git", "-C", str(repo), "add", "docs/work/pkg/spec.md")
        run("git", "-C", str(repo), "commit", "-qm", "add scoped spec")

        # Worker closes EAV-2 cleanly, advancing HEAD and modifying eav2.txt -> during review, eav2.txt changed since base_for_handoff!
        # Also test a related-but-untouched caller file `caller.py` so review handoff stays CURRENT after worker edits eav2.txt!
        (repo / "caller.py").write_text("def call_loader():\n    return True\n", encoding="utf-8")
        run("git", "-C", str(repo), "add", "caller.py")
        run("git", "-C", str(repo), "commit", "-qm", "add caller")
        caller_base = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
        self._write_spec(
            repo,
            acceptance="Full embedded acceptance for EAV-2 must remain inline.",
            metadata={
                "base": caller_base,
                "files": ["caller.py"],
                "role_sections": {"worker": ["Worker Only"], "review": ["Review Checklist"]},
                "required_sections": ["Shared Safety"],
            },
            handoff_body=handoff_body,
        )
        run("git", "-C", str(repo), "add", "docs/work/pkg/spec.md")
        run("git", "-C", str(repo), "commit", "-qm", "anchor handoff to caller.py")

        worker_prompts: list[str] = []
        review_prompts: list[str] = []

        def fake_spawn(repo_arg, cfg, worker, prompt, pf, **kwargs):
            worker_prompts.append(prompt)
            (repo_arg / "eav2.txt").write_text("implemented\n", encoding="utf-8")
            qp = repo_arg / "TASK_QUEUE.md"
            qp.write_text(qp.read_text(encoding="utf-8").replace("`TODO`", "`DONE`"), encoding="utf-8")
            run("git", "-C", str(repo_arg), "add", "eav2.txt", "TASK_QUEUE.md")
            run("git", "-C", str(repo_arg), "commit", "-qm", "complete EAV-2")
            return self.gate.WorkerResult(0, "done"), None

        def fake_judge(repo_arg, cfg, prompt, run_dir, tag, run_id, n_tasks=1):
            review_prompts.append(prompt)
            return self.gate.normalize_judge_verdict(verdict(93, "pass", findings=[])), "fable"

        with (
            mock.patch.dict(os.environ, {"GATE_CONFIG": ""}),
            mock.patch.object(self.gate, "resolve_tool", side_effect=lambda n: n),
            mock.patch.object(self.gate, "spawn_worker", side_effect=fake_spawn),
            mock.patch.object(self.gate, "judge_once", side_effect=fake_judge),
        ):
            self.gate.cmd_run(SimpleNamespace(repo=str(repo), max_tasks=1, strict_admit=True, force=True))

        self.assertEqual(len(worker_prompts), 1)
        self.assertEqual(len(review_prompts), 1)

        # Worker prompt got Worker Only + Shared Safety + Prelude, omitted Review Checklist body.
        self.assertIn("Worker-specific step that reviewer does not select.", worker_prompts[0])
        self.assertIn("Required invariant: preserve `check-commit`", worker_prompts[0])
        self.assertNotIn("Inline review guidance: verify exact boundary", worker_prompts[0])

        # Review prompt got inline Review Checklist + Shared Safety + Prelude + full Acceptance, omitted Worker Only body!
        self.assertIn("Full embedded acceptance for EAV-2 must remain inline.", review_prompts[0])
        self.assertIn("handoffs: ", review_prompts[0])
        self.assertIn("Inline review guidance: verify exact boundary and citation `ev_review_inline_01`.", review_prompts[0])
        self.assertIn("Required invariant: preserve `check-commit`", review_prompts[0])
        self.assertNotIn("Worker-specific step that reviewer does not select.", review_prompts[0])

        # Saved review receipt and .packet.json pass artifact_valid and receipt_valid.
        receipts = [e for e in rpacket.events(repo) if e.get("event") == "review_verdict"]
        self.assertEqual(len(receipts), 1)
        self.assertTrue(rpacket.artifact_valid(receipts[0]))
        self.assertTrue(rpacket.receipt_valid(repo, self.gate.load_config(repo), receipts[0], ["EAV-2"]))
        saved_pkt = json.loads(Path(receipts[0]["packet"]).read_text(encoding="utf-8"))
        self.assertEqual(saved_pkt["handoffs"]["EAV-2"]["mode"], "scoped")
        self.assertEqual(saved_pkt["handoffs"]["EAV-2"]["omitted_sections"], ["Worker Only"])

    def test_offline_il12_and_il13_fixture_replay_preserves_mandatory_components_without_token_claim(self) -> None:
        il12_spec_text = (
            "# Followups\n\n## Acceptance\n"
            "- Scoped grants, claim checks, and policy edge paths fail closed.\n"
            "- Existing offline learner and model policy unit suites pass.\n"
        )
        il13_spec_text = (
            "# Evidence Packet Followup\n\n## Acceptance\n"
            "- Synthesis prompts remain bounded within 24000 bytes.\n"
            "- Assessor citations and bounded Trending coverage stay intact.\n"
        )
        il12_acc = il12_spec_text.split("## Acceptance\n", 1)[1].strip()
        il13_acc = il13_spec_text.split("## Acceptance\n", 1)[1].strip()

        il12_fixture = _build_historical_prompt_fixture(
            worker_header=(
                "Implement IL-12 only using existing owners and its bounded acceptance. "
                "Offline tests only. No nested models, real DB, installed/global changes or push. "
                "Native verify --task IL-12 and hook-enabled commit source plus DONE. Preserve CR-1."
            ),
            queue_row="| 28 | IL-12 | Repair scoped grant, claim and policy edge paths | `TODO` | v6 accepted; four known findings | pending |",
            declared_files=(
                "learner/cli.py, learner/store.py, learner/assessment.py, "
                "learner/tests/test_assessment_and_cli.py, learner/tests/test_budget_and_recovery.py, "
                "learner/tests/test_evidence_quality.py, gate/model_policy.py, gate/gate.py, "
                "gate/test_model_policy.py, REVIEW-NOTES.md"
            ),
            local_check=(
                "/usr/bin/python3 /tmp/flow-runtime/gate/gate.py "
                "verify --task IL-12 --repo /tmp/flow-repo  "
                "(runs: python3 -m unittest discover -s learner/tests -p 'test_*.py' && "
                "python3 -m unittest discover -s gate -p 'test_model_policy.py' && "
                "python3 -m unittest discover -s gate -p 'test_learning.py'; timeout 900s)"
            ),
            task="IL-12",
            spec_rel="docs/work/sample-package/followups.md",
            acceptance_text=il12_acc,
            pin_line=(
                "pins: skill=flow-run@6d987f4de1 inspect=0ac4180734 "
                "model_policy=v1:e7e49423c7 pin=.gate/pins/IL-12.json "
                "skill_pin=.gate/pins/IL-12/skills/flow-run (pinned)"
            ),
        )
        il13_fixture = _build_historical_prompt_fixture(
            worker_header=(
                "Implement IL-13 only, existing bounded packet/source owners. See acceptance "
                "and saved reproducible evidence. No live network/models, real DB, "
                "installed/global changes, nested workers or push. Keep 24000-byte packet "
                "bound, exact citations, dissent, no adoption. Native verify --task IL-13 "
                "and hook-enabled source+DONE commit. IL-12 already complete; do not redo."
            ),
            queue_row="| 29 | IL-13 | Preserve synthesis citations and bounded Trending coverage | `TODO` | cached citation loss; Trending 512KiB overflow | pending |",
            declared_files=(
                "learner/assessment.py, learner/sources.py, "
                "learner/tests/test_evidence_quality.py, learner/tests/test_assessment_and_cli.py, "
                "docs/usage.md"
            ),
            local_check=(
                "/usr/bin/python3 /tmp/flow-runtime/gate/gate.py "
                "verify --task IL-13 --repo /tmp/flow-repo  "
                "(runs: python3 -m unittest discover -s learner/tests -p 'test_*.py' && "
                "python3 -m unittest discover -s gate -p 'test_learning.py'; timeout 900s)"
            ),
            task="IL-13",
            spec_rel="docs/work/sample-package/evidence-packet-followup.md",
            acceptance_text=il13_acc,
            pin_line=(
                "pins: skill=flow-run@6d987f4de1 inspect=0ac4180734 "
                "model_policy=v1:e7e49423c7 pin=.gate/pins/IL-13.json "
                "skill_pin=.gate/pins/IL-13/skills/flow-run (pinned)"
            ),
        )

        # Replay in an isolated temporary repository without touching historical specs or .gate state.
        repo, _ = self._make_repo("replay-il12-13")
        (repo / "docs" / "work" / "sample-package").mkdir(parents=True, exist_ok=True)
        (repo / "docs" / "work" / "sample-package" / "followups.md").write_text(il12_spec_text, encoding="utf-8")
        (repo / "docs" / "work" / "sample-package" / "evidence-packet-followup.md").write_text(
            il13_spec_text, encoding="utf-8"
        )
        (repo / "anchor.txt").write_text("verified source anchor\n", encoding="utf-8")
        run("git", "-C", str(repo), "add", "docs/work/sample-package", "anchor.txt")
        run("git", "-C", str(repo), "commit", "-qm", "seed replay specs")
        anchor_base = run("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()

        il12_files = [
            "learner/cli.py",
            "learner/store.py",
            "learner/assessment.py",
            "learner/tests/test_assessment_and_cli.py",
            "learner/tests/test_budget_and_recovery.py",
            "learner/tests/test_evidence_quality.py",
            "gate/model_policy.py",
            "gate/gate.py",
            "gate/test_model_policy.py",
            "REVIEW-NOTES.md",
        ]
        il13_files = [
            "learner/assessment.py",
            "learner/sources.py",
            "learner/tests/test_evidence_quality.py",
            "learner/tests/test_assessment_and_cli.py",
            "docs/usage.md",
        ]
        replay_queue = (
            "# TASK_QUEUE\n\n"
            "| # | ID | name | status | start baseline | end baseline |\n"
            "|---|---|---|---|---|---|\n"
            "| 28 | IL-12 | Repair scoped grant, claim and policy edge paths | `TODO` | v6 accepted; four known findings | pending |\n"
            f"<!-- task:IL-12 files: {', '.join(il12_files)} -->\n"
            '<!-- task:IL-12 verify: {"cmd":"python3 -m unittest discover -s learner/tests -p \'test_*.py\' && python3 -m unittest discover -s gate -p \'test_model_policy.py\' && python3 -m unittest discover -s gate -p \'test_learning.py\'","timeout_s":900} -->\n'
            "<!-- task:IL-12 impact: shared -->\n"
            "<!-- task:IL-12 spec: docs/work/sample-package/followups.md -->\n"
            "| 29 | IL-13 | Preserve synthesis citations and bounded Trending coverage | `TODO` | cached citation loss; Trending 512KiB overflow | pending |\n"
            f"<!-- task:IL-13 files: {', '.join(il13_files)} -->\n"
            '<!-- task:IL-13 verify: {"cmd":"python3 -m unittest discover -s learner/tests -p \'test_*.py\' && python3 -m unittest discover -s gate -p \'test_learning.py\'","timeout_s":900} -->\n'
            "<!-- task:IL-13 impact: shared -->\n"
            "<!-- task:IL-13 spec: docs/work/sample-package/evidence-packet-followup.md -->\n"
        )
        (repo / "TASK_QUEUE.md").write_text(replay_queue, encoding="utf-8")
        cfg = self.gate.load_config(repo)
        for tid in ("IL-12", "IL-13"):
            flow_skills.pin_task_start(repo, tid, skill_name="flow-run")

        replay_records: dict[str, dict] = {}
        for tid, orig_prompt, spec_file, decl_files in (
            ("IL-12", il12_fixture, repo / "docs/work/sample-package/followups.md", il12_files),
            ("IL-13", il13_fixture, repo / "docs/work/sample-package/evidence-packet-followup.md", il13_files),
        ):
            orig_comp = _parse_packet_components(orig_prompt)
            cand_legacy_pkt = self.gate.task_packet(repo, cfg, replay_queue, tid, decl_files)
            cand_legacy_comp = _parse_packet_components(cand_legacy_pkt)
            self.assertNotIn("handoff", cand_legacy_comp)

            # Stable mandatory components match the historical prompt verbatim.
            for key in ("queue_row", "declared_files", "closing", "scope_protocol", "spec", "acceptance", "disciplines"):
                self.assertEqual(cand_legacy_comp[key], orig_comp[key], f"{tid}:{key}")

            # Now attach an opted-in scoped handoff on the isolated fixture copy and verify mandatory component hashes stay identical!
            orig_spec_body = spec_file.read_text(encoding="utf-8")
            scoped_handoff = (
                f"\n\n## Handoff {tid}\n"
                + "<!-- handoff: "
                + json.dumps(
                    {
                        "base": anchor_base,
                        "files": ["anchor.txt"],
                        "role_sections": {"worker": ["Implementation Focus"], "review": ["Review Boundaries"]},
                        "required_sections": ["Safety & Citations"],
                    }
                )
                + " -->\n"
                + f"Prelude for {tid}: preserve citation `ev_6dfe590e09f1`.\n\n"
                + "### Implementation Focus\nReuse established owners without adding parallel registries.\n\n"
                + "### Safety & Citations\nRetain full Acceptance, pin integrity, and scope-request stop conditions.\n\n"
                + "### Review Boundaries\nVerify exact candidate diff and offline test evidence.\n"
            )
            spec_file.write_text(orig_spec_body + scoped_handoff, encoding="utf-8")

            cand_scoped_pkt = self.gate.task_packet(repo, cfg, replay_queue, tid, decl_files)
            cand_scoped_comp = _parse_packet_components(cand_scoped_pkt)
            w_ext = self.gate.extract_spec_handoff(repo, cfg, replay_queue, tid, role="worker")
            r_ext = self.gate.extract_spec_handoff(repo, cfg, replay_queue, tid, role="review")

            for key in ("queue_row", "declared_files", "local_check", "closing", "scope_protocol", "spec", "acceptance", "pins", "disciplines"):
                self.assertEqual(
                    _sha256_text(cand_scoped_comp[key]),
                    _sha256_text(cand_legacy_comp[key]),
                    f"{tid} mandatory component {key} mutated under scoped handoff",
                )

            replay_records[tid] = {
                "original_prompt_bytes": len(orig_prompt.encode("utf-8")),
                "candidate_legacy_packet_bytes": len(cand_legacy_pkt.encode("utf-8")),
                "candidate_scoped_worker_packet_bytes": len(cand_scoped_pkt.encode("utf-8")),
                "full_handoff_bytes": w_ext["full_bytes"],
                "scoped_worker_handoff_bytes": w_ext["selected_bytes"],
                "scoped_review_handoff_bytes": r_ext["selected_bytes"],
                "mandatory_components_preserved": True,
                "measurement_kind": "packet_bytes_only",
                "token_savings_claimed": False,
            }

        self.assertGreater(replay_records["IL-12"]["original_prompt_bytes"], 0)
        self.assertGreater(replay_records["IL-13"]["original_prompt_bytes"], 0)
        for tid in ("IL-12", "IL-13"):
            rec = replay_records[tid]
            self.assertTrue(rec["mandatory_components_preserved"])
            self.assertFalse(rec["token_savings_claimed"])
            self.assertEqual(rec["measurement_kind"], "packet_bytes_only")
            self.assertLess(rec["scoped_worker_handoff_bytes"], rec["full_handoff_bytes"])
            self.assertLess(rec["scoped_review_handoff_bytes"], rec["full_handoff_bytes"])

    def test_bug_76d4d568a721_legacy_comments_remain_non_opted_in_and_compatible(self) -> None:
        repo, base = self._make_repo("bug-76d4d568a721")
        cfg = self.gate.load_config(repo)
        qtext = (repo / "TASK_QUEUE.md").read_text(encoding="utf-8")
        legacy_body = (
            "Prelude prose mentioning role_sections: [\"Impl\"] and required_sections in body.\n\n"
            "### Worker Steps\nFull legacy worker prose with citation `ev_legacy_01`.\n\n"
            "### Review Notes\nFull legacy review prose."
        )

        # (label, metadata_arg, expected_current, expected_reason_substr)
        legacy_cases: list[tuple[str, dict | str, bool, str]] = [
            (
                "unknown_legacy_key_current",
                {"base": base, "files": ["eav2.txt"], "note": "extra legacy metadata"},
                True,
                "related files unchanged against base",
            ),
            (
                "literal_key_mention_in_legacy_json_value",
                {
                    "base": base,
                    "files": ["eav2.txt"],
                    "note": "mentions role_sections and required_sections: [\"Safety\"] in value",
                },
                True,
                "related files unchanged against base",
            ),
            (
                "duplicate_legacy_comments_first_valid",
                f'<!-- handoff: {{"base": "{base}", "files": ["eav2.txt"]}} -->\n<!-- handoff: {{"note": "second legacy"}} -->',
                True,
                "related files unchanged against base",
            ),
            (
                "malformed_legacy_json",
                "<!-- handoff: {not valid json -->",
                False,
                "missing or invalid base/files metadata",
            ),
            (
                "malformed_legacy_braced_json",
                "<!-- handoff: {not: valid} -->",
                False,
                "missing or invalid base/files metadata",
            ),
            (
                "unclosed_legacy_comment",
                '<!-- handoff: {"base": "unclosed"',
                False,
                "missing or invalid base/files metadata",
            ),
            (
                "unclosed_legacy_before_later_html_comment",
                '<!-- handoff: {"base": "unclosed"\nBody prose.\n<!-- normal html comment -->',
                False,
                "missing or invalid base/files metadata",
            ),
            (
                "non_object_legacy_list",
                "<!-- handoff: [1, 2, 3] -->",
                False,
                "missing or invalid base/files metadata",
            ),
            (
                "non_object_list_with_literal_key_names",
                '<!-- handoff: ["role_sections", "required_sections"] -->',
                False,
                "missing or invalid base/files metadata",
            ),
            (
                "non_object_string_with_literal_key_syntax",
                '<!-- handoff: "role_sections: not an object" -->',
                False,
                "missing or invalid base/files metadata",
            ),
            (
                "malformed_freeform_literal_key_mention",
                "<!-- handoff: legacy note mentioning role_sections and required_sections without key syntax -->",
                False,
                "missing or invalid base/files metadata",
            ),
            (
                "malformed_json_value_literal_key_colon_mention",
                '<!-- handoff: {"note": "role_sections: [Impl]", broken -->',
                False,
                "missing or invalid base/files metadata",
            ),
        ]

        for label, meta, exp_current, exp_reason in legacy_cases:
            with self.subTest(case=label):
                self._write_spec(repo, metadata=meta, handoff_body=legacy_body)
                raw = self.gate._find_handoff_raw(
                    (repo / "docs" / "work" / "pkg" / "spec.md").read_text(encoding="utf-8"),
                    "EAV-2",
                )
                assert raw is not None
                parsed = self.gate.parse_handoff_section(raw)
                self.assertFalse(parsed["opted_in"], label)
                self.assertIsNone(parsed["schema_error"], label)
                self.assertIsNone(self.gate.handoff_schema_error(repo, cfg, qtext, "EAV-2"), label)

                for role in ("worker", "review"):
                    ext = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role=role)
                    self.assertFalse(ext["opted_in"], f"{label}:{role}")
                    self.assertIsNone(ext["schema_error"], f"{label}:{role}")
                    self.assertEqual(ext["mode"], "legacy", f"{label}:{role}")
                    self.assertFalse(ext["filtered"], f"{label}:{role}")
                    self.assertEqual(ext["current"], exp_current, f"{label}:{role}")
                    self.assertEqual(
                        ext["status"],
                        "CURRENT" if exp_current else "STALE/UNVERIFIED",
                        f"{label}:{role}",
                    )
                    self.assertIn(exp_reason, ext["reason"], f"{label}:{role}")
                    self.assertNotIn("handoff_provenance:", ext["text"], f"{label}:{role}")
                    self.assertIn("Full legacy worker prose with citation `ev_legacy_01`.", ext["text"])
                    self.assertIn("Full legacy review prose.", ext["text"])

                verdicts = self.gate.admit_verdicts(
                    cfg,
                    repo,
                    self.gate.parse_queue(qtext),
                    self.gate.parse_task_files(qtext),
                    queue_text=qtext,
                )
                self.assertEqual(verdicts["EAV-2"]["verdict"], "admit", label)
                self.assertFalse(verdicts["EAV-2"]["invalid_handoff"], label)
                notes = self.gate.planning_diagnostics(repo, cfg, qtext, ["EAV-2"])
                self.assertFalse(any(n["level"] == "error" for n in notes), f"{label}:{notes}")

        # Drifting eav2.txt on unknown-key legacy metadata falls back to STALE/UNVERIFIED with reread warning and no refusal.
        self._write_spec(
            repo,
            metadata={"base": base, "files": ["eav2.txt"], "note": "legacy extra key"},
            handoff_body=legacy_body,
        )
        (repo / "eav2.txt").write_text("drifted\n", encoding="utf-8")
        drifted_ext = self.gate.extract_spec_handoff(repo, cfg, qtext, "EAV-2", role="worker")
        self.assertFalse(drifted_ext["opted_in"])
        self.assertIsNone(drifted_ext["schema_error"])
        self.assertEqual(drifted_ext["mode"], "legacy")
        self.assertFalse(drifted_ext["current"])
        self.assertEqual(drifted_ext["status"], "STALE/UNVERIFIED")
        self.assertIn("related files drifted", drifted_ext["reason"])
        self.assertIn("reread the related source/callers and replan", drifted_ext["text"])
        self.assertIn("Full legacy review prose.", drifted_ext["text"])


if __name__ == "__main__":
    unittest.main()

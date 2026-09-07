#!/usr/bin/env python3
"""
audit.py — read-only project-tree auditor for the $flow workflow.

Classifies every file in a project tree into the nine categories from
docs/design.md, with deterministic heuristics only — no model judgement,
no network, and NO WRITE MODE: the one output is a report written OUTSIDE
the audited tree. Anything the heuristics cannot place confidently is
`unknown`, never guessed.

Usage:
  python audit.py <target-dir> [--out report.md] [--json data.json]
                  [--stale-days 90] [--active-days 14] [--oversize-kb 64]

The report path must not be inside the target tree; the script refuses it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# Never descend into these: dependencies, VCS internals, build output, caches.
EXCLUDE_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build",
    ".next", ".nuxt", ".cache", ".turbo", "coverage", ".idea", ".vscode",
    "playwright-report", "test-results",
}

# Category precedence is the list order below: first hit wins.
CANONICAL_NAMES = {
    "README.md", "AGENTS.md", "CLAUDE.md", "LICENSE", "LICENSE.md",
    "CHANGELOG.md", "CONTEXT.md", "TASK_QUEUE.md", "SKILL.md", "INDEX.md",
    "config.json", "package.json", "pyproject.toml", ".gitignore",
    ".gitattributes", "Makefile",
}
RUNTIME_EXT = {".log", ".ndjson", ".pid", ".lock", ".tmp"}
RUNTIME_DIR_PARTS = {"runs", "runtime", "logs", "tmp"}
GENERATED_EXT = {".pyc", ".pyo", ".map", ".min.js", ".min.css"}
STALE_NAME_RE = re.compile(
    r"(?:^|[-_.])(final2?|v\d+|new|latest|old|copy|backup|bak|orig|draft)"
    r"(?:[-_.]|$)",
    re.IGNORECASE,
)
# The middle alternative is the Chinese phrase "superseded by", written as
# escapes so the source stays ASCII.
SUPERSEDED_RE = re.compile("superseded by|\u5df2\u88ab.{0,12}\u53d6\u4ee3|deprecated", re.IGNORECASE)
DOMAIN_DIR_PARTS = {"skills", "capabilities", "adr", "templates", "references"}
DOC_EXT = {".md", ".txt", ".rst"}

HASH_CAP = 5 * 1024 * 1024  # don't content-hash files bigger than this
REF_SCAN_CAP = 512 * 1024   # don't reference-scan docs bigger than this


def scan_tree(root: Path) -> list[Path]:
    out: list[Path] = []
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir())
        except OSError:
            continue
        for e in entries:
            if e.is_dir():
                if e.name not in EXCLUDE_DIRS:
                    stack.append(e)
            elif e.is_file():
                out.append(e)
    return out


def sha256(p: Path) -> str | None:
    try:
        if p.stat().st_size > HASH_CAP:
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def build_references(root: Path, files: list[Path]) -> dict[str, list[str]]:
    """Map rel-path -> list of rel-paths of docs that mention it.

    A mention is the file's basename when that basename is unique in the
    tree, otherwise its full relative path. Advisory-grade by design.
    """
    rels = {str(f.relative_to(root)).replace("\\", "/") for f in files}
    by_base: dict[str, list[str]] = {}
    for r in rels:
        by_base.setdefault(r.rsplit("/", 1)[-1], []).append(r)
    needles = {
        r: (r.rsplit("/", 1)[-1] if len(by_base[r.rsplit("/", 1)[-1]]) == 1 else r)
        for r in rels
    }
    refs: dict[str, list[str]] = {r: [] for r in rels}
    for f in files:
        if f.suffix.lower() not in DOC_EXT:
            continue
        try:
            if f.stat().st_size > REF_SCAN_CAP:
                continue
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        src = str(f.relative_to(root)).replace("\\", "/")
        for r, needle in needles.items():
            if r != src and needle in text:
                refs[r].append(src)
    return refs


def classify(
    root: Path, f: Path, now: float, args, hashes: dict[str, list[str]],
    refs: dict[str, list[str]],
) -> tuple[str, str]:
    rel = str(f.relative_to(root)).replace("\\", "/")
    parts = set(rel.lower().split("/")[:-1])
    name = f.name
    try:
        st = f.stat()
    except OSError:
        return "unknown", "unreadable"
    age_d = (now - st.st_mtime) / 86400

    if name in CANONICAL_NAMES:
        return "canonical", "fixed-role control file"
    if parts & RUNTIME_DIR_PARTS or f.suffix.lower() in RUNTIME_EXT:
        return "runtime", "run-scoped data (dir or extension)"
    if f.suffix.lower() in GENERATED_EXT or name.endswith((".bak",)):
        return "generated", "rebuildable build/backup artefact"
    # State words only condemn documents. Code names follow framework
    # conventions (Next.js `new/` routes, schema-v2 modules) and are out of
    # scope for renaming per flow-design.md §7.1.
    stem_hit = STALE_NAME_RE.search(f.stem)
    if stem_hit and f.suffix.lower() in DOC_EXT:
        return "stale", f"state word in name: '{stem_hit.group(1)}'"
    if f.suffix.lower() in DOC_EXT:
        try:
            head = f.read_text(encoding="utf-8", errors="replace")[:2048]
            m = SUPERSEDED_RE.search(head)
            if m:
                return "stale", f"header says '{m.group(0)}'"
        except OSError:
            pass
    h = sha256(f)
    if h and len(hashes.get(h, [])) > 1 and st.st_size > 0:
        twins = [r for r in hashes[h] if r != rel]
        return "duplicate", f"byte-identical to {', '.join(twins[:3])}"
    if parts & DOMAIN_DIR_PARTS:
        return "domain", "lives in a declared domain directory"
    if age_d <= args.active_days or refs.get(rel):
        why = (
            f"modified {age_d:.0f}d ago"
            if age_d <= args.active_days
            else f"referenced by {len(refs[rel])} doc(s)"
        )
        return "active", why
    if age_d > args.stale_days and f.suffix.lower() in DOC_EXT:
        return "historical", f"doc untouched for {age_d:.0f}d, unreferenced"
    return "unknown", "no heuristic matched confidently"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("target")
    ap.add_argument("--out", default=None, help="report path (default: cwd)")
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--stale-days", type=int, default=90)
    ap.add_argument("--active-days", type=int, default=14)
    ap.add_argument("--oversize-kb", type=int, default=64)
    args = ap.parse_args()

    root = Path(args.target).resolve()
    if not root.is_dir():
        sys.exit(f"not a directory: {root}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out or f"flow-audit-{root.name}-{stamp}.md").resolve()
    jout = Path(args.json_out).resolve() if args.json_out else None
    for p in (out, jout):
        if p and root in p.parents:
            sys.exit(f"refusing to write inside the audited tree: {p}")

    now = time.time()
    files = scan_tree(root)
    hashes: dict[str, list[str]] = {}
    for f in files:
        h = sha256(f)
        if h:
            hashes.setdefault(h, []).append(
                str(f.relative_to(root)).replace("\\", "/")
            )
    refs = build_references(root, files)

    rows = []
    for f in files:
        cat, why = classify(root, f, now, args, hashes, refs)
        rel = str(f.relative_to(root)).replace("\\", "/")
        st = f.stat()
        rows.append({
            "path": rel,
            "category": cat,
            "reason": why,
            "size": st.st_size,
            "age_days": round((now - st.st_mtime) / 86400, 1),
            "referenced_by": refs.get(rel, []),
        })

    oversize = [
        r for r in rows
        if r["path"].endswith(".md") and r["size"] > args.oversize_kb * 1024
        and r["category"] in ("active", "canonical", "unknown")
    ]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["category"]] = counts.get(r["category"], 0) + 1

    order = ["canonical", "domain", "active", "duplicate", "stale",
             "historical", "generated", "runtime", "unknown"]
    lines = [
        f"# flow audit — `{root}`",
        "",
        f"Scanned {len(rows)} files on {time.strftime('%Y-%m-%d %H:%M')}. "
        "Read-only: this report proposes, it never moves or deletes.",
        "",
        "| category | files |",
        "|---|---|",
    ]
    lines += [f"| {c} | {counts.get(c, 0)} |" for c in order]
    if oversize:
        lines += [
            "",
            f"## Oversize active documents (> {args.oversize_kb} KB)",
            "",
            "The unbounded-append disease. Split, summarise, or close:",
            "",
        ]
        lines += [
            f"- `{r['path']}` — {r['size'] // 1024} KB, {r['age_days']}d old"
            for r in sorted(oversize, key=lambda r: -r["size"])
        ]
    MAX_ROWS = 100
    for cat in ("duplicate", "stale", "historical", "unknown"):
        sel = [r for r in rows if r["category"] == cat]
        if not sel:
            continue
        lines += ["", f"## {cat} ({len(sel)})", "",
                  "| path | reason | age (d) | referenced by |", "|---|---|---|---|"]
        for r in sorted(sel, key=lambda r: r["path"])[:MAX_ROWS]:
            by = ", ".join(r["referenced_by"][:3]) or "—"
            lines.append(
                f"| `{r['path']}` | {r['reason']} | {r['age_days']} | {by} |"
            )
        if len(sel) > MAX_ROWS:
            lines.append(
                f"| … | +{len(sel) - MAX_ROWS} more rows — full list via --json | | |"
            )
    lines += [
        "",
        "## Suggested next step",
        "",
        "Human-review the four tables above. Nothing here authorises a move "
        "or delete; per flow-design.md §6 a write mode does not exist in v1.",
        "",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    if jout:
        jout.write_text(
            json.dumps({"root": str(root), "counts": counts, "files": rows},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    print(f"audited {len(rows)} files: " +
          ", ".join(f"{c}={counts.get(c, 0)}" for c in order if counts.get(c)))
    print(f"report: {out}")
    if jout:
        print(f"json  : {jout}")


if __name__ == "__main__":
    main()

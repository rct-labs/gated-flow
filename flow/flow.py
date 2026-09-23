#!/usr/bin/env python3
"""
flow.py — the one command for the $flow/$run workflow (docs/design.md).

Thin dispatcher; the machinery lives elsewhere and is not duplicated here:
  init          scaffold a project (create-if-missing, never overwrites)
  home          print the checkout directory (gate scripts live in <home>/gate)
  audit         read-only tree audit                    -> flow/audit.py
  admit | doctor | run | verify | usage | metrics | install-hook | check-commit | audit-gate
                passthrough                             -> gate/gate.py

Usage:
  flow home
  flow init  [--repo DIR] [--verify-cmd CMD]
  flow audit [DIR] [audit.py options]
  flow admit --plan [--repo DIR]
  flow metrics [--repo DIR] [--last N] [--json]
  flow admit|doctor|run|verify|usage|install-hook [gate.py options]
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATE = HERE.parent / "gate" / "gate.py"
AUDIT = HERE / "audit.py"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

QUEUE_SKELETON = """# TASK_QUEUE

| # | ID | name | status | start baseline | end baseline |
|---|---|---|---|---|---|

<!-- Add tasks as table rows. The status only counts when wrapped in
backticks, so this template row is inert until you add them:

| 1 | WP-1 | short verifiable goal | TODO | 10 passed | 12 passed |

Real row: change TODO to `TODO` (backticks). Every task also needs a scope
line directly below the table so admission can judge it, shaped like
"task:WP-1 files: src/foo.py, tests/test_foo.py" inside an HTML comment
of its own. -->
"""

# Inlined so init works standalone; flow/templates/CONTEXT.md is the
# canonical template — change that first, mirror here.
CONTEXT_SKELETON = """# CONTEXT — {name}

> Rebuilt {today}. Keep under 200 lines. Rebuild, never append.

## 1. Goal & red lines

## 2. Now

## 3. Last stable checkpoint

## 4. Next step

## 5. Waiting on a human
"""


def die(msg: str) -> None:
    print(f"flow: {msg}", file=sys.stderr)
    sys.exit(1)


def run_py(script: Path, argv: list[str]) -> int:
    return subprocess.call([sys.executable, str(script), *argv])


def cmd_init(argv: list[str]) -> int:
    import argparse
    import json
    import time

    ap = argparse.ArgumentParser(prog="flow init")
    ap.add_argument("--repo", default=".")
    ap.add_argument(
        "--verify-cmd",
        default="",
        help="the acceptance command. Left empty, admit refuses every task "
        "with no-oracle until you set it — loud beats a guessed lie.",
    )
    ap.add_argument(
        "--queue-file",
        default="TASK_QUEUE.md",
        help="project-relative queue path (default: TASK_QUEUE.md)",
    )
    ap.add_argument(
        "--context-file",
        default="CONTEXT.md",
        help="project-relative human-context path (default: CONTEXT.md)",
    )
    ap.add_argument(
        "--external-context",
        action="store_true",
        help="the project owns context_file; verify it exists but never scaffold it",
    )
    ap.add_argument(
        "--handoff-glob",
        action="append",
        default=[],
        help="project-relative handoff glob owned outside flow; repeatable",
    )
    ap.add_argument(
        "--hook-mode",
        choices=("install", "preserve"),
        default="install",
        help="install a direct hook, or preserve an existing hook for explicit chaining",
    )
    args = ap.parse_args(argv)
    proj = Path(args.repo).resolve()
    if not proj.is_dir():
        die(f"not a directory: {proj}")
    inside_git = (
        subprocess.run(
            ["git", "-C", str(proj), "rev-parse", "--git-dir"],
            capture_output=True,
        ).returncode
        == 0
    )
    if not inside_git:
        die(
            f"{proj} is not inside a git repository. The gate is a pre-commit "
            "hook; it needs git. Run `git init` there first if this is "
            "intentional."
        )

    def project_rel_path(raw: str, label: str) -> Path:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts:
            die(f"{label} must be a project-relative path without '..': {raw}")
        return path

    queue_rel = project_rel_path(args.queue_file, "--queue-file")
    context_rel = project_rel_path(args.context_file, "--context-file")
    handoff_globs = [
        str(project_rel_path(raw, "--handoff-glob")).replace("\\", "/")
        for raw in args.handoff_glob
    ]

    made, kept = [], []

    def scaffold(path: Path, content: str) -> None:
        if path.exists():
            kept.append(str(path.relative_to(proj)).replace("\\", "/"))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            made.append(str(path.relative_to(proj)).replace("\\", "/"))

    scaffold(proj / queue_rel, QUEUE_SKELETON)
    context_path = proj / context_rel
    if args.external_context:
        if not context_path.exists():
            die(f"external context file does not exist: {context_path}")
        kept.append(str(context_rel).replace("\\", "/") + " (external)")
    else:
        scaffold(
            context_path,
            CONTEXT_SKELETON.format(name=proj.name, today=time.strftime("%Y-%m-%d")),
        )

    # gate init refuses to overwrite an existing config on its own.
    root = subprocess.run(
        ["git", "-C", str(proj), "rev-parse", "--show-toplevel"],
        capture_output=True, text=True,
    ).stdout.strip()
    cfg_path = Path(root) / ".gate" / "config.json"
    if cfg_path.exists():
        kept.append(".gate/config.json")
    else:
        rc = run_py(GATE, ["init", "--repo", str(proj)])
        if rc != 0:
            return rc
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["verify_cmd"] = args.verify_cmd
        cfg["delivery"]["stage"] = "development"
        cfg["queue_file"] = str(queue_rel).replace("\\", "/")
        cfg["context_file"] = str(context_rel).replace("\\", "/")
        cfg["external_context"] = bool(args.external_context)
        cfg["handoff_globs"] = handoff_globs
        if handoff_globs:
            cfg["doc_only_globs"] = list(
                dict.fromkeys([*cfg.get("doc_only_globs", []), *handoff_globs])
            )
        if root != str(proj).replace("\\", "/"):
            cfg["project_prefix"] = (
                str(proj.relative_to(root)).replace("\\", "/")
            )
        cfg_path.write_text(
            json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        made.append(".gate/config.json")

    if args.hook_mode == "install":
        rc = run_py(GATE, ["install-hook", "--repo", str(proj)])
        if rc != 0:
            return rc

    print(f"flow init — {proj}")
    if made:
        print("  created : " + ", ".join(made))
    if kept:
        print("  kept    : " + ", ".join(kept) + "  (never overwritten)")
    print(
        "  hook    : pre-commit installed"
        if args.hook_mode == "install"
        else "  hook    : preserved (chain gate explicitly, then run flow doctor)"
    )
    if not args.verify_cmd and ".gate/config.json" in made:
        print(
            "\n  NEXT (required): set verify_cmd in .gate/config.json — the "
            "command that\n  exits non-zero when the project is broken. Until "
            "then, admit refuses\n  every task with no-oracle, by design."
        )
    print("  Then: flow doctor --repo . ; flow admit --repo .")
    return 0


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__.strip())
        print(
            f"\nManual: {HERE.parent / 'docs' / 'usage.md'}"
            "\nZero-memory path: tell your agent what you want"
            " ($flow how do I ... / $flow tidy the project / $run)."
        )
        sys.exit(0)
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "home":
        print(HERE.parent)
        sys.exit(0)
    if cmd == "init":
        sys.exit(cmd_init(rest))
    if cmd == "audit":
        if not AUDIT.exists():
            die(f"missing {AUDIT}")
        sys.exit(run_py(AUDIT, rest or ["."]))
    if cmd in ("admit", "doctor", "run", "verify", "usage", "metrics", "install-hook",
               "check-commit"):
        sys.exit(run_py(GATE, [cmd, *rest]))
    if cmd == "audit-gate":  # gate.py's history audit, distinct from tree audit
        sys.exit(run_py(GATE, ["audit", *rest]))
    die(f"unknown subcommand: {cmd}\n\n{__doc__.strip()}")


if __name__ == "__main__":
    main()

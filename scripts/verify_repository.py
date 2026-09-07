"""Repository integrity check for the public release; runs offline.

Checks:
  - every file present is listed in publish-manifest.json, and every listed
    file exists (the explicit publish whitelist);
  - English-only content and filenames (no Han characters);
  - no absolute local paths (drive letters, user-profile directories);
  - optional extra forbidden patterns from a file that is NOT published
    (--forbidden FILE or RELEASE_FORBIDDEN_FILE), one regex per line;
  - LF line endings in text files; a shebang in shell scripts;
  - JSON parses, Python parses, Markdown fences close, local links resolve;
  - skills/*/SKILL.md frontmatter has a matching name and a description;
  - PowerShell scripts parse when pwsh is available.

Usage: python scripts/verify_repository.py [--forbidden FILE] [--json]
"""
from pathlib import Path
import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "publish-manifest.json"
EXCLUDED = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
TEXT_SUFFIXES = {".md", ".json", ".py", ".sh", ".ps1", ".svg", ".txt", ".yml", ".yaml", ".toml"}
TEXT_NAMES = {"LICENSE", ".gitignore", ".gitattributes"}
HAN = re.compile(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002fa1f]")
# Machine-specific paths never belong in a reusable repository.
LOCAL_PATH_PATTERNS = [
    (re.compile(r"\b[A-Za-z]:[\\/]"), "absolute drive path"),
    (re.compile(r"/(?:c|d|e)/Users/", re.IGNORECASE), "MSYS user-profile path"),
    (re.compile(r"/home/[a-z][a-z0-9_-]*/"), "home-directory path"),
    (re.compile(r"/Users/[A-Za-z][A-Za-z0-9_-]*/"), "macOS user path"),
]
# Lines that legitimately show a drive letter as a placeholder or in a regex.
LOCAL_PATH_ALLOW = re.compile(r"<[A-Za-z]:|[A-Za-z]:\\\\|\[A-Za-z\]:|\[A-Z\]:|X:\\|`[A-Z]:\\`|drive letter")


def is_text(path: Path) -> bool:
    return path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES


def load_forbidden(explicit: str | None) -> list[tuple[re.Pattern, str]]:
    source = explicit or os.environ.get("RELEASE_FORBIDDEN_FILE")
    if not source:
        return []
    patterns = []
    for raw in Path(source).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append((re.compile(line, re.IGNORECASE), f"forbidden pattern {line!r}"))
    return patterns


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--forbidden", help="file of extra forbidden regexes (kept outside the repository)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON")
    args = ap.parse_args()
    errors: list[str] = []
    forbidden = load_forbidden(args.forbidden)

    files = sorted(
        p for p in ROOT.rglob("*")
        if p.is_file() and not EXCLUDED.intersection(p.relative_to(ROOT).parts)
    )
    rels = {p.relative_to(ROOT).as_posix() for p in files}

    # ---- explicit publish whitelist --------------------------------------
    if not MANIFEST.exists():
        errors.append("publish-manifest.json is missing")
        listed: set[str] = set()
    else:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        listed = set(manifest.get("files", []))
        for rel in sorted(rels - listed):
            errors.append(f"{rel}: present but not in publish-manifest.json")
        for rel in sorted(listed - rels):
            errors.append(f"{rel}: listed in publish-manifest.json but missing")

    # ---- per-file content checks -----------------------------------------
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        if HAN.search(rel):
            errors.append(f"{rel}: non-English filename")
        if not is_text(path):
            continue
        raw = path.read_bytes()
        if b"\r\n" in raw:
            errors.append(f"{rel}: CRLF line endings (use LF)")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            errors.append(f"{rel}: expected UTF-8: {exc}")
            continue
        if text.startswith("\ufeff"):
            errors.append(f"{rel}: UTF-8 BOM")
        for index, line in enumerate(text.splitlines(), 1):
            if HAN.search(line):
                errors.append(f"{rel}:{index}: untranslated Chinese text")
            if not LOCAL_PATH_ALLOW.search(line):
                for pattern, label in LOCAL_PATH_PATTERNS:
                    if pattern.search(line):
                        errors.append(f"{rel}:{index}: {label}")
                        break
            for pattern, label in forbidden:
                if pattern.search(line):
                    errors.append(f"{rel}:{index}: {label}")
        if path.suffix == ".sh" and not text.startswith("#!"):
            errors.append(f"{rel}: shell script without a shebang")
        if path.suffix == ".json":
            try:
                json.loads(text)
            except ValueError as exc:
                errors.append(f"{rel}: invalid JSON: {exc}")
        if path.suffix == ".py":
            try:
                ast.parse(text)
            except SyntaxError as exc:
                errors.append(f"{rel}: invalid Python: {exc}")
        if path.suffix == ".md":
            if len(re.findall(r"^\s*```", text, re.MULTILINE)) % 2:
                errors.append(f"{rel}: unclosed fenced block")
            for target in re.findall(r"\]\(([^)]+)\)", text):
                if "://" in target or target.startswith(("#", "mailto:")):
                    continue
                target = target.split("#", 1)[0]
                if not target or "<" in target:
                    continue
                if not (path.parent / target).exists():
                    errors.append(f"{rel}: missing link target {target}")

    # ---- skill entry points ------------------------------------------------
    skills = sorted(ROOT.glob("skills/*/SKILL.md"))
    if not skills:
        errors.append("no skills discovered under skills/")
    for path in skills:
        text = path.read_text(encoding="utf-8")
        match = re.match(r"^---\n(.*?)\n---(?:\n|$)", text, re.DOTALL)
        if not match:
            errors.append(f"{path.parent.name}: missing frontmatter")
            continue
        header = match.group(1)
        if not re.search(r"^name:\s*" + re.escape(path.parent.name) + r"\s*$", header, re.MULTILINE):
            errors.append(f"{path.parent.name}: frontmatter name does not match directory")
        if not re.search(r"^description:\s*\S", header, re.MULTILINE):
            errors.append(f"{path.parent.name}: missing description")

    # ---- PowerShell syntax (when pwsh exists) --------------------------------
    ps_files = [p for p in files if p.suffix == ".ps1"]
    pwsh = shutil.which("pwsh")
    pwsh_checked = False
    if ps_files and pwsh:
        script = (
            "$bad = 0; foreach ($f in $args) { $errs = $null; "
            "[void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$null, [ref]$errs); "
            "if ($errs) { $bad++; $errs | ForEach-Object { Write-Output (\"$f: \" + $_.Message) } } }; exit $bad"
        )
        proc = subprocess.run([pwsh, "-NoProfile", "-Command", script, *map(str, ps_files)],
                              capture_output=True, text=True)
        pwsh_checked = True
        if proc.returncode != 0:
            errors.extend(f"PowerShell parse error: {line}" for line in proc.stdout.splitlines() if line.strip())

    report = {
        "files_checked": len(files),
        "skills": [p.parent.name for p in skills],
        "forbidden_patterns": len(forbidden),
        "powershell_parsed": pwsh_checked,
        "errors": errors,
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"files checked: {report['files_checked']}; skills: {', '.join(report['skills'])}; "
              f"extra forbidden patterns: {len(forbidden)}; pwsh parsed: {pwsh_checked}")
        for err in errors:
            print(f"  ERROR {err}")
        print("OK" if not errors else f"{len(errors)} error(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())

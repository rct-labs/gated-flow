#!/bin/sh
# gate selftest — builds a throwaway repo that reproduces the fake-completion shape
# and proves the gate blocks what it claims to block.
#
#   sh selftest.sh [workdir]
#
# Exits 0 only if all cases behave as documented in README.md.

set -e
GATE="$(cd "$(dirname "$0")" && pwd)/gate.py"
S="${1:-${TMPDIR:-/tmp}/gate-selftest}"
PY="${PYTHON:-python}"

rm -rf "$S"; mkdir -p "$S/proj/src" "$S/proj/tests"; cd "$S"
git init -q .
git config user.email selftest@local
git config user.name selftest

cat > proj/TASK_QUEUE.md <<'EOF'
# TASK_QUEUE

| # | ID | name | status | start baseline | end baseline |
|---|---|---|---|---|---|
| 1 | WP-1 | first package | `TODO` | 10 passed | 12 passed |
| 2 | WP-2 | second package | `TODO` | 12 passed | 15 passed |
EOF
echo "print('hi')" > proj/src/app.py
echo "unrelated" > other-project.txt
git add -A && git commit -qm init

"$PY" "$GATE" init --repo "$S/proj" >/dev/null
"$PY" - "$S/.gate/config.json" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p, encoding="utf-8"))
c["project_prefix"] = "proj"
c["verify_cmd"] = "echo 12 passed"
c["frozen_globs"] = ["tests/*"]
json.dump(c, open(p, "w", encoding="utf-8"), indent=2)
PY
"$PY" "$GATE" install-hook --repo "$S/proj" >/dev/null

pass=0; fail=0
expect_block() { # name, then the commit is attempted by the caller
  if git commit -qm "$2" >/dev/null 2>&1; then
    echo "FAIL  $1 — commit was allowed"; fail=$((fail+1)); git reset -q --soft HEAD~1
  else
    echo "ok    $1 — blocked"; pass=$((pass+1))
  fi
  git reset -q
}
expect_allow() {
  if git commit -qm "$2" >/dev/null 2>&1; then
    echo "ok    $1 — allowed"; pass=$((pass+1))
  else
    echo "FAIL  $1 — was blocked"; fail=$((fail+1)); git reset -q
  fi
}

sed -i 's/| 1 | WP-1 | first package | `TODO`/| 1 | WP-1 | first package | `DONE`/' proj/TASK_QUEUE.md

git add proj/TASK_QUEUE.md
expect_block "1 fake completion (queue-only DONE flip)" "WP-1 done"

echo changed >> other-project.txt
git add proj/TASK_QUEUE.md other-project.txt
expect_block "2 scope violation (path outside the project)" "WP-1 done"
git checkout -q -- other-project.txt

echo "def login(): pass" >> proj/src/app.py
git add proj/TASK_QUEUE.md proj/src/app.py
expect_block "3 code present but no verdict" "WP-1 done"

"$PY" "$GATE" verify --repo proj --cmd "echo 99 passed" >/dev/null 2>&1
git add proj/TASK_QUEUE.md proj/src/app.py
expect_block "4 verdict count disagrees with the queue" "WP-1 done"

"$PY" "$GATE" verify --repo proj --cmd "echo 12 passed" >/dev/null 2>&1
git add proj/TASK_QUEUE.md proj/src/app.py
expect_allow "5 honest completion (code + matching verdict)" "feat: WP-1 login"

echo more >> other-project.txt
git add other-project.txt
expect_allow "6 unrelated commit in another project" "chore(other): unrelated"

sed -i 's/| 2 | WP-2 | second package | `TODO`/| 2 | WP-2 | second package | `DONE`/' proj/TASK_QUEUE.md
echo "assert True" > proj/tests/test_x.py
echo "x=2" >> proj/src/app.py
"$PY" "$GATE" verify --repo proj --cmd "echo 15 passed" >/dev/null 2>&1
git add proj/TASK_QUEUE.md proj/tests/test_x.py proj/src/app.py
expect_block "7 frozen oracle edited in a DONE commit" "WP-2 done"

rm -f proj/tests/test_x.py
git checkout -q -- proj/src/app.py
git add proj/TASK_QUEUE.md
git commit -q --no-verify -m "bot(WP-2): done"
if "$PY" "$GATE" audit --repo proj --last 10 2>&1 | grep -q FAKE_COMPLETION; then
  echo "ok    8 --no-verify bypass is caught by audit"; pass=$((pass+1))
else
  echo "FAIL  8 audit missed a bypassed fake completion"; fail=$((fail+1))
fi

# ---- admission (cases 9-10) ------------------------------------------------
# WP-5 has no scope declaration, WP-3 fits the sweet spot, WP-4 is too broad.
cat >> proj/TASK_QUEUE.md <<'EOF'
| 3 | WP-5 | undeclared scope task | `TODO` | 15 passed | 16 passed |
| 4 | WP-3 | small isolated task | `TODO` | 16 passed | 17 passed |
| 5 | WP-4 | broad refactor task | `TODO` | 17 passed | 18 passed |

<!-- task:WP-3 files: src/app.py, tests/test_a.py -->
<!-- task:WP-4 files: a.py, b.py, c.py, d.py, e.py, f.py, g.py, h.py -->
EOF

ADMIT="$("$PY" "$GATE" admit --repo proj 2>&1)"
if echo "$ADMIT" | grep -q "ok  *WP-3" \
  && echo "$ADMIT" | grep -q "REFUSE  *WP-4" \
  && echo "$ADMIT" | grep -q "UNDECLARED  *WP-5"; then
  echo "ok    9 admit report: pass / refuse / undeclared three-state"; pass=$((pass+1))
else
  echo "FAIL  9 admit report wrong:"; echo "$ADMIT"; fail=$((fail+1))
fi

# Strict run must stop on the refused head task (WP-5) before spawning any
# worker. Workers are pinned to "python" so the case never depends on which
# AI CLIs this machine has installed — no worker is ever spawned anyway.
"$PY" - "$S/.gate/config.json" <<'PY'
import json, sys
p = sys.argv[1]
c = json.load(open(p, encoding="utf-8"))
c["workers"] = ["python"]
json.dump(c, open(p, "w", encoding="utf-8"), indent=2)
PY
RUN="$("$PY" "$GATE" run --repo proj --strict-admit --max-tasks 1 2>&1 || true)"
if echo "$RUN" | grep -q "refused admission" \
  && grep -q '"admit_refused"' "$S/.gate/journal.ndjson"; then
  echo "ok    10 strict admission stops the run on an undeclared head task"; pass=$((pass+1))
else
  echo "FAIL  10 strict admission did not stop the run:"; echo "$RUN"; fail=$((fail+1))
fi

# ---- task-local verify (case 11) -------------------------------------------
# WP-3 admits its own check; a task-scoped verdict closes exactly that row.
cat >> proj/TASK_QUEUE.md <<'EOF'
<!-- task:WP-3 verify: {"cmd": "echo 3 passed", "timeout_s": 60} -->
EOF
git add proj/TASK_QUEUE.md
git commit -q --no-verify -m "declare WP-3 local check"
echo "def wp3(): pass" >> proj/src/app.py
"$PY" "$GATE" verify --repo proj --task WP-3 >/dev/null 2>&1
sed -i 's/| 4 | WP-3 | small isolated task | `TODO`/| 4 | WP-3 | small isolated task | `DONE`/' proj/TASK_QUEUE.md
git add proj/TASK_QUEUE.md proj/src/app.py
expect_allow "11 task-local verdict closes its own task" "feat: WP-3"

echo
echo "$pass passed, $fail failed   (workdir: $S)"
[ "$fail" -eq 0 ]

# Already-admitted queue preflight

## 1. Find the runner and preflight

Use the host's authorized scope, risk, stage and cumulative limits from
[shared workflow rules](../../../docs/autonomy.md). Reuse unchanged preflight.

`flow home` prints the checkout; `gate.py` is `<home>/gate/gate.py`.

```
flow doctor --repo <project>
flow admit --plan --repo <project>
```

Reuse flow-run's preflight if queue, spec and configuration are unchanged;
do not repeat it just because this skill was loaded. Show actionable results.
Stop and report if the pre-commit hook is not installed, the
queue parses to zero tasks, or the head task is BLOCKED. If the head task is
REFUSE or UNDECLARED, say so before launching: advisory mode proceeds with a
warning, `strict_admit` stops. Offer to add the missing scope line:

```
<!-- task:WP-7 files: src/foo.py, tests/test_foo.py -->
```

## 2. Confirm once

Use the task count already authorized or supplied by flow-run; otherwise
the configured default. Do not ask again for decisions already settled.

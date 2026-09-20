# CONTEXT — <project>

> Rebuilt <YYYY-MM-DD>. ≤200 lines. Rebuild, never append.

## 1. Goal & red lines
<!-- What this project is for; constraints that must never be violated
     (e.g. "work directly on main, no worktrees"); decisions the user made
     that the next session must not reopen. -->

## 2. Now
<!-- The active work package / task id and one sentence of state.
     If work is interrupted, say so; it is not done until the gate says so.
     Keep: the failing command exactly as run and its key error line, the
     uncommitted paths (`git status --short`) or "tree clean", and where the
     evidence is (log, run report, test file). A failed attempt stays here
     until it is resolved. Point at durable files; do not paste logs. -->

## 3. Last stable checkpoint
<!-- Commit sha + what was verified green there, and the command that
     verified it. -->

## 4. Next step
<!-- The single next action, concrete enough to start cold, without
     replaying the conversation. -->

## 5. Waiting on a human
<!-- Decisions only a person can make. Empty section = nothing blocked. -->

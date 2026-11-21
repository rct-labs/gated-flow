# Detach an authorized queue run

This procedure never installs a scheduler, enables lingering, creates a recurring
schedule or starts business work as a side effect. Execute it only for the
already-authorized admitted queue. Initialize the [journal cursor](watch.md)
before launch. Resolve absolute repository, flow-home and interpreter paths;
carry the existing check/worker environment, including its required PATH.
Check for a live runner/IN_PROGRESS owner before starting one service.

## Linux

When `systemd-run` exists and `systemctl --user show-environment` can reach the
user manager, use a real user-owned transient **service**, not a transient scope,
a tool-shell background job or sudo. For example, substitute resolved paths and
an unused unit name:

```bash
systemd-run --user --unit=flow-<slug> --property=Type=exec \
  --working-directory=/absolute/project --setenv="PATH=$PATH" \
  /absolute/python /absolute/flow/gate/gate.py run \
  --repo /absolute/project --max-tasks <N>
```

Record the unit name in existing host state. Check `systemctl --user status
flow-<slug>.service` and `journalctl --user -u flow-<slug>.service` on launch
failure; gate task evidence remains in `.gate/`. User-manager lifetime depends
on the host's existing login/linger setup: inspect it and disclose logout limits;
never enable lingering as an implicit workflow step. Do not claim survival
beyond the user manager. Never start a second writer after a failed launch
without checking service and journal ownership.

## Windows

Use the existing Task Scheduler launcher, which owns the process and wraps
`supervise.ps1`. Preserve its gate stop/restart contract:

```powershell
pwsh -NoProfile -File "$(flow home)/gate/launch-detached.ps1" `
  -Repo <project> -MaxTasks <N> -TaskName gate-<slug>
```

## No supported detached owner

Do not install systemd, PowerShell or a scheduler. Report the lifetime limitation
and use an attached `python <flow-home>/gate/gate.py run --repo <project>
--max-tasks <N>` only while this host remains responsible for it and existing
authorization permits. An attached run cannot promise unattended survival.
No automatic restart after a gate stop or budget exhaustion.

## Durable batch supervisor

For a newly authorized batch with a known exact origin session, default to the
adapter in the Linux transient service above. Explicitly opt out when local
session metadata or a durable host owner is unavailable:

```bash
systemd-run --user --unit=flow-<unused-slug> --property=Type=exec \
  --working-directory=/absolute/project --setenv="PATH=$PATH" \
  --setenv="CODEX_THREAD_ID=$CODEX_THREAD_ID" \
  /absolute/python /absolute/flow/flow/flow.py supervise launch \
  --repo /absolute/project --session <exact-origin-session> \
  --package docs/work/<package>/spec.md --tasks <A,B> \
  --max-calls <authorized-cumulative-cap> --seconds <authorized-deadline-seconds> \
  --transport codex --watch
```

The actual adapter binds before launching the ordinary gate runner, shares its
writer lease and checks stop/scope/cumulative limits before every model spawn.
Do not initialize a second watcher over the supervision cursor. `launch --watch`
keeps one service owner; `watch` can reconnect to persisted state without creating
a new runner or thread. Without systemd, use an attached owner and disclose its
lifetime. Never change existing projects or schedules just to adopt supervision.
The Codex adapter verifies local UUID/repository metadata, uses a bounded status
turn and checks exact-thread output. It needs no custom IPC receiver. Generic
IPC remains optional; unavailable/ambiguous delivery stays visibly pending. See
[the IPC contract and rollback](../../../docs/usage.md#durable-supervision-of-an-authorized-batch).

Initial and mid-task `no_workers` endings remain supervised waits; retry only
when local eligibility changes within the original envelope. User stop prevents
automatic recovery. `flow supervise resume --repo ...` requires the same bound
owner/session, defaulting to `CODEX_THREAD_ID` or explicit `--session <same-ID>`.
Codex binding requires invocation in that exact originating thread; nested
repositories under its persisted working directory are supported. A terminal
watcher exits after its delivery attempt, retaining undeliverable evidence.
The built-in Carbon review uses session-only scoped read permissions, including
`.gate/final-review-evidence`, with mutation, commands and remote tools denied.

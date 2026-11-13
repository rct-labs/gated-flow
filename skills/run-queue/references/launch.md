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

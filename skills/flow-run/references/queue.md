# Prepare the next admitted batch

Use [flow's queue procedure](../../flow/references/planning.md) for row format,
scope, irreversible approval and admission. Do not maintain a second queue.

Declare impact from callers and shared behavior, not the directory name:
`local`, `shared`, or `unknown`. Shared changes need `delivery.checks` mappings
including affected callers; `delivery.full_globs` names project-wide impact.
An undeclared/unknown impact, uncovered shared paths or undeclared actual code
changes escalates to full acceptance. Do not mark uncertain work local merely
to avoid a slow test. At integration, configure `delivery.integration_cmd` or
accept the full-suite fallback. Delivery always uses `verify_cmd`.

Before dispatch, inspect implementation, affected callers and tests for each
behaviour. Include the files needed to connect the vertical slice; a file cap
is not a reason to omit a necessary caller. Order or split connected work
before launch. Reconcile earlier scope requests against the spec and saved log;
do not relaunch an unchanged request or automatically accept every requested file.

Then `flow admit --plan --repo .`. This read-only preview catches malformed rows,
invalid checks/spec links and oversized packets, shows earlier scope requests,
and lists declared stage checks. Fix errors and resolve relevant warnings.
It does not infer dependencies or prove coverage. Broad task checks can be
justified; otherwise select affected tests and mapped callers before admission.
Do not mark shared work local or weaken accepted requirements for speed.
Use `verify --task` for the final task check, avoiding a separate identical run
immediately before it. Changed candidates still require fresh evidence.

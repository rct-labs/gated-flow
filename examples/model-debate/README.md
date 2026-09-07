# Try an evidence-first review

This synthetic fixture gives a small, inspectable target for `model-debate`.
The expected findings below are verified locally; they are not represented as
the output of a completed live multi-model debate.

## 1. Check the evidence

```bash
python examples/model-debate/evidence.py
```

The script uses only the Python standard library. It reports two retries with
delays of 1000 and 3000 milliseconds and shows that a newly created queue loses
the failed-task record.

## 2. Ask your agent to review

```text
Use model-debate to review examples/model-debate/draft.md.
Use evidence.py in that directory as local evidence and respect constraints.md.
Select two available, distinct underlying models with two roles each.
Choose complementary roles for implementation, operations and counterevidence.
The host must not occupy a debating seat.
Disclose the roster, call budget and preflight results before calls.
For this local-only fixture, use --no-web.
```

The normal skill default includes live research; this example explicitly opts
out because every relevant fact is contained in the fixture. Model identity and
read boundaries still need to pass preflight. A blocked preflight is a reported
limitation, not a successful debate.

## 3. Compare the findings

| Item | Expected finding | Evidence or constraint |
|---|---|---|
| C001 | Confirmed | The delay sequence contains two entries. |
| C002 | Refuted | The second delay is 3000 ms, not 1000 ms. |
| C003 | Refuted | Failed work is stored only on the instance; a fresh instance has an empty map. |
| P001 | Conflicts with an owner constraint | K002 preserves the existing plain status page. |

Useful role differences include implementation correctness, retry timing and
operational recovery expectations. Each seat should reference the same claim
IDs and point to the actual evidence, rather than vote on which prose sounds
more convincing.

A corrected proposal preserves the retry limit, describes the actual backoff,
states that restart persistence is absent, and retains the existing status page.
If persistence is required, record it as a separate design decision to evaluate
against K001 instead of pretending it already exists.

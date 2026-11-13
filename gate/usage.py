"""Observed provider tokens only; no conversion from bytes, prices or CLI calls.

Codex turn usage includes cached input. Claude result usage excludes its two
cache counters from input_tokens and is cumulative for the session. modelUsage
is a breakdown of that same result, never another amount to add. Unidentified
repeated turns cannot establish a complete total. Raw output remains in logs.
"""
import json

TOKEN_FIELDS = ('uncached_input_tokens', 'cache_read_input_tokens',
                'cache_creation_input_tokens', 'output_tokens', 'total_tokens')


def counter(value):
    return value if type(value) is int and value >= 0 else None


def unknown(provider, status='missing'):
    return dict.fromkeys(('input_tokens',) + TOKEN_FIELDS) | {
        'provider': provider, 'status': status, 'input_semantics': None}


def _record(provider, raw):
    result = unknown(provider, 'partial')
    if not isinstance(raw, dict):
        return result
    result['input_tokens'] = counter(raw.get('input_tokens'))
    result['output_tokens'] = counter(raw.get('output_tokens'))
    if provider == 'codex':
        result['input_semantics'] = 'includes_cache_read'
        cached = counter(raw.get('cached_input_tokens'))
        result['cache_read_input_tokens'] = cached
        result['cache_creation_input_tokens'] = 0  # Not a separate Codex bucket.
        total_input = result['input_tokens']
        if total_input is not None and cached is not None and cached <= total_input:
            result['uncached_input_tokens'] = total_input - cached
    else:
        result['input_semantics'] = 'excludes_cache_read_and_creation'
        result['uncached_input_tokens'] = result['input_tokens']
        for field in ('cache_read_input_tokens', 'cache_creation_input_tokens'):
            result[field] = counter(raw.get(field))
    values = [result[k] for k in TOKEN_FIELDS[:-1]]
    if all(v is not None for v in values):
        result['total_tokens'] = sum(values)
        result['status'] = 'complete'
    return result


def extract_usage(worker, output):
    """Parse terminal JSONL envelopes, ignoring prose and nonterminal events.

    Missing cache fields stay unknown. Claude results use the last cumulative
    snapshot only for the same identified session (or a single result). Codex
    identified turns are summed once; ambiguous duplicates stay unknown.
    """
    if worker not in ('codex', 'claude'):
        return unknown(worker)
    records = []
    damaged = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            # A visibly damaged terminal envelope invalidates a prior total.
            if line.lstrip().startswith('{') and any(
                    tag in line for tag in ('turn.completed', '"result"')):
                damaged = True
            continue
        if not isinstance(event, dict):
            continue
        terminal = 'turn.completed' if worker == 'codex' else 'result'
        if event.get('type') == terminal:
            records.append(event)
    if not records:
        return unknown(worker)
    if damaged:
        return unknown(worker, 'partial')
    if worker == 'claude':
        sessions = [r.get('session_id') for r in records]
        if len(records) > 1 and (not all(isinstance(s, str) and s for s in sessions)
                                 or len(set(sessions)) != 1):
            return unknown(worker, 'ambiguous')
        return _record(worker, records[-1].get('usage'))
    turns = {}
    for index, event in enumerate(records):
        turn = event.get('turn_id')
        if turn is None and len(records) > 1:
            return unknown(worker, 'ambiguous')
        if turn is not None and (not isinstance(turn, str) or not turn):
            return unknown(worker, 'ambiguous')
        key = turn if turn is not None else index
        record = _record(worker, event.get('usage'))
        if key in turns and record != turns[key]:
            return unknown(worker, 'ambiguous')
        turns[key] = record
    rows = list(turns.values())
    if len(rows) == 1:
        return rows[0]
    result = unknown(worker, 'complete')
    result['input_semantics'] = 'includes_cache_read'
    for field in ('input_tokens',) + TOKEN_FIELDS:
        values = [r[field] for r in rows]
        result[field] = sum(values) if all(v is not None for v in values) else None
    if result['total_tokens'] is None:
        result['status'] = 'partial'
    return result


def summarize_usage(events):
    """Join call receipts by id; duplicate/conflicting receipts never inflate sums.

    totals require complete coverage; observed subtotals explicitly describe only
    available counters. Legacy/no-call journals cannot assert zero provider use.
    """
    calls = {}
    ends = {}
    for index, event in enumerate(events):
        kind = event.get('event')
        if kind == 'model_call':
            key = event.get('call') or ('legacy', index)
            calls[key] = event
        elif kind == 'model_call_end' and event.get('call'):
            key = event['call']
            if key not in ends:
                ends[key] = event
            elif ends[key] != event:
                ends[key] = {}  # Conflicting receipt: disclose missing coverage.

    def subtotal(selected):
        rows = [ends.get(key, {}) for key in selected]
        usages = [r.get('usage') if isinstance(r.get('usage'), dict) else {} for r in rows]
        observed, totals = {}, {}
        for field in TOKEN_FIELDS:
            values = [counter(u.get(field)) for u in usages]
            known = [v for v in values if v is not None]
            observed[field] = sum(known) if known else None
            totals[field] = sum(known) if values and len(known) == len(values) else None
        result = {
            'calls': len(selected), 'finished_calls': sum(bool(r) for r in rows),
            'measured_calls': sum(any(counter(u.get(k)) is not None for k in
                                      ('input_tokens', 'cache_read_input_tokens', 'output_tokens'))
                                  or (u.get('provider') == 'claude' and
                                      counter(u.get('cache_creation_input_tokens')) is not None)
                                  for u in usages),
            'complete_calls': sum(u.get('status') == 'complete' and
                                  all(counter(u.get(k)) is not None for k in TOKEN_FIELDS)
                                  for u in usages),
            'observed': observed, 'totals': totals,
        }
        for field in ('prompt_bytes', 'output_bytes'):
            values = [counter(r.get(field)) for r in rows]
            result[field] = sum(values) if values and all(v is not None for v in values) else None
        return result

    result = subtotal(calls)
    roles = sorted({e.get('role') or 'unknown' for e in calls.values()})
    result['roles'] = {role: subtotal({k: e for k, e in calls.items()
                                     if (e.get('role') or 'unknown') == role}) for role in roles}
    return result


def render_usage(summary):
    """Human report uses the same totals/coverage as the machine metrics."""
    def shown(value):
        return str(value) if value is not None else 'unknown'

    lines = ["| role | complete / calls | prompt bytes | output bytes | total tokens | observed subtotal |",
             "|---|---:|---:|---:|---:|---:|"]
    for role, data in [('all', summary), *summary['roles'].items()]:
        lines.append(f"| {role} | {data['complete_calls']} / {data['calls']} | "
                     f"{shown(data['prompt_bytes'])} | {shown(data['output_bytes'])} | "
                     f"{shown(data['totals']['total_tokens'])} | {shown(data['observed']['total_tokens'])} |")
    lines += ["", "Totals require complete receipts; observed subtotals cover only reported usage. "
              "Missing usage is unknown. Bytes measure UTF-8 prompt and captured output text, "
              "not provider tokens. No subscription charge or token saving is inferred.", "",
              "| role | uncached input | cache read | cache creation | output |",
              "|---|---:|---:|---:|---:|"]
    for role, data in [('all', summary), *summary['roles'].items()]:
        lines.append('| ' + role + ' | ' + ' | '.join(shown(data['totals'][k])
                     for k in TOKEN_FIELDS[:-1]) + ' |')
    return lines

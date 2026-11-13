"""Worker decisions belong to final assistant text, never nested tool output.

This pure protocol owner supports current provider envelopes and legacy prose.
A result can stop a runner; it cannot certify completion or settlement.
"""
import json
import re


class ProtocolError(ValueError):
    """An explicit worker decision cannot be safely interpreted."""


def final_text(output):
    """Prefer terminal provider result, otherwise the latest assistant message.

    Once structured events are present, unrecognized events and non-JSON log
    lines cannot become a plain-text decision. Plain providers remain supported.
    """
    try:
        whole = json.loads(output)
    except (ValueError, TypeError):
        whole = None
    events = [whole] if isinstance(whole, dict) else []
    if not events:
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
    if not events:
        return output
    assistant, terminal = '', None
    for event in events:
        kind = event.get('type')
        if kind in ('item.completed', 'item.updated'):
            item = event.get('item')
            if isinstance(item, dict) and item.get('type') == 'agent_message':
                if isinstance(item.get('text'), str):
                    assistant = item['text']
        elif kind == 'assistant':
            message = event.get('message')
            content = message.get('content') if isinstance(message, dict) else None
            if isinstance(content, str):
                assistant = content
            elif isinstance(content, list):
                assistant = '\n'.join(block['text'] for block in content
                                      if isinstance(block, dict) and block.get('type') == 'text'
                                      and isinstance(block.get('text'), str))
        elif kind == 'result':
            terminal = event['result'] if isinstance(event.get('result'), str) else ''
    return terminal if terminal is not None else assistant


def decision_text(text):
    """Exclude Markdown source examples and block quotes from declarations."""
    lines, fence = [], None
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(('```', '~~~')):
            token = stripped[:3]
            fence = None if fence == token else token if fence is None else fence
            continue
        if fence is None and not stripped.startswith('>'):
            lines.append(line)
    return '\n'.join(lines)


def safe_path(path):
    return (isinstance(path, str) and 0 < len(path) <= 512
            and not any(ord(c) < 32 for c in path)
            and not any(c in path for c in ('\\', ':'))
            and all(part not in ('', '.', '..') for part in path.split('/')))


SCOPE_PATTERNS = (
    re.compile(r'\u8981\u4f60\u51b3\u5b9a\s*\|\s*(?!\u65e0\b|None\b|—|-|\s*\|)\S', re.I),
    re.compile(r'\b(?:needs? your decision|awaiting (?:your )?(?:approval|decision))\b', re.I),
    re.compile(r'\b(?:blocked by (?:a )?required scope expansion|please add .+? to .+?scope)\b', re.I),
    re.compile(r'^scope request\s*:', re.I | re.M),
)
SCOPE_HINT = re.compile(r'\u6269\u56f4|\u58f0\u660e(?:\u6587\u4ef6|\u5217\u8868|\u8303\u56f4)|declared files|files:|scope', re.I)
SCOPE_PATH = re.compile(r'(?<![\w/])(?:[\w.-]+/)+[\w.\[\]()-]+\.[A-Za-z]{1,10}')


def scope_request(output, task=None, *, include_legacy=True):
    """Validate explicit markers first; allow callers to defer weaker prose."""
    text = decision_text(final_text(output))
    markers = [line.lstrip()[len('FLOW_RESULT:'):].strip() for line in text.splitlines()
               if line.lstrip().startswith('FLOW_RESULT:')]
    if markers:
        if len(markers) != 1:
            raise ProtocolError('Multiple FLOW_RESULT markers; emit one final scope_request for ' + str(task))
        try:
            body = json.loads(markers[0])
        except ValueError as exc:
            raise ProtocolError('Malformed FLOW_RESULT JSON; emit the task packet contract') from exc
        if not isinstance(body, dict) or type(body.get('version')) is not int or body['version'] != 1:
            raise ProtocolError('FLOW_RESULT requires integer version 1')
        if (not isinstance(body.get('task'), str) or not body['task']
                or (task is not None and body['task'] != task)):
            raise ProtocolError('FLOW_RESULT task must match dispatched task ' + str(task))
        if body.get('status') != 'scope_request':
            raise ProtocolError('FLOW_RESULT status must be scope_request; completion uses the gate')
        reason, files = body.get('reason'), body.get('files')
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ProtocolError('FLOW_RESULT reason must be nonempty text of at most 2000 characters')
        if not isinstance(files, list) or not 1 <= len(files) <= 100 or not all(map(safe_path, files)):
            raise ProtocolError('FLOW_RESULT files requires 1-100 normalized repository-relative paths; no traversal')
        return {'files': sorted(set(files)), 'reason': reason.strip()}
    if not include_legacy:
        return None
    # Legacy prose may quote an old request as evidence. Match intent outside
    # inline quotes/code, while retaining quoted filenames for path evidence.
    prose = re.sub(r'"[^"\n]*"|`[^`\n]*`|(?<!\w)\'[^\'\n]+\'(?!\w)', '', text)
    if any(pattern.search(prose) for pattern in SCOPE_PATTERNS) and SCOPE_HINT.search(prose):
        return {'files': sorted({p for p in SCOPE_PATH.findall(text) if safe_path(p)}),
                'reason': text.strip()[:2000]}
    return None

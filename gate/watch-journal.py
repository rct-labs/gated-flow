#!/usr/bin/env python3
"""Incremental, bounded gate events for hosts without a persistent Monitor."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time

EVENTS = frozenset('run_start attempt_start task_done worker_left_changes scope_drift '
    'scope_request prompt_too_large review_start review_verdict review_skipped '
    'review_findings stage_acceptance full_acceptance full_acceptance_wait '
    'needs_approval task_end tool_disabled admit_refused run_end'.split())
FIELDS = ('at', 'event', 'run', 'task', 'worker', 'commit', 'stop', 'result',
          'count', 'reason', 'outcome', 'member', 'verdict', 'log', 'passed', 'score',
          'stage', 'seconds')
MAX_LINE = 256 * 1024
MAX_READ = 1024 * 1024


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(state), encoding='utf-8')
    os.replace(tmp, path)


def identity(stream):
    stat = os.fstat(stream.fileno())
    return [stat.st_dev, stat.st_ino]


def initialize(journal, cursor):
    state = {'journal': str(journal.resolve()), 'offset': 0, 'idle': 0, 'identity': None,
             'prefix_size': 0, 'prefix': '', 'discard': False}
    if journal.exists():
        with journal.open('rb') as stream:
            prefix = stream.read(64)
            state.update(identity=identity(stream), prefix_size=len(prefix),
                         prefix=hashlib.sha256(prefix).hexdigest())
            stream.seek(0, 2)
            state['offset'] = stream.tell()
            if state['offset']:
                stream.seek(-1, 2)
                state['discard'] = stream.read(1) != b'\n'
    save(cursor, state)
    return {'status': 'armed', 'offset': state['offset']}


def compact(event):
    # Do not forward prompts, heartbeat log tails, full config or arbitrary payloads.
    return {key: (value[:256] if isinstance(value, str) else value)
            for key in FIELDS if isinstance((value := event.get(key)), (str, int, float, bool))}


def read_events(journal, cursor, limit=20):
    state = json.loads(cursor.read_text(encoding='utf-8'))
    if state['journal'] != str(journal.resolve()):
        raise ValueError('cursor belongs to a different journal')
    events, consumed, scanned, incomplete = [], 0, 0, False
    if journal.exists():
        with journal.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            prefix = stream.read(state['prefix_size'])
            reset = (state['identity'] != identity(stream)
                     or stat.st_size < state['offset']
                     or hashlib.sha256(prefix).hexdigest() != state['prefix'])
            if reset:
                state.update(offset=0, discard=False)
                if state['identity'] is not None:
                    events.append({'event': 'journal_reset'})
            stream.seek(0)
            prefix = stream.read(64)
            state.update(identity=identity(stream), prefix_size=len(prefix),
                         prefix=hashlib.sha256(prefix).hexdigest())
            stream.seek(state['offset'])
            while len(events) < limit and consumed < MAX_READ:
                start = stream.tell()
                line = stream.readline(MAX_LINE + 1)
                if not line:
                    break
                consumed += len(line)
                if state['discard']:
                    state['discard'] = not line.endswith(b'\n')
                    state['offset'] = stream.tell()
                    continue
                if not line.endswith(b'\n'):
                    if len(line) > MAX_LINE:
                        events.append({'event': 'journal_warning', 'reason': 'oversized_record'})
                        state.update(offset=stream.tell(), discard=True)
                    else:
                        stream.seek(start)  # Writer has not finished this record yet.
                        incomplete = True
                    break
                state['offset'] = stream.tell()
                scanned += 1
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError('record is not an object')
                    if isinstance(event.get('at'), str):
                        state['last_activity_at'] = event['at'][:64]
                    if event.get('event') in EVENTS:
                        events.append(compact(event))
                except (ValueError, UnicodeError):
                    events.append({'event': 'journal_warning', 'reason': 'invalid_record'})
            more = not incomplete and stream.tell() < stat.st_size
    else:
        more = False
    state['idle'] = 0 if events else min(state['idle'] + 1, 3)
    save(cursor, state)
    return {'status': 'events' if events else 'idle', 'events': events,
            'more': more, 'next_poll_s': 0 if more else (60, 120, 300, 300)[state['idle']],
            'bytes_read': consumed, 'records_scanned': scanned,
            'last_activity_at': state.get('last_activity_at')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('repo', type=Path)
    parser.add_argument('--cursor', type=Path, help='one cursor per host; do not share concurrent readers')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--init', action='store_true', help='arm at EOF BEFORE launching')
    mode.add_argument('--once', action='store_true', help='read only unseen complete records')
    mode.add_argument('--follow', action='store_true', help='stream events to a persistent Monitor')
    parser.add_argument('--wait', type=float, default=0, help='wait for events, capped at 55 seconds')
    parser.add_argument('--limit', type=int, default=20, help='maximum emitted events per read (1..100)')
    args = parser.parse_args()
    if not 0 <= args.wait <= 55 or not 1 <= args.limit <= 100:
        parser.error('--wait must be 0..55; --limit must be 1..100')
    journal = args.repo / '.gate/journal.ndjson'
    cursor = args.cursor or args.repo / '.gate/watch-journal.cursor.json'
    if args.init:
        print(json.dumps(initialize(journal, cursor)))
        return
    if not cursor.exists():
        parser.error('missing cursor; use --init before launching the run')
    deadline = time.monotonic() + args.wait
    while True:
        result = read_events(journal, cursor, args.limit)
        if result['events'] or (not args.follow and time.monotonic() >= deadline):
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if not args.follow or any(e['event'] == 'run_end' for e in result['events']):
                return
        time.sleep(1)


if __name__ == '__main__':
    main()

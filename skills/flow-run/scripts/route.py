#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import re
import shutil
import sqlite3
import sys
import time
from typing import Any

_route_parents = Path(__file__).resolve().parents
_route_source = (_route_parents[3] / "gate" / "model_routes.py"
                 if len(_route_parents) > 3 else Path(__file__).with_name("model_routes.py"))
if not _route_source.is_file():
    _route_source = Path(__file__).with_name("model_routes.py")
if not _route_source.is_file():
    # Copied skills resolve the shared runtime through the existing installer
    # shim protocol. Read its fixed template; never execute shell contents or
    # spend another Python startup on normal local route selection.
    _shim = shutil.which("flow")
    if _shim:
        with Path(_shim).open("rb") as _stream:
            _shim_text = _stream.read(8193).decode("utf-8", errors="replace")
        _match = re.fullmatch(r'#!/bin/sh\nexec (?:python|python3) "([^"\n]+)/flow/flow.py" "\$@"\n', _shim_text)
        if _match:
            _route_source = Path(_match[1]) / "gate/model_routes.py"
_spec = importlib.util.spec_from_file_location("_flow_model_routes", _route_source)
if _spec is None or _spec.loader is None:
    raise ImportError("Cannot load shared model route eligibility")
_routes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_routes)

ROLES = ("gemini", "codex")
CLASSES = ("routine", "complex")
DEFAULT_ORDER: dict[str, tuple[str, str]] = {
    "routine": ("gemini", "codex"),
    "complex": ("codex", "gemini"),
}
PROVISIONAL_WINDOW = 4
RESCUE_WINDOW = 5
RESCUE_THRESHOLD = 3
INVALID_TOKENS = frozenset({"unknown", "none", "null", "n/a", "na", "missing", "unspecified"})


class RoutingError(Exception):
    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Pin:
    model: str
    effort: str

    @property
    def label(self) -> str:
        return f"{self.model}@{self.effort}"


@dataclass
class ClassState:
    task_class: str
    primary_role: str
    backup_role: str
    primary_model: str | None
    primary_effort: str | None
    backup_model: str | None
    backup_effort: str | None
    epoch: int
    cursor_id: int
    backup_cursor_id: int
    provisional: int
    last_change_kind: str
    demoted_pins: set[tuple[str, str, str]] = field(default_factory=set)


def _clean_ident(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RoutingError(f"Invalid {field}: expected string")
    cleaned = value.strip()
    if not cleaned or any(ch in cleaned for ch in ("\x00", "\r", "\n")):
        raise RoutingError(f"Invalid {field}: empty or control characters")
    if cleaned.lower() in INVALID_TOKENS:
        raise RoutingError(f"Invalid {field}: placeholder value {cleaned!r}")
    return cleaned


def _default_policy_path() -> Path:
    return Path.home() / ".config" / "mini-model-routing" / "policy.json"


def load_policy_pins(policy_path: Path) -> dict[str, Pin]:
    if not policy_path.is_file():
        raise RoutingError(f"Policy file not found: {policy_path}")
    try:
        raw = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RoutingError(f"Invalid policy JSON at {policy_path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("coding"), dict):
        raise RoutingError("Policy JSON missing 'coding' object")
    coding = raw["coding"]
    pins: dict[str, Pin] = {}
    for role in ROLES:
        entry = coding.get(role)
        if not isinstance(entry, dict):
            raise RoutingError(f"Policy missing coding.{role} object")
        model = _clean_ident(entry.get("model"), f"coding.{role}.model")
        effort = _clean_ident(entry.get("effort"), f"coding.{role}.effort")
        pins[role] = Pin(model=model, effort=effort)
    return pins


def _db_path(repo: Path) -> Path:
    if not repo.is_dir():
        raise RoutingError(f"Repository directory does not exist: {repo}")
    return repo / ".gate" / "routing.sqlite3"


def _run_with_sqlite_retry(fn):
    for attempt in range(25):
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if ("locked" in msg or "busy" in msg) and attempt < 24:
                time.sleep(0.005 * (attempt + 1) + random.uniform(0.0, 0.01))
                continue
            raise RoutingError(f"SQLite error: {exc}") from exc


def _parse_demoted(raw: str | None) -> set[tuple[str, str, str]]:
    if not raw:
        return set()
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    out: set[tuple[str, str, str]] = set()
    if isinstance(items, list):
        for item in items:
            if isinstance(item, list) and len(item) == 3 and all(isinstance(x, str) for x in item):
                out.add((item[0], item[1], item[2]))
    return out


def _dump_demoted(pins: set[tuple[str, str, str]]) -> str:
    return json.dumps([list(item) for item in sorted(pins)])


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            task_class TEXT NOT NULL CHECK (task_class IN ('routine', 'complex')),
            epoch INTEGER NOT NULL,
            eligible INTEGER NOT NULL DEFAULT 1 CHECK (eligible IN (0, 1)),
            primary_role TEXT NOT NULL CHECK (primary_role IN ('gemini', 'codex')),
            primary_model TEXT NOT NULL,
            primary_effort TEXT NOT NULL,
            primary_outcome TEXT NOT NULL CHECK (primary_outcome IN ('pass', 'quality_fail')),
            backup_role TEXT CHECK (backup_role IN ('gemini', 'codex')),
            backup_model TEXT,
            backup_effort TEXT,
            backup_outcome TEXT CHECK (backup_outcome IN ('pass', 'fail')),
            recorded_at TEXT NOT NULL,
            UNIQUE(run_id, task_id)
        )
        """
    )
    ev_cols = {r[1] for r in conn.execute("PRAGMA table_info(events)").fetchall()}
    if "eligible" not in ev_cols:
        conn.execute("ALTER TABLE events ADD COLUMN eligible INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_scope_id
            ON events(task_class, eligible, primary_role, primary_model, primary_effort, id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_provisional_decisive
            ON events(task_class, primary_role, primary_model, primary_effort, id)
            WHERE eligible = 1 AND backup_outcome IS NOT 'fail'
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_task_id
            ON events(task_class, eligible, task_id, id)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS class_state (
            task_class TEXT PRIMARY KEY CHECK (task_class IN ('routine', 'complex')),
            primary_role TEXT NOT NULL CHECK (primary_role IN ('gemini', 'codex')),
            backup_role TEXT NOT NULL CHECK (backup_role IN ('gemini', 'codex')),
            primary_model TEXT,
            primary_effort TEXT,
            backup_model TEXT,
            backup_effort TEXT,
            epoch INTEGER NOT NULL DEFAULT 0,
            cursor_id INTEGER NOT NULL DEFAULT 0,
            backup_cursor_id INTEGER NOT NULL DEFAULT 0,
            provisional INTEGER NOT NULL DEFAULT 0 CHECK (provisional IN (0, 1)),
            demoted_pins TEXT NOT NULL DEFAULT '[]',
            last_change_kind TEXT NOT NULL DEFAULT 'default'
        )
        """
    )
    cs_cols = {r[1] for r in conn.execute("PRAGMA table_info(class_state)").fetchall()}
    if "demoted_pins" not in cs_cols:
        conn.execute("ALTER TABLE class_state ADD COLUMN demoted_pins TEXT NOT NULL DEFAULT '[]'")


def _default_state(task_class: str) -> ClassState:
    p_role, b_role = DEFAULT_ORDER[task_class]
    return ClassState(
        task_class=task_class,
        primary_role=p_role,
        backup_role=b_role,
        primary_model=None,
        primary_effort=None,
        backup_model=None,
        backup_effort=None,
        epoch=0,
        cursor_id=0,
        backup_cursor_id=0,
        provisional=0,
        last_change_kind="default",
    )


def _load_state(conn: sqlite3.Connection, task_class: str) -> ClassState | None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(class_state)").fetchall()}
    if not cols:
        return None
    demoted_expr = "demoted_pins" if "demoted_pins" in cols else "'[]'"
    row = conn.execute(
        f"""
        SELECT task_class, primary_role, backup_role, primary_model, primary_effort,
               backup_model, backup_effort, epoch, cursor_id, backup_cursor_id,
               provisional, last_change_kind, {demoted_expr}
        FROM class_state WHERE task_class = ?
        """,
        (task_class,),
    ).fetchone()
    if row is None:
        return None
    return ClassState(*(row[i] for i in range(12)), demoted_pins=_parse_demoted(row[12]))


def _save_state(conn: sqlite3.Connection, state: ClassState) -> None:
    conn.execute(
        """
        INSERT INTO class_state (
            task_class, primary_role, backup_role, primary_model, primary_effort,
            backup_model, backup_effort, epoch, cursor_id, backup_cursor_id,
            provisional, demoted_pins, last_change_kind
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(task_class) DO UPDATE SET
            primary_role = excluded.primary_role,
            backup_role = excluded.backup_role,
            primary_model = excluded.primary_model,
            primary_effort = excluded.primary_effort,
            backup_model = excluded.backup_model,
            backup_effort = excluded.backup_effort,
            epoch = excluded.epoch,
            cursor_id = excluded.cursor_id,
            backup_cursor_id = excluded.backup_cursor_id,
            provisional = excluded.provisional,
            demoted_pins = excluded.demoted_pins,
            last_change_kind = excluded.last_change_kind
        """,
        (
            state.task_class,
            state.primary_role,
            state.backup_role,
            state.primary_model,
            state.primary_effort,
            state.backup_model,
            state.backup_effort,
            state.epoch,
            state.cursor_id,
            state.backup_cursor_id,
            state.provisional,
            _dump_demoted(state.demoted_pins),
            state.last_change_kind,
        ),
    )


def cmd_recommend(repo: Path, task_class: str) -> dict[str, Any]:
    pins = load_policy_pins(_default_policy_path())
    db_file = _db_path(repo)
    state: ClassState | None = None
    if db_file.is_file() and db_file.stat().st_size > 0:
        def _read() -> ClassState | None:
            conn = sqlite3.connect(str(db_file), timeout=30.0)
            try:
                conn.execute("PRAGMA busy_timeout = 30000")
                return _load_state(conn, task_class)
            finally:
                conn.close()

        state = _run_with_sqlite_retry(_read)

    if state is None:
        state = _default_state(task_class)

    p_role, b_role = state.primary_role, state.backup_role
    p_pin, b_pin = pins[p_role], pins[b_role]

    pin_changed = (
        state.primary_model is not None
        and (p_pin.model, p_pin.effort) != (state.primary_model, state.primary_effort)
    )
    if (
        pin_changed
        and (p_role, p_pin.model, p_pin.effort) in state.demoted_pins
        and (b_role, b_pin.model, b_pin.effort) not in state.demoted_pins
    ):
        p_role, b_role = b_role, p_role
        p_pin, b_pin = pins[p_role], pins[b_role]
        is_provisional = False
        change_kind = "provisional_demotion"
    else:
        is_provisional = (
            (bool(state.provisional) or pin_changed)
            and (p_role, p_pin.model, p_pin.effort) not in state.demoted_pins
        )
        change_kind = state.last_change_kind

    if is_provisional:
        reason = (
            f"Provisional {task_class} primary {p_role} ({p_pin.label}) inherited slot "
            f"with zero transferred scores; backup {b_role} ({b_pin.label})."
        )
    elif change_kind == "provisional_demotion":
        reason = (
            f"Restored {p_role} ({p_pin.label}) ahead of {b_role} ({b_pin.label}) for {task_class} "
            f"after provisional primary rollback (epoch {state.epoch})."
        )
    elif change_kind == "rescue_swap":
        reason = (
            f"Promoted {p_role} ({p_pin.label}) over {b_role} ({b_pin.label}) for {task_class} "
            f"from verified rescues (epoch {state.epoch})."
        )
    else:
        reason = (
            f"Default {task_class} order: {p_role} ({p_pin.label}) primary, "
            f"{b_role} ({b_pin.label}) backup."
        )

    primary_obj = {
        "rank": 1,
        "role": p_role,
        "model": p_pin.model,
        "effort": p_pin.effort,
        "pin": p_pin.label,
    }
    backup_obj = {
        "rank": 2,
        "role": b_role,
        "model": b_pin.model,
        "effort": b_pin.effort,
        "pin": b_pin.label,
    }
    try:
        inventory = _routes.read_json(_routes.inventory_path())
        ranked, excluded = [], []
        for obj in (primary_obj, backup_obj):
            blocked = _routes.unavailable(obj["role"], obj["model"], state=inventory)
            if blocked:
                excluded.append(dict(obj, eligibility=blocked))
            else:
                ranked.append(obj)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise RoutingError(f"Invalid local eligibility state: {exc}") from exc
    if not ranked:
        raise RoutingError("No eligible local coding route; no discovery or inference attempted")
    for rank, obj in enumerate(ranked, 1): obj["rank"] = rank
    primary_obj = ranked[0]
    backup_obj = ranked[1] if len(ranked) > 1 else None
    if excluded:
        reason += " Local exclusions: " + ", ".join(o["role"] + ":" + o["eligibility"] for o in excluded) + "."
    return {
        "class": task_class,
        "ranked": ranked,
        "excluded": excluded,
        "capacity": "unknown unless locally evidenced; catalog visibility is not entitlement",
        "primary": {k: v for k, v in primary_obj.items() if k != "rank"},
        "backup": {k: v for k, v in backup_obj.items() if k != "rank"} if backup_obj else None,
        "provisional": is_provisional,
        "epoch": state.epoch,
        "reason": reason,
    }


def _recent_primary_tasks(
    conn: sqlite3.Connection,
    task_class: str,
    cursor_id: int,
    p_role: str,
    p_model: str,
    p_effort: str,
    limit: int = RESCUE_WINDOW,
) -> list[list[sqlite3.Row]]:
    if limit <= 0:
        return []
    rows = conn.execute(
        """
        SELECT id, task_id, primary_outcome, backup_role, backup_model, backup_effort, backup_outcome
        FROM events
        WHERE task_class = ? AND eligible = 1
          AND primary_role = ? AND primary_model = ? AND primary_effort = ?
          AND id > ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (task_class, p_role, p_model, p_effort, cursor_id, limit),
    ).fetchall()
    return [[row] for row in reversed(rows)]


def _provisional_decisive_tasks(
    conn: sqlite3.Connection,
    task_class: str,
    cursor_id: int,
    p_role: str,
    p_model: str,
    p_effort: str,
    limit: int = PROVISIONAL_WINDOW,
) -> list[list[sqlite3.Row]]:
    if limit <= 0:
        return []
    rows = conn.execute(
        """
        SELECT id, task_id, primary_outcome, backup_outcome
        FROM events
        WHERE task_class = ? AND eligible = 1 AND backup_outcome IS NOT 'fail'
          AND primary_role = ? AND primary_model = ? AND primary_effort = ?
          AND id > ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (task_class, p_role, p_model, p_effort, cursor_id, limit),
    ).fetchall()
    return [[row] for row in rows]


def _evaluate_transitions(
    conn: sqlite3.Connection,
    state: ClassState,
    event_id: int,
    acting_p_model: str,
    acting_p_effort: str,
) -> str:
    if state.provisional == 1 and state.primary_model is not None and state.primary_effort is not None:
        decisive = _provisional_decisive_tasks(
            conn,
            state.task_class,
            state.cursor_id,
            state.primary_role,
            state.primary_model,
            state.primary_effort,
        )
        active_backup_failed = False
        if state.backup_model is not None and state.backup_effort is not None:
            b_cut = max(state.cursor_id, state.backup_cursor_id)
            fail_row = conn.execute(
                """
                SELECT 1 FROM events
                WHERE task_class = ? AND eligible = 1 AND id > ?
                  AND primary_role = ? AND primary_model = ? AND primary_effort = ?
                  AND backup_role = ? AND backup_model = ? AND backup_effort = ?
                  AND backup_outcome = 'fail'
                LIMIT 1
                """,
                (
                    state.task_class,
                    b_cut,
                    state.primary_role,
                    state.primary_model,
                    state.primary_effort,
                    state.backup_role,
                    state.backup_model,
                    state.backup_effort,
                ),
            ).fetchone()
            active_backup_failed = fail_row is not None

        attr_failures = sum(
            1 for grp in decisive if grp[-1]["primary_outcome"] == "quality_fail"
        )
        if attr_failures >= 2 and not active_backup_failed:
            state.demoted_pins.add((state.primary_role, acting_p_model, acting_p_effort))
            restored_model, restored_effort = state.backup_model, state.backup_effort
            state.primary_role, state.backup_role = state.backup_role, state.primary_role
            state.primary_model, state.primary_effort = restored_model, restored_effort
            state.backup_model, state.backup_effort = acting_p_model, acting_p_effort
            state.provisional = 0
            state.epoch += 1
            state.cursor_id = event_id
            state.backup_cursor_id = event_id
            state.last_change_kind = "provisional_demotion"
            return "provisional_demotion"

        if len(decisive) >= PROVISIONAL_WINDOW:
            state.provisional = 0

    if (
        state.primary_model is not None
        and state.primary_effort is not None
        and state.backup_model is not None
        and state.backup_effort is not None
    ):
        recent = _recent_primary_tasks(
            conn,
            state.task_class,
            state.cursor_id,
            state.primary_role,
            state.primary_model,
            state.primary_effort,
        )
        b_cut = max(state.cursor_id, state.backup_cursor_id)
        b_tuple = (state.backup_role, state.backup_model, state.backup_effort)
        has_backup_fail = False
        rescues = 0
        for grp in recent:
            if any(
                r["id"] > b_cut
                and (r["backup_role"], r["backup_model"], r["backup_effort"]) == b_tuple
                and r["backup_outcome"] == "fail"
                for r in grp
            ):
                has_backup_fail = True
                break
            last = grp[-1]
            if (
                last["id"] > b_cut
                and last["primary_outcome"] == "quality_fail"
                and (last["backup_role"], last["backup_model"], last["backup_effort"]) == b_tuple
                and last["backup_outcome"] == "pass"
            ):
                rescues += 1

        if not has_backup_fail and rescues >= RESCUE_THRESHOLD:
            old_p = (state.primary_model, state.primary_effort)
            win_b = (state.backup_model, state.backup_effort)
            state.primary_role, state.backup_role = state.backup_role, state.primary_role
            state.primary_model, state.primary_effort = win_b
            state.backup_model, state.backup_effort = old_p
            state.provisional = 0
            state.epoch += 1
            state.cursor_id = event_id
            state.backup_cursor_id = event_id
            state.last_change_kind = "rescue_swap"
            return "rescue_swap"

    return "none"


def cmd_record(args: argparse.Namespace) -> dict[str, Any]:
    pins = load_policy_pins(_default_policy_path())
    repo = Path(args.repo)
    db_file = _db_path(repo)
    task_class = args.task_class
    run_id = _clean_ident(args.run_id, "run-id")
    task_id = _clean_ident(args.task_id, "task-id")

    p_role = args.primary_role
    if p_role not in ROLES:
        raise RoutingError(f"Invalid primary-role: {p_role!r}")
    p_model = _clean_ident(args.primary_model, "primary-model")
    p_effort = _clean_ident(args.primary_effort, "primary-effort")

    if args.primary_outcome not in ("pass", "quality_fail"):
        raise RoutingError(f"Invalid primary-outcome: {args.primary_outcome!r}")
    p_outcome = args.primary_outcome

    b_fields = (args.backup_role, args.backup_model, args.backup_effort, args.backup_outcome)
    any_backup = any(v is not None for v in b_fields)
    if p_outcome == "pass":
        if any_backup:
            raise RoutingError("Backup fields forbidden when primary-outcome is 'pass'")
        b_role = b_model = b_effort = b_outcome = None
    else:
        if any_backup:
            if not all(v is not None for v in b_fields):
                raise RoutingError("Complete backup identity and outcome required when backup is provided")
            if args.backup_role not in ROLES or args.backup_role == p_role:
                raise RoutingError("backup-role must be the distinct secondary role")
            b_role = args.backup_role
            b_model = _clean_ident(args.backup_model, "backup-model")
            b_effort = _clean_ident(args.backup_effort, "backup-effort")
            raw_bo = str(args.backup_outcome).strip()
            if raw_bo in ("pass", "success"):
                b_outcome = "pass"
            elif raw_bo in ("fail", "failure", "quality_fail"):
                b_outcome = "fail"
            else:
                raise RoutingError(f"Invalid backup-outcome: {args.backup_outcome!r}")
        else:
            b_role = b_model = b_effort = b_outcome = None

    db_file.parent.mkdir(parents=True, exist_ok=True)
    payload_tuple = (
        task_class,
        p_role,
        p_model,
        p_effort,
        p_outcome,
        b_role,
        b_model,
        b_effort,
        b_outcome,
    )

    def _tx() -> dict[str, Any]:
        conn = sqlite3.connect(str(db_file), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                _ensure_schema(conn)
                existing = conn.execute(
                    """
                    SELECT task_class, primary_role, primary_model, primary_effort, primary_outcome,
                           backup_role, backup_model, backup_effort, backup_outcome
                    FROM events WHERE run_id = ? AND task_id = ?
                    """,
                    (run_id, task_id),
                ).fetchone()
                if existing is not None:
                    if tuple(existing) != payload_tuple:
                        raise RoutingError(
                            f"Conflict for ({run_id!r}, {task_id!r}): refusing silent rewrite"
                        )
                    st = _load_state(conn, task_class) or _default_state(task_class)
                    conn.execute("COMMIT")
                    return {
                        "status": "idempotent",
                        "idempotent": True,
                        "class": task_class,
                        "epoch": st.epoch,
                        "primary_role": st.primary_role,
                        "backup_role": st.backup_role,
                        "provisional": bool(st.provisional),
                        "change": "none",
                    }

                state = _load_state(conn, task_class) or _default_state(task_class)
                on_policy = (p_model, p_effort) == (pins[p_role].model, pins[p_role].effort) and (
                    b_role is None
                    or (b_model, b_effort) == (pins[b_role].model, pins[b_role].effort)
                )
                cur_sp = pins[state.primary_role]
                cur_sb = pins[state.backup_role]
                suppressed_flip = (
                    on_policy
                    and state.primary_model is not None
                    and (cur_sp.model, cur_sp.effort) != (state.primary_model, state.primary_effort)
                    and (state.primary_role, cur_sp.model, cur_sp.effort) in state.demoted_pins
                    and (state.backup_role, cur_sb.model, cur_sb.effort) not in state.demoted_pins
                )
                effective_p_role = state.backup_role if suppressed_flip else state.primary_role
                eligible = 1 if (on_policy and p_role == effective_p_role) else 0
                if eligible == 1:
                    prior_eligible = conn.execute(
                        """
                        SELECT 1 FROM events
                        WHERE task_class = ? AND eligible = 1 AND task_id = ?
                          AND +primary_role = ? AND +primary_model = ? AND +primary_effort = ?
                        LIMIT 1
                        """,
                        (state.task_class, task_id, p_role, p_model, p_effort),
                    ).fetchone()
                    if prior_eligible is not None:
                        eligible = 0

                now_iso = datetime.now(timezone.utc).isoformat()
                cur = conn.execute(
                    """
                    INSERT INTO events (
                        run_id, task_id, task_class, epoch, eligible,
                        primary_role, primary_model, primary_effort, primary_outcome,
                        backup_role, backup_model, backup_effort, backup_outcome, recorded_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        task_class,
                        state.epoch,
                        eligible,
                        p_role,
                        p_model,
                        p_effort,
                        p_outcome,
                        b_role,
                        b_model,
                        b_effort,
                        b_outcome,
                        now_iso,
                    ),
                )
                event_id = int(cur.lastrowid)
                change = "none"

                if eligible == 1:
                    if suppressed_flip:
                        state.primary_role, state.backup_role = state.backup_role, state.primary_role
                        state.primary_model, state.primary_effort = p_model, p_effort
                        state.backup_model, state.backup_effort = cur_sp.model, cur_sp.effort
                        state.provisional = 0
                        state.epoch += 1
                        state.cursor_id = event_id - 1
                        state.backup_cursor_id = event_id - 1
                        state.last_change_kind = "provisional_demotion"
                        change = "provisional_demotion"

                    cur_b = pins[state.backup_role]
                    if state.primary_model is None:
                        state.primary_model = p_model
                        state.primary_effort = p_effort
                    elif (p_model, p_effort) != (state.primary_model, state.primary_effort):
                        state.cursor_id = event_id - 1
                        state.backup_cursor_id = max(state.backup_cursor_id, event_id - 1)
                        state.primary_model = p_model
                        state.primary_effort = p_effort
                        state.provisional = (
                            0 if (state.primary_role, p_model, p_effort) in state.demoted_pins else 1
                        )

                    if state.backup_model is None:
                        state.backup_model = cur_b.model
                        state.backup_effort = cur_b.effort
                    elif (cur_b.model, cur_b.effort) != (state.backup_model, state.backup_effort):
                        state.backup_cursor_id = max(state.backup_cursor_id, event_id - 1)
                        state.backup_model = cur_b.model
                        state.backup_effort = cur_b.effort

                    eval_change = _evaluate_transitions(conn, state, event_id, p_model, p_effort)
                    if eval_change != "none":
                        change = eval_change
                    _save_state(conn, state)

                conn.execute("COMMIT")
                return {
                    "status": "recorded",
                    "idempotent": False,
                    "class": task_class,
                    "epoch": state.epoch,
                    "primary_role": state.primary_role,
                    "backup_role": state.backup_role,
                    "provisional": bool(state.provisional),
                    "change": change,
                }
            except Exception:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()

    return _run_with_sqlite_retry(_tx)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="route.py")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    rec = sub.add_parser("recommend")
    rec.add_argument("--repo", required=True)
    rec.add_argument("--class", "--task-class", dest="task_class", required=True, choices=CLASSES)

    wrt = sub.add_parser("record")
    wrt.add_argument("--repo", required=True)
    wrt.add_argument("--class", "--task-class", dest="task_class", required=True, choices=CLASSES)
    wrt.add_argument("--run-id", required=True)
    wrt.add_argument("--task-id", required=True)
    wrt.add_argument("--primary-role", required=True)
    wrt.add_argument("--primary-model", required=True)
    wrt.add_argument("--primary-effort", required=True)
    wrt.add_argument("--primary-outcome", "--first-pass", dest="primary_outcome", required=True)
    wrt.add_argument("--backup-role", default=None)
    wrt.add_argument("--backup-model", default=None)
    wrt.add_argument("--backup-effort", default=None)
    wrt.add_argument("--backup-outcome", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.subcommand == "recommend":
            result = cmd_recommend(Path(args.repo), args.task_class)
        else:
            result = cmd_record(args)
    except RoutingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.code
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

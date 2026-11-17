#!/usr/bin/env python3
"""
publication.py - read-only exact remote branch verification and receipt projection.

  publish-verify  verify configured remote refs/heads/<branch> equals expected full SHA,
                  recording a bounded append-only receipt in .gate/publication.ndjson.

Observed remote facts are recorded separately from local delivery acceptance.
No push, PR, retry, credential change, interactive prompt, verdict edit or journal edit
is ever performed. No receipt grants push or release authority.

Usage:
  flow publish-verify --repo DIR --remote NAME --branch NAME --expect FULL_SHA [--timeout SEC] [--json]
"""

from __future__ import annotations

from datetime import datetime
import errno
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Callable
from urllib.parse import unquote, urlsplit
import uuid

HERE = Path(__file__).resolve().parent
GATE_PATH = HERE.parent / "gate" / "gate.py"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

RECEIPT_REL_PATH = ".gate/publication.ndjson"
PUBLICATION_TAIL_BYTES = 256 * 1024
MAX_RECEIPT_BYTES = 8192
MAX_TARGETS_IN_SUMMARY = 8
MAX_PUSH_URLS = 32
DEFAULT_TIMEOUT_S = 15.0
MAX_TIMEOUT_S = 120.0

AUTHORITY_NOTE = "Observed remote state only; receipts never grant push or release authority."

STATUS_EXIT_CODES: dict[str, int] = {
    "match": 0,
    "invalid": 2,
    "mismatch": 3,
    "absent": 4,
    "unavailable": 5,
}

FULL_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*)://")
HELPER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*::")
WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
LS_REMOTE_LINE_RE = re.compile(r"^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})\t([^\s\x00-\x1f\x7f]+)$")
ISO_TS_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})$"
)

KNOWN_FLAGS = frozenset({
    "--repo",
    "--remote",
    "--branch",
    "--expect",
    "--timeout",
    "--json",
    "-h",
    "--help",
})

_GATE = None


def _gate():
    global _GATE
    if _GATE is None:
        spec = importlib.util.spec_from_file_location("flow_publication_gate", GATE_PATH)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load {GATE_PATH}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _GATE = module
    return _GATE


def _no_json_constants(val: str):
    raise ValueError(f"non-standard JSON constant: {val}")


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _valid_timestamp(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    m = ISO_TS_RE.match(s)
    if not m:
        return None
    base, frac, tz = m.groups()
    norm_tz = "+00:00" if tz == "Z" else tz
    norm_frac = f".{frac[:6]}" if frac else ""
    try:
        datetime.fromisoformat(f"{base}{norm_frac}{norm_tz}")
    except ValueError:
        return None
    return s


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _local_git(cwd: Path, *args: str, timeout_s: float = 10.0) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
        env=env,
    )


# ------------------------------------------------ validation & sanitization


def resolve_repo_root(raw_repo: object) -> tuple[Path | None, str | None, str | None]:
    if not isinstance(raw_repo, str) or not raw_repo.strip() or _has_control_chars(raw_repo):
        return None, "invalid_repo", "repository path must be a valid local directory"
    candidate = Path(raw_repo).expanduser()
    try:
        if not candidate.is_dir():
            return None, "invalid_repo", "repository path is not an existing directory"
        resolved_start = candidate.resolve()
    except OSError:
        return None, "invalid_repo", "repository path could not be resolved"
    try:
        proc = _local_git(resolved_start, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None, "invalid_repo", "failed to inspect git repository"
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, "invalid_repo", "path is not inside a git repository"
    try:
        root = Path(proc.stdout.strip()).resolve()
    except OSError:
        return None, "invalid_repo", "git toplevel path could not be resolved"
    return root, None, None


def validate_remote_name(raw: object) -> tuple[str | None, str | None, str | None]:
    if not isinstance(raw, str) or not raw:
        return None, "invalid_remote", "remote name is required"
    if _has_control_chars(raw) or any(ch.isspace() for ch in raw):
        return None, "invalid_remote", "remote name must not contain whitespace or control characters"
    if raw.startswith("-"):
        return None, "invalid_remote", "remote name must not start with '-'"
    if "://" in raw or "::" in raw or "/" in raw or "\\" in raw or ":" in raw or "@" in raw:
        return (
            None,
            "invalid_remote",
            "remote must be a configured remote name, not a URL, transport helper, or path",
        )
    if ".." in raw or raw.endswith(".lock") or not REMOTE_NAME_RE.match(raw):
        return None, "invalid_remote", "remote name contains unsupported characters"
    return raw, None, None


def validate_branch_name(repo_root: Path, raw: object) -> tuple[str | None, str | None, str | None]:
    if not isinstance(raw, str) or not raw:
        return None, "invalid_branch", "branch name is required"
    if len(raw) > 255:
        return None, "invalid_branch", "branch name exceeds maximum length"
    if _has_control_chars(raw) or any(ch.isspace() for ch in raw):
        return None, "invalid_branch", "branch name must not contain whitespace or control characters"
    if raw.startswith("-"):
        return None, "invalid_branch", "branch name must not start with '-'"
    if raw == "HEAD" or raw.startswith("refs/"):
        return None, "invalid_branch", "pass an explicit branch name without 'HEAD' or 'refs/' prefix"
    if raw.startswith("/") or raw.endswith("/") or "//" in raw:
        return None, "invalid_branch", "branch name has empty or leading/trailing slash segments"
    if any(tok in raw for tok in ("..", "@{", "\\", "~", "^", ":", "*", "?", "[", "]")):
        return None, "invalid_branch", "branch name contains forbidden ref or glob characters"
    segments = raw.split("/")
    for seg in segments:
        if not seg or seg in (".", "..") or seg.startswith("-") or seg.endswith(".lock") or seg.endswith("."):
            return None, "invalid_branch", "branch name contains an invalid path segment"
    try:
        chk = _local_git(repo_root, "check-ref-format", f"refs/heads/{raw}")
    except (OSError, subprocess.SubprocessError):
        return None, "invalid_branch", "failed to validate branch ref format"
    if chk.returncode != 0:
        return None, "invalid_branch", "branch name failed git check-ref-format validation"
    return raw, None, None


def validate_expected_sha(raw: object) -> tuple[str | None, str | None, str | None]:
    if not isinstance(raw, str) or not raw:
        return None, "invalid_expect", "expected commit must be a full 40- or 64-character hex SHA"
    if not FULL_SHA_RE.match(raw):
        return None, "invalid_expect", "expected commit must be a full 40- or 64-character hex SHA"
    lowered = raw.lower()
    if set(lowered) == {"0"}:
        return None, "invalid_expect", "expected commit must not be the all-zero null OID"
    return lowered, None, None


def validate_timeout(raw: object) -> tuple[float | None, str | None, str | None]:
    if raw is None:
        return DEFAULT_TIMEOUT_S, None, None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None, "invalid_timeout", "timeout must be a positive number of seconds"
    if isinstance(raw, bool) or not math.isfinite(val) or val <= 0 or val > MAX_TIMEOUT_S:
        return None, "invalid_timeout", f"timeout must be > 0 and <= {int(MAX_TIMEOUT_S)} seconds"
    return val, None, None


def validate_and_sanitize_remote_url(
    raw_url: str,
    repo_root: Path,
) -> tuple[dict | None, str | None, str | None]:
    """Validate configured remote URL and derive a credential-free canonical target hash."""
    if not isinstance(raw_url, str) or not raw_url or _has_control_chars(raw_url):
        return None, "unsafe_remote_url", "configured remote URL is empty or contains control characters"
    if raw_url != raw_url.strip():
        return None, "unsafe_remote_url", "configured remote URL has leading or trailing whitespace"
    if raw_url.startswith("-"):
        return None, "unsafe_remote_option", "option-like configured remote URL is not permitted"

    first_slash = raw_url.find("/")
    prefix_before_slash = raw_url if first_slash < 0 else raw_url[:first_slash]
    if HELPER_RE.match(raw_url) or "::" in prefix_before_slash:
        return None, "unsafe_remote_transport", "unsafe helper/ext/fd remote transport is not permitted"

    scheme_match = SCHEME_RE.match(raw_url)
    if scheme_match:
        if any(ch.isspace() for ch in raw_url):
            return None, "unsafe_remote_url", "whitespace is not permitted in scheme remote URL"
        parsed = urlsplit(raw_url)
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc
        userinfo, has_at, _ = netloc.rpartition("@")

        if scheme in ("ext", "fd"):
            return None, "unsafe_remote_transport", "unsafe ext/fd remote transport is not permitted"

        if scheme == "http":
            if has_at or parsed.username is not None or parsed.password is not None:
                return None, "embedded_credentials", "embedded HTTP credentials are not permitted in remote URL"
            return (
                None,
                "unsafe_remote_transport",
                "plaintext http:// remote transport is not permitted; use https://, ssh://, scp, or local bare remote",
            )

        if scheme == "https":
            if has_at or parsed.username is not None or parsed.password is not None:
                return None, "embedded_credentials", "embedded HTTP credentials are not permitted in remote URL"
            if parsed.query or parsed.fragment:
                return None, "unsafe_remote_url", "query strings and fragments are not permitted in remote URL"
            try:
                port = parsed.port
            except ValueError:
                return None, "unsafe_remote_url", "configured HTTPS remote URL has an invalid port"
            host = parsed.hostname
            if not host or host.startswith("-"):
                return None, "unsafe_remote_url", "configured HTTPS remote URL has an invalid host"
            if not parsed.path or parsed.path == "/" or parsed.path.startswith("/-"):
                return None, "unsafe_remote_url", "configured HTTPS remote URL has an invalid repository path"
            fmt_host = f"[{host.lower()}]" if ":" in host else host.lower()
            port_part = f":{port}" if port and port != 443 else ""
            sanitized = f"https://{fmt_host}{port_part}{parsed.path}"
            kind = "https"

        elif scheme == "ssh":
            if parsed.password is not None or (has_at and ":" in userinfo):
                return None, "embedded_credentials", "embedded password/secret in SSH remote URL is not permitted"
            if parsed.query or parsed.fragment:
                return None, "unsafe_remote_url", "query strings and fragments are not permitted in remote URL"
            try:
                port = parsed.port
            except ValueError:
                return None, "unsafe_remote_url", "configured SSH remote URL has an invalid port"
            user = parsed.username
            if has_at and (not user or user.startswith("-")):
                return None, "unsafe_remote_option", "configured SSH remote URL has an invalid or option-like user"
            host = parsed.hostname
            if not host or host.startswith("-"):
                return None, "unsafe_remote_option", "configured SSH remote URL has an invalid or option-like host"
            if not parsed.path or parsed.path == "/" or parsed.path.startswith("/-"):
                return None, "unsafe_remote_url", "configured SSH remote URL has an invalid repository path"
            user_part = f"{user}@" if user else ""
            fmt_host = f"[{host.lower()}]" if ":" in host else host.lower()
            port_part = f":{port}" if port and port != 22 else ""
            sanitized = f"ssh://{user_part}{fmt_host}{port_part}{parsed.path}"
            kind = "ssh"

        elif scheme == "file":
            if has_at or parsed.username is not None or parsed.password is not None:
                return None, "embedded_credentials", "embedded credentials are not permitted in file:// remote URL"
            if parsed.query or parsed.fragment:
                return None, "unsafe_remote_url", "query strings and fragments are not permitted in file:// remote URL"
            if netloc.lower() not in ("", "localhost"):
                return None, "unsafe_remote_url", "non-local file:// remote host is not permitted"
            local_path = unquote(parsed.path)
            if not local_path or _has_control_chars(local_path) or local_path.startswith("-"):
                return None, "unsafe_remote_url", "configured file:// remote path is invalid"
            try:
                resolved = Path(local_path).resolve().as_posix()
            except OSError:
                return None, "unsafe_remote_url", "configured file:// remote path could not be resolved"
            sanitized = f"file://{resolved}"
            kind = "local"

        else:
            return None, "unsafe_remote_transport", "unsupported or unsafe remote transport scheme"

    elif (
        ":" in raw_url
        and not WINDOWS_DRIVE_RE.match(raw_url)
        and (first_slash < 0 or raw_url.find(":") < first_slash)
    ):
        if any(ch.isspace() for ch in raw_url) or "?" in raw_url or "#" in raw_url:
            return None, "unsafe_remote_url", "configured SCP remote URL contains invalid characters"
        if "@" in prefix_before_slash:
            user, rest = raw_url.split("@", 1)
            if ":" in user:
                return None, "embedded_credentials", "embedded credentials in SCP remote URL are not permitted"
            if not user or "@" in user or user.startswith("-"):
                return None, "unsafe_remote_option", "configured SCP remote URL has an invalid or option-like user"
            rest_slash = rest.find("/")
            if ":" not in rest or (rest_slash >= 0 and rest.find(":") > rest_slash):
                return None, "unsafe_remote_url", "configured SCP remote URL is missing host:path separator"
            host, path_part = rest.split(":", 1)
        else:
            user = ""
            host, path_part = raw_url.split(":", 1)
        if not host or "@" in host or ":" in host or host.startswith("-"):
            return None, "unsafe_remote_option", "configured SCP remote URL has an invalid or option-like host"
        if not path_part or path_part.startswith("-"):
            return None, "unsafe_remote_option", "configured SCP remote URL has an invalid or option-like path"
        user_part = f"{user}@" if user else ""
        sanitized = f"{user_part}{host.lower()}:{path_part}"
        kind = "scp"

    else:
        p = Path(raw_url)
        try:
            resolved = (p if p.is_absolute() else (repo_root / p)).resolve().as_posix()
        except OSError:
            return None, "unsafe_remote_url", "configured local remote path could not be resolved"
        sanitized = f"file://{resolved}"
        kind = "local"

    url_hash = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
    return {"kind": kind, "url_hash": url_hash}, None, None


def resolve_configured_remote(
    repo_root: Path,
    remote_name: str,
) -> tuple[dict | None, str | None, str | None]:
    """Inspect local Git config for a named remote and validate transport safety before any query."""
    try:
        remotes_proc = _local_git(repo_root, "remote")
    except (OSError, subprocess.SubprocessError):
        return None, "unavailable_git", "failed to list configured git remotes"
    if remotes_proc.returncode != 0:
        return None, "unavailable_git", "failed to list configured git remotes"
    configured = {ln.strip() for ln in remotes_proc.stdout.splitlines() if ln.strip()}
    if remote_name not in configured:
        return None, "unconfigured_remote", f"remote '{remote_name}' is not a configured git remote"

    # Reject dangerous per-remote helper/uploadpack/proxy/local-command overrides before transport.
    for key, err_code, msg in (
        (f"remote.{remote_name}.vcs", "unsafe_remote_helper", f"remote '{remote_name}' configures an external vcs helper"),
        (f"remote.{remote_name}.uploadpack", "unsafe_remote_option", f"remote '{remote_name}' configures a custom uploadpack"),
    ):
        chk = _local_git(repo_root, "config", "--get-all", key)
        if chk.returncode == 0 and chk.stdout.strip():
            return None, err_code, msg

    proxy_chk = _local_git(repo_root, "config", "--get-all", f"remote.{remote_name}.proxy")
    if proxy_chk.returncode == 0 and proxy_chk.stdout.strip():
        for line in proxy_chk.stdout.splitlines():
            if "@" in line or line.strip().startswith("-"):
                return None, "embedded_credentials", f"remote '{remote_name}' configures unsafe or credentialed proxy settings"

    for local_key in ("core.sshCommand", "core.gitProxy"):
        local_chk = _local_git(repo_root, "config", "--local", "--get-all", local_key)
        if local_chk.returncode == 0 and local_chk.stdout.strip():
            return (
                None,
                "unsafe_remote_option",
                f"repository local config overrides transport command ({local_key})",
            )

    raw_cfg = _local_git(repo_root, "config", "--get-all", f"remote.{remote_name}.url")
    raw_urls = [ln for ln in raw_cfg.stdout.splitlines() if ln != ""] if raw_cfg.returncode == 0 else []
    if not raw_urls:
        return None, "unconfigured_remote", f"remote '{remote_name}' has no configured URL"
    if len(raw_urls) > 1:
        return None, "ambiguous_remote_url", f"remote '{remote_name}' configures multiple URLs"

    _, err_code, reason = validate_and_sanitize_remote_url(raw_urls[0], repo_root)
    if err_code is not None:
        return None, err_code, reason

    eff_proc = _local_git(repo_root, "remote", "get-url", "--", remote_name)
    eff_urls = [ln for ln in eff_proc.stdout.splitlines() if ln != ""] if eff_proc.returncode == 0 else []
    if len(eff_urls) != 1:
        return None, "unconfigured_remote", f"failed to resolve unique URL for remote '{remote_name}'"

    eff_info, eff_err_code, eff_reason = validate_and_sanitize_remote_url(eff_urls[0], repo_root)
    if eff_err_code is not None or eff_info is None:
        return None, eff_err_code or "unsafe_remote_url", eff_reason or "unsafe resolved remote URL"

    push_binding = "unverified"
    push_url_hashes: list[str] | None = None
    push_kinds: list[str] | None = None
    push_error: str | None = None

    try:
        raw_push_cfg = _local_git(repo_root, "config", "--get-all", f"remote.{remote_name}.pushurl")
        raw_push_urls = (
            [ln for ln in raw_push_cfg.stdout.splitlines() if ln != ""]
            if raw_push_cfg.returncode == 0
            else []
        )
        for rp_url in raw_push_urls:
            _, rp_code, rp_reason = validate_and_sanitize_remote_url(rp_url, repo_root)
            if rp_code is not None:
                push_error = rp_reason or rp_code
                break

        if push_error is None:
            push_proc = _local_git(repo_root, "remote", "get-url", "--push", "--all", "--", remote_name)
            if push_proc.returncode != 0:
                push_error = f"failed to resolve effective push URL(s) for remote '{remote_name}'"
            else:
                eff_push_urls = [ln for ln in push_proc.stdout.splitlines() if ln != ""]
                if not eff_push_urls:
                    push_error = f"no effective push URL resolved for remote '{remote_name}'"
                elif len(eff_push_urls) > MAX_PUSH_URLS:
                    push_error = f"remote '{remote_name}' configures more than {MAX_PUSH_URLS} push URLs"
                else:
                    p_hashes: list[str] = []
                    p_kinds: list[str] = []
                    for ep_url in eff_push_urls:
                        p_info, p_code, p_reason = validate_and_sanitize_remote_url(ep_url, repo_root)
                        if p_code is not None or p_info is None:
                            push_error = p_reason or p_code or "unsafe resolved push URL"
                            break
                        p_hashes.append(p_info["url_hash"])
                        p_kinds.append(p_info["kind"])
                    if push_error is None:
                        push_url_hashes = p_hashes
                        push_kinds = p_kinds
                        if len(p_hashes) > 1:
                            push_binding = "multiple"
                        elif p_hashes[0] == eff_info["url_hash"]:
                            push_binding = "same"
                        else:
                            push_binding = "different"
    except (OSError, subprocess.SubprocessError):
        push_binding = "unverified"
        push_url_hashes = None
        push_kinds = None
        push_error = f"failed to inspect effective push URL(s) for remote '{remote_name}'"

    return {
        "remote": remote_name,
        "kind": eff_info["kind"],
        "url_hash": eff_info["url_hash"],
        "observed_endpoint": "fetch",
        "push_binding": push_binding,
        "push_url_hashes": push_url_hashes,
        "push_kinds": push_kinds,
        "push_error": push_error,
    }, None, None


def compute_target_id(
    remote_name: str,
    remote_url_hash: str,
    ref: str,
    *,
    push_binding: str = "same",
    push_url_hashes: list[str] | None = None,
) -> str:
    push_part = f"{push_binding}:{','.join(push_url_hashes or ())}"
    payload = f"{remote_name}\n{remote_url_hash}\n{ref}\n{push_part}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# ----------------------------------------- storage containment & tail read


def check_storage_containment(repo_root: Path) -> tuple[Path | None, str | None]:
    """Ensure .gate and .gate/publication.ndjson do not traverse symlinks or non-regular files."""
    canonical_root = repo_root.resolve()
    gate_dir = canonical_root / ".gate"
    pub_path = gate_dir / "publication.ndjson"

    if gate_dir.is_symlink():
        return None, "refusing symlink at .gate"
    if os.path.lexists(gate_dir):
        if not gate_dir.is_dir():
            return None, "refusing non-directory at .gate"
        try:
            if gate_dir.resolve() != gate_dir:
                return None, "refusing .gate path resolving outside repository"
        except OSError:
            return None, "failed to resolve .gate directory"

    if pub_path.is_symlink():
        return None, "refusing symlink at .gate/publication.ndjson"
    if os.path.lexists(pub_path):
        if not pub_path.is_file():
            return None, "refusing non-regular file at .gate/publication.ndjson"
        try:
            if pub_path.resolve() != pub_path:
                return None, "refusing .gate/publication.ndjson resolving outside .gate"
        except OSError:
            return None, "failed to resolve .gate/publication.ndjson"

    return pub_path, None


def _append_receipt(repo_root: Path, receipt: dict) -> tuple[bool, str | None]:
    pub_path, err = check_storage_containment(repo_root)
    if err is not None or pub_path is None:
        return False, err or "unsafe receipt storage"

    gate_dir = pub_path.parent
    try:
        if not os.path.lexists(gate_dir):
            gate_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False, "failed to create .gate directory"

    pub_path, err = check_storage_containment(repo_root)
    if err is not None or pub_path is None:
        return False, err or "unsafe receipt storage"

    encoded = (json.dumps(receipt, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_RECEIPT_BYTES:
        return False, f"publication receipt exceeds {MAX_RECEIPT_BYTES} byte cap"

    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(pub_path), flags, 0o644)
    except OSError as exc:
        if exc.errno == getattr(errno, "ELOOP", None):
            return False, "refusing symlink at .gate/publication.ndjson"
        return False, "failed to open .gate/publication.ndjson for append"

    try:
        fd_stat = os.fstat(fd)
        path_stat = os.lstat(pub_path)
        if not stat.S_ISREG(fd_stat.st_mode) or not os.path.samestat(fd_stat, path_stat):
            return False, "refusing unsafe or replaced file at .gate/publication.ndjson"
        view = memoryview(encoded)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    except OSError:
        return False, "failed to write publication receipt"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return True, None


def _is_valid_receipt_record(obj: object) -> bool:
    if not isinstance(obj, dict):
        return False
    if not isinstance(obj.get("id"), str) or not obj["id"].strip():
        return False
    if obj.get("status") not in STATUS_EXIT_CODES:
        return False
    if not isinstance(obj.get("target"), dict):
        return False
    return True


def read_publication_tail(repo_root: Path, *, max_bytes: int | None = None) -> dict:
    """Bounded read-only tail reader for .gate/publication.ndjson with symlink refusal."""
    limit = PUBLICATION_TAIL_BYTES if max_bytes is None else max(1, int(max_bytes))
    pub_path, err = check_storage_containment(repo_root)
    if err is not None or pub_path is None:
        return {
            "exists": False,
            "size": 0,
            "records": [],
            "malformed_records": 0,
            "window_truncated": False,
            "unterminated_record": False,
            "unsafe_storage": True,
            "storage_error": err or "unsafe receipt storage",
        }
    if not os.path.lexists(pub_path):
        return {
            "exists": False,
            "size": 0,
            "records": [],
            "malformed_records": 0,
            "window_truncated": False,
            "unterminated_record": False,
            "unsafe_storage": False,
            "storage_error": None,
        }

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(pub_path), flags)
    except OSError as exc:
        is_loop = exc.errno == getattr(errno, "ELOOP", None)
        return {
            "exists": False,
            "size": 0,
            "records": [],
            "malformed_records": 0,
            "window_truncated": False,
            "unterminated_record": False,
            "unsafe_storage": is_loop,
            "storage_error": "refusing symlink at .gate/publication.ndjson" if is_loop else "cannot read .gate/publication.ndjson",
        }

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return {
                "exists": False,
                "size": 0,
                "records": [],
                "malformed_records": 0,
                "window_truncated": False,
                "unterminated_record": False,
                "unsafe_storage": True,
                "storage_error": "refusing non-regular file at .gate/publication.ndjson",
            }
        size = st.st_size
        with os.fdopen(fd, "rb") as stream:
            start = max(0, size - limit)
            stream.seek(start)
            chunk = stream.read()
    except OSError:
        return {
            "exists": False,
            "size": 0,
            "records": [],
            "malformed_records": 0,
            "window_truncated": False,
            "unterminated_record": False,
            "unsafe_storage": False,
            "storage_error": "failed reading .gate/publication.ndjson",
        }

    window_truncated = start > 0
    unterminated_record = bool(chunk) and not chunk.endswith(b"\n")
    lines = chunk.split(b"\n")
    if window_truncated and lines:
        lines = lines[1:]
    if unterminated_record and lines:
        lines.pop()

    records: list[dict] = []
    malformed = 0
    for raw in lines:
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw.decode("utf-8"), parse_constant=_no_json_constants)
            if not _is_valid_receipt_record(rec):
                raise ValueError("invalid receipt schema")
            records.append(rec)
        except (ValueError, UnicodeDecodeError):
            malformed += 1

    return {
        "exists": True,
        "size": size,
        "records": records,
        "malformed_records": malformed,
        "window_truncated": window_truncated,
        "unterminated_record": unterminated_record,
        "unsafe_storage": False,
        "storage_error": None,
    }


# ------------------------------------ local acceptance & candidate state


def _load_repo_config(repo_root: Path) -> tuple[bool, bool, dict]:
    gm = _gate()
    cfg = dict(gm.DEFAULT_CONFIG)
    cfg_path = repo_root / ".gate" / "config.json"
    if cfg_path.is_symlink() or not cfg_path.is_file():
        return False, True, cfg
    try:
        loaded = json.loads(cfg_path.read_text(encoding="utf-8"), parse_constant=_no_json_constants)
        if not isinstance(loaded, dict):
            raise ValueError("not an object")
        cfg.update(loaded)
        return True, True, cfg
    except (OSError, ValueError):
        return True, False, cfg


def _compute_tree_clean(repo_root: Path, cfg: dict, context_rel: str | None = None) -> bool:
    prefix = (cfg.get("project_prefix") or "").strip("/")
    if context_rel is None:
        raw_ctx = cfg.get("context_file") or "CONTEXT.md"
        ctx_candidate = Path(raw_ctx)
        if not ctx_candidate.is_absolute() and ".." not in ctx_candidate.parts:
            proj_dir = (repo_root / prefix).resolve() if prefix else repo_root
            if proj_dir.is_relative_to(repo_root):
                ctx_path = proj_dir / ctx_candidate
                if ctx_path.resolve().is_relative_to(proj_dir):
                    context_rel = ctx_path.relative_to(repo_root).as_posix()
    pathspec = [prefix] if prefix else []
    proc = _local_git(
        repo_root,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--",
        *pathspec,
    )
    if proc.returncode != 0:
        return False
    for entry in proc.stdout.split("\0"):
        if len(entry) < 4:
            continue
        path = entry[3:]
        if path.startswith(".gate/") or path == context_rel or path.endswith(".flow-lock"):
            continue
        return False
    return True


def evaluate_local_state(
    repo_root: Path,
    *,
    expected_sha: str | None,
    cfg: dict | None = None,
    onboarded: bool | None = None,
    config_valid: bool = True,
    head_sha: str | None = None,
    tree_clean: bool | None = None,
    context_rel: str | None = None,
) -> dict:
    """Inspect local Git HEAD, candidate cleanliness, and .gate/verdict.json delivery freshness."""
    if cfg is None or onboarded is None:
        onboarded, config_valid, cfg = _load_repo_config(repo_root)

    if head_sha is None:
        head_proc = _local_git(repo_root, "rev-parse", "--verify", "HEAD")
        raw_head = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
        head_sha = raw_head.lower() if FULL_SHA_RE.match(raw_head) else None

    if tree_clean is None:
        tree_clean = _compute_tree_clean(repo_root, cfg, context_rel=context_rel)

    current_fp: str | None = None
    if onboarded and config_valid:
        try:
            current_fp = _gate().candidate_fingerprint(repo_root, cfg)
        except (SystemExit, Exception):  # noqa: BLE001
            current_fp = None

    vpath = repo_root / ".gate" / "verdict.json"
    verdict_exists = False
    verdict_readable = False
    v_raw: dict | None = None
    if vpath.is_symlink():
        verdict_exists = True
        verdict_readable = False
    elif vpath.is_file():
        verdict_exists = True
        try:
            parsed_v = json.loads(vpath.read_text(encoding="utf-8"), parse_constant=_no_json_constants)
            if isinstance(parsed_v, dict):
                v_raw = parsed_v
                verdict_readable = True
        except (OSError, ValueError):
            verdict_readable = False

    verdict_summary = {
        "exists": verdict_exists,
        "readable": verdict_readable,
        "result": v_raw.get("result") if v_raw else None,
        "scope": v_raw.get("scope") if v_raw else None,
        "stage": v_raw.get("stage") if v_raw else None,
        "task": v_raw.get("task") if v_raw else None,
        "full_suite": v_raw.get("full_suite") if v_raw else None,
        "head": str(v_raw["head"]).lower() if v_raw and isinstance(v_raw.get("head"), str) and v_raw.get("head") else None,
        "candidate_fingerprint": v_raw.get("candidate_fingerprint") if v_raw and v_raw.get("candidate_fingerprint") else None,
        "fingerprint": v_raw.get("fingerprint") if v_raw and v_raw.get("fingerprint") else None,
        "at": v_raw.get("at") if v_raw else None,
    }

    issues: list[str] = []
    coverage_issues: list[str] = []
    head_issues: list[str] = []

    if not onboarded:
        coverage_issues.append("project is not onboarded (.gate/config.json missing)")
    elif not config_valid:
        coverage_issues.append(".gate/config.json is unreadable")

    if not verdict_exists:
        coverage_issues.append("missing .gate/verdict.json")
    elif not verdict_readable or v_raw is None:
        coverage_issues.append(".gate/verdict.json is unreadable")
    else:
        v_res = v_raw.get("result")
        v_scope = v_raw.get("scope")
        v_stage = v_raw.get("stage")
        v_task = v_raw.get("task")
        v_full_suite = v_raw.get("full_suite")
        v_unverified = v_raw.get("unverified")
        v_cmd = str(v_raw.get("cmd") or "").strip()
        cfg_cmd = str(cfg.get("verify_cmd") or "").strip()

        if v_res != "PASS":
            coverage_issues.append(f"verdict result is {v_res or 'missing'}, not PASS")
        if v_scope == "task" or v_task:
            coverage_issues.append(f"task-scoped acceptance ({v_task or 'task'}) does not cover full delivery")
        elif v_scope != "queue":
            coverage_issues.append(f"acceptance scope '{v_scope or 'missing'}' does not cover full delivery")
        if v_full_suite is False or v_unverified:
            coverage_issues.append("partial stage acceptance does not cover full delivery")
        if v_stage not in (None, "", "delivery"):
            coverage_issues.append(f"stage '{v_stage}' acceptance does not cover delivery stage")
        if v_cmd and cfg_cmd and v_cmd != cfg_cmd:
            coverage_issues.append("verdict command does not match configured verify_cmd")

        v_head = verdict_summary["head"]
        if not head_sha:
            head_issues.append("repository has no current HEAD commit")
        elif not v_head:
            head_issues.append("verdict does not record measured HEAD")
        elif v_head != head_sha:
            head_issues.append(f"verdict measured {v_head[:7]}, current HEAD is {head_sha[:7]}")

        v_cand_fp = verdict_summary["candidate_fingerprint"]
        if not v_cand_fp:
            head_issues.append("verdict has no candidate_fingerprint for full candidate coverage")
        elif current_fp is None or v_cand_fp != current_fp:
            head_issues.append("project content changed since acceptance check")

    if not tree_clean:
        head_issues.append("working tree has uncommitted candidate changes")

    covers_delivery = bool(onboarded and config_valid and verdict_readable and not coverage_issues)
    fresh_for_head = bool(onboarded and config_valid and verdict_readable and not head_issues)

    for msg in (*coverage_issues, *head_issues):
        if msg not in issues:
            issues.append(msg)

    matches_expected = False
    if expected_sha is None:
        issues.append("expected SHA is missing or invalid")
    else:
        exp_issues: list[str] = []
        if head_sha != expected_sha:
            exp_issues.append(f"current HEAD {(head_sha or 'none')[:7]} differs from expected {expected_sha[:7]}")
        v_head = verdict_summary["head"]
        if not verdict_readable or not v_head or v_head != expected_sha:
            exp_issues.append(f"verdict HEAD {(v_head or 'none')[:7]} does not cover expected {expected_sha[:7]}")
        matches_expected = len(exp_issues) == 0
        for msg in exp_issues:
            if msg not in issues:
                issues.append(msg)

    fresh_delivery = bool(covers_delivery and fresh_for_head and matches_expected)
    return {
        "head_sha": head_sha,
        "tree_clean": tree_clean,
        "candidate_fingerprint": current_fp,
        "verdict": verdict_summary,
        "acceptance": {
            "onboarded": bool(onboarded),
            "covers_delivery": covers_delivery,
            "fresh_for_head": fresh_for_head,
            "matches_expected": matches_expected,
            "fresh_delivery": fresh_delivery,
            "issues": issues,
        },
    }


# ------------------------------------------------ transport & parsing


TransportRunner = Callable[[list[str], Path, dict[str, str], float], subprocess.CompletedProcess[str]]


def _default_transport_runner(
    cmd: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout_s: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )


def _classify_ls_remote_failure(returncode: int, stderr: str) -> tuple[str, str]:
    """Map non-zero ls-remote failure to a static category without emitting raw stderr or secrets."""
    low = (stderr or "").lower()
    if any(
        tok in low
        for tok in (
            "terminal prompts disabled",
            "could not read username",
            "could not read password",
            "authentication failed",
            "access denied",
            "permission denied",
            "publickey",
            "http basic",
            "401",
            "403",
        )
    ):
        return (
            "auth_unavailable",
            f"remote authentication unavailable or prompts disabled (exit {returncode})",
        )
    if any(
        tok in low
        for tok in (
            "does not appear to be a git repository",
            "repository not found",
            "could not resolve host",
            "unable to access",
            "connection refused",
            "network is unreachable",
            "no route to host",
        )
    ):
        return "remote_inaccessible", f"remote target inaccessible (exit {returncode})"
    return "ls_remote_failed", f"git ls-remote failed (exit {returncode})"


def parse_ls_remote_exact_branch(
    stdout: str,
    branch: str,
    expected_sha: str,
) -> tuple[str, str | None, str | None, str]:
    """Parse git ls-remote output strictly for exact refs/heads/<branch>.

    Returns (status, observed_sha, error_code, reason).
    """
    if "\x00" in stdout:
        return "unavailable", None, "malformed_output", "malformed git ls-remote output (NUL byte)"

    target_ref = f"refs/heads/{branch}"
    peeled_ref = f"{target_ref}^{{}}"

    if not stdout:
        return "absent", None, "ref_absent", f"remote ref {target_ref} is absent"

    raw_lines = stdout.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()

    matched_shas: list[str] = []
    for raw_line in raw_lines:
        line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
        if not line:
            return "unavailable", None, "malformed_output", "malformed git ls-remote output (empty record line)"
        m = LS_REMOTE_LINE_RE.match(line)
        if not m:
            return "unavailable", None, "malformed_output", "malformed git ls-remote output line"
        sha_raw, ref_name = m.groups()
        sha_low = sha_raw.lower()
        if set(sha_low) == {"0"}:
            return "unavailable", None, "malformed_output", "malformed git ls-remote output (null OID)"
        if ref_name == peeled_ref:
            return "unavailable", None, "ambiguous_ref", f"ambiguous peeled ref returned for {target_ref}"
        if ref_name == target_ref:
            matched_shas.append(sha_low)

    if not matched_shas:
        return "absent", None, "ref_absent", f"remote ref {target_ref} is absent"
    if len(matched_shas) > 1:
        return (
            "unavailable",
            None,
            "duplicate_ref",
            f"ambiguous git ls-remote response: {len(matched_shas)} rows for {target_ref}",
        )

    observed = matched_shas[0]
    if observed == expected_sha:
        return "match", observed, None, f"remote {target_ref} matches expected commit {expected_sha[:12]}"
    return (
        "mismatch",
        observed,
        "sha_mismatch",
        f"remote {target_ref} is at {observed[:12]}, expected {expected_sha[:12]}",
    )


def verify_publication(
    repo: str | Path,
    *,
    remote: object,
    branch: object,
    expect: object,
    timeout_s: object = DEFAULT_TIMEOUT_S,
    transport_runner: TransportRunner | None = None,
) -> dict:
    """Verify exact remote branch SHA and append a bounded receipt under .gate/publication.ndjson."""
    now_utc = _utc_now()
    attempt_id = str(uuid.uuid4())

    repo_root, repo_err_code, repo_err_reason = resolve_repo_root(str(repo) if isinstance(repo, Path) else repo)
    if repo_root is None:
        return {
            "version": 1,
            "id": attempt_id,
            "at": now_utc,
            "status": "invalid",
            "exit_code": STATUS_EXIT_CODES["invalid"],
            "error_code": repo_err_code or "invalid_repo",
            "reason": repo_err_reason or "invalid repository",
            "target": {
                "remote": None,
                "branch": None,
                "ref": None,
                "observed_endpoint": None,
                "remote_kind": None,
                "remote_url_hash": None,
                "push_binding": None,
                "push_url_hashes": None,
                "push_kinds": None,
                "push_differs": None,
                "push_error": None,
                "target_id": None,
            },
            "expected_sha": None,
            "observed_sha": None,
            "head_sha": None,
            "tree_clean": False,
            "candidate_fingerprint": None,
            "verdict": None,
            "acceptance": None,
            "authority_note": AUTHORITY_NOTE,
            "receipt_written": False,
            "receipt_path": RECEIPT_REL_PATH,
        }

    # Check receipt storage containment before any transport or write.
    _, storage_err = check_storage_containment(repo_root)
    clean_remote, rem_name_code, rem_name_reason = validate_remote_name(remote)
    clean_branch, branch_code, branch_reason = validate_branch_name(repo_root, branch)
    clean_expect, expect_code, expect_reason = validate_expected_sha(expect)
    clean_timeout, timeout_code, timeout_reason = validate_timeout(timeout_s)

    local_state = evaluate_local_state(repo_root, expected_sha=clean_expect)

    if storage_err is not None:
        return {
            "version": 1,
            "id": attempt_id,
            "at": now_utc,
            "status": "invalid",
            "exit_code": STATUS_EXIT_CODES["invalid"],
            "error_code": "unsafe_storage",
            "reason": storage_err,
            "target": {
                "remote": clean_remote,
                "branch": clean_branch,
                "ref": f"refs/heads/{clean_branch}" if clean_branch else None,
                "observed_endpoint": None,
                "remote_kind": None,
                "remote_url_hash": None,
                "push_binding": None,
                "push_url_hashes": None,
                "push_kinds": None,
                "push_differs": None,
                "push_error": None,
                "target_id": None,
            },
            "expected_sha": clean_expect,
            "observed_sha": None,
            "head_sha": local_state["head_sha"],
            "tree_clean": local_state["tree_clean"],
            "candidate_fingerprint": local_state["candidate_fingerprint"],
            "verdict": local_state["verdict"],
            "acceptance": local_state["acceptance"],
            "authority_note": AUTHORITY_NOTE,
            "receipt_written": False,
            "receipt_path": RECEIPT_REL_PATH,
        }

    remote_info: dict | None = None
    rem_cfg_code: str | None = None
    rem_cfg_reason: str | None = None
    if clean_remote is not None:
        remote_info, rem_cfg_code, rem_cfg_reason = resolve_configured_remote(repo_root, clean_remote)

    ref_name = f"refs/heads/{clean_branch}" if clean_branch else None
    observed_endpoint = remote_info["observed_endpoint"] if remote_info else None
    remote_kind = remote_info["kind"] if remote_info else None
    remote_url_hash = remote_info["url_hash"] if remote_info else None
    push_binding = remote_info["push_binding"] if remote_info else None
    push_url_hashes = remote_info["push_url_hashes"] if remote_info else None
    push_kinds = remote_info["push_kinds"] if remote_info else None
    push_differs = (push_binding != "same") if push_binding is not None else None
    push_error = remote_info["push_error"] if remote_info else None
    target_id = (
        compute_target_id(
            clean_remote,
            remote_url_hash,
            ref_name,
            push_binding=push_binding or "unverified",
            push_url_hashes=push_url_hashes,
        )
        if clean_remote and remote_url_hash and ref_name
        else None
    )

    first_validation_error: tuple[str, str] | None = None
    for code, msg in (
        (rem_name_code, rem_name_reason),
        (rem_cfg_code, rem_cfg_reason),
        (branch_code, branch_reason),
        (expect_code, expect_reason),
        (timeout_code, timeout_reason),
    ):
        if code is not None:
            first_validation_error = (code, msg or code)
            break

    status: str
    observed_sha: str | None = None
    error_code: str | None = None
    reason: str

    if first_validation_error is not None:
        error_code, reason = first_validation_error
        if error_code == "unavailable_git":
            status = "unavailable"
        else:
            status = "invalid"
    else:
        assert clean_remote is not None
        assert clean_branch is not None
        assert clean_expect is not None
        assert clean_timeout is not None
        assert ref_name is not None

        cmd = [
            "git",
            "-C",
            str(repo_root),
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "credential.interactive=never",
            "-c",
            "core.askPass=",
            "ls-remote",
            "--",
            clean_remote,
            ref_name,
        ]
        safe_env = dict(os.environ)
        safe_env["GIT_TERMINAL_PROMPT"] = "0"
        safe_env["GCM_INTERACTIVE"] = "never"
        safe_env["GIT_ASKPASS"] = ""
        safe_env["SSH_ASKPASS"] = ""
        safe_env["SSH_ASKPASS_REQUIRE"] = "never"

        runner = _default_transport_runner if transport_runner is None else transport_runner
        try:
            proc = runner(cmd, repo_root, safe_env, clean_timeout)
        except subprocess.TimeoutExpired:
            status = "unavailable"
            observed_sha = None
            error_code = "timeout"
            reason = f"remote query timed out after {clean_timeout:g}s"
        except OSError:
            status = "unavailable"
            observed_sha = None
            error_code = "transport_exec_error"
            reason = "failed to execute git ls-remote"
        else:
            if proc.returncode != 0:
                status = "unavailable"
                observed_sha = None
                error_code, reason = _classify_ls_remote_failure(proc.returncode, proc.stderr or "")
            else:
                status, observed_sha, error_code, reason = parse_ls_remote_exact_branch(
                    proc.stdout or "",
                    clean_branch,
                    clean_expect,
                )

        if push_binding == "different" and push_url_hashes:
            reason = (
                f"{reason}; observed fetch endpoint only "
                f"(effective push endpoint differs: push_sha256={push_url_hashes[0][:12]}; push target not verified)"
            )
        elif push_binding == "multiple" and push_url_hashes is not None:
            reason = (
                f"{reason}; observed fetch endpoint only "
                f"({len(push_url_hashes)} effective push endpoints configured; push targets not verified)"
            )
        elif push_binding == "unverified":
            reason = (
                f"{reason}; observed fetch endpoint only "
                f"(effective push endpoint binding unverified: {push_error or 'unknown'}; push target not verified)"
            )

    exit_code = STATUS_EXIT_CODES[status]
    receipt = {
        "version": 1,
        "id": attempt_id,
        "at": now_utc,
        "status": status,
        "exit_code": exit_code,
        "error_code": error_code,
        "reason": reason,
        "target": {
            "remote": clean_remote,
            "branch": clean_branch,
            "ref": ref_name,
            "observed_endpoint": observed_endpoint,
            "remote_kind": remote_kind,
            "remote_url_hash": remote_url_hash,
            "push_binding": push_binding,
            "push_url_hashes": push_url_hashes,
            "push_kinds": push_kinds,
            "push_differs": push_differs,
            "push_error": push_error,
            "target_id": target_id,
        },
        "expected_sha": clean_expect,
        "observed_sha": observed_sha,
        "head_sha": local_state["head_sha"],
        "tree_clean": local_state["tree_clean"],
        "candidate_fingerprint": local_state["candidate_fingerprint"],
        "verdict": local_state["verdict"],
        "acceptance": local_state["acceptance"],
        "authority_note": AUTHORITY_NOTE,
    }

    written, write_err = _append_receipt(repo_root, receipt)
    if not written:
        # Storage containment or write failure after validation: never claim verified match.
        is_symlink_err = bool(write_err and "symlink" in write_err)
        fail_status = "invalid" if is_symlink_err else "unavailable"
        return {
            **receipt,
            "status": fail_status,
            "exit_code": STATUS_EXIT_CODES[fail_status],
            "error_code": "unsafe_storage" if is_symlink_err else "receipt_write_failed",
            "reason": write_err or "failed to persist publication receipt",
            "receipt_written": False,
            "receipt_path": RECEIPT_REL_PATH,
        }

    return {
        **receipt,
        "receipt_written": True,
        "receipt_path": RECEIPT_REL_PATH,
    }


# ----------------------------------------- resume projection & rendering


def _project_receipt_against_current(
    rec: dict,
    *,
    configured_remotes: dict[str, tuple[dict | None, str | None, str | None]],
    current_head: str | None,
    tree_clean: bool,
    current_fp: str | None,
    current_local_acceptance: dict,
    superseded_reason: str | None = None,
) -> dict:
    target = rec.get("target") or {}
    remote_name = target.get("remote") if isinstance(target.get("remote"), str) else None
    branch_name = target.get("branch") if isinstance(target.get("branch"), str) else None
    ref_name = target.get("ref") if isinstance(target.get("ref"), str) else None
    recorded_url_hash = target.get("remote_url_hash") if isinstance(target.get("remote_url_hash"), str) else None
    target_id = target.get("target_id") if isinstance(target.get("target_id"), str) else None

    raw_p_bind = target.get("push_binding")
    raw_p_hashes = target.get("push_url_hashes")
    valid_p_hashes = (
        isinstance(raw_p_hashes, list)
        and bool(raw_p_hashes)
        and all(isinstance(h, str) and bool(FULL_SHA_RE.match(h)) for h in raw_p_hashes)
    )
    recorded_push_hashes = [h.lower() for h in raw_p_hashes] if valid_p_hashes else None
    raw_p_kinds = target.get("push_kinds")
    recorded_push_kinds = (
        list(raw_p_kinds)
        if isinstance(raw_p_kinds, list) and all(isinstance(k, str) for k in raw_p_kinds)
        else None
    )
    recorded_push_error = target.get("push_error") if isinstance(target.get("push_error"), str) else None

    if raw_p_bind in ("same", "different", "multiple", "unverified"):
        recorded_push_binding: str | None = raw_p_bind
    elif raw_p_bind is None and recorded_url_hash is not None:
        recorded_push_binding = "unknown"
    elif raw_p_bind is None:
        recorded_push_binding = None
    else:
        recorded_push_binding = "unverified"

    recorded_observed_endpoint = (
        target.get("observed_endpoint")
        if isinstance(target.get("observed_endpoint"), str)
        else ("fetch" if recorded_url_hash is not None else None)
    )
    recorded_push_differs = (
        False
        if recorded_push_binding == "same"
        else (True if recorded_push_binding in ("different", "multiple", "unverified") else None)
    )

    expected_sha = rec.get("expected_sha") if isinstance(rec.get("expected_sha"), str) else None
    observed_sha = rec.get("observed_sha") if isinstance(rec.get("observed_sha"), str) else None
    recorded_head = rec.get("head_sha") if isinstance(rec.get("head_sha"), str) else None
    recorded_fp = rec.get("candidate_fingerprint") if isinstance(rec.get("candidate_fingerprint"), str) else None
    status = str(rec.get("status") or "unavailable")

    stale_reasons: list[str] = []
    remote_drift = False
    head_drift = False
    dirty_candidate = False
    current_push_binding: str | None = None
    current_push_hashes: list[str] | None = None

    if remote_name and recorded_url_hash:
        rem_info, _, rem_reason = configured_remotes[remote_name]
        if rem_info is None:
            remote_drift = True
            stale_reasons.append(rem_reason or f"remote '{remote_name}' is no longer valid")
        else:
            current_push_binding = rem_info.get("push_binding")
            current_push_hashes = rem_info.get("push_url_hashes")
            if rem_info["url_hash"] != recorded_url_hash:
                remote_drift = True
                stale_reasons.append(f"configured URL for remote '{remote_name}' changed since receipt")
            elif recorded_push_binding in ("same", "different", "multiple"):
                if (
                    current_push_binding != recorded_push_binding
                    or current_push_hashes != recorded_push_hashes
                ):
                    remote_drift = True
                    if current_push_binding == "unverified":
                        stale_reasons.append(
                            f"effective push URL binding for remote '{remote_name}' is unverified"
                            + (f" ({rem_info.get('push_error')})" if rem_info.get("push_error") else "")
                        )
                    else:
                        stale_reasons.append(
                            f"configured push URL for remote '{remote_name}' changed since receipt"
                        )
            elif recorded_push_binding == "unverified":
                if current_push_binding != "unverified":
                    remote_drift = True
                    stale_reasons.append(
                        f"configured push URL for remote '{remote_name}' changed since receipt"
                    )
    elif remote_name:
        remote_drift = True
        stale_reasons.append(f"receipt for remote '{remote_name}' has no valid remote URL binding")

    if superseded_reason:
        remote_drift = True
        stale_reasons.append(superseded_reason)

    if not current_head:
        head_drift = True
        stale_reasons.append("repository has no current HEAD commit")
    else:
        if recorded_head != current_head:
            head_drift = True
            stale_reasons.append(f"HEAD moved from {(recorded_head or 'none')[:7]} to {current_head[:7]}")
        if expected_sha and expected_sha != current_head:
            head_drift = True
            stale_reasons.append(f"receipt expected {expected_sha[:7]}, current HEAD is {current_head[:7]}")
        if status == "match" and observed_sha != current_head:
            head_drift = True
            stale_reasons.append(f"observed remote SHA {(observed_sha or 'none')[:7]} differs from current HEAD {current_head[:7]}")

    if not tree_clean:
        dirty_candidate = True
        stale_reasons.append("working tree has uncommitted candidate changes")
    if recorded_fp and current_fp and recorded_fp != current_fp:
        dirty_candidate = True
        stale_reasons.append("project content changed since receipt")

    has_valid_same_push_binding = bool(
        recorded_observed_endpoint == "fetch"
        and recorded_push_binding == "same"
        and target.get("push_differs") is not True
        and recorded_push_hashes is not None
        and len(recorded_push_hashes) == 1
        and recorded_push_hashes[0] == recorded_url_hash
    )

    push_disclosure: str | None = None
    if recorded_push_binding == "different":
        ph = recorded_push_hashes[0][:12] if recorded_push_hashes else "unknown"
        push_disclosure = f"observed fetch endpoint only; effective push endpoint differs (push_sha256={ph})"
    elif recorded_push_binding == "multiple":
        n_p = len(recorded_push_hashes or [])
        push_disclosure = f"observed fetch endpoint only; {n_p} effective push endpoints configured"
    elif recorded_push_binding == "unverified":
        push_disclosure = (
            "observed fetch endpoint only; effective push endpoint binding unverified"
            + (f" ({recorded_push_error})" if recorded_push_error else "")
        )
    elif recorded_push_binding == "unknown":
        push_disclosure = "legacy receipt lacks effective push endpoint binding (unknown)"

    stale = bool(remote_drift or head_drift or dirty_candidate)
    current_delivery_verified = bool(
        status == "match"
        and target_id is not None
        and has_valid_same_push_binding
        and not stale
        and current_local_acceptance["fresh_delivery"]
        and observed_sha is not None
        and observed_sha == current_head
        and expected_sha == current_head
    )

    return {
        "target": {
            "remote": remote_name,
            "branch": branch_name,
            "ref": ref_name,
            "observed_endpoint": recorded_observed_endpoint,
            "remote_kind": target.get("remote_kind"),
            "remote_url_hash": recorded_url_hash,
            "push_binding": recorded_push_binding,
            "push_url_hashes": recorded_push_hashes,
            "push_kinds": recorded_push_kinds,
            "push_differs": recorded_push_differs,
            "push_error": recorded_push_error,
            "current_push_binding": current_push_binding,
            "current_push_url_hashes": current_push_hashes,
            "target_id": target_id,
        },
        "observed": {
            "receipt_id": rec.get("id"),
            "at": _valid_timestamp(rec.get("at")),
            "status": status,
            "error_code": rec.get("error_code"),
            "reason": rec.get("reason"),
            "expected_sha": expected_sha,
            "observed_sha": observed_sha,
            "head_at_observation": recorded_head,
        },
        "recorded_acceptance": rec.get("acceptance"),
        "local_acceptance": current_local_acceptance,
        "push_disclosure": push_disclosure,
        "stale": stale,
        "remote_drift": remote_drift,
        "head_drift": head_drift,
        "dirty_candidate": dirty_candidate,
        "stale_reasons": stale_reasons,
        "current_delivery_verified": current_delivery_verified,
    }


def build_publication_summary(
    repo_root: Path,
    *,
    cfg: dict | None = None,
    onboarded: bool | None = None,
    config_valid: bool = True,
    head_sha: str | None = None,
    tree_clean: bool | None = None,
    context_rel: str | None = None,
    max_bytes: int | None = None,
) -> dict:
    """Read bounded receipts and separate observed remote facts at time T from current local acceptance."""
    tail = read_publication_tail(repo_root, max_bytes=max_bytes)
    warnings: list[str] = []
    problems: list[str] = []

    if tail["unsafe_storage"] or tail["storage_error"]:
        msg = tail["storage_error"] or ".gate/publication.ndjson unreadable"
        problems.append(msg)
        warnings.append(msg)
    if tail["window_truncated"]:
        warnings.append(
            f"publication history truncated to last {max_bytes or PUBLICATION_TAIL_BYTES} bytes; older attempts omitted"
        )
    if tail["unterminated_record"]:
        warnings.append("publication receipt log ends with an incomplete/truncated record")
    if tail["malformed_records"]:
        warnings.append(f"{tail['malformed_records']} malformed publication receipt(s) skipped")

    integrity = {
        "ok": not warnings,
        "malformed_records": tail["malformed_records"],
        "window_truncated": tail["window_truncated"],
        "unterminated_record": tail["unterminated_record"],
        "unsafe_storage": tail["unsafe_storage"],
        "warnings": warnings,
    }

    records = tail["records"]
    if not records:
        return {
            "exists": tail["exists"],
            "total_attempts_in_tail": 0,
            "latest": None,
            "targets": [],
            "problems": problems,
            "integrity": integrity,
            "authority_note": AUTHORITY_NOTE,
        }

    local = evaluate_local_state(
        repo_root,
        expected_sha=head_sha,
        cfg=cfg,
        onboarded=onboarded,
        config_valid=config_valid,
        head_sha=head_sha,
        tree_clean=tree_clean,
        context_rel=context_rel,
    )
    eff_head = local["head_sha"]
    if head_sha is None and eff_head is not None:
        # Re-evaluate matches_expected against actual HEAD when caller did not pre-supply head_sha.
        local = evaluate_local_state(
            repo_root,
            expected_sha=eff_head,
            cfg=cfg,
            onboarded=onboarded,
            config_valid=config_valid,
            head_sha=eff_head,
            tree_clean=local["tree_clean"],
            context_rel=context_rel,
        )

    configured_remotes: dict[str, tuple[dict | None, str | None, str | None]] = {}
    by_target_id: dict[str, tuple[int, dict]] = {}
    latest_by_remote_ref: dict[tuple[str, str], tuple[int, str | None]] = {}

    for idx, rec in enumerate(records):
        target = rec.get("target") or {}
        rem_name = target.get("remote") if isinstance(target.get("remote"), str) else None
        ref_name = target.get("ref") if isinstance(target.get("ref"), str) else None
        tid = target.get("target_id") if isinstance(target.get("target_id"), str) and target.get("target_id") else None

        if rem_name and rem_name not in configured_remotes:
            valid_rem, r_code, r_reason = validate_remote_name(rem_name)
            if valid_rem is None:
                configured_remotes[rem_name] = (None, r_code, r_reason)
            else:
                configured_remotes[rem_name] = resolve_configured_remote(repo_root, valid_rem)

        if rem_name and ref_name:
            latest_by_remote_ref[(rem_name, ref_name)] = (idx, tid)
        if tid:
            by_target_id[tid] = (idx, rec)

    ordered_targets = sorted(by_target_id.values(), key=lambda pair: -pair[0])[:MAX_TARGETS_IN_SUMMARY]
    projected_by_tid: dict[str, dict] = {}
    projected_targets: list[dict] = []

    for idx, rec in ordered_targets:
        target = rec.get("target") or {}
        rem_name = target.get("remote") if isinstance(target.get("remote"), str) else None
        ref_name = target.get("ref") if isinstance(target.get("ref"), str) else None
        tid = str(target["target_id"])
        superseded: str | None = None
        if rem_name and ref_name:
            newest_idx, newest_tid = latest_by_remote_ref[(rem_name, ref_name)]
            if newest_idx > idx and newest_tid != tid:
                superseded = f"superseded by a newer attempt for {rem_name} {ref_name}"

        proj = _project_receipt_against_current(
            rec,
            configured_remotes=configured_remotes,
            current_head=local["head_sha"],
            tree_clean=local["tree_clean"],
            current_fp=local["candidate_fingerprint"],
            current_local_acceptance=local["acceptance"],
            superseded_reason=superseded,
        )
        projected_by_tid[tid] = proj
        projected_targets.append(proj)

    last_rec = records[-1]
    last_tid = (last_rec.get("target") or {}).get("target_id")
    if isinstance(last_tid, str) and last_tid in projected_by_tid:
        latest_proj = projected_by_tid[last_tid]
    else:
        latest_proj = _project_receipt_against_current(
            last_rec,
            configured_remotes=configured_remotes,
            current_head=local["head_sha"],
            tree_clean=local["tree_clean"],
            current_fp=local["candidate_fingerprint"],
            current_local_acceptance=local["acceptance"],
        )

    return {
        "exists": tail["exists"],
        "total_attempts_in_tail": len(records),
        "latest": latest_proj,
        "targets": projected_targets,
        "problems": problems,
        "integrity": integrity,
        "authority_note": AUTHORITY_NOTE,
    }


def publication_lines(pub: dict) -> list[str]:
    """Render concise human-readable publication receipt facts for flow resume."""
    lines: list[str] = []
    if pub.get("problems"):
        for prob in pub["problems"]:
            lines.append(f"- publication storage problem: {prob}")

    targets = pub.get("targets") or []
    latest = pub.get("latest")
    items = list(targets)
    if latest and all(
        (t.get("observed") or {}).get("receipt_id") != (latest.get("observed") or {}).get("receipt_id")
        for t in items
    ):
        items.insert(0, latest)

    if not items and not pub.get("problems") and (pub.get("integrity") or {}).get("ok", True):
        return []

    for item in items[:4]:
        tgt = item.get("target") or {}
        obs = item.get("observed") or {}
        acc = item.get("local_acceptance") or {}
        label = f"{tgt.get('remote') or '<invalid-remote>'} {tgt.get('ref') or '<invalid-ref>'}"
        obs_sha = (obs.get("observed_sha") or "none")[:10]
        exp_sha = (obs.get("expected_sha") or "none")[:10]
        at_str = obs.get("at") or "unknown"
        status = obs.get("status") or "unknown"
        p_bind = tgt.get("push_binding")
        p_hashes = tgt.get("push_url_hashes") or []
        cur_p_bind = tgt.get("current_push_binding")
        cur_p_hashes = tgt.get("current_push_url_hashes") or []
        if p_bind == "same":
            endpoint_note = "; endpoint=fetch, push=same"
        elif p_bind == "different":
            ph = p_hashes[0][:10] if p_hashes else "diff"
            endpoint_note = f"; endpoint=fetch, push=different({ph})"
        elif p_bind == "multiple":
            phs = ",".join(h[:10] for h in p_hashes[:3])
            if len(p_hashes) > 3:
                phs += ",..."
            endpoint_note = f"; endpoint=fetch, push=multiple({len(p_hashes)}:{phs})"
        elif p_bind == "unverified":
            endpoint_note = "; endpoint=fetch, push=unverified"
        elif p_bind == "unknown":
            cur_extra = ""
            if cur_p_bind == "different" and cur_p_hashes:
                cur_extra = f", current_push=different({cur_p_hashes[0][:10]})"
            elif cur_p_bind == "multiple":
                cur_extra = f", current_push=multiple({len(cur_p_hashes)})"
            elif cur_p_bind == "unverified":
                cur_extra = ", current_push=unverified"
            endpoint_note = f"; endpoint=fetch, push=unknown{cur_extra}"
        else:
            endpoint_note = ""

        fresh_part = (
            "stale (" + "; ".join(item.get("stale_reasons") or ["drift"]) + ")"
            if item.get("stale")
            else "target & HEAD current"
        )
        acc_part = (
            "local acceptance: fresh delivery PASS"
            if acc.get("fresh_delivery")
            else "local acceptance: NOT ready (" + "; ".join((acc.get("issues") or ["unverified"])[:2]) + ")"
        )
        if item.get("current_delivery_verified"):
            delivery_part = "current delivery: verified at receipt time (observed only; no push authority)"
        elif p_bind == "different":
            delivery_part = (
                "current delivery: NOT verified "
                "(effective push endpoint differs from observed fetch endpoint; no push authority)"
            )
        elif p_bind == "multiple":
            delivery_part = (
                f"current delivery: NOT verified "
                f"({len(p_hashes)} effective push endpoints configured, only fetch endpoint observed; no push authority)"
            )
        elif p_bind == "unverified":
            delivery_part = (
                "current delivery: NOT verified "
                "(effective push endpoint binding unverified; no push authority)"
            )
        elif p_bind == "unknown":
            delivery_part = (
                "current delivery: NOT verified "
                "(legacy receipt lacks push endpoint binding; no push authority)"
            )
        else:
            delivery_part = "current delivery: NOT verified (no push authority)"
        lines.append(
            f"- {label}: observed {status} (observed={obs_sha}, expected={exp_sha}{endpoint_note}) at {at_str}; "
            f"{fresh_part}; {acc_part}; {delivery_part}"
        )

    integ = pub.get("integrity") or {}
    if integ.get("warnings"):
        lines.append("- publication integrity warnings: " + "; ".join(integ["warnings"]))
    return lines


def render_publish_verify(result: dict) -> str:
    tgt = result.get("target") or {}
    acc = result.get("acceptance") or {}
    p_bind = tgt.get("push_binding")
    p_hashes = tgt.get("push_url_hashes") or []
    push_part = ""
    if p_bind == "same":
        push_part = " (observed=fetch, push=same)"
    elif p_bind == "different":
        ph = p_hashes[0][:12] if p_hashes else "diff"
        push_part = f" (observed=fetch ONLY; push=different push_sha256={ph})"
    elif p_bind == "multiple":
        phs = ",".join(h[:12] for h in p_hashes[:3])
        if len(p_hashes) > 3:
            phs += ",..."
        push_part = f" (observed=fetch ONLY; push=multiple[{len(p_hashes)}] {phs})"
    elif p_bind == "unverified":
        push_part = f" (observed=fetch ONLY; push=unverified: {tgt.get('push_error') or 'unknown'})"
    lines = [
        f"flow publish-verify: {result['status'].upper()} (exit {result['exit_code']})",
        f"  receipt     : {result['id']} ({'written to ' + result['receipt_path'] if result.get('receipt_written') else 'not written'})",
        f"  at          : {result['at']}",
        f"  target      : {tgt.get('remote') or '<invalid>'} {tgt.get('ref') or '<invalid>'}"
        + (f" [{tgt['remote_kind']} url_sha256={tgt['remote_url_hash'][:12]}]" if tgt.get("remote_url_hash") else "")
        + push_part,
        f"  expected    : {result.get('expected_sha') or '<invalid>'}",
        f"  observed    : {result.get('observed_sha') or 'none'}",
        f"  local HEAD  : {result.get('head_sha') or 'none'} ({'clean' if result.get('tree_clean') else 'dirty'})",
        f"  reason      : {result.get('reason') or '-'}",
    ]
    if acc:
        if acc.get("fresh_delivery"):
            lines.append("  acceptance  : fresh delivery PASS covers expected commit")
        else:
            lines.append("  acceptance  : NOT fresh delivery (" + "; ".join(acc.get("issues") or ["unverified"]) + ")")
    if p_bind in ("different", "multiple", "unverified"):
        lines.append("  delivery    : NOT verified for push destination (observed fetch endpoint only)")
    lines.append(f"  authority   : {result.get('authority_note') or AUTHORITY_NOTE}")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI


def _parse_cli_argv(argv: list[str]) -> tuple[dict | None, str | None, bool, bool]:
    """Parse CLI options while preserving leading-dash flag values (e.g. --branch -bad)
    for structured invalid-status handling and avoiding raw secret echoing."""
    json_mode = "--json" in argv
    opts: dict[str, object] = {
        "repo": ".",
        "remote": None,
        "branch": None,
        "expect": None,
        "timeout": DEFAULT_TIMEOUT_S,
        "json": False,
    }
    value_flags = {"--repo": "repo", "--remote": "remote", "--branch": "branch", "--expect": "expect", "--timeout": "timeout"}

    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-h", "--help"):
            return None, None, json_mode, True
        if tok == "--json":
            opts["json"] = True
            json_mode = True
            i += 1
            continue
        if "=" in tok and tok.startswith("--"):
            flag, val = tok.split("=", 1)
            if flag in value_flags:
                opts[value_flags[flag]] = val
                i += 1
                continue
            return None, "unrecognized CLI option (no URL, push, or extra transport flags allowed)", json_mode, False
        if tok in value_flags:
            if i + 1 >= len(argv):
                return None, f"missing value for {tok}", json_mode, False
            nxt = argv[i + 1]
            if nxt in KNOWN_FLAGS:
                return None, f"missing value for {tok}", json_mode, False
            opts[value_flags[tok]] = nxt
            i += 2
            continue
        return None, "unrecognized or positional CLI argument (use --repo, --remote, --branch, --expect)", json_mode, False

    return opts, None, json_mode, False


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    opts, cli_err, json_mode, show_help = _parse_cli_argv(argv)
    if show_help:
        print(__doc__.strip())
        return 0

    if cli_err is not None or opts is None:
        err_payload = {
            "version": 1,
            "id": str(uuid.uuid4()),
            "at": _utc_now(),
            "status": "invalid",
            "exit_code": STATUS_EXIT_CODES["invalid"],
            "error_code": "invalid_cli_args",
            "reason": cli_err or "invalid CLI arguments",
            "target": {
                "remote": None,
                "branch": None,
                "ref": None,
                "observed_endpoint": None,
                "remote_kind": None,
                "remote_url_hash": None,
                "push_binding": None,
                "push_url_hashes": None,
                "push_kinds": None,
                "push_differs": None,
                "push_error": None,
                "target_id": None,
            },
            "expected_sha": None,
            "observed_sha": None,
            "head_sha": None,
            "tree_clean": False,
            "candidate_fingerprint": None,
            "verdict": None,
            "acceptance": None,
            "authority_note": AUTHORITY_NOTE,
            "receipt_written": False,
            "receipt_path": RECEIPT_REL_PATH,
        }
        if json_mode:
            print(json.dumps(err_payload, ensure_ascii=False, indent=2))
        else:
            print(f"flow publish-verify: INVALID - {err_payload['reason']}", file=sys.stderr)
        return STATUS_EXIT_CODES["invalid"]

    result = verify_publication(
        str(opts["repo"]),
        remote=opts["remote"],
        branch=opts["branch"],
        expect=opts["expect"],
        timeout_s=opts["timeout"],
    )
    if opts["json"]:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        stream = sys.stderr if result["status"] == "invalid" else sys.stdout
        print(render_publish_verify(result), file=stream)
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())

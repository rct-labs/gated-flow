"""Privacy guards, URL/redirect SSRF validation, and untrusted-content containment."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path
import re
import socket
from typing import Any, Callable, Iterable, Sequence
from urllib.parse import urlsplit

from learner.contracts import (
    PrivacyViolationError,
    ProjectProfile,
    UnsafeUrlError,
)

_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".arpa", ".home.arpa", ".lan")
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}")

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_instructions", re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?", re.I)),
    ("permission_escalation", re.compile(r"(?:execution_paused|learning_enabled|grant\s+permission|bypass\s+gate)", re.I)),
    ("shell_execution", re.compile(r"(?:os\.system|subprocess\.|rm\s+-rf|\|\s*sh\b|curl\s+[^|]+\|\s*bash)", re.I)),
    ("system_override", re.compile(r"(?:<\s*system\s*>|system\s+prompt\s*:|you\s+are\s+now\s+authorized)", re.I)),
)

_SECRET_LIKE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bSENTINEL_[A-Za-z0-9_]{3,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(?:api[_-]?key|secret[_-]?key|access[_-]?token|private[_-]?key|password)\s*[:=]\s*['\"]?[A-Za-z0-9_.-]{6,}"),
)

# Approved generic public-concept vocabulary. Outbound concepts derived from private docs
# may ONLY be drawn from this fixed catalog (or an explicit reviewed export mapping),
# never from arbitrary words in private maintained documentation.
APPROVED_PUBLIC_CONCEPTS: dict[str, tuple[str, ...]] = {
    "evaluation harness": ("evaluation", "harness", "benchmark", "benchmarks", "eval", "evals"),
    "task scheduling": ("schedule", "scheduling", "scheduler", "cadence", "task queue"),
    "deterministic verification": ("deterministic", "verification", "replay", "regression", "verifying"),
    "test automation": ("unittest", "pytest", "test runner", "automated testing", "test suite"),
    "static analysis": ("static analysis", "lint", "linter", "typecheck", "ast"),
    "sandboxed execution": ("sandbox", "sandboxed", "isolation", "containment"),
    "workflow orchestration": ("workflow", "workflows", "orchestration", "pipeline", "state machine"),
    "agent skills": ("skill", "skills", "prompting", "tool selection"),
    "release management": ("release", "releases", "rollback", "pinning", "versioning"),
    "sqlite persistence": ("sqlite", "wal", "transactional", "idempotency"),
    "schema validation": ("schema", "contract", "contracts", "validation"),
    "citation verification": ("citation", "citations", "evidence", "provenance", "traceability"),
    "caching optimization": ("cache", "caching", "deduplication", "memoization"),
    "observability telemetry": ("telemetry", "observability", "logging", "metrics"),
    "dependency audit": ("dependency", "dependencies", "packaging", "supply chain"),
    "security hardening": ("security", "ssrf", "injection", "sanitization"),
    "code review": ("code review", "review", "diff", "audit"),
    "retrieval indexing": ("retrieval", "indexing", "search", "ranking"),
}

_GENERIC_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "using", "project",
        "system", "tool", "tools", "code", "workflow", "workflows", "agent", "agents",
        "model", "models", "build", "make", "keep", "only", "when", "where", "which",
        "without", "within", "across", "existing", "current", "local", "safe", "bounded",
    }
)


def _is_forbidden_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or getattr(ip, "is_site_local", False)
    )


def validate_public_url(
    url: str,
    *,
    allowed_hosts: Sequence[str] = (),
    resolve_dns: bool = False,
    resolver: Callable[[str], Sequence[str]] | None = None,
) -> str:
    """Validate that URL uses HTTPS and targets a public, non-local endpoint."""
    raw = str(url or "").strip()
    if not raw:
        raise UnsafeUrlError("Empty URL is not allowed")
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme != "https":
        raise UnsafeUrlError(f"Only HTTPS URLs are permitted for public fetch, got {scheme!r}")
    if parts.username or parts.password:
        raise UnsafeUrlError("URLs with embedded credentials are forbidden")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise UnsafeUrlError(f"URL missing host: {url!r}")
    if host in _BLOCKED_HOSTNAMES or any(host.endswith(suffix) for suffix in _BLOCKED_SUFFIXES):
        raise UnsafeUrlError(f"Blocked local/internal host: {host!r}")

    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if _is_forbidden_ip(ip_obj):
            raise UnsafeUrlError(f"Blocked private/local IP address: {host!r}")
    elif resolve_dns:
        resolved = _resolve_host_ips(host, resolver)
        if not resolved:
            raise UnsafeUrlError(f"Could not resolve public host: {host!r}")
        for addr in resolved:
            addr_ip = ipaddress.ip_address(addr)
            if _is_forbidden_ip(addr_ip):
                raise UnsafeUrlError(f"Host {host!r} resolved to private/local IP {addr!r}")

    if allowed_hosts:
        normalized_allowed = {h.lower().lstrip(".") for h in allowed_hosts if h.strip()}
        if host not in normalized_allowed and not any(host.endswith(f".{h}") for h in normalized_allowed):
            raise UnsafeUrlError(f"Host {host!r} is outside allowed_hosts policy")

    return raw


def _resolve_host_ips(host: str, resolver: Callable[[str], Sequence[str]] | None) -> list[str]:
    if resolver is not None:
        return list(resolver(host))
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeUrlError(f"DNS resolution failed for {host!r}: {exc}") from exc
    ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and isinstance(sockaddr[0], str):
            ips.append(sockaddr[0].split("%", 1)[0])
    return ips


def _profile_private_markers(profile: ProjectProfile, *, include_mission_tokens: bool = False) -> list[str]:
    markers: list[str] = []
    for candidate in (
        profile.project_id,
        profile.name,
        profile.root_path,
        Path(profile.root_path).name if profile.root_path else "",
        profile.mission,
        *profile.private_terms,
    ):
        text = str(candidate or "").strip()
        if len(text) >= 3:
            markers.append(text)
    for token in _WORD_RE.findall(profile.name):
        if len(token) >= 4 and token.lower() not in _GENERIC_STOPWORDS:
            markers.append(token)
    if include_mission_tokens:
        concept_tokens = {
            tok.lower()
            for concept in profile.public_concepts
            for tok in _WORD_RE.findall(concept)
        }
        for token in _WORD_RE.findall(profile.mission):
            low = token.lower()
            if len(token) >= 5 and low not in _GENERIC_STOPWORDS and low not in concept_tokens:
                markers.append(token)
    # Deduplicate preserving order (longest first for clear error messages).
    seen: set[str] = set()
    unique: list[str] = []
    for item in sorted(markers, key=len, reverse=True):
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _contains_marker(haystack: str, marker: str) -> bool:
    low_hay = haystack.lower()
    low_marker = marker.lower()
    if len(low_marker) >= 6 or " " in low_marker or "/" in low_marker or "-" in low_marker or "_" in low_marker:
        return low_marker in low_hay
    return re.search(rf"(?<![A-Za-z0-9]){re.escape(low_marker)}(?![A-Za-z0-9])", low_hay) is not None


def contains_secret_like_term(text: str) -> bool:
    """Return True if text contains recognizable secret/sentinel token patterns."""
    raw = str(text or "")
    return any(pattern.search(raw) for pattern in _SECRET_LIKE_PATTERNS)


def redact_secrets_and_markers(text: str, markers: Sequence[str] = ()) -> str:
    """Redact secret-like patterns and explicit private markers from text."""
    out = str(text or "")
    for pattern in _SECRET_LIKE_PATTERNS:
        out = pattern.sub("[redacted]", out)
    for marker in sorted({m for m in markers if len(str(m).strip()) >= 3}, key=len, reverse=True):
        if _contains_marker(out, marker):
            out = re.sub(re.escape(marker), "[redacted]", out, flags=re.I)
    return " ".join(out.split()).strip()


def assert_no_private_leak(
    text: str,
    profiles: Iterable[ProjectProfile],
    *,
    context: str = "outbound query",
    include_mission_tokens: bool = False,
) -> str:
    """Fail closed if text contains private project identifiers, paths, mission text, private terms, or secrets."""
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return ""
    if contains_secret_like_term(cleaned):
        raise PrivacyViolationError(f"Blocked secret-like term in {context}")
    for profile in profiles:
        for marker in _profile_private_markers(profile, include_mission_tokens=include_mission_tokens):
            if _contains_marker(cleaned, marker):
                raise PrivacyViolationError(
                    f"Blocked private term in {context} for project {profile.project_id!r}"
                )
    return cleaned


def build_public_queries(profile: ProjectProfile, *, max_queries: int = 3) -> list[str]:
    """Derive bounded outbound public queries solely from explicitly sanitized public_concepts."""
    if not profile.export_policy.allow_public_concept_queries:
        return []
    queries: list[str] = []
    for concept in profile.public_concepts:
        sanitized = assert_no_private_leak(concept, (profile,), context="public_concepts")
        if sanitized and sanitized not in queries:
            queries.append(sanitized)
        if len(queries) >= max_queries:
            break
    return queries


def derive_sanitized_concepts(
    texts: Sequence[str],
    *,
    forbidden_terms: Sequence[str] = (),
    max_concepts: int = 6,
    approved_vocabulary: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Derive public concepts ONLY from an approved generic vocabulary, never arbitrary doc words."""
    vocab = approved_vocabulary if approved_vocabulary is not None else APPROVED_PUBLIC_CONCEPTS
    forbidden = {str(t).strip().lower() for t in forbidden_terms if len(str(t).strip()) >= 3}
    concepts: list[str] = []
    safe_lines: list[str] = []
    for raw in texts:
        cleaned = _CONTROL_CHARS_RE.sub(" ", str(raw or "")).strip()
        cleaned = re.sub(r"^[#*\-\d.\s]+", "", cleaned).strip()
        if not cleaned or len(cleaned) < 4:
            continue
        for pat in _SECRET_LIKE_PATTERNS:
            cleaned = pat.sub(" ", cleaned)
        safe_lines.append(cleaned.lower())
    combined = " ".join(safe_lines)
    if not combined:
        return ()

    for concept, triggers in vocab.items():
        concept_low = concept.lower()
        if any(f in concept_low for f in forbidden):
            continue
        matched = False
        for trig in triggers:
            t_low = trig.lower().strip()
            if not t_low or t_low in forbidden:
                continue
            if " " in t_low:
                if t_low in combined:
                    matched = True
                    break
            elif re.search(rf"(?<![a-z0-9]){re.escape(t_low)}(?![a-z0-9])", combined):
                matched = True
                break
        if matched and concept not in concepts:
            concepts.append(concept)
            if len(concepts) >= max_concepts:
                break
    return tuple(concepts)


def build_sanitized_profile_summary(
    profile: ProjectProfile,
    *,
    alias: str,
    preferences: dict[str, Sequence[str]] | None = None,
    negative_outcomes: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Build a privacy-approved English project summary for model packets without private identifiers or secrets."""
    # Never expose project_id, name, root_path, raw mission prose, private_terms, or secrets.
    explicit_markers = [
        profile.project_id,
        profile.name,
        Path(profile.root_path).name if profile.root_path else "",
        profile.root_path,
        profile.mission,
        *profile.private_terms,
    ]
    for token in _WORD_RE.findall(profile.name):
        if len(token) >= 4 and token.lower() not in _GENERIC_STOPWORDS:
            explicit_markers.append(token)

    def scrub_list(items: Sequence[str], limit: int = 8) -> list[str]:
        out: list[str] = []
        for item in items[:limit]:
            cleaned = redact_secrets_and_markers(item, explicit_markers)
            residual = cleaned.replace("[redacted]", "").strip(" .,;:-_/()[]{}")
            if cleaned and residual and cleaned not in out:
                out.append(cleaned[:160])
        return out

    sanitized_concepts = scrub_list(profile.public_concepts, limit=6)
    if not sanitized_concepts:
        sanitized_concepts = list(
            derive_sanitized_concepts(
                (*profile.goals, *profile.known_gaps, *profile.existing_capabilities),
                forbidden_terms=explicit_markers,
                max_concepts=6,
            )
        )

    sanitized_outcomes: list[dict[str, str]] = []
    for out_item in negative_outcomes[:4]:
        summary_text = redact_secrets_and_markers(
            str(out_item.get("summary") or out_item.get("outcome_summary") or ""),
            explicit_markers,
        )
        note_text = redact_secrets_and_markers(
            str(out_item.get("feedback_note") or ""),
            explicit_markers,
        )
        sanitized_outcomes.append(
            {
                "status": str(out_item.get("status") or ""),
                "source_url": str(out_item.get("canonical_url") or ""),
                "summary": summary_text[:140],
                "note": note_text[:100],
            }
        )

    prefs = preferences or {}
    return {
        "project_alias": alias,
        "public_concepts": sanitized_concepts,
        "goals": scrub_list(profile.goals, limit=6),
        "existing_capabilities": scrub_list(profile.existing_capabilities, limit=8),
        "known_gaps": scrub_list(profile.known_gaps, limit=6),
        "desired_outcomes": scrub_list(profile.desired_outcomes, limit=4),
        "exclusions": scrub_list(profile.exclusions, limit=6),
        "preferences": {
            "focus_more_on": scrub_list(prefs.get("focus_more_on", ()), limit=6),
            "less_like": scrub_list(prefs.get("less_like", ()), limit=6),
        },
        "prior_negative_outcomes": sanitized_outcomes,
    }


def _extract_project_outbound_text(raw: str) -> tuple[bool, str]:
    """Return (is_structured_packet, json_of_project_derived_fields) for structured model packets."""
    try:
        obj = json.loads(raw)
    except ValueError:
        return False, raw
    if not isinstance(obj, dict) or "task" not in obj:
        return False, raw
    project_fields = {
        key: obj[key]
        for key in ("project", "projects", "queries")
        if key in obj
    }
    return True, json.dumps(project_fields, sort_keys=True, ensure_ascii=True)


def assert_safe_model_packet(
    packet_text: str,
    profiles: Iterable[ProjectProfile],
    *,
    context: str = "model packet",
    max_bytes: int = 24_000,
) -> str:
    """Validate packet size, secrets across packet, and private identifiers in project-derived fields."""
    raw = str(packet_text or "")
    encoded = raw.encode("utf-8")
    if len(encoded) > max_bytes:
        raise PrivacyViolationError(f"{context} exceeds max_bytes={max_bytes} ({len(encoded)} bytes)")
    if contains_secret_like_term(raw):
        raise PrivacyViolationError(f"Blocked secret-like term in {context}")
    profile_list = list(profiles)
    is_structured, target_text = _extract_project_outbound_text(raw)
    if is_structured:
        if target_text and target_text != "{}":
            assert_no_private_leak(
                target_text,
                profile_list,
                context=f"{context} project fields",
                include_mission_tokens=False,
            )
    else:
        assert_no_private_leak(raw, profile_list, context=context, include_mission_tokens=False)
    return raw


def sanitize_untrusted_text(text: str, *, max_len: int = 2000) -> tuple[str, tuple[str, ...]]:
    """Neutralize control characters, bound length, and flag prompt-injection directives."""
    raw = str(text or "")
    no_ctrl = _CONTROL_CHARS_RE.sub(" ", raw)
    collapsed = " ".join(no_ctrl.split())
    bounded = collapsed[:max_len]
    flags: list[str] = []
    for label, pattern in _INJECTION_PATTERNS:
        if pattern.search(raw):
            flags.append(label)
    return bounded, tuple(flags)

"""Reusable bounded intake collectors for GitHub Trending/Search, Releases, and arXiv."""

from __future__ import annotations

from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import json
import re
import time
from typing import Any, Callable, Protocol, Sequence
import urllib.error
from urllib.parse import quote_plus, unquote, urljoin, urlsplit
import urllib.request
import xml.etree.ElementTree as ET

from learner.contracts import (
    IngestionCoverage,
    LearnerError,
    PILOT_MAX_METADATA_ITEMS_PER_DAY,
    ProjectProfile,
    SourceArtifact,
    UnsafeUrlError,
    canonicalize_url,
    canonicalize_version,
    make_source_id,
    sha256_bytes,
    sha256_json,
)
from learner.privacy import (
    assert_no_private_leak,
    build_public_queries,
    sanitize_untrusted_text,
    validate_public_url,
)

DEFAULT_TIMEOUT_S = 10.0
DEFAULT_MAX_BYTES = 524_288
DEFAULT_TRENDING_MAX_BYTES = 1_048_576
DEFAULT_MAX_REDIRECTS = 3
PRIMARY_EXTRACTION_VERSION = 2
_REPO_SLUG_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_STARS_TODAY_RE = re.compile(r"([\d,]+)\s+stars?\s+(today|this\s+week|this\s+month)", re.I)
_STAR_WINDOW_BY_PHRASE = {
    "today": "daily",
    "this week": "weekly",
    "this month": "monthly",
}
_ARXIV_ID_VER_RE = re.compile(r"/abs/([^/?#]+?)(v\d+)?$")
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}
_TRANSIENT_HTTP_STATUSES = frozenset({429, 502, 503, 504})
_NON_REPO_FIRST_SEGMENTS = frozenset(
    {
        "login",
        "signup",
        "sponsors",
        "trending",
        "topics",
        "orgs",
        "features",
        "marketplace",
        "collections",
        "apps",
        "about",
        "settings",
        "explore",
        "search",
        "site",
        "pricing",
        "enterprise",
    }
)
_FORBIDDEN_FOLLOWUP_EXTENSIONS = frozenset(
    {
        ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".exe", ".bin", ".dll", ".so",
        ".dylib", ".whl", ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".png",
        ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp", ".pdf", ".mp4", ".mov", ".wasm", ".jar",
    }
)
_ALLOWED_FOLLOWUP_EXTENSIONS = frozenset(
    {
        "", ".md", ".markdown", ".txt", ".rst", ".adoc", ".py", ".ts", ".tsx", ".js",
        ".mjs", ".go", ".rs", ".java", ".kt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    }
)


def _parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        secs = float(raw)
        return max(0.0, secs) if secs == secs else None
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            return None
        base = time.time() if now is None else float(now)
        return max(0.0, dt.timestamp() - base)
    except (TypeError, ValueError, OverflowError):
        return None


class SourceFetchError(LearnerError):
    """Raised on bounded transport or feed parse errors."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        status_code: int | None = None,
        retry_after_s: float | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code
        self.retry_after_s = retry_after_s
        self.headers = dict(headers or {})


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    body: bytes
    headers: dict[str, str]
    final_url: str
    truncated: bool = False
    total_bytes_hint: int | None = None


class Transport(Protocol):
    def fetch(
        self,
        url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        allowed_hosts: Sequence[str] = (),
        allow_truncated: bool = False,
    ) -> TransportResponse: ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _parse_content_length(headers: dict[str, str]) -> int | None:
    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        val = int(str(raw).strip())
        return val if val >= 0 else None
    except ValueError:
        return None


class HttpTransport:
    """Bounded stdlib HTTPS transport with hop-by-hop redirect SSRF validation."""

    def __init__(
        self,
        *,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        resolve_dns: bool = True,
        resolver: Callable[[str], Sequence[str]] | None = None,
        user_agent: str = "project-learner/0.1",
    ) -> None:
        self.max_redirects = max_redirects
        self.resolve_dns = resolve_dns
        self.resolver = resolver
        self.user_agent = user_agent

    def fetch(
        self,
        url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        allowed_hosts: Sequence[str] = (),
        allow_truncated: bool = False,
    ) -> TransportResponse:
        current = validate_public_url(
            url,
            allowed_hosts=allowed_hosts,
            resolve_dns=self.resolve_dns,
            resolver=self.resolver,
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        for _ in range(self.max_redirects + 1):
            req = urllib.request.Request(
                current,
                headers={"User-Agent": self.user_agent, "Accept": "application/json, application/atom+xml, text/html"},
            )
            try:
                with opener.open(req, timeout=timeout_s) as resp:
                    raw = resp.read(max_bytes + 1)
                    headers = {k.lower(): v for k, v in resp.headers.items()}
                    total_hint = _parse_content_length(headers)
                    truncated = False
                    if len(raw) > max_bytes:
                        if not allow_truncated:
                            raise SourceFetchError(
                                "payload_too_large",
                                f"Response exceeded max_bytes={max_bytes}",
                                headers=headers,
                            )
                        raw = raw[:max_bytes]
                        truncated = True
                        if total_hint is not None and total_hint < len(raw):
                            total_hint = None
                    return TransportResponse(
                        status_code=int(getattr(resp, "status", 200)),
                        body=raw,
                        headers=headers,
                        final_url=current,
                        truncated=truncated,
                        total_bytes_hint=total_hint,
                    )
            except urllib.error.HTTPError as exc:
                if exc.code in (301, 302, 303, 307, 308):
                    location = exc.headers.get("Location") if exc.headers else None
                    if not location:
                        raise SourceFetchError("http_status", f"Redirect {exc.code} missing Location") from exc
                    target = urljoin(current, location)
                    current = validate_public_url(
                        target,
                        allowed_hosts=allowed_hosts,
                        resolve_dns=self.resolve_dns,
                        resolver=self.resolver,
                    )
                    continue
                err_headers = {k.lower(): v for k, v in exc.headers.items()} if exc.headers else {}
                raise SourceFetchError(
                    "http_status",
                    f"HTTP {exc.code} for {current}",
                    status_code=int(exc.code),
                    retry_after_s=_parse_retry_after(err_headers.get("retry-after")),
                    headers=err_headers,
                ) from exc
            except TimeoutError as exc:
                raise SourceFetchError("timeout", f"Timed out fetching {current}") from exc
            except urllib.error.URLError as exc:
                if isinstance(exc.reason, TimeoutError):
                    raise SourceFetchError("timeout", f"Timed out fetching {current}") from exc
                raise SourceFetchError("network", f"Network error fetching {current}: {exc.reason}") from exc
        raise SourceFetchError("http_status", f"Exceeded max_redirects={self.max_redirects}")


class FixtureTransport:
    """Deterministic offline transport enforcing URL, redirect, timeout, header, and byte-size contracts."""

    def __init__(
        self,
        routes: dict[str, TransportResponse | Exception | Callable[[str], TransportResponse | Exception]] | None = None,
        *,
        resolve_dns: bool = False,
        resolver: Callable[[str], Sequence[str]] | None = None,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
    ) -> None:
        self.routes = dict(routes or {})
        self.resolve_dns = resolve_dns
        self.resolver = resolver
        self.max_redirects = max_redirects
        self.calls: list[str] = []

    def fetch(
        self,
        url: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_bytes: int = DEFAULT_MAX_BYTES,
        allowed_hosts: Sequence[str] = (),
        allow_truncated: bool = False,
    ) -> TransportResponse:
        if timeout_s <= 0:
            raise SourceFetchError("timeout", f"Timed out fetching {url}")
        current = validate_public_url(
            url,
            allowed_hosts=allowed_hosts,
            resolve_dns=self.resolve_dns,
            resolver=self.resolver,
        )
        for _ in range(self.max_redirects + 1):
            self.calls.append(current)
            handler = self._match(current)
            if handler is None:
                raise SourceFetchError("http_status", f"HTTP 404 (no offline fixture) for {current}", status_code=404)
            if isinstance(handler, Exception):
                raise handler
            outcome = handler(current) if callable(handler) else handler
            if isinstance(outcome, Exception):
                raise outcome
            resp = outcome
            headers = {str(k).lower(): str(v) for k, v in (resp.headers or {}).items()}
            if resp.status_code in (301, 302, 303, 307, 308):
                location = headers.get("location")
                if not location:
                    raise SourceFetchError("http_status", f"Redirect {resp.status_code} missing Location")
                target = urljoin(current, location)
                current = validate_public_url(
                    target,
                    allowed_hosts=allowed_hosts,
                    resolve_dns=self.resolve_dns,
                    resolver=self.resolver,
                )
                continue
            if resp.status_code >= 400:
                raise SourceFetchError(
                    "http_status",
                    f"HTTP {resp.status_code} for {current}",
                    status_code=int(resp.status_code),
                    retry_after_s=_parse_retry_after(headers.get("retry-after")),
                    headers=headers,
                )
            body = resp.body
            truncated = bool(resp.truncated)
            total_hint = resp.total_bytes_hint if resp.total_bytes_hint is not None else _parse_content_length(headers)
            if len(body) > max_bytes:
                if not allow_truncated:
                    raise SourceFetchError("payload_too_large", f"Response exceeded max_bytes={max_bytes}", headers=headers)
                total_hint = len(body) if total_hint is None else max(total_hint, len(body))
                body = body[:max_bytes]
                truncated = True
            return TransportResponse(
                status_code=int(resp.status_code),
                body=body,
                headers=headers,
                final_url=resp.final_url or current,
                truncated=truncated,
                total_bytes_hint=total_hint,
            )
        raise SourceFetchError("http_status", f"Exceeded max_redirects={self.max_redirects}")

    def _match(self, url: str) -> TransportResponse | Exception | Callable[[str], TransportResponse | Exception] | None:
        if url in self.routes:
            return self.routes[url]
        for pattern, handler in self.routes.items():
            if pattern.endswith("*") and url.startswith(pattern[:-1]):
                return handler
        return None


@dataclass(frozen=True)
class CollectionResult:
    artifacts: list[SourceArtifact]
    raw_evidence: dict[str, bytes]
    coverage: list[IngestionCoverage]
    metadata: dict[str, Any] = field(default_factory=dict)


def _extract_trending_repo_href(href: str) -> str | None:
    raw = str(href or "").strip()
    if not raw or raw.startswith("#"):
        return None
    if raw.startswith(("http://", "https://")):
        parts = urlsplit(raw)
        if (parts.hostname or "").lower() not in ("github.com", "www.github.com"):
            return None
        path = parts.path
    else:
        path = urlsplit(raw).path
    segs = [p for p in path.split("/") if p]
    if len(segs) != 2:
        return None
    if segs[0].lower() in _NON_REPO_FIRST_SEGMENTS:
        return None
    slug = f"{segs[0]}/{segs[1]}"
    return f"/{slug}" if _REPO_SLUG_RE.match(slug) else None


class _TrendingHtmlParser(HTMLParser):
    """Resilient HTML parser for GitHub Trending repo cards (<article class='Box-row'> or <li class='Box-row'>)."""

    def __init__(self, *, since: str = "daily") -> None:
        super().__init__()
        self._since = since
        self.items: list[dict[str, Any]] = []
        self._card_tag: str | None = None
        self._card_depth = 0
        self._heading_depth = 0
        self._current: dict[str, Any] | None = None
        self._capture: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = dict(attrs)
        cls = (attr_map.get("class") or "").split()
        if self._card_tag is None and ((tag == "article") or (tag == "li" and "Box-row" in cls)):
            self._card_tag = tag
            self._card_depth = 1
            self._heading_depth = 0
            self._current = {"href": None, "href_in_heading": False, "description": "", "text_chunks": []}
            return
        if self._card_tag is None or self._current is None:
            return
        if tag == self._card_tag:
            self._card_depth += 1
        if tag in ("h1", "h2", "h3"):
            self._heading_depth += 1
        if tag == "a":
            repo_href = _extract_trending_repo_href(attr_map.get("href") or "")
            in_heading = self._heading_depth > 0
            if repo_href is not None and (
                self._current["href"] is None or (in_heading and not self._current["href_in_heading"])
            ):
                self._current["href"] = repo_href
                self._current["href_in_heading"] = in_heading
                self._capture = "title"
                self._buf = []
        elif tag == "p" and not self._current["description"]:
            self._capture = "description"
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._card_tag is None or self._current is None:
            return
        self._current["text_chunks"].append(data)
        if self._capture:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._card_tag is None or self._current is None:
            return
        if self._capture == "title" and tag == "a":
            self._current["title"] = " ".join("".join(self._buf).split())
            self._capture = None
        elif self._capture == "description" and tag == "p":
            self._current["description"] = " ".join("".join(self._buf).split())
            self._capture = None
        if tag in ("h1", "h2", "h3") and self._heading_depth > 0:
            self._heading_depth -= 1
        if tag == self._card_tag:
            self._card_depth -= 1
            if self._card_depth <= 0:
                if self._current.get("href"):
                    full_text = " ".join(" ".join(self._current["text_chunks"]).split())
                    matches = list(_STARS_TODAY_RE.finditer(full_text))
                    chosen = next(
                        (
                            m
                            for m in matches
                            if _STAR_WINDOW_BY_PHRASE.get(" ".join(m.group(2).lower().split())) == self._since
                        ),
                        matches[0] if matches else None,
                    )
                    if chosen is not None:
                        count = int(chosen.group(1).replace(",", ""))
                        phrase = " ".join(chosen.group(2).lower().split())
                        window = _STAR_WINDOW_BY_PHRASE[phrase]
                        self._current["stars_in_window"] = count
                        self._current["stars_window"] = window
                        if window == "daily" and self._since == "daily":
                            self._current["stars_today"] = count
                    self._current.pop("href_in_heading", None)
                    self.items.append(self._current)
                self._card_tag = None
                self._current = None


def _build_artifact(
    *,
    raw_url: str,
    source_class: str,
    publisher: str,
    title: str,
    summary: str,
    raw_version: str,
    trust_class: str,
    observed_at: float,
    raw_bytes: bytes,
    released_at: str | None = None,
    license_id: str | None = None,
    is_actual_trending: bool = False,
    measured_star_velocity: bool = False,
    popularity_signal_only: bool = True,
    degraded_fallback: bool = False,
    extra_metadata: dict[str, Any] | None = None,
) -> SourceArtifact:
    canon_url = canonicalize_url(raw_url)
    canon_ver = canonicalize_version(raw_version)
    clean_title, title_flags = sanitize_untrusted_text(title, max_len=300)
    clean_summary, summary_flags = sanitize_untrusted_text(summary, max_len=2000)
    flags = tuple(dict.fromkeys((*title_flags, *summary_flags)))
    source_id = make_source_id(canon_url, canon_ver)
    evidence_digest = sha256_bytes(raw_bytes)
    claims = (
        {
            "claim_kind": "source_claim",
            "title": clean_title,
            "summary": clean_summary,
            "publisher": publisher,
            "canonical_url": canon_url,
            "version": canon_ver,
            "untrusted": True,
        },
    )
    content_digest = sha256_json(
        {
            "canonical_url": canon_url,
            "canonical_version": canon_ver,
            "title": clean_title,
            "summary": clean_summary,
        }
    )
    return SourceArtifact(
        source_id=source_id,
        canonical_url=canon_url,
        raw_url=raw_url,
        source_class=source_class,
        publisher=publisher,
        title=clean_title,
        summary=clean_summary,
        canonical_version=canon_ver,
        raw_version=raw_version,
        content_digest=content_digest,
        evidence_digest=evidence_digest,
        trust_class=trust_class,
        observed_at=observed_at,
        released_at=released_at,
        license_id=license_id,
        retrieval_status="degraded" if degraded_fallback else "ok",
        is_actual_trending=is_actual_trending,
        measured_star_velocity=measured_star_velocity,
        popularity_signal_only=popularity_signal_only,
        degraded_fallback=degraded_fallback,
        untrusted_directive_flags=flags,
        source_claims=claims,
        metadata=dict(extra_metadata or {}),
    )


def fetch_github_popular(
    transport: Transport,
    *,
    query: str = "stars:>100",
    clock: Callable[[], float] | None = None,
    max_items: int = 20,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allowed_hosts: Sequence[str] = (),
    fallback_mode: bool = False,
    run_id: str = "direct",
    mode: str = "opportunity-led",
    profiles: Sequence[ProjectProfile] = (),
) -> CollectionResult:
    """Fetch bounded GitHub repository search results (never labelled actual Trending)."""
    now = (clock or time.time)()
    safe_query = assert_no_private_leak(query, profiles, context="github_popular query")
    if not safe_query:
        safe_query = "stars:>100"
    url = (
        "https://api.github.com/search/repositories"
        f"?q={quote_plus(safe_query)}&sort=stars&order=desc&per_page={max(1, min(max_items, 50))}"
    )
    query_key = f"github_popular:{safe_query}"
    try:
        resp = transport.fetch(url, timeout_s=timeout_s, max_bytes=max_bytes, allowed_hosts=allowed_hosts)
        payload = json.loads(resp.body.decode("utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise SourceFetchError("malformed", "GitHub search JSON missing 'items' list")
    except (SourceFetchError, UnsafeUrlError, UnicodeDecodeError, ValueError) as exc:
        cov = IngestionCoverage(
            run_id=run_id,
            source_class="github_popular",
            query_key=query_key,
            mode=mode,
            status="error",
            incomplete=True,
            items_found=0,
            items_new=0,
            error=str(exc),
            last_success_at=None,
            recorded_at=now,
        )
        return CollectionResult(artifacts=[], raw_evidence={}, coverage=[cov])

    artifacts: list[SourceArtifact] = []
    raw_evidence: dict[str, bytes] = {}
    for item in payload["items"][:max_items]:
        if not isinstance(item, dict) or not item.get("html_url"):
            continue
        full_name = str(item.get("full_name") or item.get("name") or "")
        publisher = full_name.split("/")[0] if "/" in full_name else "github"
        lic = item.get("license")
        license_id = lic.get("spdx_id") if isinstance(lic, dict) else None
        raw_ver = ""
        item_bytes = json.dumps(item, sort_keys=True).encode("utf-8")
        art = _build_artifact(
            raw_url=str(item["html_url"]),
            source_class="github_popular",
            publisher=publisher,
            title=full_name or str(item["html_url"]),
            summary=str(item.get("description") or ""),
            raw_version=raw_ver,
            trust_class="public_community",
            observed_at=now,
            raw_bytes=item_bytes,
            released_at=str(item.get("pushed_at") or "") or None,
            license_id=license_id,
            is_actual_trending=False,
            measured_star_velocity=False,
            popularity_signal_only=True,
            degraded_fallback=fallback_mode,
            extra_metadata={
                "stargazers_count": item.get("stargazers_count"),
                "language": item.get("language"),
                "topics": list(item.get("topics") or ())[:10],
                "source_label": "github_search_fallback" if fallback_mode else "github_search_popular",
                "qualification_status": "intake_only",
            },
        )
        artifacts.append(art)
        raw_evidence[art.source_id] = item_bytes

    cov = IngestionCoverage(
        run_id=run_id,
        source_class="github_popular",
        query_key=query_key,
        mode=mode,
        status="degraded" if fallback_mode else "ok",
        incomplete=fallback_mode,
        items_found=len(artifacts),
        items_new=0,
        error="degraded_search_fallback_from_trending" if fallback_mode else None,
        last_success_at=now if not fallback_mode else None,
        recorded_at=now,
    )
    return CollectionResult(artifacts=artifacts, raw_evidence=raw_evidence, coverage=[cov])


def fetch_github_trending(
    transport: Transport,
    *,
    since: str = "daily",
    clock: Callable[[], float] | None = None,
    max_items: int = 20,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_TRENDING_MAX_BYTES,
    allowed_hosts: Sequence[str] = (),
    allow_search_fallback: bool = True,
    fallback_query: str = "stars:>100",
    run_id: str = "direct",
    mode: str = "opportunity-led",
    profiles: Sequence[ProjectProfile] = (),
) -> CollectionResult:
    """Fetch GitHub Trending with honest degraded fallback to labelled GitHub Search."""
    now = (clock or time.time)()
    safe_since = since if since in ("daily", "weekly", "monthly") else "daily"
    url = f"https://github.com/trending?since={safe_since}"
    query_key = f"github_trending:{safe_since}"

    try:
        resp = transport.fetch(url, timeout_s=timeout_s, max_bytes=max_bytes, allowed_hosts=allowed_hosts)
        text = resp.body.decode("utf-8")
        if "<article" not in text and "Box-row" not in text:
            raise SourceFetchError("malformed", "GitHub Trending page missing <article> entries")
        parser = _TrendingHtmlParser(since=safe_since)
        parser.feed(text)
        if not parser.items:
            raise SourceFetchError("malformed", "GitHub Trending page parsed zero entries")
    except (SourceFetchError, UnsafeUrlError, UnicodeDecodeError, ValueError) as exc:
        trending_cov = IngestionCoverage(
            run_id=run_id,
            source_class="github_trending",
            query_key=query_key,
            mode=mode,
            status="degraded" if allow_search_fallback else "error",
            incomplete=True,
            items_found=0,
            items_new=0,
            error=str(exc),
            last_success_at=None,
            recorded_at=now,
        )
        if not allow_search_fallback:
            return CollectionResult(artifacts=[], raw_evidence={}, coverage=[trending_cov])
        fallback = fetch_github_popular(
            transport,
            query=fallback_query,
            clock=lambda: now,
            max_items=max_items,
            timeout_s=timeout_s,
            # Preserve the 512 KiB search bound when Trending uses its 1 MiB default while honoring smaller caps.
            max_bytes=min(max_bytes, DEFAULT_MAX_BYTES),
            allowed_hosts=allowed_hosts,
            fallback_mode=True,
            run_id=run_id,
            mode=mode,
            profiles=profiles,
        )
        return CollectionResult(
            artifacts=fallback.artifacts,
            raw_evidence=fallback.raw_evidence,
            coverage=[trending_cov, *fallback.coverage],
        )

    artifacts: list[SourceArtifact] = []
    raw_evidence: dict[str, bytes] = {}
    for entry in parser.items[:max_items]:
        href = entry["href"]
        repo_slug = href.strip("/")
        publisher = repo_slug.split("/")[0] if "/" in repo_slug else "github"
        raw_url = f"https://github.com/{repo_slug}"
        stars_in_window = entry.get("stars_in_window")
        stars_window = entry.get("stars_window")
        stars_today = entry.get("stars_today")
        if stars_window != safe_since:
            stars_today = None
        measured_velocity = stars_in_window is not None and stars_window == safe_since
        item_bytes = json.dumps(entry, sort_keys=True).encode("utf-8")
        art = _build_artifact(
            raw_url=raw_url,
            source_class="github_trending",
            publisher=publisher,
            title=entry.get("title") or repo_slug,
            summary=entry.get("description") or "",
            raw_version="",
            trust_class="public_community",
            observed_at=now,
            raw_bytes=item_bytes,
            is_actual_trending=True,
            measured_star_velocity=measured_velocity,
            popularity_signal_only=True,
            degraded_fallback=False,
            extra_metadata={
                "stars_today": stars_today,
                "stars_in_window": stars_in_window,
                "stars_window": stars_window,
                "since": safe_since,
                "qualification_status": "intake_only",
            },
        )
        artifacts.append(art)
        raw_evidence[art.source_id] = item_bytes

    cov = IngestionCoverage(
        run_id=run_id,
        source_class="github_trending",
        query_key=query_key,
        mode=mode,
        status="ok",
        incomplete=False,
        items_found=len(artifacts),
        items_new=0,
        error=None,
        last_success_at=now,
        recorded_at=now,
    )
    return CollectionResult(artifacts=artifacts, raw_evidence=raw_evidence, coverage=[cov])


def fetch_github_releases(
    transport: Transport,
    *,
    repo: str,
    clock: Callable[[], float] | None = None,
    max_items: int = 10,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allowed_hosts: Sequence[str] = (),
    run_id: str = "direct",
    mode: str = "project-led",
    profiles: Sequence[ProjectProfile] = (),
) -> CollectionResult:
    """Fetch official repository releases from GitHub Releases JSON API."""
    now = (clock or time.time)()
    clean_repo = assert_no_private_leak(repo, profiles, context="github_releases repo")
    if not _REPO_SLUG_RE.match(clean_repo):
        raise SourceFetchError("malformed", f"Invalid GitHub repo slug: {repo!r}")
    url = f"https://api.github.com/repos/{clean_repo}/releases?per_page={max(1, min(max_items, 30))}"
    query_key = f"github_releases:{clean_repo}"
    publisher = clean_repo.split("/")[0]
    try:
        resp = transport.fetch(url, timeout_s=timeout_s, max_bytes=max_bytes, allowed_hosts=allowed_hosts)
        payload = json.loads(resp.body.decode("utf-8"))
        if not isinstance(payload, list):
            raise SourceFetchError("malformed", "GitHub releases payload must be a JSON array")
    except (SourceFetchError, UnsafeUrlError, UnicodeDecodeError, ValueError) as exc:
        cov = IngestionCoverage(
            run_id=run_id,
            source_class="github_releases",
            query_key=query_key,
            mode=mode,
            status="error",
            incomplete=True,
            items_found=0,
            items_new=0,
            error=str(exc),
            last_success_at=None,
            recorded_at=now,
        )
        return CollectionResult(artifacts=[], raw_evidence={}, coverage=[cov])

    artifacts: list[SourceArtifact] = []
    raw_evidence: dict[str, bytes] = {}
    for rel in payload[:max_items]:
        if not isinstance(rel, dict) or rel.get("draft"):
            continue
        tag = str(rel.get("tag_name") or "").strip()
        raw_url = str(rel.get("html_url") or f"https://github.com/{clean_repo}/releases/tag/{tag}")
        item_bytes = json.dumps(rel, sort_keys=True).encode("utf-8")
        art = _build_artifact(
            raw_url=raw_url,
            source_class="github_releases",
            publisher=publisher,
            title=str(rel.get("name") or f"{clean_repo} {tag}").strip(),
            summary=str(rel.get("body") or ""),
            raw_version=tag,
            trust_class="public_official",
            observed_at=now,
            raw_bytes=item_bytes,
            released_at=str(rel.get("published_at") or "") or None,
            is_actual_trending=False,
            measured_star_velocity=False,
            popularity_signal_only=False,
            degraded_fallback=False,
            extra_metadata={
                "repo": clean_repo,
                "prerelease": bool(rel.get("prerelease", False)),
                "qualification_status": "intake_only",
            },
        )
        artifacts.append(art)
        raw_evidence[art.source_id] = item_bytes

    cov = IngestionCoverage(
        run_id=run_id,
        source_class="github_releases",
        query_key=query_key,
        mode=mode,
        status="ok",
        incomplete=False,
        items_found=len(artifacts),
        items_new=0,
        error=None,
        last_success_at=now,
        recorded_at=now,
    )
    return CollectionResult(artifacts=artifacts, raw_evidence=raw_evidence, coverage=[cov])


def _parse_arxiv_xml_entries(
    xml_bytes: bytes,
    *,
    max_items: int,
    observed_at: float,
    degraded_fallback: bool = False,
    cached_from_at: float | None = None,
) -> tuple[list[SourceArtifact], dict[str, bytes]]:
    xml_text = xml_bytes.decode("utf-8")
    upper = xml_text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise SourceFetchError("malformed", "Rejected unsafe XML DTD/ENTITY declaration")
    root = ET.fromstring(xml_text)

    artifacts: list[SourceArtifact] = []
    raw_evidence: dict[str, bytes] = {}
    entries = root.findall("atom:entry", _ATOM_NS)
    for entry in entries[:max_items]:
        entry_id = (entry.findtext("atom:id", default="", namespaces=_ATOM_NS) or "").strip()
        if not entry_id:
            continue
        if entry_id.startswith("http://arxiv.org/"):
            entry_id = "https://" + entry_id[len("http://"):]
        ver_match = _ARXIV_ID_VER_RE.search(entry_id)
        raw_ver = ver_match.group(2) if ver_match and ver_match.group(2) else "v1"
        title = entry.findtext("atom:title", default="", namespaces=_ATOM_NS) or ""
        summary = entry.findtext("atom:summary", default="", namespaces=_ATOM_NS) or ""
        published = entry.findtext("atom:published", default="", namespaces=_ATOM_NS) or None
        authors = [
            a.findtext("atom:name", default="", namespaces=_ATOM_NS).strip()
            for a in entry.findall("atom:author", _ATOM_NS)
        ]
        authors = [a for a in authors if a]
        item_bytes = ET.tostring(entry, encoding="utf-8")
        extra_meta: dict[str, Any] = {
            "authors": authors[:10],
            "qualification_status": "intake_only",
            "source_label": "arxiv_cached_fallback" if degraded_fallback else "arxiv_live",
        }
        if cached_from_at is not None:
            extra_meta["cached_from_at"] = cached_from_at
        art = _build_artifact(
            raw_url=entry_id,
            source_class="arxiv",
            publisher=authors[0] if authors else "arXiv",
            title=title,
            summary=summary,
            raw_version=raw_ver,
            trust_class="public_academic",
            observed_at=observed_at,
            raw_bytes=item_bytes,
            released_at=published,
            is_actual_trending=False,
            measured_star_velocity=False,
            popularity_signal_only=False,
            degraded_fallback=degraded_fallback,
            extra_metadata=extra_meta,
        )
        artifacts.append(art)
        raw_evidence[art.source_id] = item_bytes
    return artifacts, raw_evidence


def fetch_arxiv_feed(
    transport: Transport,
    *,
    query: str,
    clock: Callable[[], float] | None = None,
    max_items: int = 15,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    allowed_hosts: Sequence[str] = (),
    run_id: str = "direct",
    mode: str = "project-led",
    profiles: Sequence[ProjectProfile] = (),
    max_retries: int = 2,
    backoff_s: float = 0.5,
    max_retry_after_s: float = 5.0,
    sleep_fn: Callable[[float], None] | None = None,
    cache_entry: dict[str, Any] | None = None,
    cache_ttl_s: float = 86_400.0,
) -> CollectionResult:
    """Fetch bounded targeted arXiv Atom feed with bounded backoff/Retry-After/cache fallback."""
    now = (clock or time.time)()
    safe_query = assert_no_private_leak(query, profiles, context="arxiv query")
    if not safe_query:
        raise SourceFetchError("malformed", "arXiv query cannot be empty")
    url = (
        "https://export.arxiv.org/api/query"
        f"?search_query={quote_plus(safe_query)}&start=0&max_results={max(1, min(max_items, 30))}"
        "&sortBy=submittedDate&sortOrder=descending"
    )
    query_key = f"arxiv:{safe_query}"
    eff_sleep = sleep_fn if sleep_fn is not None else ((lambda _: None) if isinstance(transport, FixtureTransport) else time.sleep)

    def _usable_cache() -> tuple[bytes, float] | None:
        if not isinstance(cache_entry, dict):
            return None
        raw_xml = cache_entry.get("raw_xml")
        cached_at = cache_entry.get("cached_at")
        if isinstance(raw_xml, (bytes, bytearray)) and raw_xml and isinstance(cached_at, (int, float)):
            if (now - float(cached_at)) <= cache_ttl_s:
                return bytes(raw_xml), float(cached_at)
        return None

    def _fallback_or_error(err_msg: str, *, retry_after_until: float | None = None, is_transient: bool = False) -> CollectionResult:
        cache_meta: dict[str, Any] = {}
        if retry_after_until is not None:
            cache_meta["retry_after_until"] = retry_after_until
        if is_transient:
            cached = _usable_cache()
            if cached is not None:
                raw_xml, cached_at = cached
                try:
                    c_arts, c_raw = _parse_arxiv_xml_entries(
                        raw_xml,
                        max_items=max_items,
                        observed_at=now,
                        degraded_fallback=True,
                        cached_from_at=cached_at,
                    )
                    cov = IngestionCoverage(
                        run_id=run_id,
                        source_class="arxiv",
                        query_key=query_key,
                        mode=mode,
                        status="degraded",
                        incomplete=True,
                        items_found=len(c_arts),
                        items_new=0,
                        error=f"arxiv_cached_fallback:{err_msg}",
                        last_success_at=cached_at,
                        recorded_at=now,
                    )
                    return CollectionResult(
                        artifacts=c_arts,
                        raw_evidence=c_raw,
                        coverage=[cov],
                        metadata={"arxiv_cache_updates": {query_key: cache_meta}} if cache_meta else {},
                    )
                except (SourceFetchError, UnicodeDecodeError, ET.ParseError):
                    pass
        cov = IngestionCoverage(
            run_id=run_id,
            source_class="arxiv",
            query_key=query_key,
            mode=mode,
            status="error",
            incomplete=True,
            items_found=0,
            items_new=0,
            error=err_msg,
            last_success_at=None,
            recorded_at=now,
        )
        return CollectionResult(
            artifacts=[],
            raw_evidence={},
            coverage=[cov],
            metadata={"arxiv_cache_updates": {query_key: cache_meta}} if cache_meta else {},
        )

    if isinstance(cache_entry, dict) and cache_entry.get("retry_after_until") is not None:
        cooldown_until = float(cache_entry["retry_after_until"])
        if now < cooldown_until:
            return _fallback_or_error(
                f"arxiv_429_cooldown_active:retry_after_until={cooldown_until:.0f}",
                retry_after_until=cooldown_until,
                is_transient=True,
            )

    last_exc: Exception | None = None
    last_transient = False
    retry_after_until: float | None = None
    attempts = max(1, 1 + max(0, max_retries))
    for attempt in range(attempts):
        try:
            resp = transport.fetch(url, timeout_s=timeout_s, max_bytes=max_bytes, allowed_hosts=allowed_hosts)
            artifacts, raw_evidence = _parse_arxiv_xml_entries(
                resp.body,
                max_items=max_items,
                observed_at=now,
                degraded_fallback=False,
            )
            cov = IngestionCoverage(
                run_id=run_id,
                source_class="arxiv",
                query_key=query_key,
                mode=mode,
                status="ok",
                incomplete=False,
                items_found=len(artifacts),
                items_new=0,
                error=None,
                last_success_at=now,
                recorded_at=now,
            )
            return CollectionResult(
                artifacts=artifacts,
                raw_evidence=raw_evidence,
                coverage=[cov],
                metadata={
                    "arxiv_cache_updates": {
                        query_key: {"raw_xml": resp.body, "cached_at": now, "retry_after_until": None}
                    }
                },
            )
        except SourceFetchError as exc:
            last_exc = exc
            is_transient = exc.kind == "timeout" or (
                exc.kind == "http_status" and exc.status_code in _TRANSIENT_HTTP_STATUSES
            )
            last_transient = is_transient
            if not is_transient:
                break
            if exc.status_code == 429 and exc.retry_after_s is not None and exc.retry_after_s > max_retry_after_s:
                retry_after_until = now + exc.retry_after_s
                break
            if attempt + 1 < attempts:
                delay = min(
                    max_retry_after_s,
                    exc.retry_after_s if exc.retry_after_s is not None else backoff_s * (2 ** attempt),
                )
                if delay > 0:
                    eff_sleep(delay)
        except (UnsafeUrlError, UnicodeDecodeError, ET.ParseError) as exc:
            last_exc = exc
            last_transient = False
            break

    err_text = str(last_exc or "arXiv fetch failed")
    if retry_after_until is not None:
        err_text = f"{err_text} (retry_after_until={retry_after_until:.0f})"
    return _fallback_or_error(err_text, retry_after_until=retry_after_until, is_transient=last_transient)


def _merge_results(results: Sequence[CollectionResult], max_items: int) -> CollectionResult:
    artifacts: list[SourceArtifact] = []
    seen_keys: set[tuple[str, str]] = set()
    raw_evidence: dict[str, bytes] = {}
    coverage: list[IngestionCoverage] = []
    merged_meta: dict[str, Any] = {}
    arxiv_updates: dict[str, Any] = {}
    for res in results:
        coverage.extend(res.coverage)
        if isinstance(res.metadata, dict):
            updates = res.metadata.get("arxiv_cache_updates")
            if isinstance(updates, dict):
                arxiv_updates.update(updates)
        for art in res.artifacts:
            key = (art.canonical_url, art.canonical_version)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            artifacts.append(art)
            if art.source_id in res.raw_evidence:
                raw_evidence[art.source_id] = res.raw_evidence[art.source_id]
            if len(artifacts) >= max_items:
                break
        if len(artifacts) >= max_items:
            break
    if arxiv_updates:
        merged_meta["arxiv_cache_updates"] = arxiv_updates
    return CollectionResult(artifacts=artifacts, raw_evidence=raw_evidence, coverage=coverage, metadata=merged_meta)


_CTX_WORD_RE = re.compile(r"[a-z0-9]{4,}")


def _extract_github_slug(raw: str) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if _REPO_SLUG_RE.match(text):
        return text.lower()
    if "://" in text:
        parts = urlsplit(text)
        if (parts.hostname or "").lower() in ("github.com", "www.github.com"):
            segs = [s for s in parts.path.split("/") if s]
            if len(segs) >= 2 and segs[0].lower() not in ("search", "trending", "topics", "orgs"):
                repo_name = segs[1][:-4] if segs[1].endswith(".git") else segs[1]
                slug = f"{segs[0]}/{repo_name}".lower()
                if _REPO_SLUG_RE.match(slug):
                    return slug
    return None


def _artifact_matches_profile_context(profile: ProjectProfile, art: SourceArtifact) -> bool:
    text = f"{art.title} {art.summary}".lower()
    if any(ex.lower() in text for ex in profile.exclusions):
        return False
    tokens = set(_CTX_WORD_RE.findall(text))
    for cap in profile.existing_capabilities:
        if (len(set(_CTX_WORD_RE.findall(cap.lower())) & tokens) >= 2 or cap.lower() in text) and not profile.known_gaps:
            return False
    for field_vals in (profile.public_concepts, profile.goals, profile.known_gaps):
        for item in field_vals:
            if set(_CTX_WORD_RE.findall(item.lower())) & tokens:
                return True
    return False


def derive_release_repos(
    profiles: Sequence[ProjectProfile] = (),
    artifacts: Sequence[SourceArtifact] = (),
    *,
    explicit_repos: Sequence[str] = (),
    max_repos: int = 3,
) -> list[str]:
    """Derive bounded public owner/repo candidates from explicit, profile, and source context."""
    chosen: list[str] = []
    seen: set[str] = set()

    def _try_add(raw: str) -> None:
        if len(chosen) >= max_repos:
            return
        slug = _extract_github_slug(raw)
        if not slug or slug in seen:
            return
        try:
            clean = assert_no_private_leak(slug, profiles, context="derived github_releases repo")
        except LearnerError:
            return
        if clean not in seen:
            seen.add(clean)
            chosen.append(clean)

    for raw in explicit_repos:
        _try_add(raw)

    for profile in profiles:
        prov = profile.provenance if isinstance(profile.provenance, dict) else {}
        for key in ("release_repos", "public_repos", "upstream_repos"):
            val = prov.get(key)
            if isinstance(val, (list, tuple)):
                for item in val:
                    _try_add(str(item))
        if isinstance(prov.get("public_repo"), str):
            _try_add(prov["public_repo"])

    for art in artifacts:
        if len(chosen) >= max_repos:
            break
        if art.source_class not in ("github_trending", "github_popular", "github_releases"):
            continue
        if profiles and not any(_artifact_matches_profile_context(p, art) for p in profiles):
            continue
        _try_add(art.canonical_url)

    return chosen[:max_repos]


def _missing_releases_coverage(
    *,
    run_id: str,
    mode: str,
    now: float,
    error: str = "no_repository_candidates_derived",
) -> CollectionResult:
    return CollectionResult(
        artifacts=[],
        raw_evidence={},
        coverage=[
            IngestionCoverage(
                run_id=run_id,
                source_class="github_releases",
                query_key="github_releases:none",
                mode=mode,
                status="missing",
                incomplete=True,
                items_found=0,
                items_new=0,
                error=error,
                last_success_at=None,
                recorded_at=now,
            )
        ],
    )


def collect_project_led(
    profile: ProjectProfile,
    transport: Transport,
    *,
    clock: Callable[[], float] | None = None,
    release_repos: Sequence[str] = (),
    auto_release_repos: bool = False,
    known_artifacts: Sequence[SourceArtifact] = (),
    run_id: str = "project-led",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = DEFAULT_MAX_BYTES,
    arxiv_cache: dict[str, dict[str, Any]] | None = None,
) -> CollectionResult:
    """Mode 1: collect bounded public material using only sanitized public concepts from a profile."""
    queries = build_public_queries(profile, max_queries=2)
    allowed = set(profile.source_policy.allowed_classes)
    per_source = profile.source_policy.max_items_per_source
    hosts = profile.source_policy.allowed_hosts
    results: list[CollectionResult] = []

    if queries and "github_popular" in allowed:
        for q in queries:
            results.append(
                fetch_github_popular(
                    transport,
                    query=q,
                    clock=clock,
                    max_items=per_source,
                    timeout_s=timeout_s,
                    max_bytes=max_bytes,
                    allowed_hosts=hosts,
                    run_id=run_id,
                    mode="project-led",
                    profiles=(profile,),
                )
            )
    if queries and "arxiv" in allowed:
        arxiv_q = " OR ".join(f'all:"{q}"' for q in queries)
        cache_entry = (arxiv_cache or {}).get(f"arxiv:{arxiv_q}")
        results.append(
            fetch_arxiv_feed(
                transport,
                query=arxiv_q,
                clock=clock,
                max_items=per_source,
                timeout_s=timeout_s,
                max_bytes=max_bytes,
                allowed_hosts=hosts,
                run_id=run_id,
                mode="project-led",
                profiles=(profile,),
                cache_entry=cache_entry,
            )
        )
    if "github_releases" in allowed:
        fetched_arts = [art for r in results for art in r.artifacts] + list(known_artifacts)
        candidate_repos = (
            derive_release_repos((profile,), fetched_arts, explicit_repos=release_repos, max_repos=3)
            if auto_release_repos
            else list(release_repos)
        )
        for repo in candidate_repos:
            results.append(
                fetch_github_releases(
                    transport,
                    repo=repo,
                    clock=clock,
                    max_items=per_source,
                    timeout_s=timeout_s,
                    max_bytes=max_bytes,
                    allowed_hosts=hosts,
                    run_id=run_id,
                    mode="project-led",
                    profiles=(profile,),
                )
            )
        if auto_release_repos and not candidate_repos:
            results.append(_missing_releases_coverage(run_id=run_id, mode="project-led", now=(clock or time.time)()))
    return _merge_results(
        results,
        min(profile.source_policy.max_items_per_sweep, PILOT_MAX_METADATA_ITEMS_PER_DAY),
    )


def collect_opportunity_led(
    transport: Transport,
    *,
    profiles: Sequence[ProjectProfile] = (),
    clock: Callable[[], float] | None = None,
    max_items_per_source: int = 20,
    max_items_per_sweep: int = 60,
    release_repos: Sequence[str] = (),
    auto_release_repos: bool = False,
    known_artifacts: Sequence[SourceArtifact] = (),
    arxiv_query: str = "cat:cs.SE OR cat:cs.AI",
    allow_search_fallback: bool = True,
    run_id: str = "opportunity-led",
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int | None = None,
    trending_max_bytes: int | None = None,
    arxiv_cache: dict[str, dict[str, Any]] | None = None,
) -> CollectionResult:
    """Mode 2: broad bounded intake across trends, popular repos, releases, and papers without user URLs."""
    bounded_per_source = max(1, min(max_items_per_source, PILOT_MAX_METADATA_ITEMS_PER_DAY))
    bounded_per_sweep = max(1, min(max_items_per_sweep, PILOT_MAX_METADATA_ITEMS_PER_DAY))
    other_max_bytes = DEFAULT_MAX_BYTES if max_bytes is None else max_bytes
    effective_trending_max_bytes = (
        trending_max_bytes
        if trending_max_bytes is not None
        else (max_bytes if max_bytes is not None else DEFAULT_TRENDING_MAX_BYTES)
    )
    arxiv_cache_entry = (arxiv_cache or {}).get(f"arxiv:{arxiv_query}")
    results: list[CollectionResult] = [
        fetch_github_trending(
            transport,
            clock=clock,
            max_items=bounded_per_source,
            timeout_s=timeout_s,
            max_bytes=effective_trending_max_bytes,
            allow_search_fallback=allow_search_fallback,
            run_id=run_id,
            mode="opportunity-led",
            profiles=profiles,
        ),
        fetch_github_popular(
            transport,
            query="stars:>200",
            clock=clock,
            max_items=bounded_per_source,
            timeout_s=timeout_s,
            max_bytes=other_max_bytes,
            run_id=run_id,
            mode="opportunity-led",
            profiles=profiles,
        ),
        fetch_arxiv_feed(
            transport,
            query=arxiv_query,
            clock=clock,
            max_items=bounded_per_source,
            timeout_s=timeout_s,
            max_bytes=other_max_bytes,
            run_id=run_id,
            mode="opportunity-led",
            profiles=profiles,
            cache_entry=arxiv_cache_entry,
        ),
    ]
    fetched_arts = [art for r in results for art in r.artifacts] + list(known_artifacts)
    candidate_repos = (
        derive_release_repos(profiles, fetched_arts, explicit_repos=release_repos, max_repos=3)
        if auto_release_repos
        else list(release_repos)
    )
    for repo in candidate_repos:
        results.append(
            fetch_github_releases(
                transport,
                repo=repo,
                clock=clock,
                max_items=bounded_per_source,
                timeout_s=timeout_s,
                max_bytes=other_max_bytes,
                run_id=run_id,
                mode="opportunity-led",
                profiles=profiles,
            )
        )
    if auto_release_repos and not candidate_repos:
        results.append(_missing_releases_coverage(run_id=run_id, mode="opportunity-led", now=(clock or time.time)()))
    return _merge_results(results, bounded_per_sweep)


class _PlainTextExtractor(HTMLParser):
    _BLOCK_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "div", "section", "blockquote", "pre", "tr", "br"})
    _SKIP_TAGS = frozenset({"script", "style", "nav", "footer"})

    def __init__(self) -> None:
        super().__init__()
        self._lines: list[str] = []
        self._buf: list[str] = []
        self._skip_depth = 0

    def _flush(self) -> None:
        line = " ".join("".join(self._buf).split())
        if line:
            self._lines.append(line)
        self._buf = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0 and data.strip():
            self._buf.append(" " + data.strip())

    def text(self) -> str:
        self._flush()
        return "\n".join(self._lines)


@dataclass(frozen=True)
class PrimaryEvidenceResult:
    ok: bool
    source_id: str
    canonical_url: str
    evidence_items: list[dict[str, Any]]
    raw_blobs: dict[str, bytes]
    license_id: str | None
    maintenance: dict[str, Any]
    version: str
    limitations: list[str]
    untrusted_directive_flags: tuple[str, ...]
    observed_at: float
    error: str | None = None
    error_kind: str | None = None
    insufficient_reason: str | None = None
    followup_attempted: list[dict[str, Any]] = field(default_factory=list)
    rejected_links: list[dict[str, str]] = field(default_factory=list)
    extraction_version: int = PRIMARY_EXTRACTION_VERSION


_BADGE_LINK_RE = re.compile(r"^(?:\[\s*!\[[^\]]*\]\([^)]*\)\s*\]\([^)]*\)|!\[[^\]]*\]\([^)]*\))$")
_BADGE_HOST_RE = re.compile(r"(?:shields\.io|badge\.fury\.io|codecov\.io|travis-ci|circleci|/workflows/[^)]+/badge)", re.I)
_HTML_CHROME_RE = re.compile(r"^<(?:!---|/?div\b|/?p\b|/?a\b|/?img\b|/?picture\b|/?source\b|/?table\b|/?tr\b|/?td\b|/?th\b|/?tbody\b|/?thead\b|/?details\b|/?summary\b|br\s*/?|hr\s*/?)", re.I)
_TOC_BULLET_RE = re.compile(r"^[-*+]\s+\[[^\]]+\]\(#[^)]+\)\s*$")
_INSTALL_CMD_RE = re.compile(
    r"^(?:[$#]\s*)?(?:pip3?|uv\s+(?:pip|add|tool)|npm|pnpm|yarn|cargo|brew|apt(?:-get)?|go\s+get|docker\s+pull)\s+(?:install|add|get|pull)\b",
    re.I,
)
_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]{1,120})\]\(([^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\)")
_HTML_HREF_RE = re.compile(r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.I | re.S)

_LOW_VALUE_HEADINGS = (
    "table of contents", "contents", "badges", "installation", "install",
    "sponsors", "sponsor", "contributors", "acknowledgments", "acknowledgements",
    "star history", "license", "citing", "citation", "social", "support",
)
_HIGH_VALUE_HEADINGS = (
    "architecture", "how it works", "design", "overview", "features", "capabilities",
    "usage", "cli", "api", "workflow", "pipeline", "evaluation", "benchmark",
    "verification", "testing", "configuration", "protocol", "specification", "spec",
    "limitations", "caveats", "security", "skill", "examples", "internals",
    "implementation", "method", "abstract", "approach",
)
_MECHANICS_TOKENS = frozenset(
    {
        "architecture", "workflow", "pipeline", "protocol", "schema", "contract",
        "command", "flags", "config", "configuration", "benchmark", "evaluation",
        "verification", "replay", "sqlite", "deterministic", "timeout", "retry",
        "backoff", "cache", "sandbox", "policy", "hook", "release", "rollback",
        "parser", "index", "retrieval", "citation", "evidence", "limitation",
    }
)
_DEFERRING_PHRASES = (
    "see ", "refer to", "documented in", "documentation", "read more",
    "details in", "details at", "architecture", "design doc", "skill", "guide",
)


def _profile_cue_tokens(profile: ProjectProfile | None) -> set[str]:
    if profile is None:
        return set()
    tokens: set[str] = set()
    for field_vals in (
        profile.public_concepts,
        profile.goals,
        profile.known_gaps,
        profile.desired_outcomes,
        profile.existing_capabilities,
    ):
        for val in field_vals:
            tokens.update(_CTX_WORD_RE.findall(str(val or "").lower()))
    return tokens


def _is_boilerplate_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if stripped in ("---", "***", "___", "===") or set(stripped) <= {"-", "=", "*", "_"}:
        return True
    if _BADGE_LINK_RE.match(stripped):
        return True
    if _BADGE_HOST_RE.search(stripped) and ("![" in stripped or "<img" in stripped.lower()):
        return True
    if _HTML_CHROME_RE.match(stripped) and stripped.endswith(">"):
        return True
    if _TOC_BULLET_RE.match(stripped):
        return True
    if "|" in stripped and any(tok in stripped.lower() for tok in ("english", "\u4e2d\u6587", "\u65e5\u672c\u8a9e", "español", "français")):
        if len(stripped) < 160 and stripped.count("]") >= 2:
            return True
    return False


def _strip_markdown_noise(text: str) -> str:
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cleaned)
    cleaned = re.sub(r"https?://\S+", " ", cleaned)
    cleaned = re.sub(r"[`*_>#|]+", " ", cleaned)
    return " ".join(cleaned.split()).strip()


def _split_document_sections(raw_text: str) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    cur_heading = ""
    cur_lines: list[dict[str, Any]] = []
    in_fence = False
    offset = 0

    for idx, line_with_nl in enumerate(raw_text.splitlines(keepends=True), start=1):
        line_no_nl = line_with_nl.rstrip("\r\n")
        stripped = line_no_nl.strip()
        line_start = offset
        line_end = offset + len(line_no_nl)
        offset += len(line_with_nl)

        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            cur_lines.append({"text": line_no_nl, "line_no": idx, "char_start": line_start, "char_end": line_end})
            continue
        if not in_fence:
            h_match = _MD_HEADING_RE.match(stripped)
            if h_match:
                if cur_lines or cur_heading:
                    sections.append({"heading": cur_heading, "lines": cur_lines})
                cur_heading = h_match.group(2).strip()
                cur_lines = [{"text": line_no_nl, "line_no": idx, "char_start": line_start, "char_end": line_end}]
                continue
        cur_lines.append({"text": line_no_nl, "line_no": idx, "char_start": line_start, "char_end": line_end})

    if cur_lines or cur_heading:
        sections.append({"heading": cur_heading, "lines": cur_lines})

    # If the document had only 1 giant headingless section with many lines, split by blank-line blocks
    # so long plain-text docs can still select the most relevant passages cleanly.
    if len(sections) == 1 and not sections[0]["heading"] and len(sections[0]["lines"]) > 18:
        blocks: list[dict[str, Any]] = []
        buf: list[dict[str, Any]] = []
        for entry in sections[0]["lines"]:
            if not entry["text"].strip() and len(buf) >= 4:
                blocks.append({"heading": "", "lines": buf})
                buf = []
            else:
                buf.append(entry)
        if buf:
            blocks.append({"heading": "", "lines": buf})
        if len(blocks) > 1:
            return blocks
    return sections


def extract_useful_sections(
    raw_text: str,
    *,
    source_url: str,
    digest: str,
    kind: str,
    source_version: str = "",
    document_truncated: bool = False,
    fetched_bytes: int | None = None,
    total_bytes_hint: int | None = None,
    cue_tokens: set[str] | None = None,
    max_sections: int = 3,
    max_chars_per_section: int = 2000,
    max_total_chars: int = 5500,
) -> list[dict[str, Any]]:
    """Inspect complete text, skip boilerplate, and select concrete project-relevant sections with truthful offsets."""
    if not raw_text.strip():
        return []
    cues = cue_tokens or set()
    sections = _split_document_sections(raw_text)
    scored: list[tuple[float, int, dict[str, Any]]] = []

    for sec_idx, sec in enumerate(sections):
        heading = str(sec["heading"] or "").strip()
        heading_low = heading.lower()
        kept: list[dict[str, Any]] = []
        install_lines = 0
        for entry in sec["lines"]:
            txt = entry["text"]
            if _MD_HEADING_RE.match(txt.strip()) and heading:
                # Keep heading line only for span anchor if section has body; skip counting heading alone as mechanics.
                continue
            if _is_boilerplate_line(txt):
                continue
            if _INSTALL_CMD_RE.match(txt.strip()):
                install_lines += 1
            kept.append(entry)

        if not kept:
            continue

        body_raw = "\n".join(e["text"] for e in kept)
        prose = _strip_markdown_noise(body_raw)
        if not prose:
            continue
        if install_lines == len(kept) and len(prose) < 120:
            # Skip install-command-only stubs when other sections exist, or score very low.
            pass

        tokens = set(_CTX_WORD_RE.findall(f"{heading_low} {prose.lower()}"))
        cue_hits = len(tokens & cues)
        mech_hits = len(tokens & _MECHANICS_TOKENS)
        is_low_heading = any(low_h in heading_low for low_h in _LOW_VALUE_HEADINGS)
        is_high_heading = any(high_h in heading_low for high_h in _HIGH_VALUE_HEADINGS)

        score = min(len(prose), 1600) / 160.0
        score += cue_hits * 3.0
        score += mech_hits * 1.5
        if is_high_heading:
            score += 4.0
        if is_low_heading:
            score -= 6.0
        if install_lines >= max(1, len(kept) // 2) and mech_hits == 0 and cue_hits == 0:
            score -= 4.0
        if len(prose) < 40 and not cues & tokens:
            score -= 2.0

        # Build bounded sanitized excerpt while tracking exact source line/char boundaries.
        acc_len = 0
        included: list[dict[str, Any]] = []
        partial_end_char: int | None = None
        section_truncated = False
        for entry in kept:
            clean_line, _ = sanitize_untrusted_text(entry["text"], max_len=max_chars_per_section)
            if not clean_line:
                continue
            sep = 1 if included else 0
            if acc_len + sep + len(clean_line) <= max_chars_per_section:
                included.append(entry)
                acc_len += sep + len(clean_line)
            else:
                remaining = max_chars_per_section - acc_len - sep
                if remaining >= 40:
                    included.append(entry)
                    partial_end_char = min(entry["char_end"], entry["char_start"] + remaining)
                section_truncated = True
                break

        if not included:
            continue
        if len(included) < len(kept):
            section_truncated = True

        span_start = included[0]["char_start"]
        span_end = partial_end_char if partial_end_char is not None else included[-1]["char_end"]
        slice_text = raw_text[span_start:span_end]
        full_body, _ = sanitize_untrusted_text(slice_text, max_len=max_chars_per_section + 1)
        if heading:
            full_excerpt = f"[{heading}] {full_body}" if full_body else heading
        else:
            full_excerpt = full_body
        if len(full_excerpt) > max_chars_per_section:
            section_truncated = True
            excerpt = full_excerpt[:max_chars_per_section]
        else:
            excerpt = full_excerpt
        if not excerpt:
            continue

        scored.append(
            (
                score,
                sec_idx,
                {
                    "kind": kind,
                    "url": source_url,
                    "source_version": source_version,
                    "digest": digest,
                    "section_title": heading or "overview",
                    "line_start": included[0]["line_no"],
                    "line_end": included[-1]["line_no"],
                    "char_start": span_start,
                    "char_end": span_end,
                    "excerpt": excerpt,
                    "truncated": bool(section_truncated or document_truncated),
                    "document_truncated": bool(document_truncated),
                    "fetched_bytes": fetched_bytes if fetched_bytes is not None else len(raw_text.encode("utf-8", errors="replace")),
                    **({"total_bytes_hint": total_bytes_hint} if total_bytes_hint is not None else {}),
                },
            )
        )

    if not scored:
        return []

    scored.sort(key=lambda tup: (-tup[0], tup[1]))
    chosen: list[dict[str, Any]] = []
    total_chars = 0
    for _, _, item in scored:
        if len(chosen) >= max_sections:
            break
        ex_len = len(item["excerpt"])
        if chosen and total_chars + ex_len > max_total_chars:
            continue
        if len(chosen) == 0:
            item["evidence_id"] = f"ev_{digest[:12]}"
        else:
            item["evidence_id"] = f"ev_{digest[:12]}_s{len(chosen)}"
        chosen.append(item)
        total_chars += ex_len
    return chosen


def _assess_mechanics_sufficiency(
    raw_text: str,
    selected_sections: Sequence[dict[str, Any]],
    *,
    has_links: bool,
) -> tuple[bool, str]:
    if not selected_sections:
        return False, "boilerplate_or_empty_only"
    combined = " ".join(str(s.get("excerpt") or "") for s in selected_sections)
    # Strip bracketed section prefix "[Heading] " and install commands to check concrete prose.
    without_headings = re.sub(r"\[[^\]]+\]\s*", " ", combined)
    prose = _strip_markdown_noise(without_headings)
    prose_no_install = " ".join(
        tok for tok in prose.split() if tok.lower() not in ("pip", "install", "npm", "yarn", "brew", "cargo")
    )
    if len(prose_no_install) < 20:
        return False, "no_concrete_mechanics"
    non_empty_lines = [ln.strip() for ln in raw_text.splitlines() if ln.strip()]
    if len(non_empty_lines) >= 5:
        boiler = sum(1 for ln in non_empty_lines if _is_boilerplate_line(ln) or _INSTALL_CMD_RE.match(ln))
        if (boiler / len(non_empty_lines)) > 0.70 and len(prose_no_install) < 100:
            return False, "boilerplate_heavy_insufficient_mechanics"
    if has_links and len(prose_no_install) < 140:
        low = without_headings.lower()
        if any(phrase in low for phrase in _DEFERRING_PHRASES):
            return False, "readme_defers_to_linked_docs"
    return True, "sufficient"


def _has_unsafe_path_segment(raw_path: str) -> str | None:
    decoded = unquote(raw_path)
    decoded_twice = unquote(decoded)
    if "\\" in raw_path or "\\" in decoded or "\\" in decoded_twice:
        return "invalid_backslash"
    for candidate in (raw_path, decoded, decoded_twice):
        segs = [s for s in candidate.split("/") if s]
        if any(s in (".", "..") for s in segs):
            return "path_traversal_rejected"
    return None


_ROOT_TRANSLATED_README_RE = re.compile(
    r"^readme(?:[._-](?:zh|ja|ko|es|fr|de|pt|ru|it|cn|tw|en)(?:[-_][a-z0-9]{2,4})?)?\.(?:md|markdown|rst|txt)$",
    re.I,
)


def github_repo_slug(canonical_url: str) -> str | None:
    """Extract 'owner/repo' slug from a GitHub or raw.githubusercontent.com URL."""
    parts = [seg for seg in urlsplit(canonical_url).path.split("/") if seg]
    if len(parts) < 2:
        return None
    return f"{parts[0]}/{parts[1]}"


def github_primary_urls(canonical_url: str) -> tuple[str, str, str] | None:
    """Return (slug, repo_api_url, readme_raw_url) for a GitHub repository URL."""
    slug = github_repo_slug(canonical_url)
    if slug is None:
        return None
    return (
        slug,
        f"https://api.github.com/repos/{slug}",
        f"https://raw.githubusercontent.com/{slug}/HEAD/README.md",
    )


def assess_readme_mechanics_and_targets(
    readme_text: str,
    readme_sections: Sequence[dict[str, Any]],
    *,
    slug: str,
    pinned_ref: str = "HEAD",
    allowed_hosts: Sequence[str] = (),
    cue_tokens: set[str] | None = None,
    max_targets: int = 2,
) -> tuple[bool, str, list[dict[str, Any]], list[dict[str, str]]]:
    """Single owner for README mechanics sufficiency and safe same-repo follow-up target extraction."""
    safe_targets, rejected_links = _extract_safe_followup_targets(
        readme_text,
        slug=slug,
        pinned_ref=pinned_ref,
        allowed_hosts=allowed_hosts,
        cue_tokens=cue_tokens or set(),
        max_targets=max_targets,
    )
    sufficient, suff_reason = _assess_mechanics_sufficiency(
        readme_text,
        readme_sections,
        has_links=bool(safe_targets or rejected_links),
    )
    return sufficient, suff_reason, safe_targets, rejected_links


def _extract_safe_followup_targets(
    readme_text: str,
    *,
    slug: str,
    pinned_ref: str,
    allowed_hosts: Sequence[str],
    cue_tokens: set[str],
    max_targets: int = 2,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Deterministically extract bounded same-repo doc/skill/source follow-up URLs, rejecting unsafe links."""
    raw_links: list[tuple[str, str]] = []
    for m in _MD_LINK_RE.finditer(readme_text):
        raw_links.append((m.group(1).strip(), m.group(2).strip()))
    for m in _HTML_HREF_RE.finditer(readme_text):
        label = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        raw_links.append((label, m.group(1).strip()))

    slug_low = slug.lower()
    candidates: dict[str, tuple[float, dict[str, Any]]] = {}
    rejected: list[dict[str, str]] = []

    def _reject(href: str, reason: str) -> None:
        if len(rejected) < 12:
            rejected.append({"href": href[:200], "reason": reason})

    for label, raw_href in raw_links:
        href = raw_href.strip("<>\"'")
        if not href or href.startswith("#"):
            continue
        low_href = href.lower()
        if low_href.startswith(("mailto:", "javascript:", "data:", "ftp://", "file://", "http://")):
            _reject(href, "forbidden_scheme")
            continue
        if "\\" in href or "\\" in unquote(href) or "\\" in unquote(unquote(href)):
            _reject(href, "invalid_backslash")
            continue

        target_url = ""
        rel_path = ""
        target_ref = pinned_ref or "HEAD"

        if "://" in href:
            parts = urlsplit(href)
            host = (parts.hostname or "").lower()
            if parts.scheme.lower() != "https":
                _reject(href, "non_https")
                continue
            if host in ("github.com", "www.github.com"):
                unsafe_reason = _has_unsafe_path_segment(parts.path)
                if unsafe_reason is not None:
                    _reject(href, unsafe_reason)
                    continue
                segs = [s for s in parts.path.split("/") if s]
                if len(segs) >= 5 and f"{segs[0]}/{segs[1]}".lower() == slug_low and segs[2] == "blob":
                    target_ref = segs[3]
                    rel_path = "/".join(segs[4:])
                    target_url = f"https://raw.githubusercontent.com/{slug_low}/{target_ref}/{rel_path}"
                elif len(segs) >= 2 and f"{segs[0]}/{segs[1]}".lower() != slug_low:
                    _reject(href, "cross_repo_link_rejected")
                    continue
                else:
                    _reject(href, "non_file_github_link")
                    continue
            elif host == "raw.githubusercontent.com":
                unsafe_reason = _has_unsafe_path_segment(parts.path)
                if unsafe_reason is not None:
                    _reject(href, unsafe_reason)
                    continue
                segs = [s for s in parts.path.split("/") if s]
                if len(segs) >= 4 and f"{segs[0]}/{segs[1]}".lower() == slug_low:
                    target_ref = segs[2]
                    rel_path = "/".join(segs[3:])
                    target_url = f"https://raw.githubusercontent.com/{slug_low}/{target_ref}/{rel_path}"
                else:
                    _reject(href, "cross_repo_raw_link_rejected")
                    continue
            else:
                _reject(href, "external_host_rejected")
                continue
        else:
            if href.startswith("//"):
                _reject(href, "protocol_relative_rejected")
                continue
            clean_path = href.split("#", 1)[0].split("?", 1)[0]
            if not clean_path or clean_path.startswith("/"):
                _reject(href, "absolute_or_empty_path_rejected")
                continue
            if clean_path.endswith("/"):
                _reject(href, "non_file_directory_link")
                continue
            while clean_path.startswith("./"):
                clean_path = clean_path[2:]
            unsafe_reason = _has_unsafe_path_segment(clean_path)
            if unsafe_reason is not None:
                _reject(href, unsafe_reason)
                continue
            raw_segs = [s for s in clean_path.split("/") if s]
            if not raw_segs:
                _reject(href, "path_traversal_rejected")
                continue
            rel_path = "/".join(raw_segs)
            target_url = f"https://raw.githubusercontent.com/{slug_low}/{target_ref}/{rel_path}"

        if rel_path.lower() in ("license", "license.md") or _ROOT_TRANSLATED_README_RE.match(rel_path):
            continue
        dot_idx = rel_path.rfind(".")
        ext = rel_path[dot_idx:].lower() if dot_idx != -1 and "/" not in rel_path[dot_idx:] else ""
        if ext in _FORBIDDEN_FOLLOWUP_EXTENSIONS:
            _reject(href, f"forbidden_extension:{ext}")
            continue
        if ext not in _ALLOWED_FOLLOWUP_EXTENSIONS:
            _reject(href, f"unsupported_extension:{ext}")
            continue

        try:
            validate_public_url(target_url, allowed_hosts=allowed_hosts)
        except UnsafeUrlError as exc:
            _reject(href, f"unsafe_url:{exc}")
            continue

        path_low = rel_path.lower()
        label_low = label.lower()
        if "skill.md" in path_low or "/skills/" in path_low or path_low.startswith("skills/"):
            f_kind = "followup_skill"
        elif ext in (".py", ".ts", ".tsx", ".js", ".mjs", ".go", ".rs", ".java", ".kt"):
            f_kind = "followup_source"
        else:
            f_kind = "followup_doc"

        score = 0.0
        for kw, pts in (
            ("skill.md", 6.0), ("architecture", 5.0), ("design", 4.5), ("spec", 4.5),
            ("usage", 4.0), ("workflow", 4.0), ("pipeline", 3.5), ("eval", 3.5),
            ("docs/", 3.0), ("skills/", 3.5), ("guide", 2.5), ("overview", 2.5),
        ):
            if kw in path_low or kw in label_low:
                score += pts
        for kw in ("changelog", "contributing", "code_of_conduct", "authors", "sponsors"):
            if kw in path_low:
                score -= 5.0
        toks = set(_CTX_WORD_RE.findall(f"{path_low} {label_low}"))
        score += len(toks & cue_tokens) * 2.5
        if f_kind == "followup_source":
            score += 1.0

        prev = candidates.get(target_url)
        info = {"url": target_url, "rel_path": rel_path, "ref": target_ref, "kind": f_kind, "label": label[:80]}
        if prev is None or score > prev[0]:
            candidates[target_url] = (score, info)

    ordered = [info for _, info in sorted(candidates.values(), key=lambda x: (-x[0], x[1]["rel_path"]))]
    return ordered[:max_targets], rejected


def _extract_limitation_lines(text: str, limit: int = 4) -> list[str]:
    markers = ("limitation", "caveat", "warning", "experimental", "not suitable", "requires", "only supports")
    found: list[str] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.strip().split())
        if not line or len(line) < 10:
            continue
        low = line.lower()
        if any(m in low for m in markers):
            clean, _ = sanitize_untrusted_text(line, max_len=240)
            if clean and clean not in found:
                found.append(clean)
                if len(found) >= limit:
                    break
    return found


def _inspect_full_text_flags(text: str) -> tuple[str, ...]:
    if not text:
        return ()
    _, flags = sanitize_untrusted_text(text, max_len=min(len(text), 4096))
    return flags


def fetch_primary_evidence(
    artifact: SourceArtifact,
    transport: Transport,
    *,
    clock: Callable[[], float] | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    max_bytes: int = 262_144,
    allowed_hosts: Sequence[str] = (),
    profile: ProjectProfile | None = None,
    cached_blobs: dict[str, bytes] | None = None,
    followup_budget: Callable[[str], tuple[bool, str | None]] | None = None,
    max_followup_files: int = 2,
    force_followup: bool = False,
) -> PrimaryEvidenceResult:
    """Fetch bounded primary evidence (metadata, non-boilerplate sections, safe same-repo follow-up) via SSRF-safe transport."""
    now = (clock or time.time)()
    items: list[dict[str, Any]] = []
    raw_blobs: dict[str, bytes] = {}
    all_flags: list[str] = list(artifact.untrusted_directive_flags)
    license_id = artifact.license_id
    version = artifact.canonical_version
    maintenance: dict[str, Any] = {}
    limitations: list[str] = []
    followup_attempted: list[dict[str, Any]] = []
    rejected_links: list[dict[str, str]] = []
    cue_tokens = _profile_cue_tokens(profile)
    blob_cache = dict(cached_blobs or {})

    def _fetch_or_cache(url: str, *, allow_truncated: bool) -> TransportResponse:
        if url in blob_cache:
            cached = blob_cache[url]
            return TransportResponse(
                status_code=200,
                body=cached[:max_bytes] if allow_truncated else cached,
                headers={"x-cache": "local-evidence-blob"},
                final_url=url,
                truncated=allow_truncated and len(cached) > max_bytes,
                total_bytes_hint=len(cached),
            )
        return transport.fetch(
            url,
            timeout_s=timeout_s,
            max_bytes=max_bytes,
            allowed_hosts=allowed_hosts,
            allow_truncated=allow_truncated,
        )

    try:
        if artifact.source_class in ("github_trending", "github_popular", "github_releases"):
            gh_urls = github_primary_urls(artifact.canonical_url)
            if gh_urls is None:
                raise SourceFetchError("malformed", f"Cannot extract owner/repo from {artifact.canonical_url!r}")
            slug, repo_url, readme_url = gh_urls
            repo_resp = _fetch_or_cache(repo_url, allow_truncated=False)
            repo_json = json.loads(repo_resp.body.decode("utf-8"))
            if not isinstance(repo_json, dict):
                raise SourceFetchError("malformed", "GitHub repo metadata must be a JSON object")
            lic = repo_json.get("license")
            if isinstance(lic, dict) and lic.get("spdx_id"):
                license_id = str(lic["spdx_id"])
            default_branch = str(repo_json.get("default_branch") or "").strip()
            maintenance = {
                "pushed_at": repo_json.get("pushed_at"),
                "archived": bool(repo_json.get("archived", False)),
                "open_issues_count": repo_json.get("open_issues_count"),
                "default_branch": repo_json.get("default_branch"),
                "stargazers_count": repo_json.get("stargazers_count"),
            }
            if repo_json.get("archived"):
                limitations.append("Repository is archived")
            repo_digest = sha256_bytes(repo_resp.body)
            raw_blobs[repo_digest] = repo_resp.body
            desc_clean, desc_flags = sanitize_untrusted_text(str(repo_json.get("description") or ""), max_len=600)
            all_flags.extend(desc_flags)
            items.append(
                {
                    "evidence_id": f"ev_{repo_digest[:12]}",
                    "kind": "github_repo_metadata",
                    "url": repo_url,
                    "source_version": version,
                    "digest": repo_digest,
                    "excerpt": desc_clean,
                    "truncated": False,
                    "document_truncated": False,
                }
            )

            pinned_ref = (
                artifact.raw_version
                if artifact.source_class == "github_releases" and artifact.raw_version
                else (str((artifact.metadata or {}).get("pinned_ref") or "").strip() or default_branch or "HEAD")
            )
            readme_resp = _fetch_or_cache(readme_url, allow_truncated=True)
            readme_text = readme_resp.body.decode("utf-8", errors="replace")
            readme_digest = sha256_bytes(readme_resp.body)
            raw_blobs[readme_digest] = readme_resp.body
            all_flags.extend(_inspect_full_text_flags(readme_text))
            limitations.extend(_extract_limitation_lines(readme_text))

            readme_sections = extract_useful_sections(
                readme_text,
                source_url=readme_url,
                digest=readme_digest,
                kind="readme",
                source_version=version or pinned_ref,
                document_truncated=readme_resp.truncated,
                fetched_bytes=len(readme_resp.body),
                total_bytes_hint=readme_resp.total_bytes_hint,
                cue_tokens=cue_tokens,
            )
            items.extend(readme_sections)

            sufficient, suff_reason, safe_targets, rejected_links = assess_readme_mechanics_and_targets(
                readme_text,
                readme_sections,
                slug=slug,
                pinned_ref=pinned_ref,
                allowed_hosts=allowed_hosts,
                cue_tokens=cue_tokens,
                max_targets=max_followup_files,
            )
            if not sufficient or (force_followup and safe_targets):
                followup_sections_added = 0
                followup_errors: list[str] = []
                for target in safe_targets:
                    if followup_budget is not None and target["url"] not in blob_cache:
                        allowed_budget, budget_reason = followup_budget(target["url"])
                        if not allowed_budget:
                            followup_errors.append(budget_reason or "followup_fetch_budget_exhausted")
                            followup_attempted.append({"url": target["url"], "status": "blocked", "reason": budget_reason})
                            break
                    try:
                        f_resp = _fetch_or_cache(target["url"], allow_truncated=True)
                        f_text = f_resp.body.decode("utf-8", errors="replace")
                        f_digest = sha256_bytes(f_resp.body)
                        raw_blobs[f_digest] = f_resp.body
                        all_flags.extend(_inspect_full_text_flags(f_text))
                        limitations.extend(_extract_limitation_lines(f_text))
                        f_sections = extract_useful_sections(
                            f_text,
                            source_url=target["url"],
                            digest=f_digest,
                            kind=target["kind"],
                            source_version=target["ref"],
                            document_truncated=f_resp.truncated,
                            fetched_bytes=len(f_resp.body),
                            total_bytes_hint=f_resp.total_bytes_hint,
                            cue_tokens=cue_tokens,
                            max_sections=2,
                            max_chars_per_section=1800,
                            max_total_chars=3600,
                        )
                        if f_sections:
                            items.extend(f_sections)
                            followup_sections_added += len(f_sections)
                        followup_attempted.append(
                            {
                                "url": target["url"],
                                "kind": target["kind"],
                                "ref": target["ref"],
                                "digest": f_digest,
                                "sections": len(f_sections),
                                "status": "ok" if f_sections else "empty",
                            }
                        )
                    except (SourceFetchError, UnsafeUrlError, UnicodeDecodeError, ValueError) as f_exc:
                        followup_errors.append(str(f_exc))
                        followup_attempted.append({"url": target["url"], "status": "error", "error": str(f_exc)})

                if not sufficient and followup_sections_added == 0:
                    if followup_errors and any("followup_fetch_budget_exhausted" in e or "budget" in e for e in followup_errors):
                        insuff_code = "followup_fetch_budget_exhausted"
                    elif followup_errors:
                        insuff_code = f"followup_fetch_failed:{followup_errors[0]}"
                    elif rejected_links and not safe_targets:
                        insuff_code = "unsafe_followup_links_rejected"
                    else:
                        insuff_code = f"{suff_reason}_no_safe_followup"
                    return PrimaryEvidenceResult(
                        ok=False,
                        source_id=artifact.source_id,
                        canonical_url=artifact.canonical_url,
                        evidence_items=items,
                        raw_blobs=raw_blobs,
                        license_id=license_id,
                        maintenance=maintenance,
                        version=version,
                        limitations=list(dict.fromkeys([*limitations, f"Insufficient primary coverage: {insuff_code}"]))[:4],
                        untrusted_directive_flags=tuple(dict.fromkeys(all_flags)),
                        observed_at=now,
                        error=f"insufficient_primary_evidence:{insuff_code}",
                        error_kind="insufficient_evidence",
                        insufficient_reason=insuff_code,
                        followup_attempted=followup_attempted,
                        rejected_links=rejected_links,
                    )

        elif artifact.source_class == "arxiv":
            resp = _fetch_or_cache(artifact.canonical_url, allow_truncated=True)
            html_text = resp.body.decode("utf-8", errors="replace")
            extractor = _PlainTextExtractor()
            extractor.feed(html_text)
            plain = extractor.text() or html_text
            digest = sha256_bytes(resp.body)
            raw_blobs[digest] = resp.body
            all_flags.extend(_inspect_full_text_flags(plain))
            limitations.extend(_extract_limitation_lines(plain))
            maintenance = {"source_type": "arxiv_preprint", "released_at": artifact.released_at}
            extracted = extract_useful_sections(
                plain,
                source_url=artifact.canonical_url,
                digest=digest,
                kind="arxiv_abstract",
                source_version=version,
                document_truncated=resp.truncated,
                fetched_bytes=len(resp.body),
                total_bytes_hint=resp.total_bytes_hint,
                cue_tokens=cue_tokens,
                max_sections=2,
            )
            if not extracted:
                clean, flags = sanitize_untrusted_text(plain, max_len=2000)
                all_flags.extend(flags)
                if clean:
                    extracted = [
                        {
                            "evidence_id": f"ev_{digest[:12]}",
                            "kind": "arxiv_abstract",
                            "url": artifact.canonical_url,
                            "source_version": version,
                            "digest": digest,
                            "excerpt": clean,
                            "truncated": bool(resp.truncated),
                            "document_truncated": bool(resp.truncated),
                        }
                    ]
            items.extend(extracted)
        else:
            resp = _fetch_or_cache(artifact.canonical_url, allow_truncated=True)
            text = resp.body.decode("utf-8", errors="replace")
            digest = sha256_bytes(resp.body)
            raw_blobs[digest] = resp.body
            all_flags.extend(_inspect_full_text_flags(text))
            limitations.extend(_extract_limitation_lines(text))
            extracted = extract_useful_sections(
                text,
                source_url=artifact.canonical_url,
                digest=digest,
                kind="primary_page",
                source_version=version,
                document_truncated=resp.truncated,
                fetched_bytes=len(resp.body),
                total_bytes_hint=resp.total_bytes_hint,
                cue_tokens=cue_tokens,
            )
            items.extend(extracted)
    except SourceFetchError as exc:
        return PrimaryEvidenceResult(
            ok=False,
            source_id=artifact.source_id,
            canonical_url=artifact.canonical_url,
            evidence_items=items,
            raw_blobs=raw_blobs,
            license_id=license_id,
            maintenance=maintenance,
            version=version,
            limitations=limitations,
            untrusted_directive_flags=tuple(dict.fromkeys(all_flags)),
            observed_at=now,
            error=str(exc),
            error_kind=exc.kind,
            followup_attempted=followup_attempted,
            rejected_links=rejected_links,
        )
    except (UnsafeUrlError, UnicodeDecodeError, ValueError) as exc:
        return PrimaryEvidenceResult(
            ok=False,
            source_id=artifact.source_id,
            canonical_url=artifact.canonical_url,
            evidence_items=items,
            raw_blobs=raw_blobs,
            license_id=license_id,
            maintenance=maintenance,
            version=version,
            limitations=limitations,
            untrusted_directive_flags=tuple(dict.fromkeys(all_flags)),
            observed_at=now,
            error=str(exc),
            error_kind="validation",
            followup_attempted=followup_attempted,
            rejected_links=rejected_links,
        )

    return PrimaryEvidenceResult(
        ok=True,
        source_id=artifact.source_id,
        canonical_url=artifact.canonical_url,
        evidence_items=items,
        raw_blobs=raw_blobs,
        license_id=license_id,
        maintenance=maintenance,
        version=version,
        limitations=list(dict.fromkeys(limitations))[:4],
        untrusted_directive_flags=tuple(dict.fromkeys(all_flags)),
        observed_at=now,
        error=None,
        followup_attempted=followup_attempted,
        rejected_links=rejected_links,
    )

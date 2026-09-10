"""Bounded, stateless collection of public, explicitly authorized source documents."""

from __future__ import annotations

import asyncio
import base64
import binascii
import codecs
import hashlib
import importlib.util
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import sys
import time
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import certifi
import httpx

from cxplorer.insights.urls import (
    SourceError,
    approved_hosts,
    is_approved_url,
    is_public_address,
    normalize_url,
)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36 Edg/152.0.0.0"
)
# Browser-compatible headers do not opt out of CXplorer's robots directives.
_ROBOT_TOKEN = "cxplorerinsights"
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_GENERIC_TYPES = frozenset({"", "application/octet-stream", "binary/octet-stream", "text/plain"})
_SPACE = re.compile(r"\s+")
_HTML_START = re.compile(
    rb"<(?:!doctype\s+html|html\b|head\b|body\b|title\b|main\b|article\b|div\b|p\b|h[1-6]\b)",
    re.IGNORECASE,
)
_CHALLENGE_TITLE = re.compile(
    rb"<title[^>]*>\s*(?:just a moment|checking your browser|attention required|"
    rb"verify (?:you are|you're) human|security verification|security check)",
    re.IGNORECASE,
)
_SCRAPE_HEADERS = frozenset(
    {
        "content-type",
        "content-length",
        "content-encoding",
        "location",
        "retry-after",
        "cf-mitigated",
        "x-amzn-waf-action",
        "server",
    }
)
_SCRAPE_ERRORS = {
    "source_timeout": "The source did not respond within the time limit.",
    "source_network_error": "A secure connection to the source failed.",
    "source_challenge": (
        "The source requires a browser challenge that could not be completed. "
        "Provide another public source."
    ),
    "source_too_large": "The source exceeds the supported download or decoded size.",
    "unsupported_media_type": "Only accessible HTML and text-based PDF sources are supported.",
    "unsupported_encoding": "The source returned an unsupported content encoding.",
    "invalid_response": "The source returned an invalid response.",
    "unapproved_host": "The browser challenge requested a destination that was not approved.",
    "unsafe_url": "The browser challenge requested an unsafe URL.",
    "source_scrape_failed": "The source could not be fetched with the browser fallback.",
    "source_scrape_unavailable": "Isolated browser fetching is unavailable on this server.",
}


@dataclass(frozen=True, slots=True)
class FetchLimits:
    request_timeout: float = 20.0
    total_timeout: float = 90.0
    max_redirects: int = 3
    max_html_bytes: int = 2_097_152
    max_pdf_bytes: int = 15_728_640
    max_robots_bytes: int = 65_536
    max_pdf_pages: int = 200
    max_text_chars: int = 160_000
    max_spans: int = 800
    max_span_chars: int = 2000
    max_links: int = 100
    min_text_chars: int = 40
    max_html_nodes: int = 100_000
    max_html_depth: int = 256
    html_parse_timeout: float = 3.0
    pdf_parse_timeout: float = 10.0
    pdf_memory_bytes: int = 268_435_456
    max_concurrent_fetches: int = 4
    max_cached_hosts: int = 256
    robots_cache_seconds: float = 3600.0
    max_crawl_delay: float = 20.0

    def __post_init__(self) -> None:
        integer_bounds = {
            "max_redirects": (0, 3),
            "max_html_bytes": (1, 2_097_152),
            "max_pdf_bytes": (1, 15_728_640),
            "max_robots_bytes": (1, 524_288),
            "max_pdf_pages": (1, 200),
            "max_text_chars": (1, 500_000),
            "max_spans": (1, 2000),
            "max_span_chars": (1, 8000),
            "max_links": (1, 256),
            "min_text_chars": (1, 1000),
            "max_html_nodes": (1, 100_000),
            "max_html_depth": (1, 256),
            "pdf_memory_bytes": (67_108_864, 536_870_912),
            "max_concurrent_fetches": (1, 32),
            "max_cached_hosts": (1, 1024),
        }
        for name, (minimum, maximum) in integer_bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside its supported safety bounds.")
        for name in (
            "request_timeout",
            "total_timeout",
            "html_parse_timeout",
            "pdf_parse_timeout",
            "robots_cache_seconds",
            "max_crawl_delay",
        ):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")
        if self.request_timeout > 20 or self.pdf_parse_timeout > 30 or self.total_timeout > 180:
            raise ValueError("Source timeouts exceed their supported safety bounds.")
        if self.html_parse_timeout > 5 or self.robots_cache_seconds > 86_400:
            raise ValueError("HTML parsing or robots caching exceeds its supported safety bounds.")


@dataclass(frozen=True, slots=True)
class TextSpan:
    id: str
    text: str
    page: int | None = None
    section: str | None = None


@dataclass(frozen=True, slots=True)
class SourceDocument:
    id: str
    original_url: str
    url: str
    title: str
    media_type: str
    text: str
    spans: tuple[TextSpan, ...]
    content_hash: str
    retrieved_at: datetime
    published_at: date | None
    links: tuple[str, ...]
    coverage: dict[str, Any]
    publication_evidence: tuple[TextSpan, ...] = ()
    purpose: str = "other"


@dataclass(slots=True)
class _Block:
    text: str
    page: int | None = None
    section: str | None = None
    publication: bool = False
    publication_date: date | None = None


@dataclass(slots=True)
class _Reply:
    url: str
    status: int
    headers: httpx.Headers
    body: bytes = b""
    media_type: str = ""


def _is_challenge(headers: httpx.Headers, body: bytes = b"") -> bool:
    if any(
        headers.get(name, "").lower() == "challenge"
        for name in ("cf-mitigated", "x-amzn-waf-action")
    ):
        return True
    sample = body[:65_536].lower()
    if headers.get("server", "").lower().startswith("cloudflare") and all(
        marker in sample
        for marker in (
            b"<form ",
            b'"challenge-form"',
            b"__cf_chl_f_tk=",
            b"/cdn-cgi/images/trace/jsch/",
        )
    ):
        return True
    return _CHALLENGE_TITLE.search(sample) is not None and any(
        marker in sample
        for marker in (
            b"/cdn-cgi/challenge-platform/",
            b"cf-chl-",
            b"cf_chl_",
            b"jschl_vc",
            b"challenges.cloudflare.com",
            b"hcaptcha.com",
            b"g-recaptcha",
        )
    )


def _clean_text(value: str) -> str:
    return _SPACE.sub(" ", value.replace("\x00", "")).strip()


def _publication_date(value: str) -> date | None:
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return date.fromisoformat(value)
        if re.match(r"^\d{4}-\d{2}-\d{2}T", value):
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    return None


def _robot_path(value: str) -> str:
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"

    def escape(match: re.Match[str]) -> str:
        character = chr(int(match[0][1:], 16))
        return character if character in unreserved else match[0].upper()

    return re.sub(r"%[a-fA-F0-9]{2}", escape, quote(value, safe="/?:@!$&'()*+,;=-._~%"))


def _robot_match(pattern: str, target: str) -> bool:
    """Match robots wildcards without a backtracking regular expression."""
    anchored_end = pattern.endswith("$")
    if anchored_end:
        pattern = pattern[:-1]
    pieces = pattern.split("*")
    if not target.startswith(pieces[0]):
        return False
    position = len(pieces[0])
    if len(pieces) == 1:
        return not anchored_end or position == len(target)
    for index, piece in enumerate(pieces[1:], 1):
        if index == len(pieces) - 1 and anchored_end:
            return target.endswith(piece) and len(target) - len(piece) >= position
        found = target.find(piece, position)
        if found < 0:
            return False
        position = found + len(piece)
    return True


@dataclass(slots=True)
class _RobotsPolicy:
    rules: tuple[tuple[str, bool], ...] = ()
    delay: float = 0.0

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        target = _robot_path(parts.path or "/")
        if "?" in url:
            target += "?" + _robot_path(parts.query)
        matched = [
            (len(re.findall(r"%[A-F0-9]{2}|.", pattern.rstrip("$").replace("*", ""))), allow)
            for pattern, allow in self.rules
            if _robot_match(pattern, target)
        ]
        return not matched or max(matched)[1]


def _parse_robots(body: bytes, content_type: str, limits: FetchLimits) -> _RobotsPolicy:
    if content_type not in _GENERIC_TYPES | {"text/x-robots"}:
        raise SourceError(
            "robots_unavailable", "The site's automated-access policy is unavailable."
        )
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError:
        raise SourceError(
            "robots_unavailable", "The site's automated-access policy could not be read."
        ) from None
    groups: list[tuple[list[str], list[tuple[str, bool]], float]] = []
    agents: list[str] = []
    rules: list[tuple[str, bool]] = []
    delay = 0.0
    has_directives = False
    recognized = False
    nonempty = False
    lines = text.splitlines()
    if len(lines) > 10_000:
        raise SourceError("robots_unavailable", "The site's automated-access policy is too large.")
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        nonempty = True
        if ":" not in line or line.startswith(("<", "{")):
            raise SourceError(
                "robots_unavailable", "The site's automated-access policy could not be read."
            )
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key in {"allow", "disallow", "crawl-delay", "request-rate"} and not agents:
            raise SourceError("robots_unavailable", "The site's access policy is invalid.")
        if key == "user-agent":
            recognized = True
            if has_directives:
                groups.append((agents, rules, delay))
                agents, rules, delay, has_directives = [], [], 0.0, False
            if not value or len(value) > 100:
                raise SourceError("robots_unavailable", "The site's access policy is invalid.")
            agents.append(value.lower())
        elif key in {"allow", "disallow"} and agents:
            has_directives = True
            if value:
                if len(value) > 2048 or not value.startswith(("/", "*")):
                    raise SourceError("robots_unavailable", "The site's access policy is invalid.")
                rules.append((_robot_path(value), key == "allow"))
        elif key in {"crawl-delay", "request-rate"} and agents:
            has_directives = True
            try:
                if key == "request-rate":
                    requests, seconds = value.split("/", 1)
                    candidate = float(seconds) / float(requests)
                else:
                    candidate = float(value)
            except (ValueError, ZeroDivisionError):
                raise SourceError(
                    "robots_unavailable", "The site's access policy is invalid."
                ) from None
            if not math.isfinite(candidate) or candidate < 0:
                raise SourceError("robots_unavailable", "The site's access policy is invalid.")
            delay = max(delay, candidate)
        elif key in {"sitemap", "host"}:
            recognized = True
    if nonempty and not recognized:
        raise SourceError("robots_unavailable", "The site's access policy could not be read.")
    if agents:
        groups.append((agents, rules, delay))
    selected: list[tuple[list[tuple[str, bool]], float]] = []
    best = -1
    for agents, rules, delay in groups:
        scores = [
            0 if agent == "*" else len(agent)
            for agent in agents
            if agent == "*" or agent in _ROBOT_TOKEN
        ]
        if not scores:
            continue
        score = max(scores)
        if score > best:
            best, selected = score, []
        if score == best:
            selected.append((rules, delay))
    rules = [rule for group_rules, _ in selected for rule in group_rules]
    delay = max((group_delay for _, group_delay in selected), default=0.0)
    if delay > limits.max_crawl_delay:
        raise SourceError(
            "robots_delay", "The site requires a crawl delay longer than this request can support."
        )
    return _RobotsPolicy(tuple(rules), delay)


@dataclass(slots=True)
class _HostState:
    network_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    policy_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    next_request_at: float = 0.0
    last_request_at: float = 0.0
    policy: _RobotsPolicy | None = None
    policy_expires: float = 0.0


class _BorrowedTransport(httpx.AsyncBaseTransport):
    """An injected test transport is closed by the fetcher, not by each request."""

    def __init__(self, transport: httpx.AsyncBaseTransport):
        self.transport = transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return await self.transport.handle_async_request(request)


async def _resolve(host: str) -> Iterable[str]:
    records = await asyncio.get_running_loop().getaddrinfo(
        host, 443, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    return tuple(dict.fromkeys(record[4][0] for record in records))


class SourceFetcher:
    """Fetch only validated addresses using isolated, certificate-verified requests.

    ``resolver`` is an async hostname-to-IP-strings function. ``transport`` is a
    trusted injectable HTTPX transport for offline tests, not a proxy. Requests
    presented to it already use numeric IP URLs, original Host headers, and an
    explicit ``sni_hostname`` extension.

    There is no automatic www/apex counterpart-verification API. Callers must
    supply explicitly approved exact hosts. An unapproved canonical redirect
    fails with ``unapproved_host`` and a safe, displayable explanation.
    """

    def __init__(
        self,
        limits: FetchLimits | None = None,
        *,
        resolver: Callable[[str], Awaitable[Iterable[str]]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.limits = limits or FetchLimits()
        self._resolver = resolver or _resolve
        self._transport = transport
        self._clock = clock or (lambda: datetime.now(UTC))
        self._semaphore = asyncio.Semaphore(self.limits.max_concurrent_fetches)
        self._hosts: dict[str, _HostState] = {}
        self._closed = False
        self._tls = ssl.create_default_context(cafile=certifi.where())

    async def __aenter__(self) -> SourceFetcher:
        if self._closed:
            raise RuntimeError("The source fetcher is closed.")
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._transport is not None:
                await self._transport.aclose()
            self._hosts.clear()

    @asynccontextmanager
    async def _host_state(self, host: str) -> AsyncIterator[_HostState]:
        state = self._hosts.get(host)
        if state is None:
            if len(self._hosts) >= self.limits.max_cached_hosts:
                candidates = [
                    (item.last_request_at, key)
                    for key, item in self._hosts.items()
                    if not item.users and item.next_request_at <= time.monotonic()
                ]
                if not candidates:
                    raise SourceError(
                        "source_busy", "Source collection is busy. Try again shortly."
                    )
                del self._hosts[min(candidates)[1]]
            state = self._hosts[host] = _HostState()
        state.users += 1
        try:
            yield state
        finally:
            state.users -= 1

    async def _addresses(self, host: str) -> tuple[str, ...]:
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            addresses = (str(address),)
        else:
            try:
                resolved = await self._resolver(host)
                addresses_list = []
                for value in resolved:
                    if len(addresses_list) >= 32:
                        raise SourceError(
                            "dns_failed", "The source hostname could not be resolved safely."
                        )
                    addresses_list.append(value)
                addresses = tuple(dict.fromkeys(addresses_list))
            except SourceError:
                raise
            except (OSError, ValueError, TypeError):
                raise SourceError(
                    "dns_failed", "The source hostname could not be resolved."
                ) from None
        if not addresses:
            raise SourceError("dns_failed", "The source hostname could not be resolved.")
        if any(
            not isinstance(address, str) or not is_public_address(address) for address in addresses
        ):
            raise SourceError(
                "unsafe_address",
                "The source hostname does not resolve exclusively to public addresses.",
            )
        return addresses

    async def fetch(
        self, url: str, *, purpose: str = "other", allowed_hosts: set[str] | None = None
    ) -> SourceDocument:
        if self._closed:
            raise RuntimeError("The source fetcher is closed.")
        original_url = normalize_url(url)
        hosts = approved_hosts([original_url]) if allowed_hosts is None else set(allowed_hosts)
        if not is_approved_url(original_url, hosts):
            raise SourceError(
                "unapproved_host", "The source is outside the approved official hosts."
            )
        try:
            async with asyncio.timeout(self.limits.total_timeout), self._semaphore:
                reply = await self._follow(original_url, hosts, robots=False)
                if reply.media_type == "application/pdf":
                    parsed = await _parse_pdf(reply.body, self.limits)
                    blocks = [_Block(item["text"], page=item["page"]) for item in parsed["blocks"]]
                    title = parsed["title"] or urlsplit(reply.url).path.rsplit("/", 1)[-1]
                    coverage = parsed["coverage"]
                    links: tuple[str, ...] = ()
                else:
                    parser = _parse_html(reply.body, reply.headers, reply.url, self.limits)
                    blocks = parser.blocks
                    title = parser.title or urlsplit(reply.url).hostname or "Untitled source"
                    coverage = parser.coverage
                    links = tuple(parser.links)
                return self._document(reply, original_url, title, blocks, coverage, links, purpose)
        except TimeoutError:
            raise SourceError(
                "source_timeout", "Source collection exceeded its time limit."
            ) from None

    async def _follow(self, url: str, hosts: set[str], *, robots: bool) -> _Reply:
        seen: set[str] = set()
        for hop in range(self.limits.max_redirects + 1):
            if url in seen:
                raise SourceError("redirect_loop", "The source redirects in a loop.")
            seen.add(url)
            if not is_approved_url(url, hosts):
                message = (
                    "The site's automated-access policy redirects to another host. "
                    "Cross-host policy redirects are not supported."
                    if robots
                    else "The source redirects to a host that was not explicitly approved. "
                    "Provide its canonical URL as a verified seed; www/apex redirects "
                    "are not automatically authorized."
                )
                raise SourceError("unapproved_host", message)
            if not robots:
                await self._check_robots(url)
            reply = await self._request(url, robots=robots)
            if reply.status not in _REDIRECTS:
                return reply
            if hop == self.limits.max_redirects:
                raise SourceError("redirect_limit", "The source has too many redirects.")
            location = reply.headers.get("location", "")
            if not location or any(ord(char) < 33 or ord(char) == 127 for char in location):
                raise SourceError("redirect_invalid", "The source returned an invalid redirect.")
            try:
                url = normalize_url(urljoin(url, location))
            except SourceError:
                raise SourceError(
                    "redirect_invalid", "The source returned an unsafe redirect."
                ) from None
        raise AssertionError("Unreachable redirect state")

    async def _check_robots(self, url: str) -> None:
        host = urlsplit(url).hostname
        assert host is not None
        async with self._host_state(host) as state, state.policy_lock:
            if state.policy is None or state.policy_expires <= time.monotonic():
                authority = f"[{host}]" if ":" in host else host
                policy_url = f"https://{authority}/robots.txt"
                try:
                    reply = await self._follow(policy_url, {host}, robots=True)
                    if reply.status in {404, 410}:
                        policy = _RobotsPolicy()
                    else:
                        policy = _parse_robots(
                            reply.body,
                            reply.headers.get("content-type", "").split(";", 1)[0].lower(),
                            self.limits,
                        )
                except SourceError as error:
                    if error.code in {
                        "unsafe_address",
                        "unsafe_url",
                        "unapproved_host",
                        "source_rate_limited",
                        "robots_delay",
                    }:
                        raise
                    raise SourceError(
                        "robots_unavailable", "The site's automated-access policy is unavailable."
                    ) from None
                state.policy = policy
                state.policy_expires = time.monotonic() + self.limits.robots_cache_seconds
                state.next_request_at = max(
                    state.next_request_at, state.last_request_at + policy.delay
                )
            if not state.policy.allows(url):
                raise SourceError(
                    "robots_disallowed", "The site does not permit automated access to this source."
                )

    async def _request(self, url: str, *, robots: bool) -> _Reply:
        host = urlsplit(url).hostname
        assert host is not None
        try:
            async with asyncio.timeout(self.limits.request_timeout):
                async with self._host_state(host) as state, state.network_lock:
                    await asyncio.sleep(max(0.0, state.next_request_at - time.monotonic()))
                    addresses = await self._addresses(host)
                    try:
                        for index, address in enumerate(addresses[:3]):
                            try:
                                reply = await self._request_ip(url, host, address, robots=robots)
                            except httpx.ConnectError:
                                if index == min(len(addresses), 3) - 1:
                                    raise
                                continue
                            if reply.status == 403 or _is_challenge(reply.headers, reply.body):
                                delay = state.policy.delay if state.policy else 0.0
                                await asyncio.sleep(delay)
                                reply = await _scrape(
                                    url, address, self.limits, robots=robots, crawl_delay=delay
                                )
                                if reply.status in {401, 429}:
                                    _check_status(reply.status, reply.headers)
                                if _is_challenge(reply.headers, reply.body):
                                    raise SourceError(
                                        "source_challenge", _SCRAPE_ERRORS["source_challenge"]
                                    )
                                if reply.status not in _REDIRECTS and not (
                                    robots and reply.status in {404, 410}
                                ):
                                    _check_status(reply.status, reply.headers)
                            return reply
                    finally:
                        state.last_request_at = time.monotonic()
                        state.next_request_at = state.last_request_at + (
                            state.policy.delay if state.policy else 0.0
                        )
        except (TimeoutError, httpx.TimeoutException):
            raise SourceError(
                "source_timeout", "The source did not respond within the time limit."
            ) from None
        except (httpx.HTTPError, OSError):
            raise SourceError(
                "source_network_error", "A secure connection to the source failed."
            ) from None
        raise AssertionError("A validated address list cannot be empty")

    async def _request_ip(self, url: str, host: str, address: str, *, robots: bool) -> _Reply:
        transport = (
            _BorrowedTransport(self._transport)
            if self._transport is not None
            else httpx.AsyncHTTPTransport(
                verify=self._tls,
                trust_env=False,
                retries=0,
                http2=False,
                limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
            )
        )
        # HTTPcore connects to this numeric address, while sni_hostname is passed
        # to start_tls for BOTH SNI and certificate hostname verification.
        pinned = httpx.URL(url).copy_with(host=address)
        headers = {
            "Host": f"[{host}]" if ":" in host else host,
            "User-Agent": USER_AGENT,
            "Accept": "text/plain" if robots else "text/html,application/pdf;q=0.9",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        # A bare public transport has no cookie jar, auth, automatic redirects,
        # environment proxy selection, or AsyncClient's full-URL INFO logging.
        async with transport:
            request = httpx.Request(
                "GET",
                pinned,
                headers=headers,
                extensions={
                    "sni_hostname": host,
                    "timeout": dict.fromkeys(
                        ("connect", "read", "write", "pool"), self.limits.request_timeout
                    ),
                },
            )
            response = await transport.handle_async_request(request)
            try:
                reply = _Reply(url, response.status_code, response.headers)
                if response.status_code in {401, 429}:
                    _check_status(response.status_code, response.headers)
                if response.status_code == 403 or _is_challenge(response.headers):
                    return reply
                if response.status_code in _REDIRECTS or (
                    robots and response.status_code in {404, 410}
                ):
                    return reply
                if response.status_code not in {200, 503}:
                    _check_status(response.status_code, response.headers)
                reply.body, reply.media_type = await _read_body(
                    response,
                    self.limits,
                    robots=robots,
                    inspect_only=response.status_code != 200,
                )
                if not _is_challenge(reply.headers, reply.body):
                    _check_status(reply.status, reply.headers)
                return reply
            finally:
                await response.aclose()

    def _document(
        self,
        reply: _Reply,
        original_url: str,
        title: str,
        blocks: list[_Block],
        coverage: dict[str, Any],
        links: tuple[str, ...],
        purpose: str,
    ) -> SourceDocument:
        content_hash = hashlib.sha256(reply.body).hexdigest()
        identity = hashlib.sha256(f"{reply.url}\0{content_hash}".encode()).hexdigest()[:24]
        source_id = f"src_{identity}"
        spans: list[TextSpan] = []
        publication_spans: list[TextSpan] = []
        dates = {block.publication_date for block in blocks if block.publication_date is not None}
        count = 0
        omitted = False
        for block in blocks:
            text = _clean_text(block.text)
            while text:
                separator = 2 if spans else 0
                remaining = self.limits.max_text_chars - count - separator
                if remaining <= 0 or len(spans) >= self.limits.max_spans:
                    omitted = True
                    break
                length = min(len(text), self.limits.max_span_chars, remaining)
                if length < len(text):
                    boundary = text.rfind(" ", 0, length + 1)
                    if boundary > length // 2:
                        length = boundary
                piece = text[:length].strip()
                if not piece:
                    break
                span = TextSpan(
                    f"sp_{identity}_{len(spans) + 1:04d}", piece, block.page, block.section
                )
                spans.append(span)
                count += len(piece) + separator
                if block.publication:
                    publication_spans.append(span)
                text = text[length:].lstrip()
            if omitted:
                break
        body_spans = [
            span for span in spans if span not in publication_spans and span.section != "Title"
        ]
        useful_text = " ".join(span.text for span in body_spans)
        if (
            len(useful_text) < self.limits.min_text_chars
            or sum(char.isalpha() for char in useful_text) < self.limits.min_text_chars // 2
            or (
                len(useful_text) < 500
                and any(
                    marker in useful_text.lower()
                    for marker in (
                        "enable javascript",
                        "javascript is required",
                        "checking your browser",
                        "verify you are human",
                        "access denied",
                        "sign in to continue",
                        "log in to continue",
                        "just a moment",
                    )
                )
            )
        ):
            raise SourceError(
                "unreadable_source",
                "The source did not provide enough accessible text. Sign-in, script-only, "
                "and scanned sources are not supported.",
            )
        coverage = dict(coverage)
        coverage["text_characters"] = count
        coverage["span_count"] = len(spans)
        coverage["publication_date_conflict"] = len(dates) > 1
        if omitted:
            coverage["truncated"] = True
            coverage["complete"] = False
            coverage["text_truncated"] = True
            coverage["omissions"] = list(
                dict.fromkeys([*coverage["omissions"], "text_or_span_limit"])
            )
        retrieved_at = self._clock()
        if retrieved_at.tzinfo is None:
            raise ValueError("The source clock must return an aware datetime.")
        return SourceDocument(
            id=source_id,
            original_url=original_url,
            url=reply.url,
            title=_clean_text(title)[:300],
            media_type=reply.media_type,
            text="\n\n".join(span.text for span in spans),
            spans=tuple(spans),
            content_hash=content_hash,
            retrieved_at=retrieved_at.astimezone(UTC),
            published_at=(
                next(iter(dates))
                if len(dates) == 1
                and not coverage.get("publication_metadata_truncated")
                and any(
                    span.section == "Publication metadata"
                    and next(iter(dates)).isoformat() in span.text
                    for span in publication_spans
                )
                else None
            ),
            links=links,
            coverage=coverage,
            publication_evidence=tuple(publication_spans),
            purpose=purpose,
        )


def _check_status(status: int, headers: httpx.Headers) -> None:
    if status == 200:
        return
    if status == 429:
        retry_after = headers.get("retry-after", "")
        delay = min(int(retry_after), 3600) if re.fullmatch(r"\d{1,6}", retry_after) else None
        raise SourceError(
            "source_rate_limited",
            "The source has rate-limited access. Try again later.",
            retry_after=delay,
        )
    if status in {401, 403}:
        raise SourceError(
            "source_forbidden", "The source requires sign-in or does not permit access."
        )
    raise SourceError("source_unavailable", "The source did not return an accessible document.")


def _sniff_type(body: bytes | bytearray, declared: str) -> str:
    leading = body[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")
    pdf = leading.startswith(b"%PDF-")
    if pdf:
        if declared not in _GENERIC_TYPES | {"application/pdf"}:
            raise SourceError(
                "unsupported_media_type", "The source's document type is inconsistent."
            )
        return "application/pdf"
    if declared == "application/pdf":
        raise SourceError("unsupported_media_type", "The source did not return a PDF document.")
    utf16 = body.startswith((b"\xff\xfe", b"\xfe\xff"))
    if b"\x00" in leading and not utf16:
        raise SourceError(
            "unsupported_media_type", "The source did not return a supported text document."
        )
    if declared in _HTML_TYPES or (
        declared in _GENERIC_TYPES and _HTML_START.search(leading) is not None
    ):
        return "text/html"
    raise SourceError(
        "unsupported_media_type", "Only accessible HTML and text-based PDF sources are supported."
    )


class _BodyReader:
    """Apply the same transfer, decompression, and format limits to both HTTP clients."""

    def __init__(
        self,
        headers: httpx.Headers,
        limits: FetchLimits,
        *,
        robots: bool,
        inspect_only: bool = False,
        decoded: bool = False,
    ):
        self.limits = limits
        self.robots = robots
        self.inspect_only = inspect_only
        self.declared = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if (
            not robots
            and not inspect_only
            and self.declared not in _HTML_TYPES | _GENERIC_TYPES | {"application/pdf"}
        ):
            raise SourceError(
                "unsupported_media_type", "Only HTML and text-based PDF sources are supported."
            )
        self.cap = (
            limits.max_robots_bytes
            if robots
            else limits.max_html_bytes
            if inspect_only or self.declared in _HTML_TYPES
            else limits.max_pdf_bytes
        )
        length = headers.get("content-length")
        if length is not None:
            if not re.fullmatch(r"\d{1,16}", length):
                raise SourceError("invalid_response", "The source returned an invalid response.")
            if int(length) > self.cap:
                raise SourceError(
                    "source_too_large", "The source exceeds the supported download size."
                )
        encoding = headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"identity", "", "gzip", "x-gzip", "deflate"}:
            raise SourceError(
                "unsupported_encoding", "The source returned an unsupported content encoding."
            )
        self.decompressor = (
            zlib.decompressobj(
                16 + zlib.MAX_WBITS if encoding in {"gzip", "x-gzip"} else zlib.MAX_WBITS
            )
            if not decoded and encoding in {"gzip", "x-gzip", "deflate"}
            else None
        )
        self.output = bytearray()
        self.transferred = 0

    def feed(self, chunk: bytes, *, transferred: int | None = None) -> None:
        self.transferred += len(chunk) if transferred is None else transferred
        if self.transferred > self.cap:
            raise SourceError("source_too_large", "The source exceeds the supported download size.")
        try:
            decoded = (
                self.decompressor.decompress(chunk, max(1, self.cap + 1 - len(self.output)))
                if self.decompressor is not None
                else chunk
            )
        except zlib.error:
            raise SourceError(
                "invalid_response", "The source returned invalid compressed content."
            ) from None
        self.output.extend(decoded)
        if len(self.output) > self.cap or (
            self.decompressor is not None and self.decompressor.unconsumed_tail
        ):
            raise SourceError("source_too_large", "The source exceeds the supported decoded size.")
        if (
            not self.robots
            and not self.inspect_only
            and len(self.output) >= 1024
            and _sniff_type(self.output, self.declared) == "text/html"
        ):
            self.cap = self.limits.max_html_bytes
            if max(len(self.output), self.transferred) > self.cap:
                raise SourceError("source_too_large", "The source exceeds the supported HTML size.")

    def finish(self) -> tuple[bytes, str]:
        if self.decompressor is not None and (
            not self.decompressor.eof or self.decompressor.unused_data
        ):
            raise SourceError("invalid_response", "The source returned invalid compressed content.")
        body = bytes(self.output)
        media_type = "" if self.robots or self.inspect_only else _sniff_type(body, self.declared)
        if (
            media_type == "text/html"
            and max(len(body), self.transferred) > self.limits.max_html_bytes
        ):
            raise SourceError("source_too_large", "The source exceeds the supported HTML size.")
        return body, media_type


async def _read_body(
    response: httpx.Response,
    limits: FetchLimits,
    *,
    robots: bool,
    inspect_only: bool = False,
) -> tuple[bytes, str]:
    reader = _BodyReader(
        response.headers,
        limits,
        robots=robots,
        inspect_only=inspect_only,
        decoded=response.is_stream_consumed,
    )
    if response.is_stream_consumed:
        # Injected transports may return decoded content rather than a raw stream.
        reader.feed(response.content, transferred=response.num_bytes_downloaded)
    else:
        async for chunk in response.aiter_raw():
            reader.feed(chunk)
    return reader.finish()


_SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "nav",
        "footer",
        "noscript",
        "template",
        "svg",
        "canvas",
        "iframe",
        "object",
        "embed",
        "form",
        "button",
        "select",
        "textarea",
        "aside",
    }
)
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "tr",
        "blockquote",
        "dl",
        "dt",
        "dd",
        "table",
        "ul",
        "ol",
        "pre",
        "figure",
    }
)
_PUBLICATION_KEYS = frozenset(
    {
        "article:published_time",
        "datepublished",
        "pubdate",
        "publishdate",
        "publication_date",
        "dc.date.issued",
        "dcterms.issued",
        "parsely-pub-date",
        "citation_publication_date",
    }
)
_HIDDEN_CLASSES = frozenset(
    {
        "hidden",
        "sr-only",
        "visually-hidden",
        "cookie-banner",
        "cookie-consent",
        "breadcrumb",
        "breadcrumbs",
        "navigation",
        "sidebar",
        "advertisement",
    }
)


class _HTMLText(HTMLParser):
    def __init__(self, url: str, limits: FetchLimits):
        super().__init__(convert_charrefs=True)
        self.url, self.limits = url, limits
        self.blocks: list[_Block] = []
        self.links: list[str] = []
        self.title = ""
        self.coverage: dict[str, Any] = {
            "complete": True,
            "truncated": False,
            "text_truncated": False,
            "omissions": [],
            "extraction": "visible_static_html",
            "scripts_executed": False,
            "links_truncated": False,
            "publication_metadata_truncated": False,
            "scope": "static_semantic_text_without_css_layout",
        }
        self._stack: list[tuple[str, bool]] = []
        self._buffer: list[str] = []
        self._title_parts: list[str] = []
        self._title_chars = 0
        self._section: str | None = None
        self._heading = False
        self._chars = 0
        self._nodes = 0
        self._metadata_count = 0
        self._deadline = time.perf_counter() + limits.html_parse_timeout

    def _tick(self) -> None:
        self._nodes += 1
        if time.perf_counter() >= self._deadline:
            raise SourceError("html_parse_timeout", "HTML extraction exceeded its time limit.")
        if (
            self._nodes > self.limits.max_html_nodes
            or len(self._stack) > self.limits.max_html_depth
        ):
            raise SourceError(
                "html_complexity", "The HTML document exceeds the supported parsing limits."
            )

    def _omitted(self) -> None:
        self.coverage.update(complete=False, truncated=True, text_truncated=True)
        if "text_limit" not in self.coverage["omissions"]:
            self.coverage["omissions"].append("text_limit")

    def _flush(self) -> None:
        text = _clean_text("".join(self._buffer))
        self._buffer = []
        if not text:
            return
        if self._heading:
            self._section = text[:300]
        self.blocks.append(_Block(text, section=self._section))

    def _metadata(self, label: str, value: str, *, publication: bool) -> None:
        if not value:
            return
        if self._metadata_count >= 16 or len(value) > 300:
            self.coverage.update(
                complete=False, truncated=True, publication_metadata_truncated=True
            )
            if "publication_metadata_limit" not in self.coverage["omissions"]:
                self.coverage["omissions"].append("publication_metadata_limit")
            return
        self._metadata_count += 1
        text = f"{label}: {_clean_text(value)}"
        self.blocks.append(
            _Block(
                text,
                section="Publication metadata" if publication else "Date metadata",
                publication=True,
                publication_date=_publication_date(value) if publication else None,
            )
        )

    def handle_starttag(self, tag: str, attributes: list[tuple[str, str | None]]) -> None:
        self._tick()
        attrs = {name: value or "" for name, value in attributes}
        parent_skip = bool(self._stack and self._stack[-1][1])
        hidden = (
            "hidden" in attrs
            or attrs.get("aria-hidden", "").lower() == "true"
            or attrs.get("role", "").lower() in {"navigation", "contentinfo", "complementary"}
            or bool(_HIDDEN_CLASSES.intersection(attrs.get("class", "").lower().split()))
            or bool(
                re.search(
                    r"(?:display\s*:\s*none|visibility\s*:\s*hidden|content-visibility\s*:\s*hidden)",
                    attrs.get("style", ""),
                    re.IGNORECASE,
                )
            )
        )
        skip = parent_skip or hidden or tag in _SKIP_TAGS
        if (
            tag == "a"
            and not hidden
            and not any(
                item[0] in {"script", "style", "template", "noscript"} for item in self._stack
            )
        ):
            href = attrs.get("href", "")
            if href and not href.startswith("#"):
                try:
                    link = normalize_url(urljoin(self.url, href))
                except SourceError:
                    link = ""
                if link and link not in self.links:
                    if len(self.links) < self.limits.max_links:
                        self.links.append(link)
                    else:
                        self.coverage["links_truncated"] = True
        if tag == "meta" and not parent_skip:
            key = (
                attrs.get("property") or attrs.get("name") or attrs.get("itemprop") or ""
            ).lower()
            if key in _PUBLICATION_KEYS:
                self._metadata(key, attrs.get("content", ""), publication=True)
        if tag == "time" and not skip:
            publication = (
                "pubdate" in attrs
                or attrs.get("itemprop", "").lower() == "datepublished"
                or bool(
                    {"published", "publication-date", "entry-date"}.intersection(
                        attrs.get("class", "").lower().split()
                    )
                )
            )
            self._metadata(
                "Published time" if publication else "Time element",
                attrs.get("datetime", ""),
                publication=publication,
            )
        if not skip:
            if tag in _BLOCK_TAGS:
                self._flush()
                self._heading = tag in {"h1", "h2", "h3", "h4", "h5", "h6"}
            elif tag in {"br", "td", "th"}:
                self._buffer.append(" ")
        elif not parent_skip:
            self._flush()
        if tag not in _VOID_TAGS:
            self._stack.append((tag, skip))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        self._tick()
        if (not self._stack or not self._stack[-1][1]) and tag in _BLOCK_TAGS:
            self._flush()
            if tag.startswith("h") and len(tag) == 2 and tag[1].isdigit():
                self._heading = False
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self._tick()
        if self._stack and self._stack[-1][1]:
            return
        if any(tag == "title" for tag, _ in self._stack):
            piece = data[: max(0, 300 - self._title_chars)]
            if piece:
                self._title_parts.append(piece)
                self._title_chars += len(piece)
            return
        if any(tag == "head" for tag, _ in self._stack):
            return
        remaining = self.limits.max_text_chars - self._chars
        if remaining <= 0:
            if data.strip():
                self._omitted()
            return
        if len(data) > remaining:
            self._omitted()
        self._buffer.append(data[:remaining])
        self._chars += min(len(data), remaining)

    def finish(self) -> None:
        self.close()
        self._flush()
        self.title = _clean_text("".join(self._title_parts))[:300]
        if self.title:
            self.blocks.insert(0, _Block(self.title, section="Title"))


def _parse_html(body: bytes, headers: httpx.Headers, url: str, limits: FetchLimits) -> _HTMLText:
    encoding = "utf-8"
    match = re.search(r"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", headers.get("content-type", ""))
    if match:
        encoding = match[1]
    elif body.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif body.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        declared = re.search(
            rb"<meta\b[^>]{0,512}\bcharset\s*=\s*[\"']?\s*([a-zA-Z0-9_-]+)",
            body[:4096],
            re.IGNORECASE,
        )
        if declared is not None:
            encoding = declared[1].decode("ascii")
    try:
        canonical = codecs.lookup(encoding).name
        if canonical not in {
            "utf-8",
            "utf-8-sig",
            "ascii",
            "iso8859-1",
            "cp1252",
            "utf-16",
            "utf-16-le",
            "utf-16-be",
        }:
            raise LookupError
        text = body.decode(canonical)
    except (LookupError, UnicodeError):
        raise SourceError(
            "unreadable_source", "The HTML source uses unreadable text encoding."
        ) from None
    parser = _HTMLText(url, limits)
    for index in range(0, len(text), 8192):
        parser.feed(text[index : index + 8192])
        parser._tick()
        if parser.cdata_elem is None and len(parser.rawdata) > 65_536:
            raise SourceError(
                "html_complexity",
                "The HTML document contains an oversized incomplete markup token.",
            )
    parser.finish()
    return parser


def _worker_environment() -> dict[str, str]:
    # One-shot workers must not inherit OAuth, AI, proxy, session, or application settings.
    return {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR") if key in os.environ}


async def _run_worker(
    *command: str,
    body: bytes,
    output_cap: int,
    output_error: SourceError,
) -> tuple[bytes, int]:
    process = None
    tasks: list[asyncio.Task[Any]] = []
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_worker_environment(),
            **({"creationflags": 0x08000000} if sys.platform == "win32" else {}),
        )
        assert process.stdin is not None and process.stdout is not None

        async def write_input() -> None:
            assert process is not None and process.stdin is not None
            try:
                process.stdin.write(body)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()
                with suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()

        async def read_output() -> bytes:
            assert process is not None and process.stdout is not None
            output = bytearray()
            while chunk := await process.stdout.read(65_536):
                output.extend(chunk)
                if len(output) > output_cap:
                    raise output_error
            return bytes(output)

        tasks = [
            asyncio.create_task(write_input()),
            asyncio.create_task(read_output()),
            asyncio.create_task(process.wait()),
        ]
        _, output, returncode = await asyncio.gather(*tasks)
        return output, returncode
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if process is not None:
            await process.wait()


async def _scrape(
    url: str,
    address: str,
    limits: FetchLimits,
    *,
    robots: bool,
    crawl_delay: float,
) -> _Reply:
    config = {
        "url": url,
        "address": address,
        "limits": asdict(limits),
        "robots": robots,
        "crawl_delay": crawl_delay,
    }
    invalid = SourceError("invalid_response", "The browser fallback returned an invalid response.")
    package_roots = [
        str(Path(path).resolve())
        for path in sys.path
        if path and Path(path).name in {"site-packages", "dist-packages"}
    ]
    body_limit = (
        limits.max_robots_bytes if robots else max(limits.max_pdf_bytes, limits.max_html_bytes)
    )
    try:
        async with asyncio.timeout(limits.request_timeout):
            output, returncode = await _run_worker(
                "-I",
                "-S",
                "-B",
                str(Path(__file__).with_name("scraper_bootstrap.py")),
                json.dumps(package_roots, separators=(",", ":")),
                body=json.dumps(config, separators=(",", ":")).encode(),
                output_cap=4 * ((body_limit + 2) // 3) + 65_536,
                output_error=invalid,
            )
    except TimeoutError:
        raise SourceError("source_timeout", _SCRAPE_ERRORS["source_timeout"]) from None
    except (OSError, NotImplementedError):
        raise SourceError(
            "source_scrape_unavailable", _SCRAPE_ERRORS["source_scrape_unavailable"]
        ) from None
    if returncode != 0:
        raise SourceError("source_scrape_failed", _SCRAPE_ERRORS["source_scrape_failed"])
    try:
        result = json.loads(output)
    except (ValueError, UnicodeError):
        raise invalid from None
    if not isinstance(result, dict):
        raise invalid
    if set(result) == {"error"}:
        code = result["error"]
        if not isinstance(code, str) or code not in _SCRAPE_ERRORS:
            raise invalid
        raise SourceError(code, _SCRAPE_ERRORS[code])
    if (
        set(result) != {"status", "headers", "body", "transferred"}
        or type(result["status"]) is not int
        or not 100 <= result["status"] <= 599
        or not isinstance(result["headers"], dict)
        or not set(result["headers"]) <= _SCRAPE_HEADERS
        or any(
            not isinstance(value, str) or len(value) > 8192 for value in result["headers"].values()
        )
        or not isinstance(result["body"], str)
        or type(result["transferred"]) is not int
        or result["transferred"] < 0
    ):
        raise invalid
    try:
        body = base64.b64decode(result["body"], validate=True)
        headers = httpx.Headers(result["headers"], encoding="latin-1")
    except (ValueError, UnicodeError, binascii.Error):
        raise invalid from None
    reply = _Reply(url, result["status"], headers)
    if reply.status in _REDIRECTS | {401, 429} or (robots and reply.status in {404, 410}):
        if body or result["transferred"]:
            raise invalid
        return reply
    reader = _BodyReader(
        headers,
        limits,
        robots=robots,
        inspect_only=reply.status != 200 or _is_challenge(headers),
        decoded=True,
    )
    reader.feed(body, transferred=result["transferred"])
    reply.body, reply.media_type = reader.finish()
    return reply


async def _parse_pdf(body: bytes, limits: FetchLimits) -> dict[str, Any]:
    spec = importlib.util.find_spec("pypdf")
    if spec is None or spec.origin is None:
        raise SourceError(
            "pdf_unavailable", "Text-based PDF support is not available on this server."
        )
    config = {
        "max_bytes": limits.max_pdf_bytes,
        "max_pages": limits.max_pdf_pages,
        "max_chars": limits.max_text_chars,
        "memory_bytes": limits.pdf_memory_bytes,
        "timeout": limits.pdf_parse_timeout,
        "package_root": str(Path(spec.origin).resolve().parent.parent),
    }
    try:
        async with asyncio.timeout(limits.pdf_parse_timeout):
            output, returncode = await _run_worker(
                "-I",
                "-S",
                "-B",
                str(Path(__file__).with_name("pdf_worker.py")),
                json.dumps(config, separators=(",", ":")),
                body=body,
                output_cap=limits.max_text_chars * 12 + limits.max_pdf_pages * 1024 + 65_536,
                output_error=SourceError(
                    "pdf_resource_limit", "PDF extraction exceeded its output limit."
                ),
            )
            if returncode != 0:
                raise SourceError(
                    "pdf_resource_limit", "The PDF could not be parsed within resource limits."
                )
            try:
                result = json.loads(output)
            except (ValueError, UnicodeError):
                raise SourceError("pdf_parse_failed", "The PDF could not be safely read.") from None
            if not isinstance(result, dict) or result.get("error"):
                code = result.get("error") if isinstance(result, dict) else None
                messages = {
                    "pdf_encrypted": "Encrypted PDFs are not supported.",
                    "pdf_scanned": "The PDF has no extractable text within the processing limits. OCR is not supported.",
                    "pdf_resource_limit": "PDF extraction exceeded its resource limits.",
                    "pdf_isolation_unavailable": "Isolated PDF processing is unavailable on this server.",
                    "pdf_parse_failed": "The PDF could not be safely read.",
                }
                if code not in messages:
                    code = "pdf_parse_failed"
                raise SourceError(code, messages[code])
            if (
                not isinstance(result.get("blocks"), list)
                or len(result["blocks"]) > limits.max_pdf_pages
                or not isinstance(result.get("coverage"), dict)
                or not isinstance(result.get("title"), str)
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("text"), str)
                    or type(item.get("page")) is not int
                    or not 1 <= item["page"] <= limits.max_pdf_pages
                    for item in result["blocks"]
                )
                or sum(len(item["text"]) for item in result["blocks"]) > limits.max_text_chars
            ):
                raise SourceError("pdf_parse_failed", "The PDF could not be safely read.")
            return result
    except TimeoutError:
        raise SourceError("pdf_parse_timeout", "PDF extraction exceeded its time limit.") from None
    except (OSError, NotImplementedError):
        raise SourceError(
            "pdf_unavailable", "Isolated PDF processing is unavailable on this server."
        ) from None

"""Public HTTPS URL syntax and narrowly scoped host authorization.

DNS authorization belongs to the fetcher: a syntactically public hostname can
still resolve to a private address. Canonical tags and redirects never add hosts.
Discovery uses exact approved hosts. Initial seeds may use the separately named,
offline-PSL canonical helper; its inferred hosts are not a company identity verdict.
"""

import ipaddress
import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
from tldextract import TLDExtract

MAX_URL_LENGTH = 2048
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_BAD_ESCAPE = re.compile(r"%(?![0-9a-fA-F]{2})")
_ENCODED_UNSAFE = re.compile(r"%(?:0[0-9a-f]|1[0-9a-f]|5c|7f)", re.IGNORECASE)
_LOCAL_SUFFIXES = (
    "localhost",
    "local",
    "localdomain",
    "internal",
    "intranet",
    "lan",
    "corp",
    "home",
    "arpa",
    "onion",
    "test",
    "invalid",
    "example",
)
_METADATA_ADDRESSES = frozenset({"168.63.129.16"})


class SourceError(ValueError):
    """A source failure whose string representation is safe for public display."""

    def __init__(self, code: str, public_message: str, *, retry_after: int | None = None):
        self.code = code
        self.public_message = public_message
        self.retry_after = retry_after
        super().__init__(public_message)


class URLValidationError(SourceError):
    """A malformed URL or a URL with an intrinsically unsafe destination."""


def is_public_address(value: str) -> bool:
    """Reject special-use IPs, IPv6 transition addresses, and cloud metadata IPs."""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if "%" in value or str(address) in _METADATA_ADDRESSES:
        return False
    if isinstance(address, ipaddress.IPv6Address) and (
        address.ipv4_mapped is not None
        or address.sixtofour is not None
        or address.teredo is not None
    ):
        return False
    return address.is_global and not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _invalid_url() -> URLValidationError:
    return URLValidationError(
        "invalid_url", "Provide a valid HTTPS URL without credentials or a nonstandard port."
    )


def _normalize_host(host: str) -> str:
    if not host or any(char in host for char in "%\\/@?#"):
        raise _invalid_url()
    host = host.lower()
    if host.endswith("."):
        host = host[:-1]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None:
        if not is_public_address(host):
            raise URLValidationError(
                "unsafe_url", "Source URLs must use public internet addresses."
            )
        return address.compressed
    try:
        host = host.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        raise _invalid_url() from None
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or not all(_LABEL.fullmatch(label) for label in labels)
        or labels[-1].isdigit()
        or any(host == suffix or host.endswith("." + suffix) for suffix in _LOCAL_SUFFIXES)
    ):
        raise URLValidationError(
            "unsafe_url", "Source URLs must use a valid public internet hostname."
        )
    return host


def normalize_url(value: str) -> str:
    """Normalize HTTPS/IDNA/fragment handling without decoding paths or queries.

    Query ordering, duplicate parameters, escaped delimiters, and path case are
    retained. RFC dot segments are normalized as HTTPX will send them, so access
    policy checks and the actual request target cannot disagree.
    """
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_URL_LENGTH
        or value != value.strip()
        or "\\" in value
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in value)
        or _BAD_ESCAPE.search(value)
        or _ENCODED_UNSAFE.search(value)
    ):
        raise _invalid_url()
    try:
        parts = urlsplit(value)
        if (
            parts.scheme.lower() != "https"
            or not parts.netloc
            or parts.username is not None
            or parts.password is not None
            or "%" in parts.netloc
            or parts.netloc.endswith(":")
            or parts.port not in (None, 443)
            or parts.hostname is None
        ):
            raise _invalid_url()
        host = _normalize_host(parts.hostname)
        if parts.netloc.startswith("[") and ":" not in host:
            raise _invalid_url()
        authority = f"[{host}]" if ":" in host else host
        path = quote(parts.path or "/", safe="/:@!$&'()*+,;=-._~%")
        query = quote(parts.query, safe="/?:@!$&'()*+,;=-._~%")
        normalized = urlunsplit(("https", authority, path, query, ""))
        if not query and "?" in value.split("#", 1)[0]:
            normalized += "?"
        normalized = str(httpx.URL(normalized))
    except SourceError:
        raise
    except (ValueError, UnicodeError, httpx.InvalidURL):
        raise _invalid_url() from None
    if len(normalized) > MAX_URL_LENGTH:
        raise _invalid_url()
    return normalized


def approved_hosts(seed_urls: Iterable[str]) -> set[str]:
    """Return only explicitly approved exact hosts, never parent domains/tenants."""
    return {urlsplit(normalize_url(url)).hostname for url in seed_urls}


@lru_cache(maxsize=1)
def _canonical_suffix_extractor() -> TLDExtract:
    return TLDExtract(
        suffix_list_urls=(),
        cache_dir=None,
        include_psl_private_domains=True,
        fallback_to_snapshot=True,
    )


def canonical_seed_hosts(seed_urls: Iterable[str]) -> set[str]:
    """Authorize narrowly bounded www/apex counterparts for INITIAL seed fetching.

    Exact normalized seed hosts are always retained. A single www label is added
    or removed only for a recognized public suffix and a hostname that is exactly
    its registrable domain or www.<registrable-domain>. Private PSL entries,
    arbitrary subdomains, IPs, and unknown suffixes receive no inferred hosts.
    Both forms must retain the same registrable boundary under PSL wildcard and
    exception rules.

    Only tldextract's bundled snapshot is read: there are no PSL network updates
    or disk-cache files. The snapshot can lag suffix/hosting changes and cannot
    establish company ownership. DNS, TLS, robots, redirects, and semantic company
    verification remain mandatory. Never reuse this expanded set for discovery
    or news: use approved_hosts with actually fetched, company-verified final URLs.
    """
    hosts = approved_hosts(seed_urls)
    allowed = set(hosts)
    for host in hosts:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            continue
        extractor = _canonical_suffix_extractor()
        parts = extractor(host)
        if not parts.suffix or not parts.domain or parts.is_private:
            continue
        registrable = f"{parts.domain}.{parts.suffix}"
        if host == registrable:
            counterpart = f"www.{registrable}"
        elif host == f"www.{registrable}":
            counterpart = registrable
        else:
            continue
        try:
            counterpart = _normalize_host(counterpart)
        except URLValidationError:
            continue
        candidate = extractor(counterpart)
        if (
            not candidate.suffix
            or not candidate.domain
            or candidate.is_private
            or f"{candidate.domain}.{candidate.suffix}" != registrable
        ):
            continue
        allowed.add(counterpart)
    return allowed


def is_approved_url(url: str, hosts: set[str]) -> bool:
    """Check syntactic safety and exact-host membership; this does not check DNS."""
    try:
        host = urlsplit(normalize_url(url)).hostname
        return host in {_normalize_host(approved) for approved in hosts}
    except (SourceError, TypeError, AttributeError):
        return False

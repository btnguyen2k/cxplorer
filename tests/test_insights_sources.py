"""Offline source collection tests; all documents, DNS, and network streams are controlled."""

import asyncio
import gzip
import io
import json
import ssl
import subprocess
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import httpcore
import httpx
import pytest
import requests
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from tldextract.cache import DiskCache

from cxplorer.insights import pdf_worker, sources, urls
from cxplorer.insights.sources import FetchLimits, SourceFetcher
from cxplorer.insights.urls import (
    SourceError,
    URLValidationError,
    approved_hosts,
    canonical_seed_hosts,
    is_approved_url,
    is_public_address,
    normalize_url,
)

PUBLIC_IP = "93.184.216.34"
HTML = (
    b"<html><head><title>Contoso</title></head><body><h1>Contoso products</h1>"
    b"<p>Contoso builds reliable software that helps customers collaborate and "
    b"manage their daily business operations.</p></body></html>"
)


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes):
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class Web:
    def __init__(self, routes=None, *, robots=(404, {}, b"")):
        self.routes = routes or {}
        self.robots = robots
        self.requests = []
        self.streams = []
        self.resolutions = []
        self.transport = httpx.MockTransport(self.handle)

    async def resolve(self, host):
        self.resolutions.append(host)
        return (PUBLIC_IP,)

    async def handle(self, request):
        self.requests.append(request)
        key = (request.headers["host"], request.url.raw_path.decode())
        default = (
            self.robots
            if request.url.path == "/robots.txt"
            else (
                200,
                {"content-type": "text/html"},
                HTML,
            )
        )
        status, headers, body = self.routes.get(key, self.routes.get(request.url.path, default))
        stream = BytesStream(*(body if isinstance(body, tuple) else (body,)))
        self.streams.append(stream)
        return httpx.Response(status, headers=headers, stream=stream)

    def fetcher(self, **kwargs):
        return SourceFetcher(transport=self.transport, resolver=self.resolve, **kwargs)

    async def fetch(self, url="https://contoso.com/", *, limits=None, allowed_hosts=None):
        async with self.fetcher(limits=limits) as fetcher:
            return await fetcher.fetch(url, allowed_hosts=allowed_hosts)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("HTTPS://CONTOSO.COM:443", "https://contoso.com/"),
        ("https://Contoso.com./About#team", "https://contoso.com/About"),
        (
            "https://contoso.com/a%2Fb?x=1+2&x=%2f&empty=#section",
            "https://contoso.com/a%2Fb?x=1+2&x=%2f&empty=",
        ),
        ("https://contoso.com/a?", "https://contoso.com/a?"),
        ("https://contoso.com//Products/", "https://contoso.com//Products/"),
        ("https://contoso.com/a/../Products", "https://contoso.com/Products"),
        ("https://contoso.com/résumé?q=✓", "https://contoso.com/r%C3%A9sum%C3%A9?q=%E2%9C%93"),
        ("https://contoso.みんな/", "https://contoso.xn--q9jyb4c/"),
        (
            "https://[2606:4700:4700:0000:0000:0000:0000:1111]:443/",
            "https://[2606:4700:4700::1111]/",
        ),
    ],
)
def test_url_normalization_preserves_meaningful_components(value, expected):
    assert normalize_url(value) == expected
    assert normalize_url(expected) == expected


@pytest.mark.parametrize(
    "url",
    [
        "",
        "contoso.com",
        "//contoso.com",
        "http://contoso.com",
        "https://",
        "https://contoso.com:80/",
        "https://contoso.com:0/",
        "https://contoso.com:444/",
        "https://contoso.com:/",
        "https://Contoso:password@contoso.com/",
        "https://@contoso.com/",
        "https://contoso.com\\@127.0.0.1/",
        "https://contoso.com/a\\b",
        "https://contoso.com/a%5cb",
        "https://contoso.com/%0d%0aheader",
        "https://contoso.com/\x00",
        "https://contoso.com/\n",
        "https://contoso.com/\u202e",
        " https://contoso.com",
        "https://contoso.com/a b",
        "https://contoso.com/%xy",
        "https://127.0.0.1/",
        "https://127.1/",
        "https://2130706433/",
        "https://0x7f000001/",
        "https://0177.0.0.1/",
        "https://%31%32%37.0.0.1/",
        "https://contoso%2ecom/",
        "https://localhost/",
        "https://contoso.localhost/",
        "https://contoso.local/",
        "https://metadata.google.internal/",
        "https://contoso.home.arpa/",
        "https://contoso.test/",
        "https://contoso.example/",
        "https://contoso.invalid/",
        "https://10.0.0.1/",
        "https://100.64.0.1/",
        "https://169.254.169.254/",
        "https://168.63.129.16/",
        "https://192.0.2.1/",
        "https://224.0.0.1/",
        "https://[::1]/",
        "https://[::]/",
        "https://[fe80::1]/",
        "https://[fd00::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://[::ffff:93.184.216.34]/",
        "https://[2002:7f00:1::]/",
        "https://[fe80::1%25eth0]/",
        "https://[v1.contoso]/",
        "https://[2606:4700::1111]contoso.com/",
        "https://-contoso.com/",
        "https://contoso..com/",
        "https://contoso_com/",
        "https://contoso.com/" + "a" * 2048,
    ],
)
def test_rejects_malformed_private_reserved_and_confusing_urls(url):
    with pytest.raises(URLValidationError) as error:
        normalize_url(url)
    assert error.value.code in {"invalid_url", "unsafe_url"}
    assert "password" not in str(error.value)
    assert error.value.public_message == str(error.value)


def test_host_authorization_never_expands_parent_domains_or_tenants():
    hosts = approved_hosts(["https://contoso.github.io/about", "https://news.contoso.com/"])
    assert hosts == {"contoso.github.io", "news.contoso.com"}
    assert is_approved_url("https://contoso.github.io/products", hosts)
    for url in (
        "https://github.io/",
        "https://contoso-second.github.io/",
        "https://contoso.com/",
        "https://www.news.contoso.com/",
        "https://news.contoso.com.attacker.net/",
    ):
        assert not is_approved_url(url, hosts)
    assert not is_approved_url("https://localhost/", {"localhost"})
    assert not is_approved_url("http://news.contoso.com/", hosts)


@pytest.mark.parametrize(
    ("seed", "expected"),
    [
        ("https://contoso.com/", {"contoso.com", "www.contoso.com"}),
        ("https://www.contoso.com/", {"contoso.com", "www.contoso.com"}),
        ("HTTPS://WWW.CONTOSO.COM.:443/About#team", {"contoso.com", "www.contoso.com"}),
        ("https://contoso.co.uk/", {"contoso.co.uk", "www.contoso.co.uk"}),
        ("https://www.contoso.co.uk/", {"contoso.co.uk", "www.contoso.co.uk"}),
        (
            "https://contoso.みんな/",
            {"contoso.xn--q9jyb4c", "www.contoso.xn--q9jyb4c"},
        ),
        (
            "https://www.contoso.公司.cn/",
            {"contoso.xn--55qx5d.cn", "www.contoso.xn--55qx5d.cn"},
        ),
    ],
)
def test_canonical_seed_hosts_expand_only_public_registrable_www_pairs(seed, expected):
    assert canonical_seed_hosts([seed]) == expected
    assert approved_hosts([seed]) < expected


@pytest.mark.parametrize(
    "host",
    [
        "news.contoso.com",
        "www.news.contoso.com",
        "www.www.contoso.com",
        "news.contoso.co.uk",
        "www.news.contoso.co.uk",
        "news.contoso.xn--q9jyb4c",
        "contoso.unknownsuffix",
        "www.contoso.unknownsuffix",
        "github.io",
        "www.github.io",
        "contoso.github.io",
        "www.contoso.github.io",
        "appspot.com",
        "www.appspot.com",
        "contoso.appspot.com",
        "www.contoso.appspot.com",
        "azurewebsites.net",
        "www.azurewebsites.net",
        "contoso.azurewebsites.net",
        "www.contoso.azurewebsites.net",
        "contoso.pages.dev",
        "www.contoso.pages.dev",
        "contoso.vercel.app",
        "www.contoso.vercel.app",
        "co.uk",
        "kawasaki.jp",
    ],
)
def test_canonical_seed_hosts_never_expand_tenants_subdomains_or_unknown_suffixes(host):
    assert canonical_seed_hosts([f"https://{host}/"]) == {host}


@pytest.mark.parametrize(
    ("seed", "host"),
    [
        ("https://93.184.216.34/", "93.184.216.34"),
        ("https://[2606:4700:4700::1111]/", "2606:4700:4700::1111"),
    ],
)
def test_canonical_seed_hosts_leave_public_ip_literals_exact(seed, host, monkeypatch):
    def no_suffix_lookup():
        pytest.fail("IP literals must not be passed to the PSL extractor.")

    monkeypatch.setattr(urls, "_canonical_suffix_extractor", no_suffix_lookup)
    assert canonical_seed_hosts([seed]) == {host}


def test_canonical_seed_hosts_do_not_strip_a_registrable_www_domain_to_its_suffix():
    assert canonical_seed_hosts(["https://www.co.uk/"]) == {"www.co.uk", "www.www.co.uk"}
    assert "co.uk" not in canonical_seed_hosts(["https://www.co.uk/"])


def test_canonical_seed_hosts_preserve_all_exact_seeds_and_do_not_recurse():
    seeds = (
        seed
        for seed in (
            "https://www.contoso.com/home",
            "https://contoso.com/about",
            "https://news.contoso.com/",
            "https://contoso.github.io/",
            "https://contoso.unknownsuffix/",
        )
    )
    assert canonical_seed_hosts(seeds) == {
        "contoso.com",
        "www.contoso.com",
        "news.contoso.com",
        "contoso.github.io",
        "contoso.unknownsuffix",
    }
    assert canonical_seed_hosts([]) == set()
    assert approved_hosts(["https://www.contoso.com/home"]) == {"www.contoso.com"}


@pytest.mark.parametrize(
    "seed",
    [
        "http://contoso.com/",
        "https://localhost/",
        "https://contoso.example/",
        "https://127.0.0.1/",
        "https://[::ffff:127.0.0.1]/",
        "https://contoso%2ecom/",
        "https://contoso.com:444/",
    ],
)
def test_canonical_seed_hosts_do_not_relax_seed_url_safety(seed):
    with pytest.raises(URLValidationError):
        canonical_seed_hosts(["https://contoso.com/", seed])


def test_canonical_seed_hosts_use_only_the_bundled_snapshot_without_network_or_disk_cache(
    monkeypatch,
):
    real_constructor = urls.TLDExtract
    configurations = []

    def constructor(**kwargs):
        configurations.append(kwargs)
        return real_constructor(**kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("Canonical seed authorization must not fetch or access a PSL disk cache.")

    urls._canonical_suffix_extractor.cache_clear()
    monkeypatch.setattr(urls, "TLDExtract", constructor)
    monkeypatch.setattr(requests.Session, "request", forbidden)
    monkeypatch.setattr(requests.Session, "send", forbidden)
    monkeypatch.setattr(DiskCache, "get", forbidden)
    monkeypatch.setattr(DiskCache, "set", forbidden)
    try:
        assert canonical_seed_hosts(["https://contoso.com/"]) == {
            "contoso.com",
            "www.contoso.com",
        }
        assert canonical_seed_hosts(["https://www.contoso.co.uk/"]) == {
            "contoso.co.uk",
            "www.contoso.co.uk",
        }
        assert canonical_seed_hosts(["https://www.github.io/"]) == {"www.github.io"}
    finally:
        urls._canonical_suffix_extractor.cache_clear()
    assert configurations == [
        {
            "suffix_list_urls": (),
            "cache_dir": None,
            "include_psl_private_domains": True,
            "fallback_to_snapshot": True,
        }
    ]


def test_canonical_seed_transport_still_enforces_exact_later_discovery_hosts():
    web = Web(
        {
            ("contoso.com", "/"): (301, {"location": "https://www.contoso.com/about"}, b""),
        }
    )
    document = asyncio.run(
        web.fetch(
            allowed_hosts=canonical_seed_hosts(["https://contoso.com/"]),
        )
    )
    assert document.url == "https://www.contoso.com/about"
    assert web.resolutions == ["contoso.com", "contoso.com", "www.contoso.com", "www.contoso.com"]
    # The pipeline, not this helper, must first verify the fetched company's identity.
    verified_hosts = approved_hosts([document.url])
    assert verified_hosts == {"www.contoso.com"}
    assert not is_approved_url("https://contoso.com/news", verified_hosts)
    assert not is_approved_url("https://news.contoso.com/announcement", verified_hosts)


def test_canonical_seed_counterpart_still_requires_public_dns():
    async def scenario():
        web = Web(
            {
                ("contoso.com", "/"): (301, {"location": "https://www.contoso.com/"}, b""),
            }
        )

        async def resolve(host):
            return ("127.0.0.1",) if host == "www.contoso.com" else (PUBLIC_IP,)

        async with SourceFetcher(resolver=resolve, transport=web.transport) as fetcher:
            with pytest.raises(SourceError) as error:
                await fetcher.fetch(
                    "https://contoso.com/",
                    allowed_hosts=canonical_seed_hosts(["https://contoso.com/"]),
                )
        assert error.value.code == "unsafe_address"
        assert all(request.headers["host"] == "contoso.com" for request in web.requests)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "0.0.0.0",
        "10.0.0.1",
        "172.16.0.1",
        "192.168.0.1",
        "169.254.169.254",
        "168.63.129.16",
        "100.100.100.200",
        "192.0.2.5",
        "198.18.0.1",
        "224.1.2.3",
        "255.255.255.255",
        "::",
        "::1",
        "fe80::1",
        "fd00::1",
        "ff02::1",
        "2001:db8::1",
        "::ffff:127.0.0.1",
        "::ffff:93.184.216.34",
        "2002:0a00:0001::",
        "2001:0000:4136:e378:8000:63bf:3fff:fdd2",
    ],
)
def test_non_global_mapped_transition_and_metadata_addresses_are_denied(address):
    assert not is_public_address(address)


def test_public_ipv4_and_ipv6_addresses_are_allowed():
    assert is_public_address(PUBLIC_IP)
    assert is_public_address("2606:4700:4700::1111")


def test_pins_host_sni_and_request_target_and_sends_no_cookies():
    async def scenario():
        web = Web(
            {
                "/start": (
                    302,
                    {"location": "/a%2Fb?x=1+2&x=%2F", "set-cookie": "session=Contoso"},
                    b"",
                ),
            },
            robots=(404, {"set-cookie": "policy=Contoso"}, b""),
        )
        document = await web.fetch("https://contoso.com/start")
        assert document.url == "https://contoso.com/a%2Fb?x=1+2&x=%2F"
        assert document.original_url == "https://contoso.com/start"
        assert len(web.requests) == 3
        for request in web.requests:
            assert request.url.host == PUBLIC_IP
            assert request.headers["host"] == "contoso.com"
            assert request.extensions["sni_hostname"] == "contoso.com"
            assert request.headers["accept-encoding"] == "identity"
            assert request.headers["connection"] == "close"
            assert request.headers["user-agent"] == sources.USER_AGENT
            assert request.headers["user-agent"].startswith("Mozilla/5.0 (Windows NT 10.0;")
            for key in ("cookie", "authorization", "proxy-authorization", "referer"):
                assert key not in request.headers
        assert web.requests[-1].url.raw_path == b"/a%2Fb?x=1+2&x=%2F"
        assert all(stream.closed for stream in web.streams)

    asyncio.run(scenario())


def test_real_httpcore_receives_pinned_ip_and_certificate_hostname(monkeypatch):
    connections = []
    tls_calls = []

    class NetworkStream(httpcore.AsyncNetworkStream):
        def __init__(self):
            self.request = bytearray()
            self.sent = False
            self.closed = False

        async def write(self, buffer, timeout=None):
            self.request.extend(buffer)

        async def read(self, max_bytes, timeout=None):
            if self.sent:
                return b""
            self.sent = True
            if b"GET /robots.txt " in self.request:
                return b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            return (
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                + str(len(HTML)).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + HTML
            )

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            tls_calls.append((ssl_context, server_hostname))
            return self

        async def aclose(self):
            self.closed = True

        def get_extra_info(self, info):
            return None

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        stream = NetworkStream()
        connections.append((host, port, stream))
        return stream

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NETRC", "Contoso-nonexistent-netrc")

    async def scenario():
        async def resolve(host):
            return (PUBLIC_IP,)

        async with SourceFetcher(resolver=resolve) as fetcher:
            await fetcher.fetch("https://contoso.com/")
            await fetcher.fetch("https://www.contoso.com/")

    asyncio.run(scenario())
    assert len(connections) == 4
    assert all(host == PUBLIC_IP and port == 443 for host, port, _ in connections)
    assert [hostname for _, hostname in tls_calls] == [
        "contoso.com",
        "contoso.com",
        "www.contoso.com",
        "www.contoso.com",
    ]
    assert all(
        context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
        for context, _ in tls_calls
    )
    assert all(stream.closed for _, _, stream in connections)
    assert b"Host: contoso.com\r\n" in connections[1][2].request
    assert b"Host: www.contoso.com\r\n" in connections[3][2].request


@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("::1",),
        ("::ffff:127.0.0.1",),
        ("168.63.129.16",),
        (PUBLIC_IP, "10.0.0.1"),
        (PUBLIC_IP, "fe80::1"),
        (PUBLIC_IP, "invalid"),
    ],
)
def test_rejects_every_non_public_dns_answer_before_any_connection(addresses):
    async def scenario():
        web = Web()
        async with SourceFetcher(
            resolver=AsyncMock(return_value=addresses),
            transport=web.transport,
        ) as fetcher:
            with pytest.raises(SourceError, match="public addresses") as error:
                await fetcher.fetch("https://contoso.com/")
        assert error.value.code == "unsafe_address"
        assert not web.requests

    asyncio.run(scenario())


def test_dns_rebinding_between_policy_and_document_is_denied():
    async def scenario():
        web = Web()
        resolver = AsyncMock(side_effect=[(PUBLIC_IP,), ("127.0.0.1",)])
        async with SourceFetcher(resolver=resolver, transport=web.transport) as fetcher:
            with pytest.raises(SourceError) as error:
                await fetcher.fetch("https://contoso.com/")
        assert error.value.code == "unsafe_address"
        assert [request.url.path for request in web.requests] == ["/robots.txt"]

    asyncio.run(scenario())


def test_dns_is_revalidated_after_every_same_host_redirect():
    async def scenario():
        web = Web({"/": (302, {"location": "/about"}, b"")})
        resolver = AsyncMock(side_effect=[(PUBLIC_IP,), (PUBLIC_IP,), ("169.254.169.254",)])
        async with SourceFetcher(resolver=resolver, transport=web.transport) as fetcher:
            with pytest.raises(SourceError) as error:
                await fetcher.fetch("https://contoso.com/")
        assert error.value.code == "unsafe_address"
        assert resolver.await_count == 3
        assert [request.url.path for request in web.requests] == ["/robots.txt", "/"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("http://contoso.com/about", "redirect_invalid"),
        ("https://127.0.0.1/", "redirect_invalid"),
        ("https://[::ffff:127.0.0.1]/", "redirect_invalid"),
        ("https://contoso.com\\@127.0.0.1/", "redirect_invalid"),
        ("https://www.contoso.com/", "unapproved_host"),
        ("https://news.contoso.com/", "unapproved_host"),
        ("https://contoso-cdn.com/", "unapproved_host"),
        ("\nhttps://contoso.com/about", "redirect_invalid"),
        (" /about", "redirect_invalid"),
        ("", "redirect_invalid"),
    ],
)
def test_redirects_cannot_authorize_another_host_or_unsafe_destination(location, code):
    web = Web({"/": (302, {"location": location}, b"")})
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == code
    if code == "unapproved_host":
        assert "canonical URL" in error.value.public_message
        assert "www/apex" in error.value.public_message
    assert len(web.requests) == 2
    assert all(stream.closed for stream in web.streams)


def test_explicitly_approved_www_redirect_gets_its_own_policy_and_dns_checks():
    web = Web({("contoso.com", "/"): (301, {"location": "https://www.contoso.com/about"}, b"")})
    document = asyncio.run(web.fetch(allowed_hosts={"contoso.com", "www.contoso.com"}))
    assert document.url == "https://www.contoso.com/about"
    assert web.resolutions == ["contoso.com", "contoso.com", "www.contoso.com", "www.contoso.com"]
    assert [request.url.path for request in web.requests] == [
        "/robots.txt",
        "/",
        "/robots.txt",
        "/about",
    ]


def test_redirect_loop_and_redirect_count_are_bounded():
    loop = Web({"/": (301, {"location": "/a"}, b""), "/a": (302, {"location": "/"}, b"")})
    with pytest.raises(SourceError) as error:
        asyncio.run(loop.fetch())
    assert error.value.code == "redirect_loop"
    chain = Web(
        {
            "/": (302, {"location": "/a"}, b""),
            "/a": (302, {"location": "/b"}, b""),
            "/b": (302, {"location": "/c"}, b""),
            "/c": (302, {"location": "/d"}, b""),
        }
    )
    with pytest.raises(SourceError) as error:
        asyncio.run(chain.fetch())
    assert error.value.code == "redirect_limit"
    assert "/d" not in [request.url.path for request in chain.requests]


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503, 204])
def test_inaccessible_robots_policy_fails_closed(status, monkeypatch):
    fallback = AsyncMock(
        return_value=sources._Reply("https://contoso.com/robots.txt", 403, httpx.Headers())
    )
    monkeypatch.setattr(sources, "_scrape", fallback)
    web = Web(robots=(status, {"retry-after": "17"}, b"Contoso private diagnostic"))
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == ("source_rate_limited" if status == 429 else "robots_unavailable")
    assert len(web.requests) == 1
    assert "diagnostic" not in str(error.value)
    assert all(stream.closed for stream in web.streams)
    assert fallback.await_count == (1 if status == 403 else 0)


@pytest.mark.parametrize("status", [404, 410])
def test_normal_absent_robots_policy_allows_document(status):
    assert asyncio.run(Web(robots=(status, {}, b"")).fetch()).spans


def test_robots_html_login_and_large_policy_fail_closed():
    for robots in (
        (200, {"content-type": "text/html"}, HTML),
        (200, {"content-type": "text/plain"}, b"<html>Contoso sign in</html>"),
        (200, {"content-type": "text/plain"}, b"Error: sign in to view Contoso"),
        (200, {"content-type": "text/plain"}, b"Disallow: /"),
        (200, {"content-type": "text/plain"}, b"User-agent: *\n" + b"#" * 65_536),
    ):
        web = Web(robots=robots)
        with pytest.raises(SourceError) as error:
            asyncio.run(web.fetch())
        assert error.value.code == "robots_unavailable"
        assert len(web.requests) == 1


def test_robots_wildcards_longest_match_and_specific_user_agent():
    policy = b"""
User-agent: *
Disallow: /

User-agent: CXplorerInsights
Disallow: /private
Disallow: /*?secret=*
Allow: /private/public$
Disallow: /equal
Allow: /equal
"""

    async def scenario():
        web = Web(robots=(200, {"content-type": "text/plain"}, policy))
        async with web.fetcher() as fetcher:
            for path in ("/", "/private/public", "/equal"):
                assert (await fetcher.fetch("https://contoso.com" + path)).text
            for path in ("/private", "/private/public/extra", "/about?secret=value"):
                with pytest.raises(SourceError) as error:
                    await fetcher.fetch("https://contoso.com" + path)
                assert error.value.code == "robots_disallowed"
        assert sum(request.url.path == "/robots.txt" for request in web.requests) == 1
        assert len(web.requests) == 4

    asyncio.run(scenario())


def test_robots_percent_encoded_unreserved_and_unicode_paths_match():
    async def scenario():
        web = Web(
            robots=(
                200,
                {"content-type": "text/plain"},
                ("User-agent: *\nDisallow: /private\nDisallow: /résumé\n").encode(),
            )
        )
        async with web.fetcher() as fetcher:
            for path in ("/%70rivate", "/r%C3%A9sum%C3%A9"):
                with pytest.raises(SourceError) as error:
                    await fetcher.fetch("https://contoso.com" + path)
                assert error.value.code == "robots_disallowed"

    asyncio.run(scenario())


def test_robots_rechecked_for_redirect_target_and_policy_redirect_never_broadens_hosts():
    web = Web(
        {"/": (302, {"location": "/private"}, b"")},
        robots=(200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /private"),
    )
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == "robots_disallowed"
    assert [request.url.path for request in web.requests] == ["/robots.txt", "/"]
    web = Web(robots=(302, {"location": "https://www.contoso.com/robots.txt"}, b""))
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch(allowed_hosts={"contoso.com", "www.contoso.com"}))
    assert error.value.code == "unapproved_host"
    assert len(web.requests) == 1


def test_robots_crawl_delay_exceeding_finite_budget_is_explicit():
    web = Web(robots=(200, {"content-type": "text/plain"}, b"User-agent: *\nCrawl-delay: 120"))
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == "robots_delay"


def test_robots_cache_expires_without_storing_documents():
    async def scenario():
        web = Web()
        async with web.fetcher(limits=FetchLimits(robots_cache_seconds=0.01)) as fetcher:
            await fetcher.fetch("https://contoso.com/")
            await asyncio.sleep(0.02)
            await fetcher.fetch("https://contoso.com/about")
        assert sum(request.url.path == "/robots.txt" for request in web.requests) == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "source_forbidden"),
        (403, "source_forbidden"),
        (404, "source_unavailable"),
        (410, "source_unavailable"),
        (500, "source_unavailable"),
        (204, "source_unavailable"),
        (206, "source_unavailable"),
        (429, "source_rate_limited"),
    ],
)
def test_document_status_errors_are_not_ingested(status, code, monkeypatch):
    fallback = AsyncMock(return_value=sources._Reply("https://contoso.com/", 403, httpx.Headers()))
    monkeypatch.setattr(sources, "_scrape", fallback)
    web = Web({"/": (status, {"retry-after": "17"}, b"Contoso private response diagnostic")})
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == code
    assert "diagnostic" not in str(error.value)
    if status == 429:
        assert error.value.retry_after == 17
    assert all(stream.closed for stream in web.streams)
    assert fallback.await_count == (1 if status == 403 else 0)


@pytest.mark.parametrize(
    ("headers", "body", "code"),
    [
        ({"content-type": "application/json"}, b'{"company":"Contoso"}', "unsupported_media_type"),
        ({"content-type": "image/svg+xml"}, b"<svg>Contoso</svg>", "unsupported_media_type"),
        ({"content-type": "application/pdf"}, HTML, "unsupported_media_type"),
        ({"content-type": "text/html"}, b"%PDF-1.7\n", "unsupported_media_type"),
        ({"content-type": "text/plain"}, b"Contoso plain text", "unsupported_media_type"),
        ({"content-type": "text/html"}, b"\x00" * 100, "unsupported_media_type"),
        ({"content-type": "text/html", "content-encoding": "br"}, HTML, "unsupported_encoding"),
        ({"content-type": "text/html", "content-length": "-1"}, HTML, "invalid_response"),
        (
            {"content-type": "text/html", "content-encoding": "gzip"},
            b"not gzip",
            "invalid_response",
        ),
        (
            {"content-type": "text/html", "content-encoding": "gzip"},
            gzip.compress(HTML) + gzip.compress(HTML),
            "invalid_response",
        ),
        (
            {"content-type": "text/html", "content-encoding": "gzip"},
            gzip.compress(HTML)[:-4],
            "invalid_response",
        ),
    ],
)
def test_types_headers_and_compression_fail_closed_and_close_streams(headers, body, code):
    web = Web({"/": (200, headers, body)})
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == code
    assert all(stream.closed for stream in web.streams)


def test_transfer_and_decoded_byte_limits_bound_gzip_bombs_and_unknown_types():
    for headers, body in (
        ({"content-type": "text/html", "content-length": "1025"}, HTML),
        ({"content-type": "text/html"}, (HTML, b" " * 1024)),
        ({"content-type": "text/html", "content-encoding": "gzip"}, gzip.compress(HTML * 20)),
        ({}, HTML * 20),
    ):
        web = Web({"/": (200, headers, body)})
        with pytest.raises(SourceError) as error:
            asyncio.run(web.fetch(limits=FetchLimits(max_html_bytes=1024)))
        assert error.value.code == "source_too_large"
        assert all(stream.closed for stream in web.streams)


def test_gzip_and_sniffed_html_are_readable_without_changing_content_hash():
    compressed = Web(
        {"/": (200, {"content-type": "text/html", "content-encoding": "gzip"}, gzip.compress(HTML))}
    )
    sniffed = Web({"/": (200, {"content-type": "application/octet-stream"}, HTML)})
    first, second = asyncio.run(compressed.fetch()), asyncio.run(sniffed.fetch())
    assert first.text == second.text
    assert first.content_hash == second.content_hash
    assert first.id == second.id


def test_no_content_is_returned_on_network_or_request_timeout():
    async def scenario():
        for failure in (
            httpx.ConnectError("Contoso diagnostic"),
            httpx.ReadTimeout("Contoso diagnostic"),
        ):
            web = Web()

            async def failing(request, error=failure, current_web=web):
                if request.url.path == "/robots.txt":
                    return await current_web.handle(request)
                raise error

            async with SourceFetcher(
                transport=httpx.MockTransport(failing),
                resolver=web.resolve,
            ) as fetcher:
                with pytest.raises(SourceError) as error:
                    await fetcher.fetch("https://contoso.com/")
                assert error.value.code in {"source_network_error", "source_timeout"}
                assert "diagnostic" not in str(error.value)
        web = Web()

        async def slow(request):
            if request.url.path != "/robots.txt":
                await asyncio.sleep(0.1)
            return await web.handle(request)

        async with SourceFetcher(
            limits=FetchLimits(request_timeout=0.01),
            transport=httpx.MockTransport(slow),
            resolver=web.resolve,
        ) as fetcher:
            with pytest.raises(SourceError) as error:
                await fetcher.fetch("https://contoso.com/")
            assert error.value.code == "source_timeout"

    asyncio.run(scenario())


def test_global_and_per_host_request_concurrency_are_bounded():
    async def scenario():
        web = Web()
        active = Counter()
        peak = Counter()
        total = 0
        total_peak = 0

        async def concurrent(request):
            nonlocal total, total_peak
            host = request.headers["host"]
            active[host] += 1
            peak[host] = max(peak[host], active[host])
            total += 1
            total_peak = max(total_peak, total)
            try:
                await asyncio.sleep(0.01)
                return await web.handle(request)
            finally:
                total -= 1
                active[host] -= 1

        async with SourceFetcher(
            limits=FetchLimits(max_concurrent_fetches=2),
            transport=httpx.MockTransport(concurrent),
            resolver=web.resolve,
        ) as fetcher:
            documents = await asyncio.gather(
                *(
                    fetcher.fetch(f"https://{host}/{index}")
                    for index, host in enumerate(("contoso.com", "news.contoso.com") * 3)
                )
            )
        assert len(documents) == 6
        assert total_peak == 2
        assert max(peak.values()) == 1

    asyncio.run(scenario())


def test_html_extraction_is_clean_quoteable_and_links_never_authorize_hosts():
    html = b"""
<!doctype html><html><head><title>Contoso &amp; Company</title>
<base href="https://contoso-cdn.com/"><link rel="canonical" href="https://contoso-cdn.com/canonical">
<meta property="article:published_time" content="2026-09-04T10:30:00Z">
<script type="application/ld+json">{"url":"https://contoso-cdn.com/script"}</script>
<style>.Contoso { color: red }</style></head><body>
<nav><a href="/about#team">About Contoso navigation</a></nav>
<main><h1>Contoso products</h1>
<p>Contoso builds <strong>reliable &amp; accessible</strong> products
for customers across the world.</p>
<h2>Company news</h2><p>Contoso announced a new collaboration service for enterprise customers.</p>
<time itemprop="datePublished" datetime="2026-09-04">September 4, 2026</time>
<a href="https://news.contoso.com/announcement">Read the announcement</a>
<a href="https://127.0.0.1/secret">Private link</a>
<a href="javascript:alert(1)">Script link</a>
<a href="/products?category=all&amp;view=detail">Products</a>
</main><p hidden>Contoso hidden content</p><div aria-hidden="true">Contoso aria content</div>
<div style="display: none">Contoso style content</div><div class="sr-only">Contoso screen content</div>
<footer>Contoso footer</footer><noscript>Enable JavaScript</noscript>
<script>Contoso script content</script></body></html>
"""
    web = Web({"/": (200, {"content-type": "text/html", "last-modified": "2026-09-07"}, html)})
    document = asyncio.run(web.fetch())
    assert document.title == "Contoso & Company"
    quote = "Contoso builds reliable & accessible products for customers across the world."
    assert any(span.text == quote and span.section == "Contoso products" for span in document.spans)
    for excluded in (
        "hidden content",
        "aria content",
        "style content",
        "screen content",
        "Contoso footer",
        "navigation",
        "script content",
        "Enable JavaScript",
    ):
        assert excluded not in document.text
    assert set(document.links) == {
        "https://contoso.com/about",
        "https://news.contoso.com/announcement",
        "https://contoso.com/products?category=all&view=detail",
    }
    assert len(web.requests) == 2
    assert document.published_at == date(2026, 9, 4)
    assert len(document.publication_evidence) == 2
    assert all(span in document.spans for span in document.publication_evidence)
    assert all("2026-09-04" in span.text for span in document.publication_evidence)
    assert len({span.id for span in document.spans}) == len(document.spans)
    assert document.coverage["complete"]


@pytest.mark.parametrize(
    ("metadata", "published", "conflict"),
    [
        (b'<time datetime="2026-09-06">September 6</time>', None, False),
        (b'<meta property="article:modified_time" content="2026-09-06">', None, False),
        (b'<meta itemprop="datePublished" content="2026-09-06">', date(2026, 9, 6), False),
        (b'<meta property="article:published_time" content="2026-02-30">', None, False),
        (
            b'<meta property="article:published_time" content="2026-09-06">'
            b'<time pubdate datetime="2026-09-04">September 4</time>',
            None,
            True,
        ),
    ],
)
def test_publication_dates_need_explicit_unambiguous_evidence(metadata, published, conflict):
    body = HTML.replace(b"<body>", b"<body>" + metadata)
    web = Web({"/": (200, {"content-type": "text/html", "last-modified": "2026-09-07"}, body)})
    document = asyncio.run(web.fetch())
    assert document.published_at == published
    assert document.coverage["publication_date_conflict"] is conflict
    assert document.retrieved_at.tzinfo == UTC


def test_source_and_span_ids_are_server_owned_stable_and_clock_is_utc():
    async def scenario():
        web = Web()
        now = datetime(2026, 9, 7, 1, 30, tzinfo=UTC)
        async with web.fetcher(clock=lambda: now) as fetcher:
            first = await fetcher.fetch("https://contoso.com/")
            second = await fetcher.fetch("https://contoso.com/#ignored")
        assert first.id == second.id
        assert first.spans == second.spans
        assert first.retrieved_at == now
        assert first.id.startswith("src_") and all(
            span.id.startswith("sp_") for span in first.spans
        )
        assert first.content_hash and first.original_url == "https://contoso.com/"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        b"<html><title>Contoso platform for global customer operations and collaboration</title>"
        b"<body><div id='app'></div><script>Contoso application</script></body></html>",
        b"<html><body><p>Please enable JavaScript to view Contoso products and services.</p></body></html>",
        b"<html><body><p>Verify you are human to access Contoso products and company information.</p></body></html>",
        b"<html><body><p>Sign in to continue reading the Contoso company information.</p></body></html>",
    ],
)
def test_script_only_sign_in_and_bot_gate_pages_are_not_usable_content(body):
    web = Web({"/": (200, {"content-type": "text/html"}, body)})
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == "unreadable_source"


def test_html_text_spans_and_links_report_their_limits():
    body = (
        b"<html><body>"
        + b"<p>Contoso develops reliable enterprise products for global customers.</p>" * 30
    )
    body += b'<a href="/about">About</a><a href="/news">News</a></body></html>'
    web = Web({"/": (200, {"content-type": "text/html"}, body)})
    document = asyncio.run(
        web.fetch(
            limits=FetchLimits(
                max_text_chars=250,
                max_spans=3,
                max_span_chars=80,
                max_links=1,
            )
        )
    )
    assert sum(len(span.text) for span in document.spans) <= 250
    assert len(document.text) <= 250
    assert len(document.spans) <= 3
    assert all(len(span.text) <= 80 for span in document.spans)
    assert document.coverage["truncated"] and document.coverage["text_truncated"]
    assert not document.coverage["complete"] and document.coverage["omissions"]
    assert document.coverage["links_truncated"] and len(document.links) == 1


def test_html_depth_and_node_bounds_fail_explicitly():
    web = Web({"/": (200, {"content-type": "text/html"}, b"<div>" * 30 + HTML + b"</div>" * 30)})
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch(limits=FetchLimits(max_html_depth=10)))
    assert error.value.code == "html_complexity"
    with pytest.raises(SourceError) as error:
        asyncio.run(Web().fetch(limits=FetchLimits(max_html_nodes=2)))
    assert error.value.code == "html_complexity"
    markup = b"<html><body><p attribute='" + b"x" * 80_000
    web = Web({"/": (200, {"content-type": "text/html"}, markup)})
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == "html_complexity"


def test_staged_parser_input_counts_toward_the_html_complexity_bound(monkeypatch):
    def stage_unparsed_input(parser, _data):
        parser.rawdata = "<p attribute='" + "x" * 32_768
        parser._pending_len = 32_769

    monkeypatch.setattr(sources._HTMLText, "feed", stage_unparsed_input)
    with pytest.raises(SourceError) as error:
        sources._parse_html(
            b"<p attribute='value",
            httpx.Headers({"content-type": "text/html"}),
            "https://contoso.com/",
            FetchLimits(),
        )
    assert error.value.code == "html_complexity"


def test_html_timeout_encoding_and_publication_metadata_omissions_are_explicit():
    with pytest.raises(SourceError) as error:
        asyncio.run(Web().fetch(limits=FetchLimits(html_parse_timeout=1e-12)))
    assert error.value.code == "html_parse_timeout"
    body = HTML.replace(b"</head>", b'<meta charset="windows-1252"></head>')
    body = body.replace(b"reliable software", b"reliable software for caf\xe9 customers")
    web = Web({"/": (200, {"content-type": "text/html"}, body)})
    assert "café customers" in asyncio.run(web.fetch()).text
    metadata = b'<meta property="article:published_time" content="2026-09-06">' * 17
    web = Web(
        {"/": (200, {"content-type": "text/html"}, HTML.replace(b"</head>", metadata + b"</head>"))}
    )
    document = asyncio.run(web.fetch())
    assert document.published_at is None
    assert document.coverage["publication_metadata_truncated"]
    assert not document.coverage["complete"]
    assert "publication_metadata_limit" in document.coverage["omissions"]


def test_publication_hint_is_absent_if_its_evidence_was_truncated():
    body = HTML.replace(
        b"</body>", b'<time pubdate datetime="2026-09-04">September 4</time></body>'
    )
    web = Web({"/": (200, {"content-type": "text/html"}, body)})
    document = asyncio.run(web.fetch(limits=FetchLimits(max_text_chars=100)))
    assert not document.publication_evidence
    assert document.published_at is None
    assert document.coverage["truncated"]


def test_preconsumed_mock_response_and_query_values_are_not_logged(caplog):
    async def scenario():
        async def handler(request):
            if request.url.path == "/robots.txt":
                return httpx.Response(404)
            return httpx.Response(200, headers={"content-type": "text/html"}, content=HTML)

        async with SourceFetcher(
            transport=httpx.MockTransport(handler),
            resolver=AsyncMock(return_value=(PUBLIC_IP,)),
        ) as fetcher:
            return await fetcher.fetch("https://contoso.com/?credential=ContosoSyntheticValue")

    with caplog.at_level("INFO"):
        assert asyncio.run(scenario()).spans
    assert "ContosoSyntheticValue" not in caplog.text


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_pdf_pages": 201},
        {"max_pdf_bytes": 15_728_641},
        {"max_html_bytes": 2_097_153},
        {"max_concurrent_fetches": 0},
        {"max_span_chars": 0},
        {"request_timeout": 21},
        {"pdf_memory_bytes": 1024},
        {"total_timeout": float("inf")},
        {"request_timeout": float("nan")},
    ],
)
def test_fetch_limits_cannot_disable_hard_resource_bounds(kwargs):
    with pytest.raises(ValueError):
        FetchLimits(**kwargs)


def make_pdf(*texts, encrypted=False):
    writer = PdfWriter()
    writer.add_metadata({"/Title": "Contoso company profile", "/CreationDate": "D:20260906000000Z"})
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        if text is None:
            continue
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {NameObject("/F1"): writer._add_object(font)}
                ),
            }
        )
        stream = DecodedStreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 50 750 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("Contoso")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


PDF_TEXT = "Contoso builds reliable products for customers and enterprise teams around the world."


def test_real_text_pdf_worker_returns_pages_provenance_and_no_inferred_publication_date():
    body = make_pdf(
        PDF_TEXT, "Contoso offers enterprise collaboration and workflow tools to customers."
    )
    web = Web({"/profile.pdf": (200, {"content-type": "application/pdf"}, body)})
    document = asyncio.run(web.fetch("https://contoso.com/profile.pdf"))
    assert document.media_type == "application/pdf"
    assert document.title == "Contoso company profile"
    assert {span.page for span in document.spans} == {1, 2}
    assert document.spans[0].text == PDF_TEXT
    assert document.published_at is None
    assert not document.publication_evidence
    assert document.coverage["complete"]
    assert document.coverage["total_pages"] == document.coverage["pages_processed"] == 2
    assert not document.coverage["ocr_performed"]
    assert not document.links


def test_pdf_magic_is_sniffed_for_generic_downloads():
    web = Web({"/profile": (200, {"content-type": "application/octet-stream"}, make_pdf(PDF_TEXT))})
    assert asyncio.run(web.fetch("https://contoso.com/profile")).media_type == "application/pdf"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (make_pdf(None), "pdf_scanned"),
        (make_pdf(PDF_TEXT, encrypted=True), "pdf_encrypted"),
        (b"%PDF-1.7\nContoso invalid PDF content\n%%EOF", "pdf_parse_failed"),
    ],
)
def test_scanned_encrypted_and_malformed_pdfs_fail_preflight(body, code):
    web = Web({"/": (200, {"content-type": "application/pdf"}, body)})
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == code
    assert all(stream.closed for stream in web.streams)


def test_pdf_page_text_and_non_text_omissions_are_explicit():
    body = make_pdf(PDF_TEXT, None, PDF_TEXT)
    web = Web({"/": (200, {"content-type": "application/pdf"}, body)})
    document = asyncio.run(web.fetch(limits=FetchLimits(max_pdf_pages=2)))
    assert document.coverage["total_pages"] == 3
    assert document.coverage["pages_processed"] == 2
    assert document.coverage["pages_without_text"] == [2]
    assert document.coverage["omitted_pages"] == 1
    assert document.coverage["truncated"] and not document.coverage["complete"]
    long_pdf = Web({"/": (200, {"content-type": "application/pdf"}, make_pdf(PDF_TEXT * 20))})
    document = asyncio.run(long_pdf.fetch(limits=FetchLimits(max_text_chars=150)))
    assert sum(len(span.text) for span in document.spans) <= 150
    assert document.coverage["text_truncated"] and "text_limit" in document.coverage["omissions"]


def test_pdf_download_limit_prevents_starting_worker(monkeypatch):
    parse = AsyncMock()
    monkeypatch.setattr(sources, "_parse_pdf", parse)
    web = Web({"/": (200, {"content-type": "application/pdf"}, make_pdf(PDF_TEXT))})
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch(limits=FetchLimits(max_pdf_bytes=100)))
    assert error.value.code == "source_too_large"
    parse.assert_not_awaited()


@pytest.mark.parametrize("cancel", [False, True])
def test_pdf_timeout_and_cancellation_kill_and_reap_only_owned_process(monkeypatch, cancel):
    real_create = asyncio.create_subprocess_exec
    processes = []

    async def scenario():
        started = asyncio.Event()

        async def sleeping_worker(*args, **kwargs):
            assert args[1:4] == ("-I", "-S", "-B")
            assert set(kwargs["env"]) <= {"SYSTEMROOT", "WINDIR"}
            assert kwargs["stdin"] == asyncio.subprocess.PIPE
            assert kwargs["stdout"] == asyncio.subprocess.PIPE
            assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
            process = await real_create(
                sys.executable, "-I", "-S", "-B", "-c", "import time; time.sleep(60)", **kwargs
            )
            processes.append(process)
            started.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", sleeping_worker)
        task = asyncio.create_task(
            sources._parse_pdf(
                make_pdf(PDF_TEXT),
                FetchLimits(pdf_parse_timeout=2 if cancel else 0.15),
            )
        )
        await started.wait()
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(SourceError) as error:
                await task
            assert error.value.code == "pdf_parse_timeout"
        assert len(processes) == 1 and processes[0].returncode is not None

    asyncio.run(scenario())


def test_worker_environment_never_inherits_application_credentials(monkeypatch):
    monkeypatch.setenv("CONTOSO_TEST_CREDENTIAL", "Contoso synthetic fixture")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("PYTHONPATH", "Contoso not a module directory")
    assert set(sources._worker_environment()) <= {"SYSTEMROOT", "WINDIR"}


def test_worker_kernel_memory_limits_and_action_restrictions_are_real():
    worker_path = str(Path(pdf_worker.__file__).resolve())
    code = f"""
import runpy
worker = runpy.run_path({worker_path!r}, run_name="limits_test")
worker["_resource_limits"]({{"memory_bytes": 67108864, "timeout": 5}})
try:
    data = bytearray(134217728)
except MemoryError:
    print("memory-bounded")
else:
    raise RuntimeError("memory limit not enforced")
import socket
worker["_disable_external_actions"]()
for action in (lambda: socket.socket(), lambda: open("Contoso-denied-document", "w")):
    try:
        action()
    except PermissionError:
        print("action-denied")
    else:
        raise RuntimeError("external action was allowed")
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", code],
        capture_output=True,
        timeout=10,
        check=True,
        env=sources._worker_environment(),
    )
    assert completed.stdout.decode().splitlines() == [
        "memory-bounded",
        "action-denied",
        "action-denied",
    ]


def test_pdf_bad_worker_output_is_not_success_shaped_content(monkeypatch):
    class Input:
        def write(self, body):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class Output:
        def __init__(self):
            self.sent = False

        async def read(self, size):
            if self.sent:
                return b""
            self.sent = True
            return json.dumps(
                {"title": "Contoso", "blocks": [{"page": 500, "text": "Contoso"}], "coverage": {}}
            ).encode()

    class Process:
        stdin = Input()
        stdout = Output()
        returncode = 0

        async def wait(self):
            return 0

    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=Process()))
    with pytest.raises(SourceError) as error:
        asyncio.run(sources._parse_pdf(make_pdf(PDF_TEXT), FetchLimits()))
    assert error.value.code == "pdf_parse_failed"


def test_fetcher_close_is_idempotent_and_disallows_reuse():
    async def scenario():
        web = Web()
        close = AsyncMock()
        web.transport.aclose = close
        fetcher = web.fetcher()
        await fetcher.fetch("https://contoso.com/")
        await fetcher.close()
        await fetcher.close()
        close.assert_awaited_once()
        with pytest.raises(RuntimeError, match="closed"):
            await fetcher.fetch("https://contoso.com/")

    asyncio.run(scenario())

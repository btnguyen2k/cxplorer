"""Offline browser-fallback, transport, and process-lifecycle regressions."""

import asyncio
import base64
import gzip
import http.client
import io
import json
import ssl
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import certifi
import httpx
import pytest
import requests
from cloudscraper import CloudScraper
from cloudscraper.interpreters import JavaScriptInterpreter
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.response import HTTPResponse

from cxplorer.insights import scraper_worker, sources
from cxplorer.insights.sources import FetchLimits, SourceFetcher
from cxplorer.insights.urls import SourceError
from tests.test_insights_sources import HTML, PUBLIC_IP, Web

CHALLENGE = (
    b"<html><head><title>Just a moment...</title></head><body>"
    b"<p>Checking your browser</p>"
    b'<script src="/cdn-cgi/challenge-platform/Contoso"></script></body></html>'
)


def reply(url="https://contoso.com/", status=200, headers=None, body=HTML):
    return sources._Reply(
        url, status, httpx.Headers(headers or {"content-type": "text/html"}), body, "text/html"
    )


@pytest.mark.parametrize(
    ("status", "headers", "body"),
    [
        (403, {}, b""),
        (403, {"content-type": "application/json"}, b'{"error":"Contoso access denied"}'),
        (200, {"content-type": "text/html"}, CHALLENGE),
        (503, {"content-type": "text/html"}, CHALLENGE),
        (200, {"cf-mitigated": "challenge"}, b""),
        (503, {"cf-mitigated": "challenge"}, b""),
        (202, {"x-amzn-waf-action": "challenge"}, b""),
    ],
)
def test_httpx_block_or_challenge_falls_back_once(status, headers, body, monkeypatch):
    web = Web({"/": (status, headers, body)})

    async def fallback(url, address, limits, *, robots, crawl_delay):
        assert all(stream.closed for stream in web.streams)
        assert url == "https://contoso.com/" and address == PUBLIC_IP
        assert not robots and crawl_delay == 0
        return reply()

    scrape = AsyncMock(side_effect=fallback)
    monkeypatch.setattr(sources, "_scrape", scrape)
    document = asyncio.run(web.fetch())
    assert document.title == "Contoso"
    assert scrape.await_count == 1


@pytest.mark.parametrize("status", [200, 401, 404, 429, 500, 503])
def test_regular_responses_do_not_trigger_browser_fallback(status, monkeypatch):
    scrape = AsyncMock()
    monkeypatch.setattr(sources, "_scrape", scrape)
    web = Web({"/": (status, {"content-type": "text/html"}, HTML)})
    if status == 200:
        assert asyncio.run(web.fetch()).title == "Contoso"
    else:
        with pytest.raises(SourceError):
            asyncio.run(web.fetch())
    scrape.assert_not_awaited()


@pytest.mark.parametrize("status", [401, 429])
def test_login_and_rate_limits_take_precedence_over_challenge_headers(status, monkeypatch):
    fallback = AsyncMock()
    monkeypatch.setattr(sources, "_scrape", fallback)
    with pytest.raises(SourceError) as error:
        asyncio.run(
            Web({"/": (status, {"cf-mitigated": "challenge", "retry-after": "17"}, b"")}).fetch()
        )
    assert error.value.code == ("source_forbidden" if status == 401 else "source_rate_limited")
    fallback.assert_not_awaited()


def test_ordinary_cloudflare_discussion_is_not_a_challenge():
    body = (
        b"<html><title>Contoso security research</title><p>"
        b"Contoso explains /cdn-cgi/challenge-platform/ and checking your browser.</p></html>"
    )
    assert not sources._is_challenge(httpx.Headers(), body)


@pytest.mark.parametrize(
    ("status", "headers", "body", "code"),
    [
        (403, {}, b"Contoso access denied", "source_forbidden"),
        (401, {}, b"", "source_forbidden"),
        (429, {"retry-after": "17"}, b"", "source_rate_limited"),
        (
            429,
            {"cf-mitigated": "challenge", "retry-after": "17"},
            b"",
            "source_rate_limited",
        ),
        (200, {"content-type": "text/html"}, CHALLENGE, "source_challenge"),
        (503, {"cf-mitigated": "challenge"}, b"", "source_challenge"),
    ],
)
def test_failed_fallback_is_explicit_and_never_recurses(status, headers, body, code, monkeypatch):
    scrape = AsyncMock(return_value=reply(status=status, headers=headers, body=body))
    monkeypatch.setattr(sources, "_scrape", scrape)
    with pytest.raises(SourceError) as error:
        asyncio.run(Web({"/": (403, {}, b"")}).fetch())
    assert error.value.code == code
    assert scrape.await_count == 1
    if status == 429:
        assert error.value.retry_after == 17


def test_robots_fallback_is_checked_before_fetching_the_document(monkeypatch):
    policy = b"User-agent: CXplorerInsights\nDisallow: /"
    scrape = AsyncMock(
        return_value=reply(
            "https://contoso.com/robots.txt",
            headers={"content-type": "text/plain"},
            body=policy,
        )
    )
    monkeypatch.setattr(sources, "_scrape", scrape)
    web = Web(robots=(403, {}, b""))
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == "robots_disallowed"
    assert [request.url.path for request in web.requests] == ["/robots.txt"]
    assert scrape.call_args.kwargs["robots"] is True


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("/private", "robots_disallowed"),
        ("https://news.contoso.com/", "unapproved_host"),
        ("https://127.0.0.1/", "redirect_invalid"),
        ("http://contoso.com/", "redirect_invalid"),
    ],
)
def test_fallback_redirects_still_pass_through_the_existing_policy(location, code, monkeypatch):
    scrape = AsyncMock(return_value=reply(status=302, headers={"location": location}, body=b""))
    monkeypatch.setattr(sources, "_scrape", scrape)
    web = Web(
        {"/": (403, {}, b"")},
        robots=(200, {"content-type": "text/plain"}, b"User-agent: *\nDisallow: /private"),
    )
    with pytest.raises(SourceError) as error:
        asyncio.run(web.fetch())
    assert error.value.code == code
    assert [request.url.path for request in web.requests] == ["/robots.txt", "/"]


def test_fetcher_uses_certifi_without_loading_system_certificates(monkeypatch):
    create = ssl.create_default_context
    options = []

    def context(*args, **kwargs):
        options.append(kwargs)
        return create(*args, **kwargs)

    monkeypatch.setattr(sources.ssl, "create_default_context", context)
    asyncio.run(Web().fetch())
    assert options == [{"cafile": certifi.where()}]


def test_fallback_obeys_crawl_delay_after_the_primary_request(monkeypatch):
    primary_finished = []
    web = Web(
        {"/": (403, {}, b"")},
        robots=(200, {"content-type": "text/plain"}, b"User-agent: *\nCrawl-delay: 0.02"),
    )

    async def request(req):
        result = await web.handle(req)
        if req.url.path == "/":
            primary_finished.append(time.monotonic())
        return result

    async def fallback(url, address, limits, *, robots, crawl_delay):
        assert crawl_delay == 0.02
        assert time.monotonic() - primary_finished[0] >= 0.02
        return reply()

    async def scenario():
        async with SourceFetcher(
            resolver=web.resolve, transport=httpx.MockTransport(request)
        ) as fetcher:
            await fetcher.fetch("https://contoso.com/")

    monkeypatch.setattr(sources, "_scrape", fallback)
    asyncio.run(scenario())


def test_fallback_stays_inside_global_and_per_host_concurrency_limits(monkeypatch):
    async def scenario():
        active = Counter()
        peak = Counter()
        total_peak = 0

        async def fallback(url, address, limits, **kwargs):
            nonlocal total_peak
            host = httpx.URL(url).host
            active[host] += 1
            peak[host] = max(peak[host], active[host])
            total_peak = max(total_peak, sum(active.values()))
            try:
                await asyncio.sleep(0.01)
                return reply(url)
            finally:
                active[host] -= 1

        monkeypatch.setattr(sources, "_scrape", fallback)
        web = Web({"/": (403, {}, b"")})
        async with web.fetcher(limits=FetchLimits(max_concurrent_fetches=2)) as fetcher:
            await asyncio.gather(
                *(
                    fetcher.fetch(f"https://{host}/")
                    for host in ("contoso.com", "news.contoso.com", "contoso.com")
                )
            )
        assert total_peak == 2 and max(peak.values()) == 1
        assert sum(active.values()) == 0

    asyncio.run(scenario())


@pytest.fixture
def browser_web(monkeypatch):
    calls = []
    routes = {}

    def urlopen(pool, method, url, **kwargs):
        assert pool.host == PUBLIC_IP and pool.port == 443
        assert pool.assert_hostname == "contoso.com"
        assert pool.conn_kw["server_hostname"] == "contoso.com"
        assert pool.conn_kw["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        headers = dict(kwargs["headers"])
        calls.append((method, url, headers))
        response = routes.get((method, url), (200, {"content-type": "text/html"}, HTML))
        if callable(response):
            response = response(headers)
        status, response_headers, body = response
        response_headers = {"Content-Length": str(len(body)), **response_headers}
        wire = (
            f"HTTP/1.1 {status} Contoso\r\n"
            + "".join(f"{name}: {value}\r\n" for name, value in response_headers.items())
            + "\r\n"
        ).encode("latin-1") + body
        original = http.client.HTTPResponse(
            SimpleNamespace(makefile=lambda *_args, **_kwargs: io.BytesIO(wire))
        )
        original.begin()
        return HTTPResponse(
            body=original,
            headers=list(original.headers.items()),
            status=status,
            original_response=original,
            preload_content=False,
            decode_content=False,
        )

    monkeypatch.setattr(HTTPConnectionPool, "urlopen", urlopen)
    return routes, calls


def scrape(url="https://contoso.com/", *, limits=None, robots=False):
    return scraper_worker.scrape(
        url, PUBLIC_IP, limits or FetchLimits(), robots=robots, crawl_delay=0
    )


def test_real_cloudscraper_uses_requested_browser_and_pinned_verified_transport(
    browser_web, monkeypatch
):
    _, calls = browser_web
    initializer = CloudScraper.__init__
    configurations = []

    def initialize(self, *args, **kwargs):
        configurations.append(kwargs)
        initializer(self, *args, **kwargs)

    monkeypatch.setattr(CloudScraper, "__init__", initialize)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NETRC", "Contoso-nonexistent-netrc")
    result = scrape("https://contoso.com/a%2Fb?x=1+2&x=%2f")
    assert base64.b64decode(result["body"]) == HTML
    assert configurations[0]["browser"] == {
        "browser": "chrome",
        "platform": "windows",
        "desktop": True,
    }
    assert configurations[0]["interpreter"] == "native"
    assert calls[0][:2] == ("GET", "/a%2Fb?x=1+2&x=%2f")
    headers = calls[0][2]
    assert headers["Host"] == "contoso.com"
    assert headers["User-Agent"] == sources.USER_AGENT
    assert headers["Accept-Encoding"] == "identity"
    assert not any(name in headers for name in ("Cookie", "Authorization", "Proxy-Authorization"))


@pytest.mark.parametrize(
    ("headers", "body", "code"),
    [
        ({"content-type": "text/html"}, HTML * 10, "source_too_large"),
        (
            {"content-type": "text/html", "content-encoding": "gzip"},
            gzip.compress(HTML * 20),
            "source_too_large",
        ),
        (
            {"content-type": "text/html", "content-encoding": "gzip"},
            gzip.compress(HTML)[:-3],
            "invalid_response",
        ),
        (
            {"content-type": "text/html", "content-encoding": "br"},
            HTML,
            "unsupported_encoding",
        ),
        ({"content-type": "application/json"}, b'{"company":"Contoso"}', "unsupported_media_type"),
    ],
    ids=["large", "gzip-bomb", "truncated-gzip", "unsupported-encoding", "unsupported-media"],
)
def test_browser_responses_use_the_same_bounded_decoder(browser_web, headers, body, code):
    routes, _ = browser_web
    routes["GET", "/"] = (200, headers, body)
    with pytest.raises(SourceError) as error:
        scrape(limits=FetchLimits(max_html_bytes=1024))
    assert error.value.code == code


def test_browser_gzip_is_decoded_once_and_cookies_are_not_returned(browser_web):
    routes, _ = browser_web
    routes["GET", "/"] = (
        200,
        {
            "content-type": "text/html",
            "content-encoding": "gzip",
            "set-cookie": "cf_clearance=Contoso; Path=/; Secure",
        },
        gzip.compress(HTML),
    )
    result = scrape()
    assert base64.b64decode(result["body"]) == HTML
    assert result["transferred"] == len(gzip.compress(HTML))
    assert "set-cookie" not in result["headers"]


def test_full_fallback_result_is_ingested_with_the_original_content_hash(browser_web, monkeypatch):
    routes, _ = browser_web
    routes["GET", "/"] = (
        200,
        {"content-type": "text/html", "content-encoding": "gzip"},
        gzip.compress(HTML),
    )

    async def run(*command, body, **kwargs):
        config = json.loads(body)
        result = scraper_worker.scrape(
            config["url"],
            config["address"],
            FetchLimits(**config["limits"]),
            robots=config["robots"],
            crawl_delay=config["crawl_delay"],
        )
        return json.dumps(result).encode(), 0

    monkeypatch.setattr(sources, "_run_worker", run)
    normal = asyncio.run(Web().fetch())
    recovered = asyncio.run(Web({"/": (403, {}, b"")}).fetch())
    assert recovered.text == normal.text
    assert recovered.content_hash == normal.content_hash
    assert recovered.url == normal.url


@pytest.mark.parametrize(
    "options",
    [
        {"verify": False},
        {"verify": True, "proxies": {"https": "http://127.0.0.1:1"}},
        {"verify": True, "cert": "Contoso-client-certificate"},
    ],
)
def test_browser_transport_cannot_disable_tls_use_proxies_or_client_credentials(options):
    adapter = scraper_worker._PinnedAdapter(
        url="https://contoso.com/",
        address=PUBLIC_IP,
        limits=FetchLimits(),
        robots=False,
        crawl_delay=0,
        ssl_context=ssl.create_default_context(cafile=certifi.where()),
    )
    try:
        request = requests.Request("GET", "https://contoso.com/").prepare()
        with pytest.raises(SourceError) as error:
            adapter.get_connection_with_tls_context(request, **options)
        assert error.value.code == "unapproved_host"
    finally:
        adapter.close()


@pytest.mark.parametrize(
    "destination",
    [
        "https://127.0.0.1/",
        "http://contoso.com/",
        "https://news.contoso.com/",
        "https://contoso.com/private",
    ],
)
def test_challenge_subrequests_cannot_escape_the_approved_target(
    browser_web, monkeypatch, destination
):
    _, calls = browser_web
    original = CloudScraper.perform_request

    def request(self, method, url, *args, **kwargs):
        original(self, method, url, *args, **kwargs)
        return original(self, "GET", destination, allow_redirects=False, stream=True)

    monkeypatch.setattr(CloudScraper, "perform_request", request)
    with pytest.raises(SourceError):
        scrape()
    assert len(calls) == 1


@pytest.mark.parametrize("path", ["/delete", "/?__cf_chl_f_tk=Contoso"])
def test_arbitrary_challenge_posts_are_not_allowed(browser_web, monkeypatch, path):
    _, calls = browser_web
    original = CloudScraper.perform_request

    def request(self, method, url, *args, **kwargs):
        original(self, method, url, *args, **kwargs)
        return original(
            self, "POST", "https://contoso.com" + path, allow_redirects=False, stream=True
        )

    monkeypatch.setattr(CloudScraper, "perform_request", request)
    with pytest.raises(SourceError) as error:
        scrape()
    assert error.value.code == "unapproved_host"
    assert len(calls) == 1


def iuam_page(action):
    return (
        "<html><head><title>Just a moment...</title></head><body>"
        f'<form id="challenge-form" action="{action}">'
        '<input type="hidden" name="r" value="Contoso"/>'
        '<input type="hidden" name="jschl_vc" value="Contoso"/>'
        '<input type="hidden" name="pass" value="Contoso"/>'
        '</form><img src="/cdn-cgi/images/trace/jsch/Contoso"/></body></html>'
    ).encode()


@pytest.fixture
def iuam_solver(monkeypatch):
    initializer = CloudScraper.__init__

    def initialize(self, *args, **kwargs):
        kwargs["delay"] = 0.001
        initializer(self, *args, **kwargs)

    monkeypatch.setattr(CloudScraper, "__init__", initialize)
    # Leave real challenge detection, form parsing, submission, and redirects intact.
    monkeypatch.setattr(
        JavaScriptInterpreter,
        "dynamicImport",
        staticmethod(lambda _name: SimpleNamespace(solveChallenge=lambda _body, _domain: "42")),
    )


@pytest.mark.parametrize("action", ["/products?__cf_chl_f_tk=Contoso", "/?__cf_chl_f_tk=Contoso"])
def test_real_iuam_flow_can_submit_its_observed_original_or_root_form(
    browser_web, iuam_solver, action
):
    routes, calls = browser_web
    challenge = (503, {"server": "cloudflare", "content-type": "text/html"}, iuam_page(action))
    routes["GET", "/products"] = lambda headers: (
        (200, {"content-type": "text/html"}, HTML)
        if headers.get("Cookie") == "cf_clearance=Contoso"
        else challenge
    )
    routes["POST", action] = (
        302,
        {"location": "/products", "set-cookie": "cf_clearance=Contoso; Path=/; Secure"},
        b"",
    )
    result = scrape("https://contoso.com/products")
    assert base64.b64decode(result["body"]) == HTML
    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/products"),
        ("POST", action),
        ("GET", "/products"),
    ]


def test_iuam_form_cannot_authorize_an_arbitrary_document_post(browser_web, iuam_solver):
    routes, calls = browser_web
    routes["GET", "/products"] = (
        503,
        {"server": "cloudflare", "content-type": "text/html"},
        iuam_page("/delete?__cf_chl_f_tk=Contoso"),
    )
    with pytest.raises(SourceError) as error:
        scrape("https://contoso.com/products")
    assert error.value.code == "unapproved_host"
    assert len(calls) == 1


def test_legacy_iuam_is_recognized_without_a_specific_page_title():
    page = iuam_page("/?__cf_chl_f_tk=Contoso").replace(
        b"<title>Just a moment...</title>", b"<title>Contoso browser protection</title>"
    )
    assert sources._is_challenge(httpx.Headers({"server": "cloudflare"}), page)
    assert not sources._is_challenge(httpx.Headers(), page)


def test_only_one_challenge_submission_is_permitted(browser_web, monkeypatch):
    _, calls = browser_web
    original = CloudScraper.perform_request

    def request(self, method, url, *args, **kwargs):
        original(self, method, url, *args, **kwargs)
        for _ in range(2):
            original(
                self,
                "POST",
                "https://contoso.com/cdn-cgi/l/chk_jschl",
                allow_redirects=False,
                stream=True,
            )

    monkeypatch.setattr(CloudScraper, "perform_request", request)
    with pytest.raises(SourceError) as error:
        scrape()
    assert error.value.code == "source_challenge"
    assert [method for method, _, _ in calls] == ["GET", "POST"]


def test_challenge_cookies_stay_within_one_attempt_and_all_requests_are_pinned(
    browser_web, monkeypatch
):
    routes, calls = browser_web
    routes["POST", "/cdn-cgi/l/chk_jschl"] = (
        302,
        {"location": "/", "set-cookie": "cf_clearance=Contoso; Path=/; Secure"},
        b"",
    )
    original = CloudScraper.perform_request

    def request(self, method, url, *args, **kwargs):
        original(self, method, url, *args, **kwargs)
        original(
            self,
            "POST",
            "https://contoso.com/cdn-cgi/l/chk_jschl",
            allow_redirects=False,
            stream=True,
        )
        return original(self, "GET", url, allow_redirects=False, stream=True)

    monkeypatch.setattr(CloudScraper, "perform_request", request)
    for _ in range(2):
        assert base64.b64decode(scrape()["body"]) == HTML
    assert len(calls) == 6
    assert "Cookie" not in calls[0][2] and "Cookie" not in calls[3][2]
    assert calls[2][2]["Cookie"] == "cf_clearance=Contoso"
    assert calls[5][2]["Cookie"] == "cf_clearance=Contoso"


def test_challenge_redirect_to_a_new_path_is_returned_without_following(browser_web, monkeypatch):
    routes, calls = browser_web
    routes["POST", "/cdn-cgi/l/chk_jschl"] = (302, {"location": "/private"}, b"")
    original = CloudScraper.perform_request

    def request(self, method, url, *args, **kwargs):
        original(self, method, url, *args, **kwargs)
        original(
            self,
            "POST",
            "https://contoso.com/cdn-cgi/l/chk_jschl",
            allow_redirects=False,
            stream=True,
        )
        return original(
            self, "GET", "https://contoso.com/private", allow_redirects=False, stream=True
        )

    monkeypatch.setattr(CloudScraper, "perform_request", request)
    result = scrape()
    assert result["status"] == 302
    assert result["headers"]["location"] == "https://contoso.com/private"
    assert len(calls) == 2


def test_relative_challenge_redirect_keeps_its_actual_response_base(browser_web, monkeypatch):
    routes, calls = browser_web
    routes["POST", "/cdn-cgi/l/chk_jschl"] = (302, {"location": "next"}, b"")
    original = CloudScraper.perform_request

    def request(self, method, url, *args, **kwargs):
        original(self, method, url, *args, **kwargs)
        original(
            self,
            "POST",
            "https://contoso.com/cdn-cgi/l/chk_jschl",
            allow_redirects=False,
            stream=True,
        )
        return original(
            self, "GET", "https://contoso.com/cdn-cgi/l/next", allow_redirects=False, stream=True
        )

    monkeypatch.setattr(CloudScraper, "perform_request", request)
    result = scrape("https://contoso.com/reports/index")
    assert result["headers"]["location"] == "https://contoso.com/cdn-cgi/l/next"
    assert len(calls) == 2


def test_worker_output_budget_includes_a_larger_robots_policy(monkeypatch):
    policy = b"User-agent: *\nAllow: /\n#" + b"Contoso " * 7500
    result = {
        "status": 200,
        "headers": {"content-type": "text/plain"},
        "body": base64.b64encode(policy).decode(),
        "transferred": len(policy),
    }
    output = json.dumps(result).encode()

    async def run(*command, output_cap, **kwargs):
        assert len(output) <= output_cap
        return output, 0

    monkeypatch.setattr(sources, "_run_worker", run)
    limits = FetchLimits(max_html_bytes=1024, max_pdf_bytes=1024)
    collected = asyncio.run(
        sources._scrape(
            "https://contoso.com/robots.txt", PUBLIC_IP, limits, robots=True, crawl_delay=0
        )
    )
    assert collected.body == policy
    assert sources._parse_robots(collected.body, "text/plain", limits).allows(
        "https://contoso.com/"
    )


def test_challenge_request_count_is_bounded(browser_web, monkeypatch):
    _, calls = browser_web
    original = CloudScraper.perform_request

    def request(self, method, url, *args, **kwargs):
        for _ in range(10):
            original(self, method, url, *args, **kwargs)
        pytest.fail("The challenge request budget was not enforced.")

    monkeypatch.setattr(CloudScraper, "perform_request", request)
    with pytest.raises(SourceError) as error:
        scrape()
    assert error.value.code == "source_challenge"
    assert len(calls) == 4


@pytest.mark.parametrize("cancel", [False, True])
def test_fallback_timeout_or_cancellation_kills_and_reaps_its_worker(monkeypatch, cancel):
    create = asyncio.create_subprocess_exec
    processes = []

    async def scenario():
        started = asyncio.Event()

        async def sleeping(*args, **kwargs):
            assert args[1:4] == ("-I", "-S", "-B")
            assert Path(args[4]).name == "scraper_bootstrap.py"
            assert all(
                Path(path).name in {"site-packages", "dist-packages"}
                for path in json.loads(args[5])
            )
            assert set(kwargs["env"]) <= {"SYSTEMROOT", "WINDIR"}
            process = await create(
                sys.executable, "-I", "-B", "-c", "import time; time.sleep(60)", **kwargs
            )
            processes.append(process)
            started.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", sleeping)
        task = asyncio.create_task(
            Web({"/": (403, {}, b"")}).fetch(
                limits=FetchLimits(request_timeout=2 if cancel else 0.2)
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
            assert error.value.code == "source_timeout"
        assert len(processes) == 1 and processes[0].returncode is not None

    asyncio.run(scenario())


def test_real_worker_rejects_an_unsafe_address_before_any_request():
    async def scenario():
        async with asyncio.timeout(20):
            await sources._scrape(
                "https://contoso.com/", "127.0.0.1", FetchLimits(), robots=False, crawl_delay=0
            )

    with pytest.raises(SourceError) as error:
        asyncio.run(scenario())
    assert error.value.code == "unsafe_url"


@pytest.mark.parametrize(
    "result",
    [
        {},
        {"error": "Contoso private diagnostic"},
        {"status": 200, "headers": {}, "body": "not base64", "transferred": 0},
        {"status": True, "headers": {}, "body": "", "transferred": 0},
        {"status": 200, "headers": {"set-cookie": "Contoso"}, "body": "", "transferred": 0},
        {"status": 200, "headers": {}, "body": "", "transferred": -1},
    ],
)
def test_invalid_worker_results_are_not_success_shaped_content(result, monkeypatch):
    run = AsyncMock(return_value=(json.dumps(result).encode(), 0))
    monkeypatch.setattr(sources, "_run_worker", run)
    with pytest.raises(SourceError) as error:
        asyncio.run(
            sources._scrape(
                "https://contoso.com/", PUBLIC_IP, FetchLimits(), robots=False, crawl_delay=0
            )
        )
    assert error.value.code == "invalid_response"
    assert "diagnostic" not in str(error.value)

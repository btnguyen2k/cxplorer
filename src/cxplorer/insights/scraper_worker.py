"""One-shot browser fallback with pinned HTTPS and no inherited application credentials."""

import base64
import json
import ssl
import sys
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit

import certifi
import httpx
import requests
from cloudscraper import CipherSuiteAdapter, CloudScraper
from cloudscraper.exceptions import CloudflareException
from urllib3.exceptions import HTTPError as TransportError
from urllib3.exceptions import TimeoutError as TransportTimeout

from cxplorer.insights.pdf_worker import _resource_limits
from cxplorer.insights.sources import (
    _REDIRECTS,
    _SCRAPE_ERRORS,
    _SCRAPE_HEADERS,
    USER_AGENT,
    FetchLimits,
    _BodyReader,
    _is_challenge,
    _robot_path,
)
from cxplorer.insights.urls import MAX_URL_LENGTH, SourceError, is_public_address, normalize_url

BROWSER = {"browser": "chrome", "platform": "windows", "desktop": True}
_MAX_REQUESTS = 4


class _ChallengeRedirect(Exception):
    def __init__(self, response: requests.Response):
        self.response = response


class _ChallengeForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.actions: list[str] = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "form" and attributes.get("id") == "challenge-form":
            action = attributes.get("action")
            if action:
                self.actions.append(action)


def _document_target(url: str) -> tuple[str, str, str]:
    parts = urlsplit(url)
    return parts.netloc, _robot_path(parts.path or "/"), _robot_path(parts.query)


class _PinnedAdapter(CipherSuiteAdapter):
    def __init__(
        self,
        *,
        url: str,
        address: str,
        limits: FetchLimits,
        robots: bool,
        crawl_delay: float,
        ssl_context: ssl.SSLContext,
        **kwargs,
    ):
        self.url = normalize_url(url)
        if not is_public_address(address):
            raise SourceError("unsafe_url", _SCRAPE_ERRORS["unsafe_url"])
        self.address = address
        self.host = urlsplit(self.url).hostname
        assert self.host is not None
        self.limits = limits
        self.robots = robots
        self.crawl_delay = crawl_delay
        self.deadline = time.monotonic() + limits.request_timeout
        self.next_request_at = 0.0
        self.requests_sent = 0
        self.last_response: requests.Response | None = None
        self.transferred = 0
        self.challenge_target: str | None = None
        self.challenge_submitted = False
        super().__init__(
            ssl_context=ssl_context,
            server_hostname=self.host,
            pool_connections=1,
            pool_maxsize=1,
            max_retries=0,
            **kwargs,
        )

    def _validate(self, request: requests.PreparedRequest) -> None:
        if request.url is None:
            raise SourceError("unsafe_url", _SCRAPE_ERRORS["unsafe_url"])
        try:
            url = normalize_url(request.url)
        except SourceError:
            if self.last_response is not None and self.last_response.status_code in _REDIRECTS:
                raise _ChallengeRedirect(self.last_response) from None
            raise SourceError("unsafe_url", _SCRAPE_ERRORS["unsafe_url"]) from None
        original = _document_target(url) == _document_target(self.url)
        if (
            not original
            and self.last_response is not None
            and self.last_response.status_code in _REDIRECTS
        ):
            raise _ChallengeRedirect(self.last_response)
        parts = urlsplit(url)
        challenge = parts.path == "/cdn-cgi/l/chk_jschl" or parts.path.startswith(
            "/cdn-cgi/challenge-platform/"
        )
        if request.method == "POST" and self.challenge_target is not None:
            challenge = challenge or _document_target(url) == _document_target(
                self.challenge_target
            )
        if (
            parts.hostname != self.host
            or request.method not in {"GET", "POST"}
            or (request.method == "POST" and not challenge)
            or (request.method == "GET" and not original and not challenge)
        ):
            raise SourceError("unapproved_host", _SCRAPE_ERRORS["unapproved_host"])
        request.headers["Host"] = f"[{self.host}]" if ":" in self.host else self.host
        request.headers["User-Agent"] = USER_AGENT
        request.headers["Accept-Encoding"] = "identity"
        request.headers["Connection"] = "close"
        if any(name in request.headers for name in ("Authorization", "Proxy-Authorization")):
            raise SourceError("unapproved_host", _SCRAPE_ERRORS["unapproved_host"])

    def _challenge_action(self, response: requests.Response, body: bytes) -> str | None:
        headers = httpx.Headers(dict(response.headers), encoding="latin-1")
        if (
            response.status_code not in {403, 503}
            or not headers.get("server", "").lower().startswith("cloudflare")
            or not _is_challenge(headers, body)
        ):
            return None
        form = _ChallengeForm()
        try:
            form.feed(body.decode(response.encoding or "utf-8"))
            form.close()
        except (UnicodeError, LookupError):
            raise SourceError("invalid_response", _SCRAPE_ERRORS["invalid_response"]) from None
        if len(form.actions) != 1:
            return None
        target = normalize_url(urljoin(response.url, form.actions[0]))
        parts = urlsplit(target)
        tokens = [
            value
            for name, value in parse_qsl(
                parts.query, keep_blank_values=True, max_num_fields=MAX_URL_LENGTH
            )
            if name == "__cf_chl_f_tk"
        ]
        if (
            parts.hostname == self.host
            and _robot_path(parts.path) in {"/", _robot_path(urlsplit(self.url).path)}
            and len(tokens) == 1
            and tokens[0]
        ):
            return target
        return None

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        self._validate(request)
        if verify is not True or cert is not None or (proxies and any(proxies.values())):
            raise SourceError("unapproved_host", _SCRAPE_ERRORS["unapproved_host"])
        return self.poolmanager.connection_from_host(
            self.address,
            port=443,
            scheme="https",
            pool_kwargs={"assert_hostname": self.host, "server_hostname": self.host},
        )

    def request_url(self, request, proxies):
        assert request.url is not None
        url = (
            self.url
            if request.method == "GET"
            and _document_target(request.url) == _document_target(self.url)
            else request.url
        )
        if (
            request.method == "POST"
            and self.challenge_target is not None
            and _document_target(request.url) == _document_target(self.challenge_target)
        ):
            url = self.challenge_target
        return httpx.URL(url).raw_path.decode("ascii")

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        self._validate(request)
        if self.requests_sent >= _MAX_REQUESTS:
            raise SourceError("source_challenge", _SCRAPE_ERRORS["source_challenge"])
        if request.method == "POST":
            if self.challenge_submitted:
                raise SourceError("source_challenge", _SCRAPE_ERRORS["source_challenge"])
            self.challenge_submitted = True
        delay = max(0.0, self.next_request_at - time.monotonic())
        if time.monotonic() + delay >= self.deadline:
            raise SourceError("source_timeout", _SCRAPE_ERRORS["source_timeout"])
        time.sleep(delay)
        self.requests_sent += 1
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SourceError("source_timeout", _SCRAPE_ERRORS["source_timeout"])
        response = super().send(
            request,
            stream=True,
            timeout=remaining,
            verify=verify,
            cert=cert,
            proxies=proxies,
        )
        try:
            self.transferred = 0
            if response.status_code in _REDIRECTS | {401, 429} or (
                self.robots and response.status_code in {404, 410}
            ):
                body = b""
            else:
                headers = httpx.Headers(dict(response.headers), encoding="latin-1")
                reader = _BodyReader(
                    headers,
                    self.limits,
                    robots=self.robots,
                    inspect_only=response.status_code != 200 or _is_challenge(headers),
                )
                for chunk in response.raw.stream(65_536, decode_content=False):
                    if time.monotonic() >= self.deadline:
                        raise SourceError("source_timeout", _SCRAPE_ERRORS["source_timeout"])
                    reader.feed(chunk)
                body, _ = reader.finish()
                self.transferred = reader.transferred
            # requests precomputes redirects, and cloudscraper examines .text.
            # Populate their decoded cache only AFTER enforcing the shared byte limits.
            response._content = body
            response._content_consumed = True
            self.challenge_target = self._challenge_action(response, body)
            self.last_response = response
            return response
        finally:
            response.close()
            self.next_request_at = time.monotonic() + self.crawl_delay


def scrape(
    url: str,
    address: str,
    limits: FetchLimits,
    *,
    robots: bool,
    crawl_delay: float,
) -> dict:
    context = ssl.create_default_context(cafile=certifi.where())
    with CloudScraper(
        browser=dict(BROWSER),
        ssl_context=context,
        interpreter="native",
        allow_brotli=False,
        doubleDown=False,
        solveDepth=1,
    ) as scraper:
        scraper.trust_env = False
        # A supplied CA context must retain cloudscraper's selected browser TLS profile.
        ciphers = scraper.cipherSuite
        context.set_ciphers(ciphers if isinstance(ciphers, str) else ":".join(ciphers))
        context.set_ecdh_curve(scraper.ecdhCurve)
        scraper.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/plain" if robots else "text/html,application/pdf;q=0.9",
                "Accept-Encoding": "identity",
                "Connection": "close",
            }
        )
        adapter = _PinnedAdapter(
            url=url,
            address=address,
            limits=limits,
            robots=robots,
            crawl_delay=crawl_delay,
            ssl_context=context,
            cipherSuite=scraper.cipherSuite,
            ecdhCurve=scraper.ecdhCurve,
        )
        for previous in scraper.adapters.values():
            previous.close()
        scraper.adapters.clear()
        scraper.mount("https://", adapter)
        # The same guard rejects HTTP URLs even if a challenge tries to downgrade.
        scraper.mount("http://", adapter)
        try:
            response = scraper.get(url, allow_redirects=False, stream=True)
        except _ChallengeRedirect as redirect:
            response = redirect.response
        headers = {
            name.lower(): value
            for name, value in response.headers.items()
            if name.lower() in _SCRAPE_HEADERS
        }
        location = headers.get("location", "")
        if (
            response.status_code in _REDIRECTS
            and location
            and not any(ord(char) < 33 or ord(char) == 127 for char in location)
        ):
            headers["location"] = urljoin(response.url, location)
        return {
            "status": response.status_code,
            "headers": headers,
            "body": base64.b64encode(response.content).decode("ascii"),
            "transferred": adapter.transferred,
        }


def main() -> None:
    try:
        config = json.loads(sys.stdin.buffer.read(16_385))
        limits = FetchLimits(**config["limits"])
        try:
            _resource_limits({"memory_bytes": 268_435_456, "timeout": limits.request_timeout})
        except (OSError, ValueError):
            raise SourceError(
                "source_scrape_unavailable", _SCRAPE_ERRORS["source_scrape_unavailable"]
            ) from None
        result = scrape(
            config["url"],
            config["address"],
            limits,
            robots=config["robots"],
            crawl_delay=config["crawl_delay"],
        )
    except SourceError as error:
        result = {"error": error.code if error.code in _SCRAPE_ERRORS else "source_scrape_failed"}
    except (requests.exceptions.Timeout, TransportTimeout):
        result = {"error": "source_timeout"}
    except CloudflareException:
        result = {"error": "source_challenge"}
    except (requests.exceptions.RequestException, TransportError, OSError):
        result = {"error": "source_network_error"}
    except (ValueError, TypeError, KeyError, MemoryError):
        result = {"error": "source_scrape_failed"}
    sys.stdout.write(json.dumps(result, separators=(",", ":"), ensure_ascii=True))


if __name__ == "__main__":
    main()

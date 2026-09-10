"""Microsoft ID token validation using real signatures and local HTTP responses."""

import time
from collections.abc import Iterator
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastapi import Request
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

from cxplorer.auth.microsoft import (
    MICROSOFT_ISSUER_TEMPLATE,
    MICROSOFT_PERSONAL_TENANT_ID,
)
from cxplorer.config import AppSettings, IdentityVendorSettings
from cxplorer.main import create_app

TENANT_A = "11111111-2222-3333-4444-555555555555"
TENANT_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CLIENT_ID = "contoso-client-id"


def _authlib_http_backend() -> ModuleType:
    for backend in (httpx2, httpx):
        if issubclass(AsyncOAuth2Client, backend.AsyncClient):
            return backend
    raise AssertionError("Authlib HTTP backend was not identified")


AUTHLIB_HTTP_BACKEND = _authlib_http_backend()


@pytest.fixture(scope="module")
def signing_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": "contoso-signing-key"})


@pytest.fixture(scope="module")
def rotated_key() -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": "contoso-rotated-key"})


class MicrosoftProvider:
    """Serve discovery, token, and key responses without contacting Microsoft."""

    def __init__(self, key: RSAKey, tenant_mode: str = "common") -> None:
        self.key = key
        self.tenant_mode = tenant_mode
        self.tenant_id = TENANT_A
        self.metadata_issuer = (
            MICROSOFT_ISSUER_TEMPLATE
            if tenant_mode in {"common", "organizations", "consumers"}
            else MICROSOFT_ISSUER_TEMPLATE.format(tenantid=tenant_mode.lower())
        )
        self.key_issuer: object = MICROSOFT_ISSUER_TEMPLATE
        self.nonce = ""
        self.claim_overrides: dict[str, object] = {}
        self.omitted_claims: set[str] = set()
        self.header_overrides: dict[str, object] = {}
        self.omit_key_id = False
        self.corrupt_signature = False
        self.duplicate_key_id = False
        self.algorithm = "RS256"
        self.jwks_requests = 0
        self.token_requests = 0

    def _id_token(self) -> str:
        now = int(time.time())
        claims = {
            "iss": MICROSOFT_ISSUER_TEMPLATE.format(tenantid=self.tenant_id),
            "tid": self.tenant_id,
            "sub": f"contoso-seller-{self.tenant_id}",
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 600,
            "nonce": self.nonce,
            "name": "Contoso seller",
            "email": "seller@contoso.example",
            **self.claim_overrides,
        }
        for name in self.omitted_claims:
            claims.pop(name, None)
        header = {"alg": self.algorithm, "kid": self.key.kid, **self.header_overrides}
        if self.omit_key_id:
            header.pop("kid", None)
        key = OctKey.generate_key(256) if self.algorithm == "HS256" else self.key
        if self.omit_key_id and isinstance(key, RSAKey):
            key = RSAKey.import_key(
                {name: value for name, value in key.as_dict(private=True).items() if name != "kid"}
            )
        encoded = jwt.encode(header, claims, key)
        if self.corrupt_signature:
            header_part, payload_part, signature = encoded.split(".")
            replacement = "A" if signature[0] != "A" else "B"
            encoded = f"{header_part}.{payload_part}.{replacement}{signature[1:]}"
        return encoded

    def respond(self, request: httpx.Request | httpx2.Request) -> httpx.Response | httpx2.Response:
        assert request.url.host == "login.microsoftonline.com"
        authority = f"https://login.microsoftonline.com/{self.tenant_mode}"
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return AUTHLIB_HTTP_BACKEND.Response(
                200,
                json={
                    "issuer": self.metadata_issuer,
                    "authorization_endpoint": f"{authority}/oauth2/v2.0/authorize",
                    "token_endpoint": f"{authority}/oauth2/v2.0/token",
                    "jwks_uri": f"{authority}/discovery/v2.0/keys",
                    "id_token_signing_alg_values_supported": ["RS256"],
                    "token_endpoint_auth_methods_supported": [
                        "client_secret_basic",
                        "client_secret_post",
                    ],
                },
            )
        if request.url.path.endswith("/oauth2/v2.0/token"):
            assert request.method == "POST"
            self.token_requests += 1
            return AUTHLIB_HTTP_BACKEND.Response(
                200,
                json={
                    "access_token": "contoso-access-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "id_token": self._id_token(),
                },
            )
        if request.url.path.endswith("/discovery/v2.0/keys"):
            self.jwks_requests += 1
            key = dict(self.key.as_dict(private=False))
            if self.key_issuer is not None:
                key["issuer"] = self.key_issuer
            keys = [key]
            if self.duplicate_key_id:
                keys.append(
                    {
                        **key,
                        "issuer": MICROSOFT_ISSUER_TEMPLATE.format(tenantid=TENANT_B),
                    }
                )
            return AUTHLIB_HTTP_BACKEND.Response(200, json={"keys": keys})
        raise AssertionError(f"Unexpected provider request path: {request.url.path}")


def provider_client(app_settings: AppSettings, provider: MicrosoftProvider) -> TestClient:
    settings = IdentityVendorSettings(
        _env_file=None,
        ms_client_id=CLIENT_ID,
        ms_client_secret="contoso-client-secret",
        ms_tenant=provider.tenant_mode,
    )
    app = create_app(app_settings, settings)
    oauth_client = app.state.oauth.create_client("microsoft")
    oauth_client.client_kwargs["transport"] = AUTHLIB_HTTP_BACKEND.MockTransport(provider.respond)

    @app.get("/_test/session", include_in_schema=False)
    def session_contents(request: Request) -> dict[str, object]:
        return dict(request.session)

    return TestClient(app)


@pytest.fixture
def oidc_client(
    app_settings: AppSettings, signing_key: RSAKey
) -> Iterator[tuple[TestClient, MicrosoftProvider]]:
    provider = MicrosoftProvider(signing_key)
    with provider_client(app_settings, provider) as client:
        yield client, provider


def finish_sign_in(
    client: TestClient, provider: MicrosoftProvider
) -> httpx.Response | httpx2.Response:
    start = client.get("/auth/microsoft/login", follow_redirects=False)
    assert start.status_code == 302
    parameters = parse_qs(urlsplit(start.headers["location"]).query)
    provider.nonce = parameters["nonce"][0]
    return client.get(
        "/auth/microsoft/callback",
        params={"code": "contoso-authorization-code", "state": parameters["state"][0]},
        follow_redirects=False,
    )


def assert_sign_in_rejected(client: TestClient, response: httpx.Response | httpx2.Response) -> None:
    assert response.status_code == 303
    assert response.headers["location"].endswith("/login?error=authentication_failed")
    assert client.get("/api/private/me").status_code == 401
    assert client.get("/_test/session").json() == {}


@pytest.mark.parametrize(
    ("tenant_mode", "tenant_id"),
    [
        ("common", TENANT_A),
        ("common", MICROSOFT_PERSONAL_TENANT_ID),
        ("organizations", TENANT_A),
        ("consumers", MICROSOFT_PERSONAL_TENANT_ID),
        (TENANT_A, TENANT_A),
        (TENANT_B.upper(), TENANT_B),
    ],
)
def test_signed_tokens_match_the_configured_tenant_audience(
    app_settings: AppSettings, signing_key: RSAKey, tenant_mode: str, tenant_id: str
) -> None:
    provider = MicrosoftProvider(signing_key, tenant_mode)
    provider.tenant_id = tenant_id
    with provider_client(app_settings, provider) as client:
        callback = finish_sign_in(client, provider)
        current_user = client.get("/api/private/me")
        session = client.get("/_test/session").json()

    assert callback.status_code == 303
    assert callback.headers["location"] == "/dashboard"
    assert current_user.status_code == 200
    assert current_user.json()["display_name"] == "Contoso seller"
    assert current_user.json()["subject"] == f"contoso-seller-{tenant_id}"
    assert set(session) == {"csrf_token", "user"}
    assert set(session["user"]) == {"provider", "subject", "display_name", "email"}
    assert provider.token_requests == 1


@pytest.mark.parametrize(
    ("tenant_mode", "tenant_id"),
    [
        ("organizations", MICROSOFT_PERSONAL_TENANT_ID),
        ("consumers", TENANT_A),
        (TENANT_A, TENANT_B),
    ],
)
def test_configured_tenant_restrictions_cannot_be_bypassed(
    app_settings: AppSettings, signing_key: RSAKey, tenant_mode: str, tenant_id: str
) -> None:
    provider = MicrosoftProvider(signing_key, tenant_mode)
    provider.tenant_id = tenant_id
    with provider_client(app_settings, provider) as client:
        assert_sign_in_rejected(client, finish_sign_in(client, provider))


@pytest.mark.parametrize(
    ("claim", "value"),
    [
        ("iss", MICROSOFT_ISSUER_TEMPLATE),
        ("iss", MICROSOFT_ISSUER_TEMPLATE.format(tenantid=TENANT_B)),
        ("iss", f"https://contoso.example/{TENANT_A}/v2.0"),
        ("iss", f"http://login.microsoftonline.com/{TENANT_A}/v2.0"),
        ("iss", f"https://login.microsoftonline.com/{TENANT_A}/v2.0/"),
        ("tid", "not-a-guid"),
        ("tid", TENANT_A.replace("-", "")),
        ("tid", f"{{{TENANT_A}}}"),
        ("tid", 123),
        ("tid", None),
        ("aud", "another-client"),
        ("nonce", "another-nonce"),
        ("exp", 1),
    ],
)
def test_invalid_claims_return_a_sign_in_error(
    oidc_client: tuple[TestClient, MicrosoftProvider], claim: str, value: object
) -> None:
    client, provider = oidc_client
    provider.claim_overrides[claim] = value
    assert_sign_in_rejected(client, finish_sign_in(client, provider))


@pytest.mark.parametrize("claim", ["iss", "tid", "aud", "sub", "exp", "iat", "nonce"])
def test_required_claims_cannot_be_omitted(
    oidc_client: tuple[TestClient, MicrosoftProvider], claim: str
) -> None:
    client, provider = oidc_client
    provider.omitted_claims.add(claim)
    assert_sign_in_rejected(client, finish_sign_in(client, provider))


def test_authorized_party_does_not_replace_the_required_audience(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    provider.claim_overrides.update({"aud": "another-client", "azp": CLIENT_ID})
    assert_sign_in_rejected(client, finish_sign_in(client, provider))


@pytest.mark.parametrize(
    "key_issuer",
    [
        None,
        "",
        ["unexpected"],
        MICROSOFT_ISSUER_TEMPLATE.format(tenantid=TENANT_B),
        "https://contoso.example/{tenantid}/v2.0",
    ],
)
def test_shared_discovery_enforces_signing_key_issuer_scope(
    oidc_client: tuple[TestClient, MicrosoftProvider], key_issuer: object
) -> None:
    client, provider = oidc_client
    provider.key_issuer = key_issuer
    assert_sign_in_rejected(client, finish_sign_in(client, provider))


def test_a_tenant_scoped_signing_key_can_validate_its_own_issuer(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    provider.key_issuer = MICROSOFT_ISSUER_TEMPLATE.format(tenantid=TENANT_A)
    assert finish_sign_in(client, provider).headers["location"] == "/dashboard"


def test_tenant_specific_discovery_can_use_standard_keys_without_issuer_extensions(
    app_settings: AppSettings, signing_key: RSAKey
) -> None:
    provider = MicrosoftProvider(signing_key, TENANT_A)
    provider.key_issuer = None
    with provider_client(app_settings, provider) as client:
        assert finish_sign_in(client, provider).headers["location"] == "/dashboard"


def test_discovery_issuer_must_match_the_token_tenant(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    provider.metadata_issuer = MICROSOFT_ISSUER_TEMPLATE.format(tenantid=TENANT_B)
    assert_sign_in_rejected(client, finish_sign_in(client, provider))


def test_ambiguous_signing_key_ids_are_rejected(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    provider.duplicate_key_id = True
    assert_sign_in_rejected(client, finish_sign_in(client, provider))


def test_invalid_signatures_are_rejected(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    provider.corrupt_signature = True
    assert_sign_in_rejected(client, finish_sign_in(client, provider))


def test_unadvertised_signing_algorithms_are_rejected(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    provider.algorithm = "HS256"
    assert_sign_in_rejected(client, finish_sign_in(client, provider))


def test_unknown_key_ids_are_rejected_after_refresh(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    provider.header_overrides["kid"] = "contoso-unknown-key"
    assert_sign_in_rejected(client, finish_sign_in(client, provider))
    assert provider.jwks_requests == 2


def test_key_ids_are_required_even_with_a_single_signing_key(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    provider.omit_key_id = True
    assert_sign_in_rejected(client, finish_sign_in(client, provider))


def test_key_rotation_uses_the_refreshed_key_issuer_metadata(
    oidc_client: tuple[TestClient, MicrosoftProvider], rotated_key: RSAKey
) -> None:
    client, provider = oidc_client
    assert finish_sign_in(client, provider).headers["location"] == "/dashboard"
    provider.key = rotated_key
    assert finish_sign_in(client, provider).headers["location"] == "/dashboard"
    assert provider.jwks_requests == 2


def test_cached_discovery_is_not_bound_to_the_first_tenant(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    assert finish_sign_in(client, provider).headers["location"] == "/dashboard"
    provider.tenant_id = TENANT_B
    assert finish_sign_in(client, provider).headers["location"] == "/dashboard"
    assert client.get("/api/private/me").json()["subject"] == f"contoso-seller-{TENANT_B}"
    assert provider.jwks_requests == 1


def test_issuer_validation_is_not_consumed_by_the_first_callback(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    assert finish_sign_in(client, provider).headers["location"] == "/dashboard"
    provider.claim_overrides["iss"] = f"https://contoso.example/{TENANT_A}/v2.0"
    assert_sign_in_rejected(client, finish_sign_in(client, provider))


def test_invalid_state_does_not_redeem_an_authorization_code(
    oidc_client: tuple[TestClient, MicrosoftProvider],
) -> None:
    client, provider = oidc_client
    callback = client.get(
        "/auth/microsoft/callback",
        params={"code": "contoso-authorization-code", "state": "wrong-state"},
        follow_redirects=False,
    )
    assert_sign_in_rejected(client, callback)
    assert provider.token_requests == 0

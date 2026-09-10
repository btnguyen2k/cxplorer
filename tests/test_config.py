"""Configuration behavior tests."""

import re
from collections.abc import Mapping
from pathlib import Path

import pytest
from dotenv.parser import parse_stream
from fastapi.testclient import TestClient
from pydantic import ValidationError

from cxplorer.config import AppSettings, IdentityVendorSettings
from cxplorer.main import create_app
from tests.conftest import TEST_SESSION_SECRET

pytestmark = pytest.mark.usefixtures("config_directory")
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SENSITIVE_SETTING = re.compile(
    r"(?:^|_)(?:SECRET(?:_KEY)?|PASSWORD|PASSWD|PWD|TOKEN|API_?KEY|PRIVATE_KEY|"
    r"SIGNING_KEY|ENCRYPTION_KEY|ACCESS_KEY(?:_ID)?|CLIENT_ID|TENANT_ID|CREDENTIALS?)$"
)
PLACEHOLDER_VALUE = re.compile(
    r"""
    (?:
        \.\.\. | placeholder | todo | tbd |
        (?:change|replace)[_\s-]?me
            (?:[_\s-](?:with|before|for|in)[_\s-][a-z][a-z_\s-]*)? |
        (?:your|example|dummy|sample|placeholder|insert)[_\s-][a-z][a-z_\s-]* |
        <[a-z][a-z_\s-]*> |
        \$\{[a-z_][a-z0-9_]*(?::-)?\} |
        (?:remember\s+to|please|todo:)\s+
            (?:generate|set|replace|configure|provide|supply|specify|insert)\s+
            [a-z][a-z_\s-]*[.!]?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _unsafe_shared_keys(configuration: Mapping[str, str | None]) -> list[str]:
    """Report setting names only, keeping sensitive values out of failure output."""
    unsafe = []
    for name, value in configuration.items():
        normalized_name = re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_")
        if normalized_name != "MS_TENANT" and not SENSITIVE_SETTING.search(normalized_name):
            continue
        text = (value or "").strip()
        if normalized_name == "MS_TENANT" and text.casefold() in {
            "common",
            "organizations",
            "consumers",
        }:
            continue
        if text and not PLACEHOLDER_VALUE.fullmatch(text):
            unsafe.append(name)
    return unsafe


def _shared_file_policy_issues(path: Path) -> tuple[list[int], list[str]]:
    """Inspect every literal assignment without expanding references or hiding duplicates."""
    invalid_lines = []
    unsafe_keys = []
    with path.open(encoding="utf-8") as stream:
        for binding in parse_stream(stream):
            if binding.error:
                invalid_lines.append(binding.original.line)
            elif binding.key is not None:
                unsafe_keys.extend(_unsafe_shared_keys({binding.key: binding.value}))
    return invalid_lines, sorted(set(unsafe_keys))


@pytest.mark.parametrize(
    "filename", ["app_config.env", "id_vendor.env", "ai_vendors.env", "ai_tasks.env"]
)
def test_shared_sensitive_settings_are_empty_or_placeholders(filename: str) -> None:
    invalid_lines, unsafe_keys = _shared_file_policy_issues(REPOSITORY_ROOT / filename)
    assert not invalid_lines, f"{filename}: invalid dotenv syntax at lines {invalid_lines}"
    assert not unsafe_keys, (
        f"{filename}: sensitive settings must be empty or clear placeholders: "
        f"{', '.join(unsafe_keys)}"
    )


def test_shared_policy_accepts_arbitrary_non_sensitive_configuration() -> None:
    assert not _unsafe_shared_keys(
        {
            "APP_NAME": "CXplorer",
            "ENVIRONMENT": "production",
            "RELOAD": "true",
            "SESSION_COOKIE_SECURE": "false",
            "DOCS_ENABLED": "true",
            "ALLOWED_HOSTS": '["contoso.example"]',
            "LOGIN_ALLOWED_EMAILS": '["*@contoso.example"]',
            "NEW_FEATURE_ENABLED": "true",
            "API_TOKEN_TTL": "3600",
            "SESSION_MAX_AGE_SECONDS": "7200",
        }
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "remember to generate a secure secret for production",
        "Remember to configure your client ID for production.",
        "please replace this secret before production",
        "TODO: provide your API key",
        "your-api-key",
        "YOUR_CLIENT_SECRET",
        "<client-id>",
        "${CONTOSO_API_KEY}",
        "${CONTOSO_API_KEY:-}",
        "change-me",
        "replace_me",
        "change_me_before_production",
        "placeholder",
        "TODO",
        "...",
    ],
)
def test_shared_policy_accepts_empty_sensitive_values_and_clear_placeholders(
    value: str | None,
) -> None:
    assert not _unsafe_shared_keys({"SESSION_SECRET": value})


@pytest.mark.parametrize(
    "name",
    [
        "SESSION_SECRET",
        "MS_CLIENT_SECRET",
        "MS_CLIENT_ID",
        "MS_TENANT",
        "CONTOSO_API_KEY",
        "APIKEY",
        "DATABASE_PASSWORD",
        "ACCESS_TOKEN",
        "SECRET_KEY",
        "PRIVATE_KEY",
        "SIGNING_KEY",
        "ENCRYPTION_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "SERVICE_CREDENTIALS",
        "tenant_id",
    ],
)
def test_shared_policy_rejects_non_placeholder_sensitive_values(name: str) -> None:
    assert _unsafe_shared_keys({name: "contoso-sensitive-value-123456789"}) == [name]


@pytest.mark.parametrize(
    "value",
    [
        "secret",
        "sk_test_contoso_123456789",
        "contoso-live-value-with-placeholder-inside",
        "change-me-123456789",
        "your-api-key-123456789",
        "<contoso-live-key-123456789>",
        "${CONTOSO_API_KEY:-contoso-live-key-123456789}",
    ],
)
def test_placeholder_words_do_not_hide_sensitive_values(value: str) -> None:
    assert _unsafe_shared_keys({"CONTOSO_API_KEY": value}) == ["CONTOSO_API_KEY"]


@pytest.mark.parametrize("audience", ["common", "organizations", "consumers"])
def test_public_microsoft_audiences_are_not_sensitive(audience: str) -> None:
    assert not _unsafe_shared_keys({"MS_TENANT": audience})


def test_shared_policy_inspects_environment_references_without_expanding_them(
    config_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = config_directory / "app_config.env"
    path.write_text("SESSION_SECRET=${CONTOSO_SESSION_SECRET}\n", encoding="utf-8")
    monkeypatch.setenv("CONTOSO_SESSION_SECRET", TEST_SESSION_SECRET)

    assert _shared_file_policy_issues(path) == ([], [])


def test_duplicate_settings_cannot_hide_sensitive_values(config_directory: Path) -> None:
    path = config_directory / "app_config.env"
    path.write_text(
        "SESSION_SECRET=contoso-sensitive-value-123456789\n"
        "SESSION_SECRET=remember to generate a secure secret for production\n",
        encoding="utf-8",
    )

    assert _shared_file_policy_issues(path) == ([], ["SESSION_SECRET"])


def test_shared_policy_reports_invalid_syntax_without_values(config_directory: Path) -> None:
    path = config_directory / "app_config.env"
    path.write_text('SESSION_SECRET="unterminated\n', encoding="utf-8")

    assert _shared_file_policy_issues(path) == ([1], [])


def test_default_application_name_uses_official_branding() -> None:
    settings = AppSettings(
        _env_file=None,
        session_secret=TEST_SESSION_SECRET,
    )

    assert settings.app_name == "CXplorer"


@pytest.mark.parametrize("value", [None, "", "  ", [], ["", "  "]])
def test_empty_login_email_allowlist_allows_all(value: object) -> None:
    settings = AppSettings(
        _env_file=None,
        session_secret=TEST_SESSION_SECRET,
        login_allowed_emails=value,
    )

    assert settings.login_allowed_emails == ()
    assert settings.allows_login_email("seller@contoso.example")


@pytest.mark.parametrize(
    "configured",
    [
        '["Seller@Contoso.Example","*@partners.contoso.example"]',
        "Seller@Contoso.Example, *@partners.contoso.example",
    ],
)
def test_login_email_allowlist_matches_exact_addresses_and_whole_patterns(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    monkeypatch.setenv("LOGIN_ALLOWED_EMAILS", configured)
    settings = AppSettings(_env_file=None, session_secret=TEST_SESSION_SECRET)

    assert settings.login_allowed_emails == (
        "seller@contoso.example",
        "*@partners.contoso.example",
    )
    assert settings.allows_login_email("SELLER@contoso.example")
    assert settings.allows_login_email("anyone@partners.contoso.example")
    assert not settings.allows_login_email("seller@other.contoso.example")
    assert not settings.allows_login_email("seller@partners.contoso.example.evil")


@pytest.mark.parametrize(
    "configured",
    [
        '["not-an-email"]',
        '["*@invalid-domain"]',
        "[123]",
        '["*@contoso.example","*@CONTOSO.EXAMPLE"]',
    ],
)
def test_invalid_login_email_allowlist_fails_configuration(configured: str) -> None:
    with pytest.raises(ValidationError, match="LOGIN_ALLOWED_EMAILS"):
        AppSettings(
            _env_file=None,
            session_secret=TEST_SESSION_SECRET,
            login_allowed_emails=configured,
        )


@pytest.mark.parametrize(
    ("client_id", "client_secret"),
    [
        ("client-id", None),
        (None, "client-secret"),
        ("client-id", " "),
        (" ", "client-secret"),
    ],
)
def test_microsoft_credentials_must_be_configured_together(
    client_id: str | None, client_secret: str | None
) -> None:
    with pytest.raises(ValidationError, match="must be configured together"):
        IdentityVendorSettings(
            _env_file=None,
            ms_client_id=client_id,
            ms_client_secret=client_secret,
        )


def test_production_defaults_are_secure() -> None:
    settings = AppSettings(
        _env_file=None,
        environment="production",
        session_secret=TEST_SESSION_SECRET,
    )

    assert settings.use_secure_cookies is True
    assert settings.expose_api_docs is False


def test_ms_environment_variables_configure_microsoft_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MS_CLIENT_ID", "client-id")
    monkeypatch.setenv("MS_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("MS_TENANT", "organizations")

    settings = IdentityVendorSettings(_env_file=None)

    assert settings.ms_client_id == "client-id"
    assert settings.ms_client_secret
    assert settings.ms_client_secret.get_secret_value() == "client-secret"
    assert settings.ms_tenant == "organizations"
    assert settings.microsoft_auth_enabled is True


def test_settings_load_their_designated_files(config_directory: Path) -> None:
    (config_directory / "app_config.env").write_text(
        f"SESSION_SECRET={TEST_SESSION_SECRET}\n"
        "ENVIRONMENT=production\n"
        "RELOAD=true\n"
        "SESSION_MAX_AGE_SECONDS=3600\n"
        'ALLOWED_HOSTS=["testserver"]\n'
        'LOGIN_ALLOWED_EMAILS=["seller@contoso.example","*@partners.contoso.example"]\n',
        encoding="utf-8",
    )
    (config_directory / "id_vendor.env").write_text(
        "MS_CLIENT_ID=contoso-client\n"
        "MS_CLIENT_SECRET=test-client-secret\n"
        "MS_TENANT=organizations\n",
        encoding="utf-8",
    )

    application = AppSettings()
    identity = IdentityVendorSettings()

    assert application.session_secret.get_secret_value() == TEST_SESSION_SECRET
    assert application.environment == "production"
    assert application.reload is True
    assert application.session_max_age_seconds == 3600
    assert application.allowed_hosts == ["testserver"]
    assert application.login_allowed_emails == (
        "seller@contoso.example",
        "*@partners.contoso.example",
    )
    assert application.use_secure_cookies is True
    assert application.expose_api_docs is False
    assert identity.ms_client_id == "contoso-client"
    assert identity.ms_client_secret
    assert identity.ms_client_secret.get_secret_value() == "test-client-secret"
    assert identity.ms_tenant == "organizations"
    assert identity.microsoft_auth_enabled is True
    assert not AppSettings.model_fields.keys() & IdentityVendorSettings.model_fields.keys()


def test_local_settings_override_shared_defaults(config_directory: Path) -> None:
    (config_directory / "app_config.env").write_text(
        "RELOAD=false\nSESSION_MAX_AGE_SECONDS=3600\n", encoding="utf-8"
    )
    (config_directory / "app_config.local.env").write_text(
        f"SESSION_SECRET={TEST_SESSION_SECRET}\nRELOAD=true\n", encoding="utf-8"
    )
    (config_directory / "id_vendor.env").write_text("MS_TENANT=common\n", encoding="utf-8")
    (config_directory / "id_vendor.local.env").write_text(
        "MS_CLIENT_ID=contoso-client\n"
        "MS_CLIENT_SECRET=local-client-secret\n"
        "MS_TENANT=organizations\n",
        encoding="utf-8",
    )

    application = AppSettings()
    identity = IdentityVendorSettings()

    assert application.session_secret.get_secret_value() == TEST_SESSION_SECRET
    assert application.reload is True
    assert application.session_max_age_seconds == 3600
    assert identity.ms_client_id == "contoso-client"
    assert identity.ms_client_secret
    assert identity.ms_client_secret.get_secret_value() == "local-client-secret"
    assert identity.ms_tenant == "organizations"
    assert identity.microsoft_auth_enabled is True


def test_settings_do_not_read_the_other_file(config_directory: Path) -> None:
    (config_directory / "app_config.env").write_text(
        f"SESSION_SECRET={TEST_SESSION_SECRET}\n"
        "MS_CLIENT_ID=wrong-file-client\n"
        "MS_CLIENT_SECRET=wrong-file-secret\n",
        encoding="utf-8",
    )
    (config_directory / "id_vendor.env").write_text(
        "SESSION_SECRET=wrong-file-secret\nRELOAD=true\nENVIRONMENT=production\n",
        encoding="utf-8",
    )
    (config_directory / "app_config.local.env").write_text(
        "MS_CLIENT_ID=wrong-local-client\nMS_CLIENT_SECRET=wrong-local-secret\n",
        encoding="utf-8",
    )
    (config_directory / "id_vendor.local.env").write_text(
        "SESSION_SECRET=wrong-local-secret\nRELOAD=true\n", encoding="utf-8"
    )

    application = AppSettings()
    identity = IdentityVendorSettings()

    assert application.session_secret.get_secret_value() == TEST_SESSION_SECRET
    assert application.environment == "development"
    assert application.reload is False
    assert identity.microsoft_auth_enabled is False
    assert identity.ms_client_id is None
    assert identity.ms_client_secret is None


def test_environment_variables_override_each_file(
    config_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (config_directory / "app_config.env").write_text(
        f"SESSION_SECRET={TEST_SESSION_SECRET}\nRELOAD=true\nENVIRONMENT=production\n",
        encoding="utf-8",
    )
    (config_directory / "id_vendor.env").write_text(
        "MS_CLIENT_ID=file-client\nMS_CLIENT_SECRET=file-secret\nMS_TENANT=common\n",
        encoding="utf-8",
    )
    (config_directory / "app_config.local.env").write_text(
        "RELOAD=true\nENVIRONMENT=development\n", encoding="utf-8"
    )
    (config_directory / "id_vendor.local.env").write_text(
        "MS_CLIENT_ID=local-client\nMS_CLIENT_SECRET=local-secret\nMS_TENANT=consumers\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RELOAD", "false")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("MS_CLIENT_ID", "environment-client")
    monkeypatch.setenv("MS_CLIENT_SECRET", "environment-secret")
    monkeypatch.setenv("MS_TENANT", "organizations")

    application = AppSettings()
    identity = IdentityVendorSettings()

    assert application.reload is False
    assert application.environment == "test"
    assert identity.ms_client_id == "environment-client"
    assert identity.ms_client_secret
    assert identity.ms_client_secret.get_secret_value() == "environment-secret"
    assert identity.ms_tenant == "organizations"


def test_legacy_env_file_is_not_loaded(config_directory: Path) -> None:
    (config_directory / ".env").write_text(
        f"SESSION_SECRET={TEST_SESSION_SECRET}\n"
        "MS_CLIENT_ID=legacy-client\nMS_CLIENT_SECRET=legacy-secret\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="session_secret"):
        AppSettings()
    assert IdentityVendorSettings().microsoft_auth_enabled is False


def test_file_loading_can_be_disabled(config_directory: Path) -> None:
    (config_directory / "app_config.env").write_text(
        "SESSION_SECRET=ignored\nRELOAD=true\n", encoding="utf-8"
    )
    (config_directory / "id_vendor.env").write_text(
        "MS_CLIENT_ID=ignored-client\nMS_CLIENT_SECRET=ignored-secret\n", encoding="utf-8"
    )
    (config_directory / "app_config.local.env").write_text(
        "SESSION_SECRET=ignored-local-secret\nRELOAD=true\n", encoding="utf-8"
    )
    (config_directory / "id_vendor.local.env").write_text(
        "MS_CLIENT_ID=ignored-local-client\nMS_CLIENT_SECRET=ignored-local-secret\n",
        encoding="utf-8",
    )

    application = AppSettings(_env_file=None, session_secret=TEST_SESSION_SECRET)
    identity = IdentityVendorSettings(_env_file=None)

    assert application.reload is False
    assert application.session_secret.get_secret_value() == TEST_SESSION_SECRET
    assert identity.microsoft_auth_enabled is False


def test_identity_configuration_is_optional_and_independent() -> None:
    settings = IdentityVendorSettings()

    assert settings.microsoft_auth_enabled is False
    assert settings.ms_tenant == "common"
    assert not hasattr(settings, "session_secret")


def test_empty_vendor_credentials_disable_sign_in(config_directory: Path) -> None:
    (config_directory / "id_vendor.env").write_text(
        "MS_CLIENT_ID=\nMS_CLIENT_SECRET=\nMS_TENANT=common\n", encoding="utf-8"
    )

    assert IdentityVendorSettings().microsoft_auth_enabled is False


def test_configuration_errors_do_not_print_secret_values() -> None:
    with pytest.raises(ValidationError) as application_error:
        AppSettings(_env_file=None, session_secret="short-secret")
    with pytest.raises(ValidationError) as identity_error:
        IdentityVendorSettings(_env_file=None, ms_client_secret="unpaired-client-secret")

    assert "session_secret" in str(application_error.value)
    assert "short-secret" not in str(application_error.value)
    assert "must be configured together" in str(identity_error.value)
    assert "unpaired-client-secret" not in str(identity_error.value)


def test_factory_loads_separate_settings_by_default(config_directory: Path) -> None:
    (config_directory / "app_config.env").write_text(
        'ENVIRONMENT=test\nALLOWED_HOSTS=["testserver"]\n',
        encoding="utf-8",
    )
    (config_directory / "app_config.local.env").write_text(
        f"SESSION_SECRET={TEST_SESSION_SECRET}\n", encoding="utf-8"
    )
    (config_directory / "id_vendor.env").write_text("MS_TENANT=common\n", encoding="utf-8")
    (config_directory / "id_vendor.local.env").write_text(
        "MS_CLIENT_ID=contoso-client\nMS_CLIENT_SECRET=test-client-secret\n",
        encoding="utf-8",
    )
    app = create_app()

    assert isinstance(app.state.app_settings, AppSettings)
    assert isinstance(app.state.identity_settings, IdentityVendorSettings)
    assert app.state.oauth.create_client("microsoft") is not None
    with TestClient(app) as client:
        login = client.get("/login")
        health = client.get("/api/health")

    assert login.status_code == 200
    assert "Continue with Microsoft Entra ID" in login.text
    assert TEST_SESSION_SECRET not in login.text
    assert "test-client-secret" not in login.text
    assert health.status_code == 200


def test_factory_keeps_explicit_settings_instances(
    app_settings: AppSettings, identity_settings: IdentityVendorSettings
) -> None:
    app = create_app(app_settings, identity_settings)

    assert app.state.app_settings is app_settings
    assert app.state.identity_settings is identity_settings
    assert app.state.oauth.create_client("microsoft") is None

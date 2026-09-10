"""Root server launcher tests."""

from pathlib import Path

import pytest
import server

from tests.conftest import TEST_APP_NAME, TEST_SESSION_SECRET

pytestmark = pytest.mark.usefixtures("config_directory")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("true", True),
        ("false", False),
    ],
)
def test_launcher_settings_parse_reload(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("RELOAD", value)

    settings = server.AppSettings(
        _env_file=None,
        app_name=TEST_APP_NAME,
        session_secret=TEST_SESSION_SECRET,
    )

    assert settings.reload is expected


@pytest.mark.parametrize(
    ("file_value", "local_value", "environment_value", "expected"),
    [
        ("true", None, None, True),
        ("false", None, None, False),
        ("false", None, "true", True),
        ("true", None, "false", False),
        ("false", "true", None, True),
        ("true", "false", None, False),
        ("false", "true", "false", False),
    ],
)
def test_main_loads_app_config_and_passes_reload_to_uvicorn(
    config_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_value: str,
    local_value: str | None,
    environment_value: str | None,
    expected: bool,
) -> None:
    call: dict[str, object] = {}
    (config_directory / "app_config.env").write_text(
        f"APP_NAME={TEST_APP_NAME}\nRELOAD={file_value}\n",
        encoding="utf-8",
    )
    local_configuration = f"SESSION_SECRET={TEST_SESSION_SECRET}\n"
    if local_value is not None:
        local_configuration += f"RELOAD={local_value}\n"
    (config_directory / "app_config.local.env").write_text(local_configuration, encoding="utf-8")
    (config_directory / "id_vendor.local.env").write_text(
        "MS_CLIENT_ID=unpaired-provider\n", encoding="utf-8"
    )

    def fake_run(app: str, **kwargs: object) -> None:
        call["app"] = app
        call.update(kwargs)

    if environment_value is not None:
        monkeypatch.setenv("RELOAD", environment_value)
    monkeypatch.setattr(server.uvicorn, "run", fake_run)

    server.main()

    assert call == {
        "app": "cxplorer.main:create_app",
        "host": "127.0.0.1",
        "port": 8000,
        "factory": True,
        "reload": expected,
    }

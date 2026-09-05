"""Root server launcher tests."""

import pytest
import server


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

    settings = server.LauncherSettings(_env_file=None)

    assert settings.reload is expected


def test_main_passes_reload_to_uvicorn(monkeypatch: pytest.MonkeyPatch) -> None:
    call: dict[str, object] = {}

    def fake_run(app: str, **kwargs: object) -> None:
        call["app"] = app
        call.update(kwargs)

    monkeypatch.setenv("RELOAD", "true")
    monkeypatch.setattr(server.uvicorn, "run", fake_run)

    server.main()

    assert call == {
        "app": "cxplorer.main:create_app",
        "host": "127.0.0.1",
        "port": 8000,
        "factory": True,
        "reload": True,
    }

"""Root server launcher tests."""

from pathlib import Path

import pytest
import server

from tests.conftest import TEST_APP_NAME, TEST_SESSION_SECRET

pytestmark = pytest.mark.usefixtures("config_directory")


def test_main_loads_app_config_and_passes_reload_to_uvicorn(
    config_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call: dict[str, object] = {}
    (config_directory / "app_config.env").write_text(
        f"APP_NAME={TEST_APP_NAME}\nRELOAD=false\n",
        encoding="utf-8",
    )
    (config_directory / "app_config.local.env").write_text(
        f"SESSION_SECRET={TEST_SESSION_SECRET}\nRELOAD=true\n",
        encoding="utf-8",
    )
    (config_directory / "id_vendor.local.env").write_text(
        "MS_CLIENT_ID=unpaired-provider\n", encoding="utf-8"
    )

    def fake_run(app: str, **kwargs: object) -> None:
        call["app"] = app
        call.update(kwargs)

    monkeypatch.setattr(server.uvicorn, "run", fake_run)

    server.main()

    assert call == {
        "app": "cxplorer.main:create_app",
        "host": "127.0.0.1",
        "port": 8000,
        "factory": True,
        "reload": True,
    }

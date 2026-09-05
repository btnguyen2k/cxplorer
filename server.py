"""Development server launcher."""

import uvicorn
from pydantic_settings import BaseSettings, SettingsConfigDict


class LauncherSettings(BaseSettings):
    """Settings used before the FastAPI application is imported."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    reload: bool = False


def main() -> None:
    """Launch CXplorer with optional code reloading."""
    settings = LauncherSettings()
    uvicorn.run(
        "cxplorer.main:create_app",
        host="127.0.0.1",
        port=8000,
        factory=True,
        reload=settings.reload,
    )


if __name__ == "__main__":
    main()

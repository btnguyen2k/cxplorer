"""Development server launcher."""

import uvicorn

from cxplorer.config import AppSettings


def main() -> None:
    """Launch CXplorer with optional code reloading."""
    settings = AppSettings()
    uvicorn.run(
        "cxplorer.main:create_app",
        host="127.0.0.1",
        port=8000,
        factory=True,
        reload=settings.reload,
    )


if __name__ == "__main__":
    main()

"""Shared Jinja template configuration."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from cxplorer.config import AppSettings


def common_template_context(request: Request) -> dict[str, object]:
    """Add stable application metadata to every template."""
    app_settings: AppSettings = request.app.state.app_settings
    return {
        "app_name": app_settings.app_name,
        "current_year": datetime.now(tz=UTC).year,
    }


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates",
    context_processors=[common_template_context],
)

"""FastAPI application factory."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cxplorer.auth.oauth import build_oauth
from cxplorer.config import Settings
from cxplorer.middleware import SecurityHeadersMiddleware
from cxplorer.routers import authentication, private, public

STATIC_DIRECTORY = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create a configured CXplorer application."""
    settings = settings or Settings()
    docs_url = "/api/docs" if settings.expose_api_docs else None
    openapi_url = "/api/openapi.json" if settings.expose_api_docs else None

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
    )
    app.state.settings = settings
    app.state.oauth = build_oauth(settings)

    # noinspection PyTypeChecker
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=settings.environment == "production",
    )

    # noinspection PyTypeChecker
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret.get_secret_value(),
        session_cookie="cxplorer_session",
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=settings.use_secure_cookies,
    )

    # noinspection PyTypeChecker
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    app.mount(
        "/static",
        StaticFiles(directory=STATIC_DIRECTORY),
        name="static",
    )
    app.include_router(public.router)
    app.include_router(authentication.router)
    app.include_router(private.web_router)
    app.include_router(private.api_router)
    return app

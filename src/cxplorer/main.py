"""FastAPI application factory."""

import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cxplorer import __version__
from cxplorer.auth.oauth import build_oauth
from cxplorer.config import AppSettings, IdentityVendorSettings
from cxplorer.insights.cache import ReportCache
from cxplorer.insights.errors import InsightError
from cxplorer.insights.jobs import JobManager, Progress, Runner
from cxplorer.insights.schemas import AUDIENCE_LABELS, AcceptedReport, InsightRequest
from cxplorer.middleware import InsightsBodyLimitMiddleware, SecurityHeadersMiddleware
from cxplorer.routers import authentication, private, public

STATIC_DIRECTORY = Path(__file__).parent / "static"
logger = logging.getLogger(__name__)


def create_app(
    app_settings: AppSettings | None = None,
    identity_settings: IdentityVendorSettings | None = None,
    *,
    insights_runner: Runner | None = None,
) -> FastAPI:
    """Create a configured CXplorer application."""
    app_settings = app_settings if app_settings is not None else AppSettings()
    identity_settings = (
        identity_settings if identity_settings is not None else IdentityVendorSettings()
    )
    docs_url = "/api/docs" if app_settings.expose_api_docs else None
    openapi_url = "/api/openapi.json" if app_settings.expose_api_docs else None

    async def unavailable(
        _report_id: str, _request: InsightRequest, _progress: Progress
    ) -> AcceptedReport:
        raise InsightError("ai_disabled", "Insights generation is not enabled.")

    manager = JobManager(insights_runner or unavailable)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info("CXplorer startup before: environment=%s", app_settings.environment)
        async with AsyncExitStack() as cleanup:
            from cxplorer.ai.config import load_ai_settings

            ai_settings = load_ai_settings(
                app_settings.ai_vendor_config_file,
                app_settings.ai_task_config_file,
            )
            limits = ai_settings.pipeline
            _app.state.ai_settings = ai_settings
            _app.state.ai_enabled = ai_settings.enabled
            _app.state.insights_config_fingerprint = ai_settings.fingerprint
            _app.state.insights_default_audiences = list(limits.default_audiences)
            _app.state.insights_max_seed_urls = limits.max_seed_urls
            active_manager = manager
            enabled_vendors = sorted(
                name for name, vendor in ai_settings.vendors.items() if vendor.enabled
            )
            if not _app.state.ai_enabled:
                logger.warning(
                    "Insights generation is disabled: no AI vendor is configured. "
                    "OpenAI requires CX_AI__OPENAI__API_KEY. AzureOpenAI requires "
                    "CX_AI__AZURE_OPENAI__ENDPOINT and either CX_AI__AZURE_OPENAI__API_KEY "
                    "or all of AZURE_TENANT_ID, AZURE_CLIENT_ID, and AZURE_CLIENT_SECRET "
                    "in the process environment. "
                    "Browser drafts and saved-report restoration remain available."
                )
            elif insights_runner is None:
                from cxplorer.ai.providers import AIProviderClient
                from cxplorer.insights.pipeline import InsightsPipeline
                from cxplorer.insights.sources import FetchLimits, SourceFetcher

                secret_text = app_settings.session_secret.get_secret_value().strip()
                placeholder = secret_text.strip("<>").casefold()
                if app_settings.environment != "test" and placeholder.startswith(
                    (
                        "remember ",
                        "please ",
                        "replace",
                        "change_me",
                        "changeme",
                        "your_",
                        "your-",
                        "placeholder",
                        "todo",
                        "test-",
                    )
                ):
                    raise ValueError(
                        "Configured AI generation requires a real SESSION_SECRET supplied "
                        "privately, not a placeholder."
                    )
                provider = AIProviderClient(ai_settings)
                cleanup.push_async_callback(provider.close)
                fetcher = SourceFetcher(
                    FetchLimits(
                        request_timeout=limits.fetch_timeout_seconds,
                        max_concurrent_fetches=limits.fetch_concurrency,
                        max_redirects=limits.max_redirects,
                        max_html_bytes=limits.max_html_bytes,
                        max_pdf_bytes=limits.max_pdf_bytes,
                        max_pdf_pages=limits.max_pdf_pages,
                    )
                )
                cleanup.push_async_callback(fetcher.close)
                pipeline = InsightsPipeline(ai_settings, provider, fetcher)
                cleanup.push_async_callback(pipeline.close)
                active_manager = JobManager(
                    pipeline.run,
                    max_running=limits.max_running_jobs,
                    max_queued=limits.max_queued_jobs,
                    max_per_owner=limits.max_active_jobs_per_user,
                    timeout_seconds=limits.job_timeout_seconds,
                    retention_seconds=limits.result_ttl_seconds,
                    retained_limit=limits.max_results,
                    forget=pipeline.forget,
                    can_retry=pipeline.can_retry,
                )
                _app.state.insights_pipeline = pipeline
                _app.state.insights_jobs = active_manager
            cleanup.push_async_callback(active_manager.close)
            logger.info(
                "CXplorer startup after: insights_enabled=%s vendors=%s "
                "max_running_jobs=%d max_queued_jobs=%d job_timeout_seconds=%.3f",
                str(_app.state.ai_enabled).lower(),
                ",".join(enabled_vendors) or "none",
                limits.max_running_jobs,
                limits.max_queued_jobs,
                limits.job_timeout_seconds,
            )
            yield

    app = FastAPI(
        title=app_settings.app_name,
        version=__version__,
        docs_url=docs_url,
        redoc_url=None,
        openapi_url=openapi_url,
        lifespan=lifespan,
    )
    app.state.app_settings = app_settings
    app.state.identity_settings = identity_settings
    app.state.oauth = build_oauth(identity_settings)
    app.state.ai_enabled = False
    app.state.insights_jobs = manager
    app.state.insights_config_fingerprint = "disabled"
    app.state.insights_default_audiences = list(AUDIENCE_LABELS)
    app.state.insights_max_seed_urls = 6
    app.state.report_cache = ReportCache(app_settings.session_secret.get_secret_value())

    app.add_middleware(InsightsBodyLimitMiddleware)
    # noinspection PyTypeChecker
    app.add_middleware(
        SecurityHeadersMiddleware,
        enable_hsts=app_settings.environment == "production",
    )

    # noinspection PyTypeChecker
    app.add_middleware(
        SessionMiddleware,
        secret_key=app_settings.session_secret.get_secret_value(),
        session_cookie="cxplorer_session",
        max_age=app_settings.session_max_age_seconds,
        same_site="lax",
        https_only=app_settings.use_secure_cookies,
    )

    # noinspection PyTypeChecker
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=app_settings.allowed_hosts)

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

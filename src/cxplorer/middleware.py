"""Small ASGI middleware used by the application."""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class SecurityHeadersMiddleware:
    """Attach browser security headers without buffering response bodies."""

    def __init__(self, app: ASGIApp, *, enable_hsts: bool = False) -> None:
        self.app = app
        self.enable_hsts = enable_hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                path = str(scope.get("path", ""))
                content_security_policy = (
                    "default-src 'self'; base-uri 'self'; form-action 'self'; "
                    "frame-ancestors 'none'; img-src 'self' data:; object-src 'none'; "
                    "script-src 'self'; style-src 'self'"
                )
                if path.startswith("/api/docs"):
                    content_security_policy = (
                        "default-src 'self'; base-uri 'self'; form-action 'self'; "
                        "frame-ancestors 'none'; img-src 'self' data: "
                        "https://fastapi.tiangolo.com; object-src 'none'; "
                        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net"
                    )
                headers["Content-Security-Policy"] = content_security_policy
                headers["Permissions-Policy"] = (
                    "camera=(), geolocation=(), microphone=(), payment=(), usb=()"
                )
                headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
                headers["X-Content-Type-Options"] = "nosniff"
                headers["X-Frame-Options"] = "DENY"

                if path == "/dashboard" or path.startswith("/api/private/"):
                    headers["Cache-Control"] = "no-store"
                if self.enable_hsts:
                    headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            await send(message)

        await self.app(scope, receive, send_with_security_headers)

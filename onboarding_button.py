from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware


def _install_voice_routes() -> None:
    import telegram_routes
    import voice_webhooks
    import whatsapp_routes

    if getattr(telegram_routes, "_voice_routes_installed", False):
        return

    for route in voice_webhooks.router.routes:
        if route.path == "/webhooks/telegram":
            telegram_routes.router.routes.insert(0, route)
        elif route.path == "/whatsapp/webhook":
            route.path = "/webhook"
            whatsapp_routes.router.routes.insert(0, route)
        elif route.path == "/voice/health":
            telegram_routes.router.routes.insert(0, route)

    telegram_routes._voice_routes_installed = True
    whatsapp_routes._voice_routes_installed = True


_install_voice_routes()


class OnboardingButtonMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path.rstrip("/") or "/"
        if path != "/admin":
            return response

        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        html = body.decode("utf-8", errors="replace")

        href = "/admin/onboarding"
        if href not in html:
            button = (
                '<a class="navbtn primary" href="/admin/onboarding">'
                'Onboard New User</a>'
            )
            marker = '<a class="navbtn" href="/admin/railway">'
            if marker in html:
                html = html.replace(marker, button + marker, 1)
            else:
                html = html.replace(
                    '<div id="live" class="live">',
                    button + '<div id="live" class="live">',
                    1,
                )

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=html,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html; charset=utf-8",
        )

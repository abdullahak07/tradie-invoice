from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware


def _install_runtime_features() -> None:
    import billing_plan_runtime

    billing_plan_runtime.install()

    import ai_data_guardrails
    import plan_enforcement
    import telegram_routes
    import voice_confirm_routes
    import voice_webhooks
    import whatsapp_routes

    ai_data_guardrails.install_guardrails()
    plan_enforcement.install_plan_enforcement()

    if getattr(telegram_routes, "_voice_routes_installed", False):
        return

    telegram_routes.router.add_api_route(
        "/webhooks/telegram",
        voice_confirm_routes.telegram_webhook,
        methods=["POST"],
    )
    telegram_route = telegram_routes.router.routes.pop()
    telegram_routes.router.routes.insert(0, telegram_route)

    whatsapp_routes.router.add_api_route(
        "/webhook",
        voice_confirm_routes.whatsapp_webhook,
        methods=["POST"],
    )
    whatsapp_route = whatsapp_routes.router.routes.pop()
    whatsapp_routes.router.routes.insert(0, whatsapp_route)

    telegram_routes.router.add_api_route(
        "/voice/health",
        voice_webhooks.voice_health,
        methods=["GET"],
    )
    health_route = telegram_routes.router.routes.pop()
    telegram_routes.router.routes.insert(0, health_route)

    telegram_routes._voice_routes_installed = True
    whatsapp_routes._voice_routes_installed = True


_install_runtime_features()


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
                "Onboard New User</a>"
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

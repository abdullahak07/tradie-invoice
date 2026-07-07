from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware


def install_runtime_features() -> None:
    import billing_plan_runtime
    import ai_data_guardrails
    import admin_source_files
    import admin_tester_debug
    import clarification_fix
    import plan_enforcement
    import profile_choice_runtime
    import telegram_routes
    import voice_confirm_routes
    import voice_webhooks
    import whatsapp_routes

    billing_plan_runtime.install()
    clarification_fix.install_clarification_fix()
    profile_choice_runtime.install()
    ai_data_guardrails.install_guardrails()
    plan_enforcement.install_plan_enforcement()

    if not getattr(telegram_routes, "_tester_debug_installed", False):
        telegram_routes.router.include_router(admin_tester_debug.router)
        telegram_routes.router.include_router(admin_source_files.router)
        telegram_routes._tester_debug_installed = True

    if not getattr(telegram_routes, "_voice_routes_installed", False):
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


install_runtime_features()


class OnboardingButtonMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        path = request.url.path.rstrip("/") or "/"
        if path != "/admin":
            return response

        if "text/html" not in response.headers.get("content-type", ""):
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        page = body.decode("utf-8", errors="replace")

        buttons = [
            ("/admin/onboarding", "Onboard New User", "navbtn primary"),
            ("/admin/tester-debug", "Tester Debug", "navbtn"),
        ]
        marker = '<a class="navbtn" href="/admin/railway">'
        for href, label, css in buttons:
            if href in page:
                continue
            button = f'<a class="{css}" href="{href}">{label}</a>'
            if marker in page:
                page = page.replace(marker, button + marker, 1)

        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(
            content=page,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html; charset=utf-8",
        )

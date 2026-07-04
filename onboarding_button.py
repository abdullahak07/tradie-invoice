from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware


def _logo_summary(data: dict, logo_path: str) -> str:
    def line(label: str, key: str) -> str:
        value = str(data.get(key) or "").strip() or "Not found"
        return f"{label}: {value}"

    if logo_path:
        logo_line = "Logo: Ready to use"
    elif bool(data.get("logo_visible")):
        logo_line = "Logo: Detected, but not clean enough to use"
    else:
        logo_line = "Logo: Not found"

    return "\n".join(
        [
            "I found these business details:",
            "",
            line("Business", "business_name"),
            line("Owner", "owner_name"),
            line("Trade", "trade_type"),
            line("ABN", "abn"),
            line("Licence", "licence_number"),
            line("Phone", "phone"),
            line("Email", "email"),
            line("Address", "address"),
            line("Account name", "bank_account_name"),
            line("BSB", "bank_bsb"),
            line("Account number", "bank_account_number"),
            logo_line,
            "",
            "Please confirm these are your business details, not your customer’s or supplier’s details.",
            "Reply LOGO to upload a clear standalone PNG/JPG logo, CONFIRM to continue without a logo, EDIT to correct details, or REUPLOAD to send another business document.",
        ]
    )


def _install_runtime_features() -> None:
    import billing_plan_runtime

    billing_plan_runtime.install()

    import ai_data_guardrails
    import logo_onboarding
    import plan_enforcement
    import self_onboarding
    import telegram_routes
    import voice_confirm_routes
    import voice_webhooks
    import whatsapp_routes

    ai_data_guardrails.install_guardrails()
    plan_enforcement.install_plan_enforcement()

    self_onboarding.summary_text = _logo_summary
    voice_confirm_routes.whatsapp_webhook = logo_onboarding.whatsapp_webhook

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

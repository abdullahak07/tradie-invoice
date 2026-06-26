from __future__ import annotations

from fastapi import Request
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware


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

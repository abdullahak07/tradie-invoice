from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


def verify_token() -> str:
    token = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    if not token:
        raise RuntimeError("WHATSAPP_VERIFY_TOKEN is not configured")
    return token


@router.get("/webhook", response_class=PlainTextResponse)
async def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> PlainTextResponse:
    if hub_mode != "subscribe":
        raise HTTPException(status_code=400, detail="Invalid hub.mode")
    if hub_verify_token != verify_token():
        raise HTTPException(status_code=403, detail="Invalid verify token")
    if hub_challenge is None:
        raise HTTPException(status_code=400, detail="Missing hub.challenge")
    return PlainTextResponse(hub_challenge)


@router.post("/webhook")
async def receive_webhook(request: Request) -> dict[str, bool]:
    payload: dict[str, Any] = await request.json()
    if payload.get("object") != "whatsapp_business_account":
        return {"ok": True}
    return {"ok": True}


@router.get("/health")
def whatsapp_health() -> dict[str, Any]:
    return {
        "ok": True,
        "verify_token_configured": bool(os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()),
        "phone_number_id_configured": bool(os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()),
        "access_token_configured": bool(os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()),
        "api_version": os.getenv("WHATSAPP_API_VERSION", "v25.0"),
    }

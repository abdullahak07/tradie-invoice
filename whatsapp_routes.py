from __future__ import annotations

import os

import httpx
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])


def access_token() -> str:
    token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    if not token:
        raise RuntimeError("WHATSAPP_ACCESS_TOKEN is not configured")
    return token


def phone_number_id() -> str:
    value = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    if not value:
        raise RuntimeError("WHATSAPP_PHONE_NUMBER_ID is not configured")
    return value


def api_version() -> str:
    return os.getenv("WHATSAPP_API_VERSION", "v25.0").strip() or "v25.0"


async def send_whatsapp_text(to: str, body: str) -> None:
    url = (
        f"https://graph.facebook.com/{api_version()}/"
        f"{phone_number_id()}/messages"
    )
    headers = {
        "Authorization": f"Bearer {access_token()}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": body[:4096],
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(url, headers=headers, json=payload)

    if response.status_code >= 400:
        raise RuntimeError(
            f"WhatsApp send failed ({response.status_code}): "
            f"{response.text[:500]}"
        )


def extract_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            for message in value.get("messages", []) or []:
                messages.append(message)

    return messages


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

    for message in extract_messages(payload):
        sender = str(message.get("from", "")).strip()
        message_type = str(message.get("type", "")).strip()

        if not sender:
            continue

        if message_type == "text":
            incoming_text = str(
                (message.get("text") or {}).get("body", "")
            ).strip()

            await send_whatsapp_text(
                sender,
                (
                    "✅ WhatsApp is connected to Tradie Invoice.\n\n"
                    f"Received: {incoming_text or '[empty message]'}\n\n"
                    "Next, we will connect invoice and quote creation."
                ),
            )
        else:
            await send_whatsapp_text(
                sender,
                "WhatsApp is connected. For now, please send a text message.",
            )

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

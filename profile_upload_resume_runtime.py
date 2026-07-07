from __future__ import annotations

import json

from fastapi import Request

import self_onboarding as so
import voice_confirm_routes
import whatsapp_routes

_INSTALLED = False


def _fresh_request(request: Request, payload: dict) -> Request:
    body = json.dumps(payload).encode("utf-8")
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(request.scope, receive)


def _session_data(session) -> dict:
    try:
        return json.loads(str(session["extracted_json"] or "{}"))
    except Exception:
        return {}


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original = voice_confirm_routes.whatsapp_webhook

    async def wrapped(request: Request):
        payload = await request.json()
        messages = whatsapp_routes.extract_messages(payload)
        if not messages:
            return await original(_fresh_request(request, payload))

        message = messages[0]
        sender = str(message.get("from") or "").strip()
        session = so.get_session("whatsapp", sender)
        pending_text = ""

        if session and str(session["state"]) == "awaiting_upload":
            pending_text = str(
                _session_data(session).get("pending_text") or ""
            ).strip()

        result = await original(_fresh_request(request, payload))

        if pending_text and str(message.get("type") or "") in {
            "image",
            "document",
        }:
            updated_session = so.get_session("whatsapp", sender)
            if updated_session and str(updated_session["state"]) == "awaiting_confirmation":
                data = _session_data(updated_session)
                data["pending_text"] = pending_text
                so.save_session(
                    "whatsapp",
                    sender,
                    str(updated_session["user_id"]),
                    "awaiting_confirmation",
                    source_path=str(updated_session["source_path"] or ""),
                    logo_path=str(updated_session["logo_path"] or ""),
                    extracted=data,
                )

        return result

    voice_confirm_routes.whatsapp_webhook = wrapped
    _INSTALLED = True

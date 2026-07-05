from __future__ import annotations

import json
import re
import uuid

from fastapi import Request

import self_onboarding as so
import whatsapp_routes

_ORIGINAL = so.whatsapp_webhook


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


def _message_text(message: dict) -> str:
    text = str((message.get("text") or {}).get("body") or "").strip()
    if text:
        return text

    interactive = message.get("interactive") or {}
    button = interactive.get("button_reply") or {}
    selected = str(button.get("id") or button.get("title") or "").strip()
    if selected:
        return selected

    list_reply = interactive.get("list_reply") or {}
    return str(list_reply.get("id") or list_reply.get("title") or "").strip()


async def whatsapp_webhook(request: Request):
    payload = await request.json()
    messages = whatsapp_routes.extract_messages(payload)
    if not messages:
        return await _ORIGINAL(_fresh_request(request, payload))

    message = messages[0]
    sender = str(message.get("from") or "")
    text = _message_text(message)
    command = text.upper().replace("_", " ").strip()
    session = so.get_session("whatsapp", sender)

    async def send(body: str):
        await whatsapp_routes.send_whatsapp_text(sender, body)

    if command in {"MANUAL", "MANUAL ENTRY", "ENTER MANUALLY"} and not so.profile_exists("whatsapp", sender):
        user = so._user("whatsapp", sender)
        manual_data = {
            "trade_type": "other",
            "default_terms": "Payment due within 7 days",
            "logo_visible": False,
        }
        so.save_session(
            "whatsapp",
            sender,
            str(user["user_id"]),
            "awaiting_edit",
            source_path="",
            logo_path="",
            extracted=manual_data,
        )
        await send(
            "Send your details in one message:\n"
            "BUSINESS NAME: ...\n"
            "ABN: ...\n"
            "PHONE: ...\n"
            "EMAIL: ...\n"
            "ADDRESS: ...\n"
            "BSB: ...\n"
            "ACCOUNT NUMBER: ..."
        )
        return {"ok": True}

    if session and command == "LOGO" and str(session["state"]) == "awaiting_confirmation":
        so.save_session(
            "whatsapp",
            sender,
            str(session["user_id"]),
            "awaiting_logo",
            source_path=str(session["source_path"]),
            logo_path=str(session["logo_path"]),
            extracted=json.loads(str(session["extracted_json"] or "{}")),
        )
        await send("Send a clear standalone PNG or JPG of your business logo. Do not send the full invoice again.")
        return {"ok": True}

    if session and str(session["state"]) == "awaiting_logo":
        if command in {"CANCEL", "SKIP"}:
            data = json.loads(str(session["extracted_json"] or "{}"))
            so.save_session(
                "whatsapp",
                sender,
                str(session["user_id"]),
                "awaiting_confirmation",
                source_path=str(session["source_path"]),
                logo_path=str(session["logo_path"]),
                extracted=data,
            )
            await send(so.summary_text(data, str(session["logo_path"])))
            return {"ok": True}

        if str(message.get("type") or "") in {"image", "document"}:
            content, mime, _ = await so._whatsapp_download(message)
            if mime not in {"image/png", "image/jpeg"}:
                await send("Please send the logo as a PNG or JPG image, or reply CANCEL.")
                return {"ok": True}
            if not content or len(content) > so.MAX_FILE_BYTES:
                await send("Please send a non-empty logo image up to 12 MB.")
                return {"ok": True}

            folder = so.business_onboarding.UPLOAD_DIR / (
                "self_whatsapp_" + re.sub(r"[^A-Za-z0-9_-]+", "_", sender)
            )
            folder.mkdir(parents=True, exist_ok=True)
            suffix = ".png" if mime == "image/png" else ".jpg"
            logo_path = folder / f"business_logo_{uuid.uuid4().hex[:8]}{suffix}"
            logo_path.write_bytes(content)

            data = json.loads(str(session["extracted_json"] or "{}"))
            so.save_session(
                "whatsapp",
                sender,
                str(session["user_id"]),
                "awaiting_confirmation",
                source_path=str(session["source_path"]),
                logo_path=str(logo_path),
                extracted=data,
            )
            await send("✅ Logo uploaded successfully.\n\n" + so.summary_text(data, str(logo_path)))
            return {"ok": True}

        await send("Please send a PNG or JPG logo, or reply CANCEL.")
        return {"ok": True}

    return await _ORIGINAL(_fresh_request(request, payload))

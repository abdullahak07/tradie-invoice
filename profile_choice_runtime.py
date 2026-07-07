from __future__ import annotations

import json
import re
from fastapi import Request

import logo_onboarding
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


def _message_text(message: dict) -> str:
    text = str((message.get("text") or {}).get("body") or "").strip()
    if text:
        return text

    interactive = message.get("interactive") or {}
    reply = interactive.get("button_reply") or interactive.get("list_reply") or {}
    return str(reply.get("id") or reply.get("title") or "").strip()


def _detected_trade(text: str) -> str:
    rules = [
        ("nursing", r"\b(?:nurse|nursing|patient care|aged care|night duty)\b"),
        ("plumber", r"\b(?:plumb|tap|toilet|dunny|drain|pipe|hot water|water heater|leak)\b"),
        ("electrician", r"\b(?:electric|sparky|power point|gpo|switchboard|downlight|wiring|rcd)\b"),
        ("mechanic", r"\b(?:mechanic|vehicle|brake|tyre|tire|engine|garage)\b"),
        ("cleaner", r"\b(?:cleaning|cleaner|window clean|end of lease|carpet clean)\b"),
        ("carpenter", r"\b(?:carpenter|carpentry|timber|door|cabinet|decking|framing)\b"),
        ("hvac", r"\b(?:hvac|air ?con|air conditioning|split system|ducting|refrigerant)\b"),
        ("painter", r"\b(?:paint|painting|undercoat|wall coating|ceiling coat)\b"),
        ("landscaper", r"\b(?:landscap|turf|mulch|irrigation|retaining wall|paving)\b"),
        ("roofer", r"\b(?:roof|roofing|gutter|flashing|ridge cap)\b"),
    ]
    for trade, pattern in rules:
        if re.search(pattern, text, re.I):
            return trade
    return ""


def _is_new_document(text: str) -> bool:
    return bool(
        re.match(
            r"^\s*(?:new\s+)?(?:invoice|inv|quote|quotation)\b",
            text,
            re.I,
        )
    )


def _profile_mismatch(profile: dict | None, text: str) -> bool:
    if not profile:
        return False

    job_trade = _detected_trade(text)
    saved_trade = str(profile.get("trade_type") or "other").strip().lower()

    if not job_trade:
        return False

    if saved_trade in {"", "other", "handyman"}:
        return False

    if job_trade == "nursing":
        return saved_trade != "other"

    return job_trade != saved_trade


def _replace_first_message_text(payload: dict, text: str) -> dict:
    cloned = json.loads(json.dumps(payload))

    for entry in cloned.get("entry", []):
        for change in entry.get("changes", []):
            messages = (change.get("value") or {}).get("messages") or []
            if not messages:
                continue

            messages[0]["type"] = "text"
            messages[0]["text"] = {"body": text}
            messages[0].pop("interactive", None)
            return cloned

    return cloned


def install() -> None:
    global _INSTALLED

    if _INSTALLED:
        return

    original = logo_onboarding.whatsapp_webhook

    async def guarded(request: Request):
        payload = await request.json()
        messages = whatsapp_routes.extract_messages(payload)

        if not messages:
            return await original(_fresh_request(request, payload))

        message = messages[0]
        sender = str(message.get("from") or "").strip()
        text = _message_text(message)
        command = text.upper().replace("_", " ").strip()
        session = so.get_session("whatsapp", sender)

        async def send(body: str):
            await whatsapp_routes.send_whatsapp_text(sender, body)

        if session and str(session["state"]) == "awaiting_profile_choice":
            saved = json.loads(str(session["extracted_json"] or "{}"))
            pending_text = str(saved.get("pending_text") or "").strip()

            if command in {
                "USE CURRENT",
                "USE CURRENT PROFILE",
                "CURRENT PROFILE",
            }:
                so.save_session(
                    "whatsapp",
                    sender,
                    str(session["user_id"]),
                    "confirmed",
                )

                if not pending_text:
                    await send("Please send the invoice or quote again.")
                    return {"ok": True}

                forwarded = _replace_first_message_text(payload, pending_text)
                return await original(_fresh_request(request, forwarded))

            if command in {
                "NEW PROFILE",
                "CREATE NEW PROFILE",
                "NEW BUSINESS",
            }:
                so.save_session(
                    "whatsapp",
                    sender,
                    str(session["user_id"]),
                    "awaiting_upload",
                )
                await send(
                    "Send a PDF or clear photo of the new business invoice, "
                    "quote or letterhead.\n\n"
                    "No document? Reply MANUAL ENTRY."
                )
                return {"ok": True}

            await send(
                "Reply USE CURRENT PROFILE to continue with the saved "
                "business, or NEW PROFILE to load different details."
            )
            return {"ok": True}

        if _is_new_document(text):
            profile = so.business_onboarding.profile_for_channel(
                "whatsapp",
                sender,
            )

            if _profile_mismatch(profile, text):
                user = so._user("whatsapp", sender)

                so.save_session(
                    "whatsapp",
                    sender,
                    str(user["user_id"]),
                    "awaiting_profile_choice",
                    extracted={"pending_text": text},
                )

                business_name = str(
                    (profile or {}).get("business_name")
                    or "Saved business"
                )
                trade_type = str(
                    (profile or {}).get("trade_type")
                    or "other"
                ).title()

                await send(
                    "This job does not appear to match your saved business "
                    "profile:\n\n"
                    f"{business_name} — {trade_type}\n\n"
                    "Reply USE CURRENT PROFILE to use this profile, or "
                    "NEW PROFILE to load different business details."
                )
                return {"ok": True}

        return await original(_fresh_request(request, payload))

    logo_onboarding.whatsapp_webhook = guarded
    voice_confirm_routes.whatsapp_webhook = guarded
    _INSTALLED = True

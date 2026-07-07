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


def _is_new_document(text: str) -> bool:
    return bool(
        re.match(
            r"^\s*(?:new\s+)?(?:invoice|inv|quote|quotation)\b",
            text,
            re.I,
        )
    )


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


def _session_data(session) -> dict:
    try:
        return json.loads(str(session["extracted_json"] or "{}"))
    except Exception:
        return {}


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
        command = re.sub(r"\s+", " ", text.upper().replace("_", " ")).strip()
        session = so.get_session("whatsapp", sender)

        async def send(body: str):
            await whatsapp_routes.send_whatsapp_text(sender, body)

        if session and str(session["state"]) == "awaiting_profile_choice":
            saved = _session_data(session)
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
                "NEW",
                "NEW PROFILE",
                "CREATE NEW PROFILE",
                "NEW BUSINESS",
            }:
                so.save_session(
                    "whatsapp",
                    sender,
                    str(session["user_id"]),
                    "awaiting_upload",
                    extracted={"pending_text": pending_text},
                )
                await send(
                    "Send a PDF or clear photo of the new business invoice, "
                    "quote or letterhead.\n\n"
                    "No document? Reply MANUAL or MANUAL ENTRY."
                )
                return {"ok": True}

            await send(
                "Reply USE CURRENT PROFILE to continue with the saved "
                "business, or NEW PROFILE to load different details."
            )
            return {"ok": True}

        if session and str(session["state"]) == "awaiting_upload" and command in {
            "MANUAL",
            "MANUAL ENTRY",
        }:
            saved = _session_data(session)
            pending_text = str(saved.get("pending_text") or "").strip()
            so.save_session(
                "whatsapp",
                sender,
                str(session["user_id"]),
                "awaiting_edit",
                extracted={
                    "trade_type": "other",
                    "default_terms": "Payment due within 7 days",
                    "pending_text": pending_text,
                },
            )
            await send(
                "Send the new business details in one message:\n"
                "BUSINESS NAME: ...\n"
                "TRADE: ...\n"
                "ABN: ...\n"
                "PHONE: ...\n"
                "EMAIL: ...\n"
                "ADDRESS: ...\n"
                "BSB: ...\n"
                "ACCOUNT NUMBER: ..."
            )
            return {"ok": True}

        if session and str(session["state"]) == "awaiting_edit" and text:
            saved = _session_data(session)
            pending_text = str(saved.get("pending_text") or "").strip()
            profile_data = dict(saved)
            profile_data.pop("pending_text", None)
            updated = so.apply_edits(profile_data, text)
            if pending_text:
                updated["pending_text"] = pending_text
            so.save_session(
                "whatsapp",
                sender,
                str(session["user_id"]),
                "awaiting_confirmation",
                source_path=str(session["source_path"] or ""),
                logo_path=str(session["logo_path"] or ""),
                extracted=updated,
            )
            display_data = dict(updated)
            display_data.pop("pending_text", None)
            await send(so.summary_text(display_data, str(session["logo_path"] or "")))
            return {"ok": True}

        if session and str(session["state"]) == "awaiting_confirmation" and command == "CONFIRM":
            saved = _session_data(session)
            pending_text = str(saved.pop("pending_text", "") or "").strip()
            try:
                so.save_profile(
                    "whatsapp",
                    sender,
                    saved,
                    str(session["source_path"] or ""),
                    str(session["logo_path"] or ""),
                )
            except Exception as exc:
                await send(str(exc))
                return {"ok": True}

            await send("✅ Business profile saved.")
            if pending_text:
                await send("✅ Continuing with your original invoice or quote…")
                forwarded = _replace_first_message_text(payload, pending_text)
                return await original(_fresh_request(request, forwarded))

            await send("Send your invoice or quote details when ready.")
            return {"ok": True}

        if _is_new_document(text):
            profile = so.business_onboarding.profile_for_channel(
                "whatsapp",
                sender,
            )

            if profile:
                user = so._user("whatsapp", sender)

                so.save_session(
                    "whatsapp",
                    sender,
                    str(user["user_id"]),
                    "awaiting_profile_choice",
                    extracted={"pending_text": text},
                )

                business_name = str(
                    profile.get("business_name")
                    or "Saved business"
                )
                trade_type = str(
                    profile.get("trade_type")
                    or "other"
                ).title()

                await send(
                    "Which business profile should I use for this "
                    "invoice or quote?\n\n"
                    f"{business_name} — {trade_type}\n\n"
                    "Reply USE CURRENT PROFILE to continue with this "
                    "profile, or NEW PROFILE to load different business "
                    "and bank details."
                )
                return {"ok": True}

        return await original(_fresh_request(request, payload))

    logo_onboarding.whatsapp_webhook = guarded
    voice_confirm_routes.whatsapp_webhook = guarded
    _INSTALLED = True

from __future__ import annotations

import json
import re
import uuid

import self_onboarding as so
import whatsapp_routes


async def whatsapp_webhook(request):
    payload = await request.json()
    messages = whatsapp_routes.extract_messages(payload)
    if not messages:
        return {"ok": True}

    message = messages[0]
    sender = str(message.get("from") or "")
    text = str((message.get("text") or {}).get("body") or "").strip()
    command = text.upper()
    session = so.get_session("whatsapp", sender)

    async def send(body: str):
        await whatsapp_routes.send_whatsapp_text(sender, body)

    if command in {"START", "TRIAL", "FREE TRIAL", "HI TRADIE INVOICE, START MY FREE TRIAL."} and not so.profile_exists("whatsapp", sender):
        user = so._user("whatsapp", sender)
        so.save_session("whatsapp", sender, str(user["user_id"]), "awaiting_upload")
        await send(so.welcome_text())
        return {"ok": True}

    if session and command == "LOGO" and str(session["state"]) == "awaiting_confirmation":
        so.save_session(
            "whatsapp", sender, str(session["user_id"]), "awaiting_logo",
            source_path=str(session["source_path"]),
            extracted=json.loads(str(session["extracted_json"] or "{}")),
        )
        await send("Send a clear standalone PNG or JPG of your business logo. Do not send the full invoice again.")
        return {"ok": True}

    if str(message.get("type") or "") in {"image", "document"}:
        content, mime, filename = await so._whatsapp_download(message)
        if session and str(session["state"]) == "awaiting_logo":
            if mime not in {"image/png", "image/jpeg"}:
                await send("Please send the logo as a PNG or JPG image.")
                return {"ok": True}
            folder = so.business_onboarding.UPLOAD_DIR / ("self_whatsapp_" + re.sub(r"[^A-Za-z0-9_-]+", "_", sender))
            folder.mkdir(parents=True, exist_ok=True)
            suffix = ".png" if mime == "image/png" else ".jpg"
            logo_path = folder / f"business_logo_{uuid.uuid4().hex[:8]}{suffix}"
            logo_path.write_bytes(content)
            data = json.loads(str(session["extracted_json"] or "{}"))
            so.save_session(
                "whatsapp", sender, str(session["user_id"]), "awaiting_confirmation",
                source_path=str(session["source_path"]), logo_path=str(logo_path), extracted=data,
            )
            await send("✅ Logo uploaded successfully.\n\n" + so.summary_text(data, str(logo_path)))
            return {"ok": True}
        await so._process_upload("whatsapp", sender, content, mime, filename, send)
        return {"ok": True}

    if session and command == "CONFIRM" and str(session["state"]) == "awaiting_confirmation":
        data = json.loads(str(session["extracted_json"] or "{}"))
        try:
            so.save_profile("whatsapp", sender, data, str(session["source_path"]), str(session["logo_path"]))
            await send("✅ Your business profile is saved. Your 14-day trial starts now with 5 free invoices or quotes.\n\nSend a job like:\nInvoice John, replace kitchen tap $220")
        except Exception as exc:
            await send(str(exc))
        return {"ok": True}

    if session and command == "EDIT" and str(session["state"]) == "awaiting_confirmation":
        so.save_session(
            "whatsapp", sender, str(session["user_id"]), "awaiting_edit",
            source_path=str(session["source_path"]), logo_path=str(session["logo_path"]),
            extracted=json.loads(str(session["extracted_json"] or "{}")),
        )
        await send("Send all corrections in one message, for example:\nBUSINESS NAME: Smith Electrical\nABN: 12 345 678 901\nBSB: 066-123")
        return {"ok": True}

    if session and str(session["state"]) == "awaiting_edit" and text:
        data = so.apply_edits(json.loads(str(session["extracted_json"] or "{}")), text)
        so.save_session(
            "whatsapp", sender, str(session["user_id"]), "awaiting_confirmation",
            source_path=str(session["source_path"]), logo_path=str(session["logo_path"]), extracted=data,
        )
        await send(so.summary_text(data, str(session["logo_path"])))
        return {"ok": True}

    if session and command in {"REUPLOAD", "UPLOAD AGAIN"}:
        so.save_session("whatsapp", sender, str(session["user_id"]), "awaiting_upload")
        await send("Send a different invoice, quote or letterhead now.")
        return {"ok": True}

    if command == "MANUAL" and not so.profile_exists("whatsapp", sender):
        user = so._user("whatsapp", sender)
        so.save_session("whatsapp", sender, str(user["user_id"]), "awaiting_edit", extracted={"trade_type": "other", "default_terms": "Payment due within 7 days"})
        await send("Send your details in one message:\nBUSINESS NAME: ...\nABN: ...\nPHONE: ...\nEMAIL: ...\nADDRESS: ...\nBSB: ...\nACCOUNT NUMBER: ...")
        return {"ok": True}

    if not so.profile_exists("whatsapp", sender) and text:
        await send(so.welcome_text())
    return {"ok": True}

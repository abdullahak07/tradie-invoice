from __future__ import annotations

import importlib.util
import json
import os
import re
import uuid
from pathlib import Path

os.environ.setdefault("TRIAL_DAYS", "14")
os.environ.setdefault("TRIAL_CREDITS", "5")
os.environ.setdefault("TRIAL_VOICE_LIMIT", "3")

import billing

billing.TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
billing.TRIAL_CREDITS = int(os.getenv("TRIAL_CREDITS", "5"))

import self_onboarding
import voice_confirm_routes
import whatsapp_routes


def _no_invoice_header_crop(content: bytes, mime: str, folder: Path, visible: bool) -> str:
    return ""


def _branding_summary(data: dict, logo_path: str) -> str:
    def line(label: str, key: str) -> str:
        value = str(data.get(key) or "").strip() or "Not found"
        return f"{label}: {value}"

    if logo_path:
        logo_line = "Logo: Ready to use"
    elif bool(data.get("logo_visible")):
        logo_line = "Logo: Detected, but not clear enough to use"
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
            "Reply LOGO to upload a clear standalone PNG/JPG logo, CONFIRM to continue without a logo, EDIT to correct details, or REUPLOAD to send another document.",
        ]
    )


self_onboarding.create_logo_candidate = _no_invoice_header_crop
self_onboarding.summary_text = _branding_summary

_original_whatsapp_onboarding = self_onboarding.whatsapp_webhook


async def _whatsapp_onboarding_with_logo(request):
    payload = await request.json()
    messages = whatsapp_routes.extract_messages(payload)
    if not messages:
        return await _original_whatsapp_onboarding(request)

    message = messages[0]
    sender = str(message.get("from") or "")
    text = str((message.get("text") or {}).get("body") or "").strip()
    command = text.upper()
    session = self_onboarding.get_session("whatsapp", sender)

    if session and command == "LOGO" and str(session["state"]) == "awaiting_confirmation":
        self_onboarding.save_session(
            "whatsapp",
            sender,
            str(session["user_id"]),
            "awaiting_logo",
            source_path=str(session["source_path"]),
            logo_path="",
            extracted=json.loads(str(session["extracted_json"] or "{}")),
        )
        await whatsapp_routes.send_whatsapp_text(
            sender,
            "Please send a clear standalone PNG or JPG of your business logo. Do not send the full invoice again.",
        )
        return {"ok": True}

    if session and str(session["state"]) == "awaiting_logo" and str(message.get("type") or "") in {"image", "document"}:
        content, mime, _ = await self_onboarding._whatsapp_download(message)
        if mime not in {"image/png", "image/jpeg"}:
            await whatsapp_routes.send_whatsapp_text(sender, "Please send the logo as a PNG or JPG image.")
            return {"ok": True}
        if not content or len(content) > self_onboarding.MAX_FILE_BYTES:
            await whatsapp_routes.send_whatsapp_text(sender, "Please send a non-empty logo image up to 12 MB.")
            return {"ok": True}

        folder = self_onboarding.business_onboarding.UPLOAD_DIR / f"self_whatsapp_{re.sub(r'[^A-Za-z0-9_-]+', '_', sender)}"
        folder.mkdir(parents=True, exist_ok=True)
        suffix = ".png" if mime == "image/png" else ".jpg"
        logo_path = folder / f"business_logo_{uuid.uuid4().hex[:8]}{suffix}"
        logo_path.write_bytes(content)
        data = json.loads(str(session["extracted_json"] or "{}"))

        self_onboarding.save_session(
            "whatsapp",
            sender,
            str(session["user_id"]),
            "awaiting_confirmation",
            source_path=str(session["source_path"]),
            logo_path=str(logo_path),
            extracted=data,
        )
        await whatsapp_routes.send_whatsapp_text(
            sender,
            "✅ Logo uploaded successfully.\n\n" + self_onboarding.summary_text(data, str(logo_path)),
        )
        return {"ok": True}

    return await _original_whatsapp_onboarding(request)


self_onboarding.whatsapp_webhook = _whatsapp_onboarding_with_logo
self_onboarding.install()
voice_confirm_routes.whatsapp_webhook = _whatsapp_onboarding_with_logo

import trade_letterheads

_original_letterhead_install = trade_letterheads.install_letterhead_routing


def _install_letterheads_and_user_profiles() -> None:
    _original_letterhead_install()
    import profile_branding_runtime

    profile_branding_runtime.install()


trade_letterheads.install_letterhead_routing = _install_letterheads_and_user_profiles

_main_file = Path(__file__).resolve().parent.parent / "main.py"
_spec = importlib.util.spec_from_file_location("tradie_invoice_main_file", _main_file)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Could not load the Tradie Invoice application")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
app = _module.app

__all__ = ["app"]

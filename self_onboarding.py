from __future__ import annotations

import io
import json
import mimetypes
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from PIL import Image, ImageChops
from fastapi import Request
from google import genai
from google.genai import types
from pydantic import BaseModel

import billing
import business_onboarding
import telegram_routes
import voice_confirm_routes
import whatsapp_routes


_INSTALLED = False
_ORIGINAL_TELEGRAM = voice_confirm_routes.telegram_webhook
_ORIGINAL_WHATSAPP = voice_confirm_routes.whatsapp_webhook
MAX_FILE_BYTES = int(os.getenv("ONBOARDING_MAX_BYTES", str(12 * 1024 * 1024)))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
TRIAL_CREDITS = int(os.getenv("TRIAL_CREDITS", "5"))


class ExtractedBusiness(BaseModel):
    business_name: str = ""
    owner_name: str = ""
    trade_type: str = "other"
    abn: str = ""
    licence_number: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    bank_account_name: str = ""
    bank_bsb: str = ""
    bank_account_number: str = ""
    default_terms: str = "Payment due within 7 days"
    footer_text: str = ""
    logo_visible: bool = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    business_onboarding.ensure_schema()
    billing.ensure_billing_schema()
    with business_onboarding.db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS onboarding_sessions (
                channel TEXT NOT NULL,
                external_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                state TEXT NOT NULL,
                source_path TEXT NOT NULL DEFAULT '',
                logo_path TEXT NOT NULL DEFAULT '',
                extracted_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (channel, external_id)
            )
            """
        )


def get_session(channel: str, external_id: str):
    ensure_schema()
    with business_onboarding.db() as conn:
        return conn.execute(
            "SELECT * FROM onboarding_sessions WHERE channel=? AND external_id=?",
            (channel, external_id),
        ).fetchone()


def save_session(channel: str, external_id: str, user_id: str, state: str, *, source_path: str = "", logo_path: str = "", extracted: dict[str, Any] | None = None) -> None:
    ensure_schema()
    now = now_iso()
    with business_onboarding.db() as conn:
        conn.execute(
            """
            INSERT INTO onboarding_sessions (
                channel, external_id, user_id, state, source_path, logo_path,
                extracted_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel, external_id) DO UPDATE SET
                user_id=excluded.user_id,
                state=excluded.state,
                source_path=excluded.source_path,
                logo_path=excluded.logo_path,
                extracted_json=excluded.extracted_json,
                updated_at=excluded.updated_at
            """,
            (
                channel,
                external_id,
                user_id,
                state,
                source_path,
                logo_path,
                json.dumps(extracted or {}, ensure_ascii=False),
                now,
                now,
            ),
        )


def _user(channel: str, external_id: str):
    return billing.get_or_create_user(channel, external_id)


def profile_exists(channel: str, external_id: str) -> bool:
    return business_onboarding.profile_for_channel(channel, external_id) is not None


def welcome_text() -> str:
    return (
        "👋 Welcome to Tradie Invoice.\n\n"
        "Send a clear photo or PDF of one of your existing invoices, quotes or letterheads. "
        "I’ll copy your business details, payment details and branding.\n\n"
        "You will review everything before it is saved.\n"
        "Free trial: 5 invoices or quotes within 14 days. No card required.\n\n"
        "No existing document? Reply MANUAL."
    )


def summary_text(data: dict[str, Any], logo_path: str) -> str:
    def line(label: str, key: str) -> str:
        value = str(data.get(key) or "").strip() or "Not found"
        return f"{label}: {value}"

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
            f"Logo: {'Found' if logo_path else 'Not found'}",
            "",
            "Please confirm these are your business details, not your customer’s or supplier’s details.",
            "Reply CONFIRM to save, EDIT to correct them, or REUPLOAD to send another document.",
        ]
    )


def _normalise_trade(value: str) -> str:
    raw = (value or "other").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "electrical": "electrician",
        "sparky": "electrician",
        "carpentry": "carpenter",
        "plumbing": "plumber",
        "air_conditioning": "hvac",
        "aircon": "hvac",
        "concrete": "concreter",
        "painting": "painter",
        "cleaning": "cleaner",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in business_onboarding.TRADE_KEYS else "other"


def extract_business(content: bytes, mime: str) -> dict[str, Any]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        contents=[
            (
                "This is an Australian tradie's own invoice, quote or letterhead used for onboarding. "
                "Extract only the tradie's business identity and payment details that are clearly visible. "
                "Do not copy customer, supplier or job-specific details. Never guess missing values. "
                "Identify the closest trade_type from electrician, carpenter, plumber, hvac, painter, landscaper, roofer, tiler, concreter, handyman, cleaner, builder, locksmith, mechanic, pest_control, solar_installer, other. "
                "Set logo_visible true only when a distinct business logo is visibly present."
            ),
            types.Part.from_bytes(data=content, mime_type=mime),
        ],
        config={
            "response_mime_type": "application/json",
            "response_schema": ExtractedBusiness,
            "temperature": 0,
        },
    )
    parsed = response.parsed or ExtractedBusiness.model_validate_json(response.text)
    data = parsed.model_dump()
    data["trade_type"] = _normalise_trade(str(data.get("trade_type") or "other"))
    return data


def _trim_image(image: Image.Image) -> Image.Image:
    rgba = image.convert("RGBA")
    white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    box = ImageChops.difference(rgba, white).getbbox()
    return rgba.crop(box) if box else rgba


def create_logo_candidate(content: bytes, mime: str, folder: Path, visible: bool) -> str:
    if not visible or mime not in {"image/png", "image/jpeg"}:
        return ""
    try:
        with Image.open(io.BytesIO(content)) as source:
            width, height = source.size
            top = source.convert("RGBA").crop((0, 0, width, max(1, int(height * 0.32))))
            top = _trim_image(top)
            if top.width < 40 or top.height < 20:
                return ""
            path = folder / "extracted_logo.png"
            top.save(path, "PNG")
            return str(path)
    except Exception:
        return ""


def save_profile(channel: str, external_id: str, data: dict[str, Any], source_path: str, logo_path: str) -> str:
    user = _user(channel, external_id)
    user_id = str(user["user_id"])
    now = now_iso()
    expiry = (datetime.now(timezone.utc) + timedelta(days=TRIAL_DAYS)).isoformat(timespec="seconds")
    business_name = str(data.get("business_name") or "").strip()
    if len(business_name) < 2:
        raise ValueError("Business name was not found. Reply EDIT and provide BUSINESS NAME: ...")
    trade = _normalise_trade(str(data.get("trade_type") or "other"))

    with business_onboarding.db() as conn:
        conn.execute(
            """
            UPDATE users SET plan='trial', status='active', trial_started_at=?,
                trial_expires_at=?, credit_balance=?, credit_limit=?,
                billing_period_start=NULL, billing_period_end=NULL, updated_at=?
            WHERE user_id=?
            """,
            (now, expiry, TRIAL_CREDITS, TRIAL_CREDITS, now, user_id),
        )
        conn.execute(
            """
            INSERT INTO business_profiles (
                profile_id,user_id,trade_type,business_name,owner_name,abn,
                licence_number,phone,email,address,gst_enabled,default_terms,
                trade_prompt,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                trade_type=excluded.trade_type,business_name=excluded.business_name,
                owner_name=excluded.owner_name,abn=excluded.abn,
                licence_number=excluded.licence_number,phone=excluded.phone,
                email=excluded.email,address=excluded.address,
                default_terms=excluded.default_terms,trade_prompt=excluded.trade_prompt,
                updated_at=excluded.updated_at
            """,
            (
                uuid.uuid4().hex,user_id,trade,business_name,
                str(data.get("owner_name") or ""),str(data.get("abn") or ""),
                str(data.get("licence_number") or ""),str(data.get("phone") or ""),
                str(data.get("email") or ""),str(data.get("address") or ""),1,
                str(data.get("default_terms") or "Payment due within 7 days"),
                business_onboarding.TRADE_PROMPTS.get(trade, business_onboarding.TRADE_PROMPTS["other"]),
                now,now,
            ),
        )
        conn.execute(
            """
            INSERT INTO business_payment_details (
                user_id,account_name,bsb,account_number,payment_reference,updated_at
            ) VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET account_name=excluded.account_name,
                bsb=excluded.bsb,account_number=excluded.account_number,
                payment_reference=excluded.payment_reference,updated_at=excluded.updated_at
            """,
            (
                user_id,str(data.get("bank_account_name") or ""),
                str(data.get("bank_bsb") or ""),str(data.get("bank_account_number") or ""),
                "Invoice number",now,
            ),
        )
        conn.execute(
            """
            INSERT INTO business_branding (
                user_id,logo_path,letterhead_path,footer_text,extraction_json,updated_at
            ) VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET logo_path=excluded.logo_path,
                letterhead_path=excluded.letterhead_path,footer_text=excluded.footer_text,
                extraction_json=excluded.extraction_json,updated_at=excluded.updated_at
            """,
            (
                user_id,logo_path,source_path,str(data.get("footer_text") or ""),
                json.dumps(data, ensure_ascii=False),now,
            ),
        )
        conn.execute(
            "UPDATE onboarding_sessions SET state='confirmed', updated_at=? WHERE channel=? AND external_id=?",
            (now, channel, external_id),
        )
    return user_id


def apply_edits(data: dict[str, Any], text: str) -> dict[str, Any]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if key:
        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=[
                "Update this extracted business profile using the user's corrections. Preserve unchanged fields. Never invent values.",
                json.dumps(data, ensure_ascii=False),
                text,
            ],
            config={"response_mime_type": "application/json", "response_schema": ExtractedBusiness, "temperature": 0},
        )
        parsed = response.parsed or ExtractedBusiness.model_validate_json(response.text)
        result = parsed.model_dump()
        result["trade_type"] = _normalise_trade(str(result.get("trade_type") or "other"))
        return result

    result = dict(data)
    fields = {
        "business name": "business_name", "owner": "owner_name", "abn": "abn",
        "licence": "licence_number", "phone": "phone", "email": "email",
        "address": "address", "account name": "bank_account_name", "bsb": "bank_bsb",
        "account number": "bank_account_number", "terms": "default_terms", "trade": "trade_type",
    }
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, value = line.split(":", 1)
        key_name = fields.get(label.strip().lower())
        if key_name:
            result[key_name] = value.strip()
    result["trade_type"] = _normalise_trade(str(result.get("trade_type") or "other"))
    return result


async def _telegram_download(message: dict[str, Any]) -> tuple[bytes, str, str]:
    media = message.get("document") or ((message.get("photo") or [])[-1] if message.get("photo") else {})
    file_id = str(media.get("file_id") or "")
    if not file_id:
        raise ValueError("No supported Telegram file found")
    async with httpx.AsyncClient(timeout=60) as client:
        meta = await client.get(f"https://api.telegram.org/bot{telegram_routes.telegram_token()}/getFile", params={"file_id": file_id})
        meta.raise_for_status()
        file_path = str((meta.json().get("result") or {}).get("file_path") or "")
        downloaded = await client.get(f"https://api.telegram.org/file/bot{telegram_routes.telegram_token()}/{file_path}")
        downloaded.raise_for_status()
    filename = str(media.get("file_name") or Path(file_path).name or "upload")
    mime = str(media.get("mime_type") or mimetypes.guess_type(filename)[0] or downloaded.headers.get("content-type") or "image/jpeg").split(";", 1)[0]
    return downloaded.content, mime, filename


async def _whatsapp_download(message: dict[str, Any]) -> tuple[bytes, str, str]:
    media = message.get("image") or message.get("document") or {}
    media_id = str(media.get("id") or "")
    if not media_id:
        raise ValueError("No supported WhatsApp file found")
    headers = {"Authorization": f"Bearer {whatsapp_routes.access_token()}"}
    async with httpx.AsyncClient(timeout=60) as client:
        meta = await client.get(f"https://graph.facebook.com/{whatsapp_routes.api_version()}/{media_id}", headers=headers)
        meta.raise_for_status()
        url = str(meta.json().get("url") or "")
        downloaded = await client.get(url, headers=headers)
        downloaded.raise_for_status()
    filename = str(media.get("filename") or f"upload-{media_id}")
    mime = str(media.get("mime_type") or downloaded.headers.get("content-type") or "image/jpeg").split(";", 1)[0]
    return downloaded.content, mime, filename


def _save_upload(channel: str, external_id: str, content: bytes, filename: str) -> Path:
    folder = business_onboarding.UPLOAD_DIR / f"self_{channel}_{re.sub(r'[^A-Za-z0-9_-]+', '_', external_id)}"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"source_{uuid.uuid4().hex[:8]}_{business_onboarding.safe_filename(filename)}"
    path.write_bytes(content)
    return path


async def _process_upload(channel: str, external_id: str, content: bytes, mime: str, filename: str, send) -> None:
    if not content or len(content) > MAX_FILE_BYTES:
        await send("Please send a non-empty PNG, JPEG or PDF up to 12 MB.")
        return
    if mime not in {"image/png", "image/jpeg", "application/pdf"}:
        await send("Please send a PNG, JPEG or PDF of your invoice, quote or letterhead.")
        return
    await send("🔎 Reading your document and extracting your business details…")
    user = _user(channel, external_id)
    path = _save_upload(channel, external_id, content, filename)
    data = extract_business(content, mime)
    logo = create_logo_candidate(content, mime, path.parent, bool(data.get("logo_visible")))
    save_session(channel, external_id, str(user["user_id"]), "awaiting_confirmation", source_path=str(path), logo_path=logo, extracted=data)
    await send(summary_text(data, logo))


async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str((message.get("chat") or {}).get("id") or "")
    if not chat_id:
        return await _ORIGINAL_TELEGRAM(request)
    text = str(message.get("text") or "").strip()
    command = text.upper()
    session = get_session("telegram", chat_id)

    if command in {"/START", "START", "TRIAL", "FREE TRIAL"} and not profile_exists("telegram", chat_id):
        _user("telegram", chat_id)
        save_session("telegram", chat_id, str(_user("telegram", chat_id)["user_id"]), "awaiting_upload")
        await telegram_routes.send_telegram(chat_id, welcome_text())
        return {"ok": True}

    if message.get("document") or message.get("photo"):
        await _process_upload("telegram", chat_id, *(await _telegram_download(message)), lambda body: telegram_routes.send_telegram(chat_id, body))
        return {"ok": True}

    if session and command == "CONFIRM" and str(session["state"]) == "awaiting_confirmation":
        data = json.loads(str(session["extracted_json"] or "{}"))
        try:
            save_profile("telegram", chat_id, data, str(session["source_path"]), str(session["logo_path"]))
            await telegram_routes.send_telegram(chat_id, "✅ Your business profile is saved. Your 14-day trial starts now with 5 free invoices or quotes.\n\nSend a job like:\nInvoice John, replace kitchen tap $220")
        except Exception as exc:
            await telegram_routes.send_telegram(chat_id, str(exc))
        return {"ok": True}

    if session and command == "EDIT" and str(session["state"]) == "awaiting_confirmation":
        save_session("telegram", chat_id, str(session["user_id"]), "awaiting_edit", source_path=str(session["source_path"]), logo_path=str(session["logo_path"]), extracted=json.loads(str(session["extracted_json"] or "{}")))
        await telegram_routes.send_telegram(chat_id, "Send all corrections in one message, for example:\nBUSINESS NAME: Smith Electrical\nABN: 12 345 678 901\nBSB: 066-123")
        return {"ok": True}

    if session and str(session["state"]) == "awaiting_edit" and text:
        data = apply_edits(json.loads(str(session["extracted_json"] or "{}")), text)
        save_session("telegram", chat_id, str(session["user_id"]), "awaiting_confirmation", source_path=str(session["source_path"]), logo_path=str(session["logo_path"]), extracted=data)
        await telegram_routes.send_telegram(chat_id, summary_text(data, str(session["logo_path"])))
        return {"ok": True}

    if session and command in {"REUPLOAD", "UPLOAD AGAIN"}:
        save_session("telegram", chat_id, str(session["user_id"]), "awaiting_upload")
        await telegram_routes.send_telegram(chat_id, "Send a different invoice, quote or letterhead now.")
        return {"ok": True}

    if not profile_exists("telegram", chat_id) and text and command not in {"MANUAL"}:
        await telegram_routes.send_telegram(chat_id, welcome_text())
        return {"ok": True}
    if command == "MANUAL" and not profile_exists("telegram", chat_id):
        user = _user("telegram", chat_id)
        save_session("telegram", chat_id, str(user["user_id"]), "awaiting_edit", extracted={"trade_type": "other", "default_terms": "Payment due within 7 days"})
        await telegram_routes.send_telegram(chat_id, "Send your details in one message:\nBUSINESS NAME: ...\nABN: ...\nPHONE: ...\nEMAIL: ...\nADDRESS: ...\nBSB: ...\nACCOUNT NUMBER: ...")
        return {"ok": True}
    return await _ORIGINAL_TELEGRAM(request)


async def whatsapp_webhook(request: Request):
    payload = await request.json()
    messages = whatsapp_routes.extract_messages(payload)
    if not messages:
        return await _ORIGINAL_WHATSAPP(request)
    message = messages[0]
    sender = str(message.get("from") or "")
    text = str((message.get("text") or {}).get("body") or "").strip()
    command = text.upper()
    session = get_session("whatsapp", sender)
    send = lambda body: whatsapp_routes.send_whatsapp_text(sender, body)

    if command in {"START", "TRIAL", "FREE TRIAL", "HI TRADIE INVOICE, START MY FREE TRIAL."} and not profile_exists("whatsapp", sender):
        user = _user("whatsapp", sender)
        save_session("whatsapp", sender, str(user["user_id"]), "awaiting_upload")
        await send(welcome_text())
        return {"ok": True}

    if str(message.get("type") or "") in {"image", "document"}:
        await _process_upload("whatsapp", sender, *(await _whatsapp_download(message)), send)
        return {"ok": True}

    if session and command == "CONFIRM" and str(session["state"]) == "awaiting_confirmation":
        data = json.loads(str(session["extracted_json"] or "{}"))
        try:
            save_profile("whatsapp", sender, data, str(session["source_path"]), str(session["logo_path"]))
            await send("✅ Your business profile is saved. Your 14-day trial starts now with 5 free invoices or quotes.\n\nSend a job like:\nInvoice John, replace kitchen tap $220")
        except Exception as exc:
            await send(str(exc))
        return {"ok": True}

    if session and command == "EDIT" and str(session["state"]) == "awaiting_confirmation":
        save_session("whatsapp", sender, str(session["user_id"]), "awaiting_edit", source_path=str(session["source_path"]), logo_path=str(session["logo_path"]), extracted=json.loads(str(session["extracted_json"] or "{}")))
        await send("Send all corrections in one message, for example:\nBUSINESS NAME: Smith Electrical\nABN: 12 345 678 901\nBSB: 066-123")
        return {"ok": True}

    if session and str(session["state"]) == "awaiting_edit" and text:
        data = apply_edits(json.loads(str(session["extracted_json"] or "{}")), text)
        save_session("whatsapp", sender, str(session["user_id"]), "awaiting_confirmation", source_path=str(session["source_path"]), logo_path=str(session["logo_path"]), extracted=data)
        await send(summary_text(data, str(session["logo_path"])))
        return {"ok": True}

    if session and command in {"REUPLOAD", "UPLOAD AGAIN"}:
        save_session("whatsapp", sender, str(session["user_id"]), "awaiting_upload")
        await send("Send a different invoice, quote or letterhead now.")
        return {"ok": True}

    if command == "MANUAL" and not profile_exists("whatsapp", sender):
        user = _user("whatsapp", sender)
        save_session("whatsapp", sender, str(user["user_id"]), "awaiting_edit", extracted={"trade_type": "other", "default_terms": "Payment due within 7 days"})
        await send("Send your details in one message:\nBUSINESS NAME: ...\nABN: ...\nPHONE: ...\nEMAIL: ...\nADDRESS: ...\nBSB: ...\nACCOUNT NUMBER: ...")
        return {"ok": True}

    if not profile_exists("whatsapp", sender) and text:
        await send(welcome_text())
        return {"ok": True}
    return await _ORIGINAL_WHATSAPP(request)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    ensure_schema()
    voice_confirm_routes.telegram_webhook = telegram_webhook
    voice_confirm_routes.whatsapp_webhook = whatsapp_webhook
    _INSTALLED = True

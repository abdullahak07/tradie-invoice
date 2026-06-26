from __future__ import annotations

import json
import os
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from google import genai
from google.genai import types
from starlette.requests import Request as StarletteRequest

import telegram_routes
import whatsapp_routes

router = APIRouter(tags=["voice"])

MAX_AUDIO_BYTES = int(os.getenv("VOICE_MAX_BYTES", str(12 * 1024 * 1024)))
VOICE_MODEL = os.getenv("VOICE_GEMINI_MODEL", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))


def _gemini_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    return key


def transcribe_audio(audio_bytes: bytes, mime_type: str) -> str:
    if not audio_bytes:
        raise ValueError("The voice message was empty")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise ValueError("Voice message is too large. Please keep it under 12 MB")

    client = genai.Client(api_key=_gemini_key())
    response = client.models.generate_content(
        model=VOICE_MODEL,
        contents=[
            "Transcribe this Australian tradie voice message exactly enough to create or edit an invoice or quote. Preserve names, suburbs, quantities, units, prices, dates, GST instructions and whether the speaker says invoice or quote. Return transcription only, with no commentary.",
            types.Part.from_bytes(data=audio_bytes, mime_type=mime_type),
        ],
        config={"temperature": 0},
    )
    text = str(response.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty transcription")
    return text


async def _telegram_audio(message: dict[str, Any]) -> tuple[bytes, str]:
    media = message.get("voice") or message.get("audio") or {}
    file_id = str(media.get("file_id", "")).strip()
    if not file_id:
        raise ValueError("Telegram voice message has no file ID")

    token = telegram_routes.telegram_token()
    async with httpx.AsyncClient(timeout=60) as client:
        meta = await client.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
        )
        meta.raise_for_status()
        payload = meta.json()
        file_path = str((payload.get("result") or {}).get("file_path", ""))
        if not file_path:
            raise RuntimeError("Telegram did not return a voice file path")
        downloaded = await client.get(
            f"https://api.telegram.org/file/bot{token}/{file_path}"
        )
        downloaded.raise_for_status()

    mime = str(media.get("mime_type") or "audio/ogg")
    return downloaded.content, mime


async def _whatsapp_audio(message: dict[str, Any]) -> tuple[bytes, str]:
    media = message.get("audio") or {}
    media_id = str(media.get("id", "")).strip()
    if not media_id:
        raise ValueError("WhatsApp voice message has no media ID")

    headers = {"Authorization": f"Bearer {whatsapp_routes.access_token()}"}
    async with httpx.AsyncClient(timeout=60) as client:
        meta = await client.get(
            f"https://graph.facebook.com/{whatsapp_routes.api_version()}/{media_id}",
            headers=headers,
        )
        meta.raise_for_status()
        media_url = str(meta.json().get("url", ""))
        if not media_url:
            raise RuntimeError("WhatsApp did not return an audio download URL")
        downloaded = await client.get(media_url, headers=headers)
        downloaded.raise_for_status()

    mime = str(media.get("mime_type") or downloaded.headers.get("content-type") or "audio/ogg")
    return downloaded.content, mime.split(";", 1)[0]


def _json_request(original: Request, payload: dict[str, Any]) -> StarletteRequest:
    body = json.dumps(payload).encode("utf-8")
    scope = dict(original.scope)
    headers = [(k, v) for k, v in scope.get("headers", []) if k.lower() != b"content-length"]
    headers.append((b"content-type", b"application/json"))
    headers.append((b"content-length", str(len(body)).encode("ascii")))
    scope["headers"] = headers
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return StarletteRequest(scope, receive)


@router.post("/webhooks/telegram")
async def telegram_voice_webhook(request: Request) -> dict[str, bool]:
    update = await request.json()
    message = update.get("message") or update.get("edited_message") or {}
    media = message.get("voice") or message.get("audio")
    if not media:
        return await telegram_routes.telegram_webhook(_json_request(request, update))

    chat_id = str((message.get("chat") or {}).get("id", ""))
    if not chat_id:
        return {"ok": True}

    try:
        await telegram_routes.send_telegram(chat_id, "🎙️ Transcribing your voice message…")
        audio, mime = await _telegram_audio(message)
        text = transcribe_audio(audio, mime)
        await telegram_routes.send_telegram(chat_id, f"📝 I heard:\n{text}")
        converted = dict(update)
        converted_message = dict(message)
        converted_message["text"] = text
        converted_message.pop("voice", None)
        converted_message.pop("audio", None)
        if update.get("message") is not None:
            converted["message"] = converted_message
        else:
            converted["edited_message"] = converted_message
        return await telegram_routes.telegram_webhook(_json_request(request, converted))
    except Exception as exc:
        telegram_routes.log_processing_error("telegram voice processing", exc)
        await telegram_routes.send_telegram(
            chat_id,
            "I could not understand that voice message. Please speak clearly, include the customer, job items and prices, then try again.",
        )
        return {"ok": True}


@router.post("/whatsapp/webhook")
async def whatsapp_voice_webhook(request: Request) -> dict[str, bool]:
    payload: dict[str, Any] = await request.json()
    messages = whatsapp_routes.extract_messages(payload)
    audio_messages = [m for m in messages if str(m.get("type", "")) == "audio"]
    if not audio_messages:
        return await whatsapp_routes.receive_webhook(_json_request(request, payload))

    converted_payload = json.loads(json.dumps(payload))
    converted_messages = whatsapp_routes.extract_messages(converted_payload)

    for index, message in enumerate(audio_messages):
        sender = str(message.get("from", "")).strip()
        if not sender:
            continue
        try:
            await whatsapp_routes.send_whatsapp_text(sender, "🎙️ Transcribing your voice message…")
            audio, mime = await _whatsapp_audio(message)
            text = transcribe_audio(audio, mime)
            await whatsapp_routes.send_whatsapp_text(sender, f"📝 I heard:\n{text}")

            for candidate in converted_messages:
                if candidate.get("id") == message.get("id"):
                    candidate["type"] = "text"
                    candidate["text"] = {"body": text}
                    candidate.pop("audio", None)
                    break
        except Exception:
            await whatsapp_routes.send_whatsapp_text(
                sender,
                "I could not understand that voice message. Please speak clearly, include the customer, job items and prices, then try again.",
            )
            for candidate in converted_messages:
                if candidate.get("id") == message.get("id"):
                    candidate["type"] = "text"
                    candidate["text"] = {"body": ""}
                    candidate.pop("audio", None)
                    break

    return await whatsapp_routes.receive_webhook(_json_request(request, converted_payload))


@router.get("/voice/health")
def voice_health() -> dict[str, Any]:
    return {
        "ok": True,
        "gemini_configured": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "model": VOICE_MODEL,
        "max_audio_bytes": MAX_AUDIO_BYTES,
        "telegram": True,
        "whatsapp": True,
    }

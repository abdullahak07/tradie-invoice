from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import Request
from starlette.requests import Request as StarletteRequest

import telegram_routes
import voice_confirm
import voice_webhooks
import whatsapp_routes
from billing import BillingError, consume_feature_usage, refund_feature_usage


def _request(original: Request, payload: dict[str, Any]) -> StarletteRequest:
    body = json.dumps(payload).encode("utf-8")
    scope = dict(original.scope)
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return StarletteRequest(scope, receive)


def _telegram_text(update: dict[str, Any], chat_id: str, text: str) -> dict[str, Any]:
    return {
        "update_id": update.get("update_id", 0),
        "message": {
            "message_id": int(datetime.now().timestamp()),
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def _whatsapp_text(payload: dict[str, Any], text: str) -> dict[str, Any]:
    converted = json.loads(json.dumps(payload))
    messages = whatsapp_routes.extract_messages(converted)
    if messages:
        message = messages[0]
        message["type"] = "text"
        message["text"] = {"body": text}
        message.pop("interactive", None)
        message.pop("button", None)
        message.pop("audio", None)
    return converted


def _telegram_voice_reference(message: dict[str, Any]) -> str:
    media = message.get("voice") or message.get("audio") or {}
    return (
        f"telegram:{message.get('message_id', '')}:"
        f"{media.get('file_unique_id') or media.get('file_id') or ''}"
    )


def _whatsapp_voice_reference(message: dict[str, Any]) -> str:
    media = message.get("audio") or {}
    return f"whatsapp:{message.get('id') or media.get('id') or ''}"


async def telegram_webhook(request: Request) -> dict[str, bool]:
    update = await request.json()
    callback = update.get("callback_query") or {}
    data = str(callback.get("data", ""))
    callback_message = callback.get("message") or {}
    chat_id = str((callback_message.get("chat") or {}).get("id", ""))

    if data.startswith("voice_") and chat_id:
        pending = voice_confirm.get("telegram", chat_id)
        await telegram_routes.answer_callback(str(callback.get("id", "")))
        if not pending:
            await telegram_routes.send_telegram(
                chat_id,
                "That transcription expired. Send the voice note again.",
            )
            return {"ok": True}
        transcript = str(pending["transcript"])
        action = data.split(":", 1)[0]
        if action == "voice_accept":
            voice_confirm.clear("telegram", chat_id)
            await telegram_routes.send_telegram(
                chat_id,
                "✅ Accepted. Creating your draft…",
            )
            return await telegram_routes.telegram_webhook(
                _request(request, _telegram_text(update, chat_id, transcript))
            )
        if action == "voice_edit":
            voice_confirm.save("telegram", chat_id, transcript, "awaiting_edit")
            await telegram_routes.send_telegram(
                chat_id,
                "✏️ Send the corrected full transcription as text.",
            )
            return {"ok": True}
        voice_confirm.clear("telegram", chat_id)
        await telegram_routes.send_telegram(chat_id, "❌ Voice request cancelled.")
        return {"ok": True}

    message = update.get("message") or update.get("edited_message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))
    media = message.get("voice") or message.get("audio")
    if media and chat_id:
        usage_event_id: str | None = None
        try:
            usage = consume_feature_usage(
                "telegram",
                chat_id,
                "voice_transcription",
                reference=_telegram_voice_reference(message),
                metadata={"message_id": message.get("message_id")},
            )
            usage_event_id = str(usage.get("event_id") or "") or None
            await telegram_routes.send_telegram(
                chat_id,
                "🎙️ Transcribing your voice message…",
            )
            audio, mime = await voice_webhooks._telegram_audio(message)
            transcript = voice_webhooks.transcribe_audio(audio, mime)
            voice_confirm.save("telegram", chat_id, transcript)
            await voice_confirm.send_telegram_preview(chat_id, transcript)
            return {"ok": True}
        except BillingError as exc:
            await telegram_routes.send_telegram(chat_id, str(exc))
            return {"ok": True}
        except Exception as exc:
            refund_feature_usage(usage_event_id)
            telegram_routes.log_processing_error(
                "telegram voice transcription",
                exc,
            )
            await telegram_routes.send_telegram(
                chat_id,
                "I could not understand that voice message. "
                "Please speak clearly and try again. Your voice allowance was not used.",
            )
            return {"ok": True}

    text = str(message.get("text", "")).strip()
    pending = voice_confirm.get("telegram", chat_id) if chat_id else None
    if pending and str(pending["state"]) == "awaiting_edit" and text:
        voice_confirm.save("telegram", chat_id, text)
        await voice_confirm.send_telegram_preview(chat_id, text)
        return {"ok": True}
    return await telegram_routes.telegram_webhook(_request(request, update))


async def whatsapp_webhook(request: Request) -> dict[str, bool]:
    payload = await request.json()
    messages = whatsapp_routes.extract_messages(payload)
    if not messages:
        return await whatsapp_routes.receive_webhook(_request(request, payload))

    message = messages[0]
    sender = str(message.get("from", "")).strip()
    action = whatsapp_routes.extract_interactive_action(message)

    if action.startswith("voice_") and sender:
        pending = voice_confirm.get("whatsapp", sender)
        if not pending:
            await whatsapp_routes.send_whatsapp_text(
                sender,
                "That transcription expired. Send the voice note again.",
            )
            return {"ok": True}
        transcript = str(pending["transcript"])
        action_name = action.split(":", 1)[0]
        if action_name == "voice_accept":
            voice_confirm.clear("whatsapp", sender)
            await whatsapp_routes.send_whatsapp_text(
                sender,
                "✅ Accepted. Creating your draft…",
            )
            return await whatsapp_routes.receive_webhook(
                _request(request, _whatsapp_text(payload, transcript))
            )
        if action_name == "voice_edit":
            voice_confirm.save("whatsapp", sender, transcript, "awaiting_edit")
            await whatsapp_routes.send_whatsapp_text(
                sender,
                "✏️ Send the corrected full transcription as text.",
            )
            return {"ok": True}
        voice_confirm.clear("whatsapp", sender)
        await whatsapp_routes.send_whatsapp_text(
            sender,
            "❌ Voice request cancelled.",
        )
        return {"ok": True}

    message_type = str(message.get("type", ""))
    if message_type == "audio" and sender:
        usage_event_id: str | None = None
        try:
            usage = consume_feature_usage(
                "whatsapp",
                sender,
                "voice_transcription",
                reference=_whatsapp_voice_reference(message),
                metadata={"message_id": message.get("id")},
            )
            usage_event_id = str(usage.get("event_id") or "") or None
            await whatsapp_routes.send_whatsapp_text(
                sender,
                "🎙️ Transcribing your voice message…",
            )
            audio, mime = await voice_webhooks._whatsapp_audio(message)
            transcript = voice_webhooks.transcribe_audio(audio, mime)
            voice_confirm.save("whatsapp", sender, transcript)
            await voice_confirm.send_whatsapp_preview(sender, transcript)
            return {"ok": True}
        except BillingError as exc:
            await whatsapp_routes.send_whatsapp_text(sender, str(exc))
            return {"ok": True}
        except Exception:
            refund_feature_usage(usage_event_id)
            await whatsapp_routes.send_whatsapp_text(
                sender,
                "I could not understand that voice message. "
                "Please speak clearly and try again. Your voice allowance was not used.",
            )
            return {"ok": True}

    text = str((message.get("text") or {}).get("body", "")).strip()
    pending = voice_confirm.get("whatsapp", sender) if sender else None
    if pending and str(pending["state"]) == "awaiting_edit" and text:
        voice_confirm.save("whatsapp", sender, text)
        await voice_confirm.send_whatsapp_preview(sender, text)
        return {"ok": True}
    return await whatsapp_routes.receive_webhook(_request(request, payload))

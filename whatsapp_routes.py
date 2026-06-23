from __future__ import annotations

import os
import re
from datetime import datetime

import httpx
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from invoice_routes import create_pdf, create_quote_pdf
from telegram_routes import (
    ai_edit,
    ai_parse,
    claim_pdf_generation,
    convert_quote_to_invoice,
    create_ai_invoice,
    create_ai_quote,
    db,
    friendly_error_message,
    get_invoice,
    get_quote,
    invoice_summary,
    quote_as_invoice,
    quote_summary,
    release_pdf_generation,
    row_to_invoice,
    row_to_quote,
    update_ai_invoice,
    update_ai_quote,
)


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


def ensure_whatsapp_tables() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS whatsapp_message_log (
                message_id TEXT PRIMARY KEY,
                sender TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )


def claim_whatsapp_message(message_id: str, sender: str) -> bool:
    ensure_whatsapp_tables()
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO whatsapp_message_log (
                message_id, sender, received_at
            ) VALUES (?, ?, ?)
            """,
            (
                message_id,
                sender,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        raw_cursor = getattr(cursor, "_cursor", cursor)
        return int(getattr(raw_cursor, "rowcount", 0) or 0) == 1


def get_invoice_by_reference(reference: str):
    value = reference.strip().upper()
    with db() as conn:
        if value.isdigit():
            row = conn.execute(
                "SELECT * FROM invoices WHERE id = ?",
                (int(value),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM invoices WHERE UPPER(invoice_number) = ?",
                (value,),
            ).fetchone()
    if not row:
        raise ValueError(f"Invoice {reference!r} was not found.")
    return row


def get_quote_by_reference(reference: str):
    value = reference.strip().upper()
    with db() as conn:
        if value.isdigit():
            row = conn.execute(
                "SELECT * FROM quotes WHERE id = ?",
                (int(value),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM quotes WHERE UPPER(quote_number) = ?",
                (value,),
            ).fetchone()
    if not row:
        raise ValueError(f"Quote {reference!r} was not found.")
    return row


async def send_whatsapp_document(to: str, pdf_path, caption: str) -> None:
    media_url = (
        f"https://graph.facebook.com/{api_version()}/"
        f"{phone_number_id()}/media"
    )
    message_url = (
        f"https://graph.facebook.com/{api_version()}/"
        f"{phone_number_id()}/messages"
    )
    auth = {"Authorization": f"Bearer {access_token()}"}

    with open(pdf_path, "rb") as document:
        async with httpx.AsyncClient(timeout=60) as client:
            upload = await client.post(
                media_url,
                headers=auth,
                data={
                    "messaging_product": "whatsapp",
                    "type": "application/pdf",
                },
                files={
                    "file": (
                        pdf_path.name,
                        document,
                        "application/pdf",
                    )
                },
            )

    if upload.status_code >= 400:
        raise RuntimeError(
            f"WhatsApp media upload failed ({upload.status_code}): "
            f"{upload.text[:500]}"
        )

    media_id = str(upload.json().get("id", "")).strip()
    if not media_id:
        raise RuntimeError("WhatsApp media upload returned no media ID.")

    async with httpx.AsyncClient(timeout=60) as client:
        sent = await client.post(
            message_url,
            headers={
                **auth,
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": to,
                "type": "document",
                "document": {
                    "id": media_id,
                    "caption": caption[:1024],
                    "filename": pdf_path.name,
                },
            },
        )

    if sent.status_code >= 400:
        raise RuntimeError(
            f"WhatsApp document send failed ({sent.status_code}): "
            f"{sent.text[:500]}"
        )


def invoice_commands(invoice) -> str:
    ref = invoice.invoice_number
    return (
        "\n\nReply with one of these commands:\n"
        f"INVOICE PDF {ref}\n"
        f"EDIT INVOICE {ref}: your changes\n"
        f"CANCEL INVOICE {ref}"
    )


def quote_commands(quote) -> str:
    ref = quote.quote_number
    return (
        "\n\nReply with one of these commands:\n"
        f"QUOTE PDF {ref}\n"
        f"EDIT QUOTE {ref}: your changes\n"
        f"CONVERT QUOTE {ref}\n"
        f"CANCEL QUOTE {ref}"
    )


async def generate_invoice_pdf_for_whatsapp(
    sender: str,
    reference: str,
) -> None:
    row = get_invoice_by_reference(reference)
    invoice = row_to_invoice(row)

    if not claim_pdf_generation("invoice", invoice.id):
        await send_whatsapp_text(
            sender,
            f"✅ Invoice PDF {invoice.invoice_number} is already being "
            "generated or was already generated. No duplicate PDF was created.",
        )
        return

    if invoice.status == "cancelled":
        release_pdf_generation("invoice", invoice.id)
        await send_whatsapp_text(
            sender,
            f"Invoice {invoice.invoice_number} is cancelled.",
        )
        return

    await send_whatsapp_text(
        sender,
        f"⏳ Generating invoice PDF {invoice.invoice_number}. Please wait…",
    )

    try:
        pdf_path = create_pdf(row)
        with db() as conn:
            conn.execute(
                "UPDATE invoices SET status = 'pdf_generated' WHERE id = ?",
                (invoice.id,),
            )
        await send_whatsapp_document(
            sender,
            pdf_path,
            (
                f"Invoice ID: {invoice.invoice_number}\n"
                f"Customer: {invoice.customer.name or 'Customer'}\n"
                f"Total: ${invoice.total:,.2f}"
            ),
        )
    except Exception:
        release_pdf_generation("invoice", invoice.id)
        raise


async def generate_quote_pdf_for_whatsapp(
    sender: str,
    reference: str,
) -> None:
    row = get_quote_by_reference(reference)
    quote = row_to_quote(row)

    if not claim_pdf_generation("quote", quote.id):
        await send_whatsapp_text(
            sender,
            f"✅ Quote PDF {quote.quote_number} is already being generated "
            "or was already generated. No duplicate PDF was created.",
        )
        return

    if quote.status in {"cancelled", "converted"}:
        release_pdf_generation("quote", quote.id)
        await send_whatsapp_text(
            sender,
            f"Quote {quote.quote_number} cannot generate a PDF while "
            f"its status is {quote.status!r}.",
        )
        return

    await send_whatsapp_text(
        sender,
        f"⏳ Generating quote PDF {quote.quote_number}. Please wait…",
    )

    try:
        pdf_path = create_quote_pdf(quote)
        with db() as conn:
            conn.execute(
                """
                UPDATE quotes
                SET status = 'pdf_generated',
                    accepted_at = COALESCE(accepted_at, ?),
                    expired_at = NULL
                WHERE id = ?
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    quote.id,
                ),
            )
        await send_whatsapp_document(
            sender,
            pdf_path,
            (
                f"Quote ID: {quote.quote_number}\n"
                f"Customer: {quote.customer.name or 'Customer'}\n"
                f"Total: ${quote.total:,.2f}\n"
                f"Valid until: {quote.expiry_date}"
            ),
        )
    except Exception:
        release_pdf_generation("quote", quote.id)
        raise


async def handle_whatsapp_command(sender: str, incoming_text: str) -> bool:
    text = incoming_text.strip()

    match = re.fullmatch(r"INVOICE\s+PDF\s+([A-Z0-9-]+)", text, re.I)
    if match:
        await generate_invoice_pdf_for_whatsapp(sender, match.group(1))
        return True

    match = re.fullmatch(r"QUOTE\s+PDF\s+([A-Z0-9-]+)", text, re.I)
    if match:
        await generate_quote_pdf_for_whatsapp(sender, match.group(1))
        return True

    match = re.fullmatch(r"CANCEL\s+INVOICE\s+([A-Z0-9-]+)", text, re.I)
    if match:
        invoice = row_to_invoice(get_invoice_by_reference(match.group(1)))
        with db() as conn:
            conn.execute(
                "UPDATE invoices SET status = 'cancelled' WHERE id = ?",
                (invoice.id,),
            )
        await send_whatsapp_text(
            sender,
            f"✅ Invoice {invoice.invoice_number} cancelled.",
        )
        return True

    match = re.fullmatch(r"CANCEL\s+QUOTE\s+([A-Z0-9-]+)", text, re.I)
    if match:
        quote = row_to_quote(get_quote_by_reference(match.group(1)))
        with db() as conn:
            conn.execute(
                "UPDATE quotes SET status = 'cancelled' WHERE id = ?",
                (quote.id,),
            )
        await send_whatsapp_text(
            sender,
            f"✅ Quote {quote.quote_number} cancelled.",
        )
        return True

    match = re.fullmatch(r"CONVERT\s+QUOTE\s+([A-Z0-9-]+)", text, re.I)
    if match:
        quote = row_to_quote(get_quote_by_reference(match.group(1)))
        invoice = convert_quote_to_invoice(quote.id)
        await send_whatsapp_text(
            sender,
            invoice_summary(invoice, "INVOICE FROM QUOTE")
            + invoice_commands(invoice),
        )
        return True

    match = re.fullmatch(
        r"EDIT\s+INVOICE\s+([A-Z0-9-]+)\s*:\s*(.+)",
        text,
        re.I | re.S,
    )
    if match:
        invoice = row_to_invoice(get_invoice_by_reference(match.group(1)))
        instruction = match.group(2).strip()
        await send_whatsapp_text(
            sender,
            f"⏳ Updating invoice {invoice.invoice_number}. Please wait…",
        )
        parsed = await ai_edit(invoice, instruction)
        if parsed.clarification_needed:
            await send_whatsapp_text(
                sender,
                parsed.clarification_question
                or "Please clarify the requested invoice edit.",
            )
            return True
        updated = update_ai_invoice(invoice.id, parsed, instruction)
        await send_whatsapp_text(
            sender,
            invoice_summary(updated, "UPDATED INVOICE")
            + invoice_commands(updated),
        )
        return True

    match = re.fullmatch(
        r"EDIT\s+QUOTE\s+([A-Z0-9-]+)\s*:\s*(.+)",
        text,
        re.I | re.S,
    )
    if match:
        quote = row_to_quote(get_quote_by_reference(match.group(1)))
        instruction = match.group(2).strip()
        await send_whatsapp_text(
            sender,
            f"⏳ Updating quote {quote.quote_number}. Please wait…",
        )
        parsed = await ai_edit(quote_as_invoice(quote), instruction)
        if parsed.clarification_needed:
            await send_whatsapp_text(
                sender,
                parsed.clarification_question
                or "Please clarify the requested quote edit.",
            )
            return True
        updated = update_ai_quote(quote.id, parsed, instruction)
        await send_whatsapp_text(
            sender,
            quote_summary(updated, "UPDATED QUOTE")
            + quote_commands(updated),
        )
        return True

    return False


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
        message_id = str(message.get("id", "")).strip()

        if not sender:
            continue

        if message_id and not claim_whatsapp_message(message_id, sender):
            continue

        if message_type != "text":
            await send_whatsapp_text(
                sender,
                "Please send invoice or quote details as a text message.",
            )
            continue

        incoming_text = str(
            (message.get("text") or {}).get("body", "")
        ).strip()

        if not incoming_text:
            await send_whatsapp_text(
                sender,
                "Please send invoice or quote details as text.",
            )
            continue

        try:
            if await handle_whatsapp_command(sender, incoming_text):
                continue

            await send_whatsapp_text(
                sender,
                "⏳ Reading your message and preparing the draft…",
            )

            parsed = await ai_parse(incoming_text)

            if parsed.clarification_needed:
                await send_whatsapp_text(
                    sender,
                    parsed.clarification_question
                    or "Please provide the missing price or job detail.",
                )
                continue

            if re.match(
                r"^\s*(?:quote|quotation)\b",
                incoming_text,
                re.I,
            ):
                quote = create_ai_quote(incoming_text, parsed)
                await send_whatsapp_text(
                    sender,
                    quote_summary(quote) + quote_commands(quote),
                )
            else:
                invoice = create_ai_invoice(incoming_text, parsed)
                await send_whatsapp_text(
                    sender,
                    invoice_summary(invoice) + invoice_commands(invoice),
                )

        except Exception as exc:
            await send_whatsapp_text(
                sender,
                friendly_error_message(exc),
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

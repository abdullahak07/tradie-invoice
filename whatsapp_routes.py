from __future__ import annotations

import os
import re
from datetime import datetime

import httpx
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from invoice_routes import create_pdf, create_quote_pdf
from billing import (
    ActivationError,
    BillingError,
    NoCreditsError,
    RateLimitError,
    TrialExpiredError,
    activate_code,
    check_ai_rate_limit,
    consume_document_credit,
    format_account_status,
    refund_document_credit,
)
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
        consume_document_credit(
            "whatsapp",
            sender,
            "invoice",
            invoice.id,
        )
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
        refund_document_credit(
            "whatsapp",
            sender,
            "invoice",
            invoice.id,
        )
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
        consume_document_credit(
            "whatsapp",
            sender,
            "quote",
            quote.id,
        )
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
        refund_document_credit(
            "whatsapp",
            sender,
            "quote",
            quote.id,
        )
        release_pdf_generation("quote", quote.id)
        raise


async def handle_whatsapp_command(sender: str, incoming_text: str) -> bool:
    text = incoming_text.strip()
    upper = text.upper()

    if upper in {"CREDITS", "ACCOUNT"}:
        await send_whatsapp_text(
            sender,
            format_account_status("whatsapp", sender),
        )
        return True

    if upper.startswith("ACTIVATE "):
        code = text.split(" ", 1)[1].strip()
        try:
            status = activate_code("whatsapp", sender, code)
            await send_whatsapp_text(
                sender,
                f"✅ Paid plan activated.\n"
                f"Plan: {status['plan'].title()}\n"
                f"Credits: {status['credit_balance']} / "
                f"{status['credit_limit']}",
            )
        except ActivationError as exc:
            await send_whatsapp_text(sender, str(exc))
        return True

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
        check_ai_rate_limit("whatsapp", sender)
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
        check_ai_rate_limit("whatsapp", sender)
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




def ensure_whatsapp_clarification_table() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS whatsapp_clarifications (
                sender TEXT PRIMARY KEY,
                flow_type TEXT NOT NULL,
                original_text TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def save_whatsapp_clarification(sender: str, flow_type: str, original_text: str) -> None:
    ensure_whatsapp_clarification_table()
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_clarifications (
                sender, flow_type, original_text, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(sender) DO UPDATE SET
                flow_type = excluded.flow_type,
                original_text = excluded.original_text,
                updated_at = excluded.updated_at
            """,
            (sender, flow_type, original_text, now),
        )


def get_whatsapp_clarification(sender: str):
    ensure_whatsapp_clarification_table()
    with db() as conn:
        return conn.execute(
            "SELECT * FROM whatsapp_clarifications WHERE sender = ?",
            (sender,),
        ).fetchone()


def clear_whatsapp_clarification(sender: str) -> None:
    ensure_whatsapp_clarification_table()
    with db() as conn:
        conn.execute("DELETE FROM whatsapp_clarifications WHERE sender = ?", (sender,))


def looks_like_new_document_request(text: str) -> bool:
    value = text.strip()
    if re.search(
        r"\b(?:new|create|generate|make|prepare|write|draft)\b.{0,30}\b(?:invoice|quote|quotation)\b",
        value,
        re.I | re.S,
    ):
        return True
    has_customer = bool(re.search(r"\b(?:for|customer|client|bill\s+to)\s+[A-Za-z]", value, re.I))
    has_money = bool(re.search(r"\$\s*\d|\d[\d,]*(?:\.\d{1,2})?\s*\$", value))
    has_work = bool(re.search(r"\b(?:install|repair|replace|service|labour|labor|roofing|landscaping|plumbing|electrical|painting|cleaning|call[- ]?out)\b", value, re.I))
    return has_customer and has_money and has_work



def explicit_gst_confirmation(pending_text: str, incoming_text: str) -> float | None:
    """Return a confirmed GST/tax rate from a clarification reply."""
    if not re.search(r"\b(?:gst|tax|rate|percent|percentage)\b", pending_text, re.I):
        return None
    match = re.search(r"(?<!\d)(\d{1,2}(?:\.\d+)?)\s*(?:%|percent\b)", incoming_text, re.I)
    if not match:
        return None
    rate = float(match.group(1))
    return rate if 0 <= rate <= 100 else None


async def handle_pending_whatsapp_clarification(sender: str, incoming_text: str) -> bool:
    pending = get_whatsapp_clarification(sender)
    if not pending:
        return False

    if looks_like_new_document_request(incoming_text):
        clear_whatsapp_clarification(sender)
        return False

    original = str(pending["original_text"])
    flow_type = str(pending["flow_type"])
    confirmed_gst_rate = explicit_gst_confirmation(original, incoming_text)
    combined = f"{original}\nClarification: {incoming_text.strip()}"
    if confirmed_gst_rate is not None:
        combined += (
            f"\nFinal confirmed instruction: apply GST at {confirmed_gst_rate:g}%. "
            "The user has explicitly confirmed this rate. Do not ask for confirmation again."
        )

    await send_whatsapp_text(sender, "⏳ Applying your answer to the existing draft…")
    check_ai_rate_limit("whatsapp", sender)
    parsed = await ai_parse(combined)

    if confirmed_gst_rate is not None:
        parsed.gst_rate_percent = confirmed_gst_rate
        parsed.clarification_needed = False
        parsed.clarification_question = ""

    if parsed.clarification_needed:
        save_whatsapp_clarification(sender, flow_type, combined)
        await send_whatsapp_text(
            sender,
            parsed.clarification_question or "Please clarify the remaining detail.",
        )
        return True

    clear_whatsapp_clarification(sender)
    if flow_type == "quote":
        quote = create_ai_quote(combined, parsed)
        await send_whatsapp_text(sender, quote_summary(quote))
        await send_quote_action_list(sender, quote)
    else:
        invoice = create_ai_invoice(combined, parsed)
        await send_whatsapp_text(sender, invoice_summary(invoice))
        await send_invoice_action_buttons(sender, invoice)
    return True


def ensure_whatsapp_session_table() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS whatsapp_sessions (
                sender TEXT PRIMARY KEY,
                document_type TEXT NOT NULL,
                document_id INTEGER NOT NULL,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def save_whatsapp_session(
    sender: str,
    document_type: str,
    document_id: int,
    state: str,
) -> None:
    ensure_whatsapp_session_table()
    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        conn.execute(
            """
            INSERT INTO whatsapp_sessions (
                sender,
                document_type,
                document_id,
                state,
                updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sender) DO UPDATE SET
                document_type = excluded.document_type,
                document_id = excluded.document_id,
                state = excluded.state,
                updated_at = excluded.updated_at
            """,
            (sender, document_type, document_id, state, now),
        )


def get_whatsapp_session(sender: str):
    ensure_whatsapp_session_table()
    with db() as conn:
        return conn.execute(
            "SELECT * FROM whatsapp_sessions WHERE sender = ?",
            (sender,),
        ).fetchone()


def clear_whatsapp_session(sender: str) -> None:
    ensure_whatsapp_session_table()
    with db() as conn:
        conn.execute(
            "DELETE FROM whatsapp_sessions WHERE sender = ?",
            (sender,),
        )


async def send_whatsapp_interactive(
    to: str,
    interactive: dict[str, Any],
) -> None:
    url = (
        f"https://graph.facebook.com/{api_version()}/"
        f"{phone_number_id()}/messages"
    )
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": interactive,
    }

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {access_token()}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"WhatsApp interactive send failed ({response.status_code}): "
            f"{response.text[:500]}"
        )


async def send_invoice_action_buttons(sender: str, invoice) -> None:
    await send_whatsapp_interactive(
        sender,
        {
            "type": "button",
            "body": {
                "text": (
                    f"Invoice {invoice.invoice_number}\n"
                    "Choose an action:"
                )
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"invoice_pdf:{invoice.invoice_number}",
                            "title": "Generate PDF",
                        },
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"invoice_edit:{invoice.invoice_number}",
                            "title": "Edit",
                        },
                    },
                    {
                        "type": "reply",
                        "reply": {
                            "id": f"invoice_cancel:{invoice.invoice_number}",
                            "title": "Cancel",
                        },
                    },
                ]
            },
        },
    )


async def send_quote_action_list(sender: str, quote) -> None:
    await send_whatsapp_interactive(
        sender,
        {
            "type": "list",
            "header": {"type": "text", "text": "Quote actions"},
            "body": {
                "text": (
                    f"Quote {quote.quote_number}\n"
                    "Choose an action:"
                )
            },
            "action": {
                "button": "Choose action",
                "sections": [
                    {
                        "title": "Available actions",
                        "rows": [
                            {
                                "id": f"quote_pdf:{quote.quote_number}",
                                "title": "Generate quote PDF",
                            },
                            {
                                "id": f"quote_edit:{quote.quote_number}",
                                "title": "Edit quote",
                            },
                            {
                                "id": f"quote_convert:{quote.quote_number}",
                                "title": "Convert to invoice",
                            },
                            {
                                "id": f"quote_cancel:{quote.quote_number}",
                                "title": "Cancel quote",
                            },
                        ],
                    }
                ],
            },
        },
    )


def extract_interactive_action(message: dict[str, Any]) -> str:
    if str(message.get("type", "")) != "interactive":
        return ""

    interactive = message.get("interactive") or {}
    interactive_type = str(interactive.get("type", ""))

    if interactive_type == "button_reply":
        return str(
            (interactive.get("button_reply") or {}).get("id", "")
        ).strip()

    if interactive_type == "list_reply":
        return str(
            (interactive.get("list_reply") or {}).get("id", "")
        ).strip()

    return ""


async def handle_whatsapp_action(
    sender: str,
    action_id: str,
) -> bool:
    if ":" not in action_id:
        return False

    action, reference = action_id.split(":", 1)
    action = action.strip().lower()
    reference = reference.strip()

    if action == "invoice_pdf":
        await generate_invoice_pdf_for_whatsapp(sender, reference)
        return True

    if action == "invoice_edit":
        invoice = row_to_invoice(get_invoice_by_reference(reference))
        save_whatsapp_session(
            sender,
            "invoice",
            invoice.id,
            "awaiting_edit",
        )
        await send_whatsapp_text(
            sender,
            f"✏️ Tell me the changes for invoice "
            f"{invoice.invoice_number}.",
        )
        return True

    if action == "invoice_cancel":
        invoice = row_to_invoice(get_invoice_by_reference(reference))
        with db() as conn:
            conn.execute(
                "UPDATE invoices SET status = 'cancelled' WHERE id = ?",
                (invoice.id,),
            )
        clear_whatsapp_session(sender)
        await send_whatsapp_text(
            sender,
            f"✅ Invoice {invoice.invoice_number} cancelled.",
        )
        return True

    if action == "quote_pdf":
        await generate_quote_pdf_for_whatsapp(sender, reference)
        return True

    if action == "quote_edit":
        quote = row_to_quote(get_quote_by_reference(reference))
        save_whatsapp_session(
            sender,
            "quote",
            quote.id,
            "awaiting_edit",
        )
        await send_whatsapp_text(
            sender,
            f"✏️ Tell me the changes for quote {quote.quote_number}.",
        )
        return True

    if action == "quote_convert":
        quote = row_to_quote(get_quote_by_reference(reference))
        invoice = convert_quote_to_invoice(quote.id)
        clear_whatsapp_session(sender)
        await send_whatsapp_text(
            sender,
            invoice_summary(invoice, "INVOICE FROM QUOTE"),
        )
        await send_invoice_action_buttons(sender, invoice)
        return True

    if action == "quote_cancel":
        quote = row_to_quote(get_quote_by_reference(reference))
        with db() as conn:
            conn.execute(
                "UPDATE quotes SET status = 'cancelled' WHERE id = ?",
                (quote.id,),
            )
        clear_whatsapp_session(sender)
        await send_whatsapp_text(
            sender,
            f"✅ Quote {quote.quote_number} cancelled.",
        )
        return True

    return False


async def handle_pending_whatsapp_edit(
    sender: str,
    incoming_text: str,
) -> bool:
    session = get_whatsapp_session(sender)
    if not session or str(session["state"]) != "awaiting_edit":
        return False

    document_type = str(session["document_type"])
    document_id = int(session["document_id"])
    instruction = incoming_text.strip()

    if not instruction:
        await send_whatsapp_text(
            sender,
            "Please tell me what you want to change.",
        )
        return True

    if document_type == "invoice":
        invoice = row_to_invoice(get_invoice(document_id))
        await send_whatsapp_text(
            sender,
            f"⏳ Updating invoice {invoice.invoice_number}. Please wait…",
        )
        check_ai_rate_limit("whatsapp", sender)
        parsed = await ai_edit(invoice, instruction)
        if parsed.clarification_needed:
            await send_whatsapp_text(
                sender,
                parsed.clarification_question
                or "Please clarify the requested invoice edit.",
            )
            return True

        updated = update_ai_invoice(
            invoice.id,
            parsed,
            instruction,
        )
        clear_whatsapp_session(sender)
        await send_whatsapp_text(
            sender,
            invoice_summary(updated, "UPDATED INVOICE"),
        )
        await send_invoice_action_buttons(sender, updated)
        return True

    if document_type == "quote":
        quote = row_to_quote(get_quote(document_id))
        await send_whatsapp_text(
            sender,
            f"⏳ Updating quote {quote.quote_number}. Please wait…",
        )
        check_ai_rate_limit("whatsapp", sender)
        parsed = await ai_edit(
            quote_as_invoice(quote),
            instruction,
        )
        if parsed.clarification_needed:
            await send_whatsapp_text(
                sender,
                parsed.clarification_question
                or "Please clarify the requested quote edit.",
            )
            return True

        updated = update_ai_quote(
            quote.id,
            parsed,
            instruction,
        )
        clear_whatsapp_session(sender)
        await send_whatsapp_text(
            sender,
            quote_summary(updated, "UPDATED QUOTE"),
        )
        await send_quote_action_list(sender, updated)
        return True

    clear_whatsapp_session(sender)
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

        action_id = extract_interactive_action(message)
        if action_id:
            try:
                await handle_whatsapp_action(sender, action_id)
            except Exception as exc:
                await send_whatsapp_text(sender, friendly_error_message(exc))
            continue

        if message_type != "text":
            await send_whatsapp_text(
                sender,
                "Please send invoice or quote details as text.",
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
            if await handle_pending_whatsapp_clarification(
                sender, incoming_text
            ):
                continue

            if await handle_pending_whatsapp_edit(sender, incoming_text):
                continue

            if await handle_whatsapp_command(sender, incoming_text):
                continue

            await send_whatsapp_text(
                sender,
                "⏳ Reading your message and preparing the draft…",
            )

            check_ai_rate_limit("whatsapp", sender)
            parsed = await ai_parse(incoming_text)

            if parsed.clarification_needed:
                flow_type = (
                    "quote"
                    if re.match(r"^\s*(?:quote|quotation)\b", incoming_text, re.I)
                    else "invoice"
                )
                save_whatsapp_clarification(sender, flow_type, incoming_text)
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
                    quote_summary(quote),
                )
                await send_quote_action_list(sender, quote)
            else:
                invoice = create_ai_invoice(incoming_text, parsed)
                await send_whatsapp_text(
                    sender,
                    invoice_summary(invoice),
                )
                await send_invoice_action_buttons(sender, invoice)

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

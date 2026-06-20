from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from google import genai
from pydantic import BaseModel, Field

from db_backend import using_postgres

from invoice_routes import (
    CustomerData,
    DEFAULT_GST_RATE,
    InvoiceDraft,
    InvoiceItem,
    create_pdf,
    db,
    get_invoice,
    row_to_invoice,
    send_email,
)

router = APIRouter()


class AIItem(BaseModel):
    description: str
    quantity: float = Field(default=1, ge=0)
    unit: str = "each"
    unit_price: float = Field(default=0, ge=0)


class AIInvoice(BaseModel):
    customer_name: str = ""
    customer_phone: str = ""
    customer_email: str = ""
    customer_address: str = ""
    items: list[AIItem] = []
    due_date: str = ""
    due_in_days: int | None = None
    notes: str = ""
    gst_included: bool = False
    discount_percent: float = Field(default=0, ge=0, le=100)
    clarification_needed: bool = False
    clarification_question: str = ""


def init_telegram_tables() -> None:
    if using_postgres():
        return

    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS telegram_sessions (
                chat_id TEXT PRIMARY KEY,
                invoice_id INTEGER,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS telegram_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                invoice_id INTEGER,
                direction TEXT NOT NULL,
                body TEXT NOT NULL,
                telegram_message_id TEXT,
                created_at TEXT NOT NULL
            );
            """
        )

        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(telegram_sessions)").fetchall()
        }
        if "pending_text" not in cols:
            conn.execute(
                "ALTER TABLE telegram_sessions ADD COLUMN pending_text TEXT NOT NULL DEFAULT ''"
            )


init_telegram_tables()


def telegram_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    return token


def gemini_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    return key


def allowed_chat(chat_id: str) -> bool:
    raw = os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
    if not raw:
        return True
    return chat_id in {x.strip() for x in raw.split(",") if x.strip()}


def log_message(
    chat_id: str,
    direction: str,
    body: str,
    invoice_id: int | None = None,
    telegram_message_id: str | None = None,
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO telegram_messages (
                chat_id, invoice_id, direction, body,
                telegram_message_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                chat_id,
                invoice_id,
                direction,
                body,
                telegram_message_id,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )


def action_keyboard(invoice_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "✅ SEND", "callback_data": f"send:{invoice_id}"},
            {"text": "✏️ EDIT", "callback_data": f"edit:{invoice_id}"},
            {"text": "❌ CANCEL", "callback_data": f"cancel:{invoice_id}"},
        ]]
    }


def paid_keyboard(invoice_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "💰 MARK PAID", "callback_data": f"paid:{invoice_id}"}
        ]]
    }


async def telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"https://api.telegram.org/bot{telegram_token()}/{method}",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram API failed"))
        return data


async def send_telegram(
    chat_id: str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4000],
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    await telegram_api("sendMessage", payload)
    log_message(chat_id, "outgoing", text)


async def answer_callback(callback_id: str, text: str = "") -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:180]

    try:
        await telegram_api("answerCallbackQuery", payload)
    except httpx.HTTPStatusError as exc:
        # Telegram returns 400 when a callback query is expired,
        # already answered, or retried. This must not stop invoice sending.
        if exc.response.status_code == 400:
            return
        raise


def get_session(chat_id: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM telegram_sessions WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()


def save_session(
    chat_id: str,
    invoice_id: int | None,
    state: str,
    pending_text: str = "",
) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO telegram_sessions (
                chat_id, invoice_id, state, updated_at, pending_text
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                invoice_id = excluded.invoice_id,
                state = excluded.state,
                updated_at = excluded.updated_at,
                pending_text = excluded.pending_text
            """,
            (
                chat_id,
                invoice_id,
                state,
                datetime.now().isoformat(timespec="seconds"),
                pending_text,
            ),
        )


def current_discount_percent(invoice: InvoiceDraft) -> float:
    for item in invoice.items:
        desc = item.description.lower()
        if "discount" in desc and item.unit_price < 0:
            import re
            match = re.search(r"(\d+(?:\.\d+)?)\s*%", item.description)
            if match:
                return float(match.group(1))
    return 0.0


def base_items(invoice: InvoiceDraft) -> list[dict[str, Any]]:
    result = []
    for item in invoice.items:
        if "discount" in item.description.lower() and item.unit_price < 0:
            continue
        result.append(
            {
                "description": item.description,
                "quantity": item.quantity,
                "unit_price": item.unit_price,
            }
        )
    return result


def generate_gemini_invoice(client, model: str, prompt: str) -> AIInvoice:
    """Call Gemini with retries and an optional fallback model."""
    retry_delays = (1, 2, 4)
    fallback_model = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
    models = [model]
    if fallback_model and fallback_model != model:
        models.append(fallback_model)

    last_error: Exception | None = None

    for candidate_model in models:
        for attempt, delay in enumerate(retry_delays, start=1):
            try:
                response = client.models.generate_content(
                    model=candidate_model,
                    contents=prompt,
                    config={
                        "response_mime_type": "application/json",
                        "response_schema": AIInvoice,
                        "temperature": 0,
                    },
                )
                if response.parsed:
                    return response.parsed
                if response.text:
                    return AIInvoice.model_validate_json(response.text)
                raise RuntimeError("Gemini returned an empty response")

            except Exception as exc:
                last_error = exc
                message = str(exc).lower()

                # Do not retry permanent configuration/authentication failures.
                permanent_error = any(
                    token in message
                    for token in (
                        "api key not valid",
                        "invalid api key",
                        "permission denied",
                        "authentication",
                        "unauthorized",
                        "400 invalid argument",
                    )
                )
                if permanent_error:
                    raise

                if attempt < len(retry_delays):
                    time.sleep(delay)

    raise RuntimeError(
        "Gemini is temporarily unavailable after automatic retries"
    ) from last_error


def ai_parse_sync(message: str) -> AIInvoice:
    client = genai.Client(api_key=gemini_key())
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    prompt = f"""
You are a highly practical Australian tradie invoice assistant.

Extract the message into structured invoice data.

Important behaviour:
- Understand free-form, messy, abbreviated, misspelled text.
- Correct obvious spelling such as "Leb bulb" to "LED bulb".
- Infer an unlabelled name, email, phone, suburb/address regardless of order.
- "LED bulb 6 @ 90$" means quantity 6 at $90 each.
- "10m wire @ 60 per metre" means quantity 10, unit metre, unit price $60.
- "call out fee 90$" means quantity 1 at $90.
- "20% discount on complete invoice" means discount_percent = 20.
- Do not include the discount as an item; use discount_percent.
- Do not calculate totals or GST.
- Do not invent missing prices.
- Ask for clarification only when the invoice amount genuinely cannot be determined.
- Do not ask clarification for spelling, formatting, word order, or obvious shorthand.
- Prefer the most commercially natural interpretation for a tradie invoice.

Message:
---BEGIN---
{message}
---END---
"""

    return generate_gemini_invoice(client, model, prompt)


def ai_edit_sync(
    invoice: InvoiceDraft,
    instruction: str,
    prior_instruction: str = "",
) -> AIInvoice:
    client = genai.Client(api_key=gemini_key())
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

    current = {
        "customer_name": invoice.customer.name,
        "customer_phone": invoice.customer.phone,
        "customer_email": invoice.customer.email,
        "customer_address": invoice.customer.address,
        "items": base_items(invoice),
        "due_date": invoice.due_date,
        "notes": invoice.notes,
        "gst_included": invoice.gst_included,
        "discount_percent": current_discount_percent(invoice),
    }

    combined_instruction = instruction
    if prior_instruction:
        combined_instruction = (
            f"Original edit request:\n{prior_instruction}\n\n"
            f"User clarification:\n{instruction}"
        )

    prompt = f"""
You are editing an existing Australian tradie invoice.

Return the COMPLETE updated invoice.

Rules:
- Preserve all existing fields unless explicitly changed.
- Apply every requested change, including multiple changes in one message.
- Never forget earlier parts of the request when the user later clarifies one part.
- "bulb quan is 10" means set bulb quantity to 10.
- "add $60 more into wire" means increase the existing wire UNIT PRICE by $60.
- "increase wire by $60" means increase wire unit price by $60.
- "add another 5m wire" means increase wire quantity by 5.
- "20% disc on complete invoice" means discount_percent = 20.
- "remove discount" means discount_percent = 0.
- "remove call out" means delete that item.
- "add labour 2 hours at $110" means add quantity 2 at unit price $110.
- Do not calculate totals, GST, or discount amount.
- Ask for clarification only if no commercially reasonable interpretation exists.
- Do not reject a request merely because the previous schema did not support it.

Current invoice:
{json.dumps(current, ensure_ascii=False)}

Requested edit:
{combined_instruction}
"""

    return generate_gemini_invoice(client, model, prompt)


async def ai_parse(message: str) -> AIInvoice:
    return await asyncio.to_thread(ai_parse_sync, message)


async def ai_edit(
    invoice: InvoiceDraft,
    instruction: str,
    prior_instruction: str = "",
) -> AIInvoice:
    return await asyncio.to_thread(
        ai_edit_sync,
        invoice,
        instruction,
        prior_instruction,
    )


def resolve_due_date(parsed: AIInvoice, existing: str | None = None) -> str:
    if parsed.due_date:
        try:
            return date.fromisoformat(parsed.due_date).isoformat()
        except ValueError:
            pass
    if parsed.due_in_days is not None:
        return (date.today() + timedelta(days=max(parsed.due_in_days, 0))).isoformat()
    return existing or (date.today() + timedelta(days=7)).isoformat()


def convert_items(parsed: AIInvoice) -> list[InvoiceItem]:
    items: list[InvoiceItem] = []
    for item in parsed.items:
        quantity = round(max(float(item.quantity), 0), 4)
        unit_price = round(max(float(item.unit_price), 0), 4)
        if quantity <= 0:
            continue
        description = item.description.strip() or "Service item"
        unit = item.unit.strip().lower()
        if unit and unit not in {"each", "item", "unit"}:
            suffix = f"({unit})"
            if suffix.lower() not in description.lower():
                description = f"{description} {suffix}"
        items.append(
            InvoiceItem(
                description=description[:120],
                quantity=quantity,
                unit_price=unit_price,
                line_total=round(quantity * unit_price, 2),
            )
        )

    if not items:
        return items

    discount_percent = round(max(min(parsed.discount_percent, 100), 0), 2)
    if discount_percent > 0:
        base_subtotal = round(sum(x.line_total for x in items), 2)
        discount_amount = round(base_subtotal * discount_percent / 100, 2)
        items.append(
            InvoiceItem(
                description=f"Discount ({discount_percent:g}%)",
                quantity=1,
                unit_price=-discount_amount,
                line_total=-discount_amount,
            )
        )
    return items


def calculate_totals(
    items: list[InvoiceItem],
    gst_included: bool,
) -> tuple[float, float, float]:
    raw = round(sum(item.line_total for item in items), 2)
    if gst_included:
        total = raw
        subtotal = round(total / (1 + DEFAULT_GST_RATE), 2)
        gst = round(total - subtotal, 2)
    else:
        subtotal = raw
        gst = round(subtotal * DEFAULT_GST_RATE, 2)
        total = round(subtotal + gst, 2)
    return subtotal, gst, total


def create_ai_invoice(source_message: str, parsed: AIInvoice) -> InvoiceDraft:
    items = convert_items(parsed)
    if not items:
        raise ValueError("No valid priced invoice items were found.")

    customer = CustomerData(
        name=parsed.customer_name.strip(),
        phone=parsed.customer_phone.strip(),
        email=parsed.customer_email.strip(),
        address=parsed.customer_address.strip(),
    )
    subtotal, gst, total = calculate_totals(items, parsed.gst_included)
    delivery = []
    if customer.email:
        delivery.append("email")
    if customer.phone:
        delivery.append("sms")

    now = datetime.now().isoformat(timespec="seconds")
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO invoices (
                invoice_number, source_message, customer_json, items_json, notes,
                due_date, subtotal, gst, total, gst_included, status,
                delivery_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_confirmation', ?, ?)
            """,
            (
                "PENDING",
                source_message,
                customer.model_dump_json(),
                json.dumps([x.model_dump() for x in items]),
                parsed.notes.strip(),
                resolve_due_date(parsed),
                subtotal,
                gst,
                total,
                bool(parsed.gst_included),
                json.dumps(delivery),
                now,
            ),
        )
        invoice_id = int(cur.lastrowid)
        number = f"INV-{date.today():%Y%m%d}-{invoice_id:04d}"
        conn.execute(
            "UPDATE invoices SET invoice_number = ? WHERE id = ?",
            (number, invoice_id),
        )
        row = conn.execute(
            "SELECT * FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
    return row_to_invoice(row)


def update_ai_invoice(
    invoice_id: int,
    parsed: AIInvoice,
    edit_instruction: str,
) -> InvoiceDraft:
    existing = row_to_invoice(get_invoice(invoice_id))
    items = convert_items(parsed)
    if not items:
        raise ValueError("The edited invoice has no valid priced items.")

    customer = CustomerData(
        name=parsed.customer_name.strip(),
        phone=parsed.customer_phone.strip(),
        email=parsed.customer_email.strip(),
        address=parsed.customer_address.strip(),
    )
    subtotal, gst, total = calculate_totals(items, parsed.gst_included)
    delivery = []
    if customer.email:
        delivery.append("email")
    if customer.phone:
        delivery.append("sms")

    with db() as conn:
        conn.execute(
            """
            UPDATE invoices
            SET customer_json = ?, items_json = ?, notes = ?, due_date = ?,
                subtotal = ?, gst = ?, total = ?, gst_included = ?,
                delivery_json = ?, status = 'awaiting_confirmation',
                pdf_path = NULL,
                source_message = source_message || ?
            WHERE id = ?
            """,
            (
                customer.model_dump_json(),
                json.dumps([x.model_dump() for x in items]),
                parsed.notes.strip(),
                resolve_due_date(parsed, existing.due_date),
                subtotal,
                gst,
                total,
                bool(parsed.gst_included),
                json.dumps(delivery),
                f"\n\nEDIT: {edit_instruction}",
                invoice_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()
    return row_to_invoice(row)


def invoice_status_list(status_group: str) -> list[InvoiceDraft]:
    where = ""
    params: tuple[Any, ...] = ()

    if status_group == "unpaid":
        where = "WHERE status IN ('sent', 'overdue', 'approved_demo')"
    elif status_group == "overdue":
        where = "WHERE status = ?"
        params = ("overdue",)
    elif status_group == "paid":
        where = "WHERE status = ?"
        params = ("paid",)

    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM invoices {where} ORDER BY id DESC LIMIT 10",
            params,
        ).fetchall()

    return [row_to_invoice(row) for row in rows]


def format_invoice_status_list(
    invoices: list[InvoiceDraft],
    heading: str,
) -> str:
    if not invoices:
        return f"{heading}\n\nNo matching invoices found."

    lines = [heading, ""]
    for invoice in invoices:
        customer = invoice.customer.name or "Customer"
        lines.append(
            f"{invoice.invoice_number} | {customer} | "
            f"${invoice.total:,.2f} | {invoice.status.upper()} | "
            f"Due {invoice.due_date}"
        )
    return "\n".join(lines)


async def mark_invoice_paid(invoice_id: int, chat_id: str) -> None:
    paid_at = datetime.now().isoformat(timespec="seconds")

    with db() as conn:
        paid_row = conn.execute(
            """
            UPDATE invoices
            SET status = 'paid', paid_at = ?
            WHERE id = ?
              AND status IN ('sent', 'overdue', 'approved_demo')
            RETURNING *
            """,
            (paid_at, invoice_id),
        ).fetchone()

    if paid_row is not None:
        invoice = row_to_invoice(paid_row)
        save_session(chat_id, invoice_id, "paid")
        await send_telegram(
            chat_id,
            f"✅ Invoice {invoice.invoice_number} marked PAID.\n"
            f"Customer: {invoice.customer.name or 'Customer'}\n"
            f"Amount: ${invoice.total:,.2f}\n"
            f"Paid: {paid_at[:10]}\n\n"
            "Automatic reminders are now stopped.",
        )
        return

    current = row_to_invoice(get_invoice(invoice_id))
    if current.status == "paid":
        save_session(chat_id, invoice_id, "paid")
        await send_telegram(
            chat_id,
            f"Invoice {current.invoice_number} is already marked paid.",
        )
    else:
        await send_telegram(
            chat_id,
            f"Invoice {current.invoice_number} cannot be marked paid "
            f"while its status is {current.status!r}.",
        )


def invoice_summary(invoice: InvoiceDraft, heading: str = "DRAFT") -> str:
    lines = [
        f"{heading} {invoice.invoice_number}",
        "",
        f"Customer: {invoice.customer.name or 'Not detected'}",
        f"Phone: {invoice.customer.phone or 'Not detected'}",
        f"Email: {invoice.customer.email or 'Not detected'}",
        f"Address: {invoice.customer.address or 'Not detected'}",
        "",
    ]
    for item in invoice.items[:15]:
        lines.append(
            f"{item.quantity:g} × {item.description} @ "
            f"${item.unit_price:,.2f} = ${item.line_total:,.2f}"
        )
    lines.extend(
        [
            "",
            f"Subtotal: ${invoice.subtotal:,.2f}",
            f"GST: ${invoice.gst:,.2f}",
            f"TOTAL: ${invoice.total:,.2f}",
            f"Due: {invoice.due_date}",
            "",
            "Choose an action below.",
        ]
    )
    return "\n".join(lines)


async def send_invoice(invoice_id: int, chat_id: str, request: Request) -> None:
    # Atomically claim the invoice so rapid double taps cannot send twice.
    with db() as conn:
        claimed_row = conn.execute(
            """
            UPDATE invoices
            SET status = 'sending'
            WHERE id = ?
              AND status NOT IN ('sent', 'sending', 'cancelled')
            RETURNING *
            """,
            (invoice_id,),
        ).fetchone()

    if claimed_row is None:
        current = row_to_invoice(get_invoice(invoice_id))

        if current.status == "sent":
            save_session(chat_id, invoice_id, "sent")
            await send_telegram(
                chat_id,
                f"✅ Invoice {current.invoice_number} was already sent. "
                "No duplicate email was sent.",
            )
            return

        if current.status == "sending":
            await send_telegram(
                chat_id,
                "⏳ This invoice is already being sent. Please wait.",
            )
            return

        if current.status == "cancelled":
            await send_telegram(chat_id, "That invoice was cancelled.")
            return

        await send_telegram(
            chat_id,
            f"Invoice cannot be sent while its status is {current.status!r}.",
        )
        return

    invoice = row_to_invoice(claimed_row)

    try:
        pdf_path = create_pdf(claimed_row)
        email_ok, email_result = send_email(invoice, pdf_path)

        public_base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
        if not public_base:
            public_base = str(request.base_url).rstrip("/")
        pdf_url = f"{public_base}/invoices/{invoice_id}/pdf"

        new_status = "sent" if email_ok else "approved_demo"
        with db() as conn:
            conn.execute(
                "UPDATE invoices SET status = ?, sent_at = ? WHERE id = ?",
                (
                    new_status,
                    datetime.now().isoformat(timespec="seconds"),
                    invoice_id,
                ),
            )

        save_session(chat_id, invoice_id, new_status)

        if email_ok:
            message = (
                f"✅ Invoice {invoice.invoice_number} sent successfully.\n"
                f"Customer: {invoice.customer.name or 'Customer'}\n"
                f"Total: ${invoice.total:,.2f}\n"
                f"Invoice: {pdf_url}"
            )
        else:
            message = (
                "Invoice saved but email was not sent.\n"
                f"Reason: {email_result}\n"
                f"Invoice: {pdf_url}"
            )

        await send_telegram(
            chat_id,
            message,
            paid_keyboard(invoice_id) if email_ok else None,
        )

    except Exception:
        with db() as conn:
            conn.execute(
                """
                UPDATE invoices
                SET status = 'awaiting_confirmation'
                WHERE id = ? AND status = 'sending'
                """,
                (invoice_id,),
            )
        save_session(chat_id, invoice_id, "awaiting_confirmation")
        raise

async def handle_callback(
    update: dict[str, Any],
    request: Request,
) -> dict[str, bool]:
    callback = update["callback_query"]
    callback_id = str(callback.get("id", ""))
    data = str(callback.get("data", ""))
    message = callback.get("message") or {}
    chat_id = str((message.get("chat") or {}).get("id", ""))

    if not chat_id or ":" not in data:
        if callback_id:
            await answer_callback(callback_id)
        return {"ok": True}

    action, raw_id = data.split(":", 1)
    try:
        invoice_id = int(raw_id)
    except ValueError:
        await answer_callback(callback_id, "Invalid invoice")
        return {"ok": True}

    if action == "send":
        await answer_callback(callback_id, "Sending…")
        await send_invoice(invoice_id, chat_id, request)
    elif action == "edit":
        save_session(chat_id, invoice_id, "awaiting_edit")
        await answer_callback(callback_id, "Edit mode")
        await send_telegram(
            chat_id,
            "✏️ Tell me all changes in normal words.\n\n"
            "Examples:\n"
            "• Bulb quantity is 10\n"
            "• Add $60 to wire price\n"
            "• Give 20% discount on complete invoice\n"
            "• Remove call-out\n"
            "• Add 2 hours labour at $110 per hour",
        )
    elif action == "paid":
        await answer_callback(callback_id, "Marking paid…")
        await mark_invoice_paid(invoice_id, chat_id)
    elif action == "cancel":
        with db() as conn:
            conn.execute(
                "UPDATE invoices SET status = 'cancelled' WHERE id = ?",
                (invoice_id,),
            )
        save_session(chat_id, invoice_id, "cancelled")
        await answer_callback(callback_id, "Cancelled")
        await send_telegram(chat_id, "❌ Invoice cancelled. Nothing was sent.")
    else:
        await answer_callback(callback_id)

    return {"ok": True}


@router.post("/webhooks/telegram")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    expected_secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if expected_secret:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if received != expected_secret:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update = await request.json()

    if update.get("callback_query"):
        return await handle_callback(update, request)

    message = update.get("message") or update.get("edited_message")
    if not message or not message.get("text"):
        return {"ok": True}

    chat_id = str((message.get("chat") or {}).get("id", ""))
    text = str(message.get("text", "")).strip()
    message_id = str(message.get("message_id", ""))

    if not chat_id:
        return {"ok": True}

    log_message(chat_id, "incoming", text, telegram_message_id=message_id)

    if not allowed_chat(chat_id):
        await send_telegram(chat_id, "This Telegram account is not authorised.")
        return {"ok": True}

    session = get_session(chat_id)
    command = text.upper()

    if command in {"/START", "/HELP"}:
        await send_telegram(
            chat_id,
            "Send customer details and invoice items in any natural format.\n\n"
            "I will extract the invoice, calculate totals, and show buttons "
            "for SEND, EDIT, or CANCEL.\n\n"
            "Commands:\n"
            "/invoices - latest invoices\n"
            "/unpaid - unpaid invoices\n"
            "/overdue - overdue invoices\n"
            "/paid - paid invoices\n\n"
            "After sending an invoice, use the MARK PAID button when payment arrives.",
        )
        return {"ok": True}

    list_commands = {
        "/INVOICES": ("all", "LATEST INVOICES"),
        "/UNPAID": ("unpaid", "UNPAID INVOICES"),
        "/OVERDUE": ("overdue", "OVERDUE INVOICES"),
        "/PAID": ("paid", "PAID INVOICES"),
    }
    if command in list_commands:
        status_group, heading = list_commands[command]
        invoices = invoice_status_list(status_group)
        await send_telegram(
            chat_id,
            format_invoice_status_list(invoices, heading),
        )
        return {"ok": True}

    if command in {"PAID", "MARK PAID"} and session and session["invoice_id"]:
        await mark_invoice_paid(int(session["invoice_id"]), chat_id)
        return {"ok": True}

    if command in {"SEND", "EDIT", "CANCEL"} and session and session["invoice_id"]:
        invoice_id = int(session["invoice_id"])
        if command == "SEND":
            await send_invoice(invoice_id, chat_id, request)
        elif command == "EDIT":
            save_session(chat_id, invoice_id, "awaiting_edit")
            await send_telegram(chat_id, "Tell me all requested changes.")
        else:
            with db() as conn:
                conn.execute(
                    "UPDATE invoices SET status = 'cancelled' WHERE id = ?",
                    (invoice_id,),
                )
            save_session(chat_id, invoice_id, "cancelled")
            await send_telegram(chat_id, "Invoice cancelled.")
        return {"ok": True}

    try:
        if session and session["invoice_id"] and session["state"] in {
            "awaiting_edit",
            "awaiting_edit_clarification",
        }:
            invoice_id = int(session["invoice_id"])
            current = row_to_invoice(get_invoice(invoice_id))
            prior = (
                session["pending_text"]
                if session["state"] == "awaiting_edit_clarification"
                else ""
            )
            parsed = await ai_edit(current, text, prior)

            if parsed.clarification_needed:
                original = prior or text
                save_session(
                    chat_id,
                    invoice_id,
                    "awaiting_edit_clarification",
                    pending_text=original,
                )
                await send_telegram(
                    chat_id,
                    parsed.clarification_question
                    or "Please clarify that requested edit.",
                )
                return {"ok": True}

            full_edit_text = text if not prior else f"{prior}\nClarification: {text}"
            invoice = update_ai_invoice(invoice_id, parsed, full_edit_text)
            save_session(chat_id, invoice.id, "awaiting_confirmation")
            await send_telegram(
                chat_id,
                invoice_summary(invoice, "UPDATED DRAFT"),
                action_keyboard(invoice.id),
            )
            return {"ok": True}

        if session and session["state"] == "awaiting_clarification":
            pending = session["pending_text"] or ""
            combined = f"{pending}\nClarification: {text}"
            parsed = await ai_parse(combined)
        else:
            combined = text
            parsed = await ai_parse(text)

        if parsed.clarification_needed:
            save_session(
                chat_id,
                None,
                "awaiting_clarification",
                pending_text=combined,
            )
            await send_telegram(
                chat_id,
                parsed.clarification_question
                or "Please clarify the missing quantity or price.",
            )
            return {"ok": True}

        invoice = create_ai_invoice(combined, parsed)
        save_session(chat_id, invoice.id, "awaiting_confirmation")
        await send_telegram(
            chat_id,
            invoice_summary(invoice),
            action_keyboard(invoice.id),
        )
        return {"ok": True}

    except Exception as exc:
        await send_telegram(
            chat_id,
            "I could not process that safely.\n\n"
            f"Error: {str(exc)[:350]}",
        )
        return {"ok": True}

from __future__ import annotations

import asyncio
import json
import os
import re
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
    create_quote_pdf,
    db,
    get_invoice,
    row_to_invoice,
    send_email,
    send_quote_email,
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


def friendly_error_message(exc: Exception) -> str:
    raw = str(exc).strip()
    lowered = raw.lower()

    if "no valid priced invoice items" in lowered or "no valid priced items" in lowered:
        return (
            "I could not find a valid item with a price.\n\n"
            "Try something like:\n"
            "Invoice John Smith, callout fee $90 and replace tap $180"
        )

    if "customer" in lowered and any(
        word in lowered for word in {"missing", "required", "not found"}
    ):
        return (
            "I could not identify the customer clearly.\n\n"
            "Please include at least the customer name, for example:\n"
            "Invoice John Smith, callout fee $90"
        )

    if "email" in lowered and any(
        word in lowered for word in {"invalid", "missing", "not configured"}
    ):
        return (
            "The invoice was created, but the customer email details need attention.\n\n"
            "Please check the email address and try again."
        )

    if "gemini" in lowered or "429" in lowered or "quota" in lowered:
        return (
            "The invoice assistant is temporarily busy. "
            "Please wait a moment and send the same message again."
        )

    if any(
        phrase in lowered
        for phrase in {
            "timed out",
            "timeout",
            "connection error",
            "network",
            "temporarily unavailable",
        }
    ):
        return (
            "A temporary connection problem occurred. "
            "Please try the same action again in a moment."
        )

    if "invoice not found" in lowered:
        return (
            "That invoice could not be found. "
            "Use /invoices to view the latest invoices."
        )

    if "cannot be marked paid" in lowered:
        return raw

    return (
        "I could not process that message safely.\n\n"
        "Please include the customer name, each job item, and a price.\n"
        "Example: Invoice John Smith, callout fee $90 and replace tap $180"
    )


def log_processing_error(context: str, exc: Exception) -> None:
    print(
        f"[ERROR] {context}: {type(exc).__name__}: {str(exc)[:1000]}",
        flush=True,
    )


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
- A message such as "this is John Smith from Canning Vale" changes the
  customer identity or address while preserving all invoice items and prices.
- Never reuse another same-name customer's phone, email, or address when
  the user supplies a different suburb or address.
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


def resolve_due_date(
    parsed: AIInvoice,
    source_text: str = "",
    existing: str | None = None,
) -> str:
    """Resolve common natural-language due dates deterministically."""
    text = source_text.strip().lower()
    today = date.today()

    if re.search(r"\b(?:due|payable)\s+today\b", text):
        return today.isoformat()

    if re.search(r"\b(?:due|payable)\s+tomorrow\b", text):
        return (today + timedelta(days=1)).isoformat()

    match = re.search(
        r"\b(?:due|payable)\s+in\s+(\d+)\s+days?\b",
        text,
    )
    if match:
        return (today + timedelta(days=int(match.group(1)))).isoformat()

    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    match = re.search(
        r"\b(?:due|payable)\s+(?:next\s+)?"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text,
    )
    if match:
        target = weekdays[match.group(1)]
        days_ahead = (target - today.weekday()) % 7
        if days_ahead == 0 or "next " in match.group(0):
            days_ahead += 7
        return (today + timedelta(days=days_ahead)).isoformat()

    if parsed.due_date:
        try:
            return date.fromisoformat(parsed.due_date).isoformat()
        except ValueError:
            pass

    if parsed.due_in_days is not None:
        return (
            today + timedelta(days=max(parsed.due_in_days, 0))
        ).isoformat()

    return existing or (today + timedelta(days=7)).isoformat()


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


def customer_name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def customer_phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if digits.startswith("61") and len(digits) >= 11:
        digits = "0" + digits[2:]
    return digits


def customer_email_key(value: str) -> str:
    return value.strip().lower()


def customer_address_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def find_saved_customer(
    name: str = "",
    phone: str = "",
    email: str = "",
    address: str = "",
):
    email_key = customer_email_key(email)
    phone_key = customer_phone_key(phone)
    name_key = customer_name_key(name)
    address_key = customer_address_key(address)

    with db() as conn:
        if email_key:
            row = conn.execute(
                "SELECT * FROM customers WHERE email_key = ? ORDER BY updated_at DESC LIMIT 1",
                (email_key,),
            ).fetchone()
            if row:
                return row

        if phone_key:
            row = conn.execute(
                "SELECT * FROM customers WHERE phone_key = ? ORDER BY updated_at DESC LIMIT 1",
                (phone_key,),
            ).fetchone()
            if row:
                return row

        if name_key:
            rows = conn.execute(
                "SELECT * FROM customers WHERE name_key = ? ORDER BY updated_at DESC LIMIT 20",
                (name_key,),
            ).fetchall()

            if address_key:
                exact_address_matches = [
                    row
                    for row in rows
                    if customer_address_key(row["address"]) == address_key
                ]
                if len(exact_address_matches) == 1:
                    return exact_address_matches[0]

                return None

            if len(rows) == 1:
                return rows[0]

    return None


def enrich_customer_from_saved(customer: CustomerData) -> CustomerData:
    saved = find_saved_customer(
        customer.name,
        customer.phone,
        customer.email,
        customer.address,
    )
    if not saved:
        return customer

    return CustomerData(
        name=customer.name or saved["name"],
        phone=customer.phone or saved["phone"],
        email=customer.email or saved["email"],
        address=customer.address or saved["address"],
    )


def save_customer(customer: CustomerData) -> None:
    name = customer.name.strip()
    phone = customer.phone.strip()
    email = customer.email.strip()
    address = customer.address.strip()

    if not any([name, phone, email]):
        return

    name_key = customer_name_key(name)
    phone_key = customer_phone_key(phone)
    email_key = customer_email_key(email)
    now = datetime.now().isoformat(timespec="seconds")
    existing = find_saved_customer(name, phone, email, address)

    with db() as conn:
        if existing:
            conn.execute(
                """
                UPDATE customers
                SET name = ?, name_key = ?, phone = ?, phone_key = ?,
                    email = ?, email_key = ?, address = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    name or existing["name"],
                    name_key or existing["name_key"],
                    phone or existing["phone"],
                    phone_key or existing["phone_key"],
                    email or existing["email"],
                    email_key or existing["email_key"],
                    address or existing["address"],
                    now,
                    existing["id"],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO customers (
                    name, name_key, phone, phone_key,
                    email, email_key, address, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name, name_key, phone, phone_key,
                    email, email_key, address, now, now,
                ),
            )


def list_saved_customers(limit: int = 20):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM customers ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def search_saved_customers(query: str, limit: int = 10):
    key = customer_name_key(query)
    if not key:
        return []

    with db() as conn:
        return conn.execute(
            "SELECT * FROM customers WHERE name_key LIKE ? ORDER BY updated_at DESC LIMIT ?",
            (f"%{key}%", limit),
        ).fetchall()


def format_customer_rows(rows, heading: str = "SAVED CUSTOMERS") -> str:
    if not rows:
        return f"{heading}\n\nNo matching customers found."

    lines = [heading, ""]
    for row in rows:
        lines.append(row["name"] or "Unnamed customer")
        if row["email"]:
            lines.append(f"Email: {row['email']}")
        if row["phone"]:
            lines.append(f"Phone: {row['phone']}")
        if row["address"]:
            lines.append(f"Address: {row['address']}")
        lines.append("")

    return "\n".join(lines).rstrip()



class QuoteDraft(BaseModel):
    id: int
    quote_number: str
    customer: CustomerData
    items: list[InvoiceItem]
    notes: str = ""
    expiry_date: str
    subtotal: float
    gst: float
    total: float
    gst_included: bool = False
    status: str
    created_at: str
    converted_invoice_id: int | None = None


def row_to_quote(row) -> QuoteDraft:
    return QuoteDraft(
        id=int(row["id"]),
        quote_number=row["quote_number"],
        customer=CustomerData(**json.loads(row["customer_json"])),
        items=[InvoiceItem(**item) for item in json.loads(row["items_json"])],
        notes=row["notes"],
        expiry_date=row["expiry_date"],
        subtotal=float(row["subtotal"]),
        gst=float(row["gst"]),
        total=float(row["total"]),
        gst_included=bool(row["gst_included"]),
        status=row["status"],
        created_at=row["created_at"],
        converted_invoice_id=(
            int(row["converted_invoice_id"])
            if row["converted_invoice_id"] is not None
            else None
        ),
    )


def get_quote(quote_id: int):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM quotes WHERE id = ?",
            (quote_id,),
        ).fetchone()
    if not row:
        raise ValueError("Quote not found")
    return row


def quote_as_invoice(quote: QuoteDraft) -> InvoiceDraft:
    return InvoiceDraft(
        id=quote.id,
        invoice_number=quote.quote_number,
        customer=quote.customer,
        items=quote.items,
        notes=quote.notes,
        due_date=quote.expiry_date,
        subtotal=quote.subtotal,
        gst=quote.gst,
        total=quote.total,
        gst_included=quote.gst_included,
        status=quote.status,
        created_at=quote.created_at,
        delivery=[],
    )


def create_ai_quote(source_message: str, parsed: AIInvoice) -> QuoteDraft:
    items = convert_items(parsed)
    if not items:
        raise ValueError("No valid priced quote items were found.")

    customer = enrich_customer_from_saved(
        CustomerData(
            name=parsed.customer_name.strip(),
            phone=parsed.customer_phone.strip(),
            email=parsed.customer_email.strip(),
            address=parsed.customer_address.strip(),
        )
    )
    subtotal, gst, total = calculate_totals(items, parsed.gst_included)
    now = datetime.now().isoformat(timespec="seconds")
    expiry_date = resolve_due_date(parsed, source_message)

    with db() as conn:
        inserted = conn.execute(
            """
            INSERT INTO quotes (
                quote_number, source_message, customer_json, items_json,
                notes, expiry_date, subtotal, gst, total, gst_included,
                status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_confirmation', ?)
            RETURNING id
            """,
            (
                "PENDING",
                source_message,
                customer.model_dump_json(),
                json.dumps([item.model_dump() for item in items]),
                parsed.notes.strip(),
                expiry_date,
                subtotal,
                gst,
                total,
                bool(parsed.gst_included),
                now,
            ),
        ).fetchone()
        quote_id = int(inserted["id"])
        quote_number = f"QT-{date.today():%Y%m%d}-{quote_id:04d}"
        conn.execute(
            "UPDATE quotes SET quote_number = ? WHERE id = ?",
            (quote_number, quote_id),
        )
        row = conn.execute(
            "SELECT * FROM quotes WHERE id = ?",
            (quote_id,),
        ).fetchone()

    quote = row_to_quote(row)
    save_customer(quote.customer)
    return quote


def update_ai_quote(
    quote_id: int,
    parsed: AIInvoice,
    edit_instruction: str,
) -> QuoteDraft:
    existing = row_to_quote(get_quote(quote_id))
    items = convert_items(parsed)
    if not items:
        raise ValueError("The edited quote has no valid priced items.")

    customer = enrich_customer_from_saved(
        CustomerData(
            name=parsed.customer_name.strip(),
            phone=parsed.customer_phone.strip(),
            email=parsed.customer_email.strip(),
            address=parsed.customer_address.strip(),
        )
    )
    subtotal, gst, total = calculate_totals(items, parsed.gst_included)

    with db() as conn:
        conn.execute(
            """
            UPDATE quotes
            SET customer_json = ?, items_json = ?, notes = ?,
                expiry_date = ?, subtotal = ?, gst = ?, total = ?,
                gst_included = ?, status = 'awaiting_confirmation',
                source_message = source_message || ?
            WHERE id = ?
            """,
            (
                customer.model_dump_json(),
                json.dumps([item.model_dump() for item in items]),
                parsed.notes.strip(),
                resolve_due_date(parsed, edit_instruction, existing.expiry_date),
                subtotal,
                gst,
                total,
                bool(parsed.gst_included),
                f"\n\nEDIT: {edit_instruction}",
                quote_id,
            ),
        )
        row = conn.execute(
            "SELECT * FROM quotes WHERE id = ?",
            (quote_id,),
        ).fetchone()

    quote = row_to_quote(row)
    save_customer(quote.customer)
    return quote


def quote_summary(quote: QuoteDraft, heading: str = "QUOTE DRAFT") -> str:
    lines = [
        f"{heading} {quote.quote_number}",
        "",
        f"Customer: {quote.customer.name or 'Not detected'}",
        f"Phone: {quote.customer.phone or 'Not detected'}",
        f"Email: {quote.customer.email or 'Not detected'}",
        f"Address: {quote.customer.address or 'Not detected'}",
        "",
    ]
    for item in quote.items[:15]:
        lines.append(
            f"{item.quantity:g} × {item.description} @ "
            f"${item.unit_price:,.2f} = ${item.line_total:,.2f}"
        )
    lines.extend(
        [
            "",
            f"Subtotal: ${quote.subtotal:,.2f}",
            f"GST: ${quote.gst:,.2f}",
            f"TOTAL: ${quote.total:,.2f}",
            f"Valid until: {quote.expiry_date}",
            "",
            "Choose an action below.",
        ]
    )
    return "\n".join(lines)


def quote_keyboard(quote_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "📨 SEND QUOTE", "callback_data": f"qsend:{quote_id}"},
            ],
            [
                {"text": "✏️ EDIT", "callback_data": f"qedit:{quote_id}"},
                {"text": "✅ ACCEPT", "callback_data": f"qaccept:{quote_id}"},
            ],
            [
                {
                    "text": "🧾 CONVERT TO INVOICE",
                    "callback_data": f"qconvert:{quote_id}",
                }
            ],
            [
                {"text": "❌ CANCEL", "callback_data": f"qcancel:{quote_id}"}
            ],
        ]
    }


def list_quotes(limit: int = 10) -> list[QuoteDraft]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM quotes ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [row_to_quote(row) for row in rows]


def format_quote_list(quotes: list[QuoteDraft]) -> str:
    if not quotes:
        return "LATEST QUOTES\n\nNo quotes found."

    lines = ["LATEST QUOTES", ""]
    for quote in quotes:
        lines.append(
            f"{quote.quote_number} | "
            f"{quote.customer.name or 'Customer'} | "
            f"${quote.total:,.2f} | {quote.status.upper()}"
        )
    return "\n".join(lines)


def convert_quote_to_invoice(quote_id: int) -> InvoiceDraft:
    quote = row_to_quote(get_quote(quote_id))

    if quote.converted_invoice_id:
        return row_to_invoice(get_invoice(quote.converted_invoice_id))

    if quote.status == "cancelled":
        raise ValueError("A cancelled quote cannot be converted.")

    now = datetime.now().isoformat(timespec="seconds")
    due_date = (date.today() + timedelta(days=7)).isoformat()
    delivery = []
    if quote.customer.email:
        delivery.append("email")
    if quote.customer.phone:
        delivery.append("sms")

    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO invoices (
                invoice_number, source_message, customer_json, items_json,
                notes, due_date, subtotal, gst, total, gst_included,
                status, delivery_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'awaiting_confirmation', ?, ?)
            """,
            (
                "PENDING",
                f"Converted from quote {quote.quote_number}",
                quote.customer.model_dump_json(),
                json.dumps([item.model_dump() for item in quote.items]),
                quote.notes,
                due_date,
                quote.subtotal,
                quote.gst,
                quote.total,
                bool(quote.gst_included),
                json.dumps(delivery),
                now,
            ),
        )
        invoice_id = int(cur.lastrowid)
        invoice_number = f"INV-{date.today():%Y%m%d}-{invoice_id:04d}"
        conn.execute(
            "UPDATE invoices SET invoice_number = ? WHERE id = ?",
            (invoice_number, invoice_id),
        )
        conn.execute(
            """
            UPDATE quotes
            SET status = 'converted', converted_invoice_id = ?
            WHERE id = ?
            """,
            (invoice_id, quote_id),
        )
        row = conn.execute(
            "SELECT * FROM invoices WHERE id = ?",
            (invoice_id,),
        ).fetchone()

    return row_to_invoice(row)


def create_ai_invoice(source_message: str, parsed: AIInvoice) -> InvoiceDraft:
    items = convert_items(parsed)
    if not items:
        raise ValueError("No valid priced invoice items were found.")

    customer = enrich_customer_from_saved(
        CustomerData(
            name=parsed.customer_name.strip(),
            phone=parsed.customer_phone.strip(),
            email=parsed.customer_email.strip(),
            address=parsed.customer_address.strip(),
        )
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
                resolve_due_date(parsed, source_message),
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
    invoice = row_to_invoice(row)
    save_customer(invoice.customer)
    return invoice


def update_ai_invoice(
    invoice_id: int,
    parsed: AIInvoice,
    edit_instruction: str,
) -> InvoiceDraft:
    existing = row_to_invoice(get_invoice(invoice_id))
    items = convert_items(parsed)
    if not items:
        raise ValueError("The edited invoice has no valid priced items.")

    customer = enrich_customer_from_saved(
        CustomerData(
            name=parsed.customer_name.strip(),
            phone=parsed.customer_phone.strip(),
            email=parsed.customer_email.strip(),
            address=parsed.customer_address.strip(),
        )
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
                resolve_due_date(parsed, edit_instruction, existing.due_date),
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
    invoice = row_to_invoice(row)
    save_customer(invoice.customer)
    return invoice


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
                "â³ This invoice is already being sent. Please wait.",
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


async def send_quote(quote_id: int, chat_id: str) -> None:
    with db() as conn:
        claimed = conn.execute(
            """
            UPDATE quotes
            SET status = 'sending'
            WHERE id = ?
              AND status IN (
                  'awaiting_confirmation',
                  'accepted',
                  'send_failed'
              )
            RETURNING *
            """,
            (quote_id,),
        ).fetchone()

    if claimed is None:
        current = row_to_quote(get_quote(quote_id))
        if current.status == "sent":
            await send_telegram(
                chat_id,
                f"Quote {current.quote_number} has already been sent.",
            )
            return
        if current.status == "converted":
            await send_telegram(
                chat_id,
                f"Quote {current.quote_number} was already converted "
                "to an invoice.",
            )
            return
        raise ValueError(
            f"Quote cannot be sent while its status is {current.status!r}."
        )

    quote = row_to_quote(claimed)

    try:
        pdf_path = create_quote_pdf(quote)
        email_ok, email_result = send_quote_email(quote, pdf_path)

        new_status = "sent" if email_ok else "send_failed"
        with db() as conn:
            conn.execute(
                "UPDATE quotes SET status = ? WHERE id = ?",
                (new_status, quote_id),
            )

        save_session(chat_id, quote_id, f"quote_{new_status}")

        if email_ok:
            await send_telegram(
                chat_id,
                f"✅ Quote {quote.quote_number} sent successfully.\n"
                f"Customer: {quote.customer.name or 'Customer'}\n"
                f"Total: ${quote.total:,.2f}\n"
                f"Valid until: {quote.expiry_date}",
                quote_keyboard(quote_id),
            )
        else:
            await send_telegram(
                chat_id,
                "Quote PDF was created but the email was not sent.\n"
                f"Reason: {email_result}",
                quote_keyboard(quote_id),
            )

    except Exception:
        with db() as conn:
            conn.execute(
                """
                UPDATE quotes
                SET status = 'send_failed'
                WHERE id = ? AND status = 'sending'
                """,
                (quote_id,),
            )
        save_session(chat_id, quote_id, "quote_send_failed")
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

    if action == "qsend":
        await answer_callback(callback_id, "Sending quote…")
        await send_quote(invoice_id, chat_id)
    elif action == "qedit":
        save_session(chat_id, invoice_id, "awaiting_quote_edit")
        await answer_callback(callback_id, "Quote edit mode")
        await send_telegram(chat_id, "Tell me the changes for this quote.")
    elif action == "qaccept":
        with db() as conn:
            conn.execute("UPDATE quotes SET status = 'accepted' WHERE id = ?", (invoice_id,))
        save_session(chat_id, invoice_id, "quote_accepted")
        await answer_callback(callback_id, "Quote accepted")
        await send_telegram(chat_id, "✅ Quote marked ACCEPTED. You can now convert it to an invoice.")
    elif action == "qconvert":
        invoice = convert_quote_to_invoice(invoice_id)
        save_session(chat_id, invoice.id, "awaiting_confirmation")
        await answer_callback(callback_id, "Converted to invoice")
        await send_telegram(chat_id, invoice_summary(invoice, "INVOICE FROM QUOTE"), action_keyboard(invoice.id))
    elif action == "qcancel":
        with db() as conn:
            conn.execute("UPDATE quotes SET status = 'cancelled' WHERE id = ?", (invoice_id,))
        save_session(chat_id, invoice_id, "quote_cancelled")
        await answer_callback(callback_id, "Quote cancelled")
        await send_telegram(chat_id, "❌ Quote cancelled.")
    elif action == "send":
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
        callback = update.get("callback_query") or {}
        callback_message = callback.get("message") or {}
        callback_chat_id = str(
            (callback_message.get("chat") or {}).get("id", "")
        )
        try:
            return await handle_callback(update, request)
        except Exception as exc:
            log_processing_error("telegram callback processing", exc)
            if callback_chat_id:
                await send_telegram(
                    callback_chat_id,
                    friendly_error_message(exc),
                )
            return {"ok": True}

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
            "/paid - paid invoices\n"
            "/customers - saved customers\n"
            "/customer NAME - find a customer\n"
            "/quotes - latest quotes\n\n"
            "After sending an invoice, use the MARK PAID button when payment arrives.",
        )
        return {"ok": True}

    if command == "/QUOTES":
        await send_telegram(chat_id, format_quote_list(list_quotes()))
        return {"ok": True}

    if command == "/CUSTOMERS":
        await send_telegram(chat_id, format_customer_rows(list_saved_customers()))
        return {"ok": True}

    if command.startswith("/CUSTOMER "):
        query = text.split(" ", 1)[1].strip()
        await send_telegram(
            chat_id,
            format_customer_rows(
                search_saved_customers(query),
                heading=f"CUSTOMER SEARCH: {query}",
            ),
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
            "awaiting_confirmation",
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

        if re.match(r"^\s*(?:quote|quotation)\b", combined, re.I):
            quote = create_ai_quote(combined, parsed)
            save_session(chat_id, quote.id, "quote_awaiting_confirmation")
            await send_telegram(
                chat_id, quote_summary(quote), quote_keyboard(quote.id)
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
        log_processing_error("telegram message processing", exc)
        await send_telegram(
            chat_id,
            friendly_error_message(exc),
        )
        return {"ok": True}


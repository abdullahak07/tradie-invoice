from __future__ import annotations

import hashlib
import re
from typing import Any, Awaitable, Callable

from billing import (
    BillingError,
    consume_document_feature_usage,
    refund_feature_usage,
)


_INSTALLED = False


def _event_id(usage: dict[str, Any] | None) -> str | None:
    if not usage or not usage.get("charged"):
        return None
    value = str(usage.get("event_id") or "").strip()
    return value or None


def _document_id(document: Any) -> int | None:
    value = getattr(document, "id", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _invoice_id_from_message(invoice_routes: Any, message: str) -> int | None:
    match = re.search(r"\binvoice\s+([A-Z0-9-]{4,40})\b", message or "", re.I)
    if not match:
        return None
    invoice_number = match.group(1).upper()
    try:
        with invoice_routes.db() as conn:
            row = conn.execute(
                "SELECT id FROM invoices WHERE UPPER(invoice_number) = ?",
                (invoice_number,),
            ).fetchone()
        return int(row["id"]) if row else None
    except Exception:
        return None


def install_plan_enforcement() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    import invoice_routes

    original_send_email: Callable[..., tuple[bool, str]] = invoice_routes.send_email
    original_send_quote_email: Callable[..., tuple[bool, str]] = invoice_routes.send_quote_email
    original_send_sms: Callable[..., Awaitable[tuple[bool, str]]] = invoice_routes.send_sms

    def limited_send_email(invoice: Any, pdf_path: Any) -> tuple[bool, str]:
        invoice_id = _document_id(invoice)
        usage = None
        if invoice_id is not None:
            try:
                usage = consume_document_feature_usage(
                    "invoice",
                    invoice_id,
                    "email_delivery",
                    reference=f"invoice:{invoice_id}:initial-email",
                )
            except BillingError as exc:
                return False, str(exc)

        event_id = _event_id(usage)
        try:
            ok, result = original_send_email(invoice, pdf_path)
        except Exception:
            refund_feature_usage(event_id)
            raise
        if not ok:
            refund_feature_usage(event_id)
        return ok, result

    def limited_send_quote_email(quote: Any, pdf_path: Any) -> tuple[bool, str]:
        quote_id = _document_id(quote)
        usage = None
        if quote_id is not None:
            try:
                usage = consume_document_feature_usage(
                    "quote",
                    quote_id,
                    "email_delivery",
                    reference=f"quote:{quote_id}:initial-email",
                )
            except BillingError as exc:
                return False, str(exc)

        event_id = _event_id(usage)
        try:
            ok, result = original_send_quote_email(quote, pdf_path)
        except Exception:
            refund_feature_usage(event_id)
            raise
        if not ok:
            refund_feature_usage(event_id)
        return ok, result

    async def limited_send_sms(phone: str, message: str) -> tuple[bool, str]:
        invoice_id = _invoice_id_from_message(invoice_routes, message)
        usage = None
        if invoice_id is not None:
            try:
                usage = consume_document_feature_usage(
                    "invoice",
                    invoice_id,
                    "sms_delivery",
                    reference=(
                        f"invoice:{invoice_id}:sms:{_short_hash(message or '')}"
                    ),
                )
            except BillingError as exc:
                return False, str(exc)

        event_id = _event_id(usage)
        try:
            ok, result = await original_send_sms(phone, message)
        except Exception:
            refund_feature_usage(event_id)
            raise
        if not ok:
            refund_feature_usage(event_id)
        return ok, result

    invoice_routes.send_email = limited_send_email
    invoice_routes.send_quote_email = limited_send_quote_email
    invoice_routes.send_sms = limited_send_sms
    invoice_routes._plan_enforcement_installed = True
    _INSTALLED = True

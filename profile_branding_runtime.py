from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

import business_onboarding
import invoice_routes


_LOCK = RLock()
_INSTALLED = False


def _document_id(document: Any) -> int | None:
    value = None

    if hasattr(document, "keys"):
        try:
            value = document["id"]
        except Exception:
            value = None

    if value is None:
        value = getattr(document, "id", None)

    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_logo_path(profile: dict[str, Any]) -> str:
    """
    Return a logo only when it is a deliberate standalone logo.

    The onboarding flow previously generated extracted_logo.png by cropping
    the top section of an uploaded invoice. That crop can contain addresses,
    table borders and other invoice content, so it must not be rendered as
    the business logo.
    """
    logo_path = str(profile.get("logo_path") or "").strip()

    if not logo_path:
        return ""

    filename = Path(logo_path).name.lower()

    if filename == "extracted_logo.png":
        return ""

    return logo_path


@contextmanager
def apply_profile(profile: dict[str, Any]):
    mapping = {
        "BUSINESS_NAME": str(
            profile.get("business_name")
            or invoice_routes.BUSINESS_NAME
        ),
        "BUSINESS_EMAIL": str(profile.get("email") or ""),
        "BUSINESS_PHONE": str(profile.get("phone") or ""),
        "BUSINESS_ABN": str(profile.get("abn") or ""),
        "BUSINESS_ADDRESS": str(profile.get("address") or ""),
        "BUSINESS_LOGO_PATH": _safe_logo_path(profile),
        "BANK_ACCOUNT_NAME": str(profile.get("account_name") or ""),
        "BANK_BSB": str(profile.get("bsb") or ""),
        "BANK_ACCOUNT_NUMBER": str(
            profile.get("account_number") or ""
        ),
        "PAYMENT_REFERENCE": str(
            profile.get("payment_reference") or "Invoice number"
        ),
    }

    original = {
        name: getattr(invoice_routes, name)
        for name in mapping
    }

    try:
        for name, value in mapping.items():
            setattr(invoice_routes, name, value)

        yield
    finally:
        for name, value in original.items():
            setattr(invoice_routes, name, value)


def install() -> None:
    global _INSTALLED

    if _INSTALLED:
        return

    import telegram_routes
    import whatsapp_routes

    original_invoice = invoice_routes.create_pdf
    original_quote = invoice_routes.create_quote_pdf

    def create_invoice(document: Any):
        document_id = _document_id(document)

        profile = (
            business_onboarding.profile_for_document(
                "invoice",
                document_id,
            )
            if document_id is not None
            else None
        )

        if not profile:
            return original_invoice(document)

        with _LOCK:
            with apply_profile(profile):
                return original_invoice(document)

    def create_quote(document: Any):
        document_id = _document_id(document)

        profile = (
            business_onboarding.profile_for_document(
                "quote",
                document_id,
            )
            if document_id is not None
            else None
        )

        if not profile:
            return original_quote(document)

        with _LOCK:
            with apply_profile(profile):
                return original_quote(document)

    invoice_routes.create_pdf = create_invoice
    invoice_routes.create_quote_pdf = create_quote

    telegram_routes.create_pdf = create_invoice
    telegram_routes.create_quote_pdf = create_quote

    whatsapp_routes.create_pdf = create_invoice
    whatsapp_routes.create_quote_pdf = create_quote

    _INSTALLED = True

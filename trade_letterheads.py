from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Any

from trade_profiles import detect_trade

_LOCK = RLock()
_INSTALLED = False


LETTERHEADS: dict[str, dict[str, str]] = {
    "electrician": {
        "business_name": "ELECTRICIAN_BUSINESS_NAME",
        "business_email": "ELECTRICIAN_BUSINESS_EMAIL",
        "business_phone": "ELECTRICIAN_BUSINESS_PHONE",
        "business_abn": "ELECTRICIAN_BUSINESS_ABN",
        "business_address": "ELECTRICIAN_BUSINESS_ADDRESS",
        "logo_path": "ELECTRICIAN_BUSINESS_LOGO_PATH",
        "bank_account_name": "ELECTRICIAN_BANK_ACCOUNT_NAME",
        "bank_bsb": "ELECTRICIAN_BANK_BSB",
        "bank_account_number": "ELECTRICIAN_BANK_ACCOUNT_NUMBER",
    },
    "carpenter": {
        "business_name": "CARPENTER_BUSINESS_NAME",
        "business_email": "CARPENTER_BUSINESS_EMAIL",
        "business_phone": "CARPENTER_BUSINESS_PHONE",
        "business_abn": "CARPENTER_BUSINESS_ABN",
        "business_address": "CARPENTER_BUSINESS_ADDRESS",
        "logo_path": "CARPENTER_BUSINESS_LOGO_PATH",
        "bank_account_name": "CARPENTER_BANK_ACCOUNT_NAME",
        "bank_bsb": "CARPENTER_BANK_BSB",
        "bank_account_number": "CARPENTER_BANK_ACCOUNT_NUMBER",
    },
}


def _env(name: str, fallback: str = "") -> str:
    value = os.getenv(name, "").strip()
    return value if value else fallback


def get_letterhead(trade_type: str) -> dict[str, str]:
    mapping = LETTERHEADS[trade_type]
    prefix = "Electrician" if trade_type == "electrician" else "Carpenter"
    return {
        "trade_type": trade_type,
        "business_name": _env(mapping["business_name"], f"{prefix} Tradie Services"),
        "business_email": _env(mapping["business_email"], os.getenv("BUSINESS_EMAIL", "")),
        "business_phone": _env(mapping["business_phone"], os.getenv("BUSINESS_PHONE", "")),
        "business_abn": _env(mapping["business_abn"], os.getenv("BUSINESS_ABN", "")),
        "business_address": _env(mapping["business_address"], os.getenv("BUSINESS_ADDRESS", "")),
        "logo_path": _env(mapping["logo_path"], os.getenv("BUSINESS_LOGO_PATH", "business_logo.png")),
        "bank_account_name": _env(mapping["bank_account_name"], os.getenv("BANK_ACCOUNT_NAME", "")),
        "bank_bsb": _env(mapping["bank_bsb"], os.getenv("BANK_BSB", "")),
        "bank_account_number": _env(mapping["bank_account_number"], os.getenv("BANK_ACCOUNT_NUMBER", "")),
    }


def _row_text(row: Any) -> str:
    parts: list[str] = []
    for key in ("source_message", "items_json", "notes"):
        try:
            value = row[key]
        except Exception:
            value = ""
        if value:
            parts.append(str(value))
    return " ".join(parts)


def _object_text(document: Any) -> str:
    parts = [str(getattr(document, "notes", "") or "")]
    for item in getattr(document, "items", []) or []:
        parts.append(str(getattr(item, "description", "") or ""))
    return " ".join(parts)


def detect_document_trade(document: Any) -> str:
    text = _row_text(document) if hasattr(document, "keys") else _object_text(document)
    return str(detect_trade(text)["trade_type"])


@contextmanager
def apply_letterhead(invoice_routes_module: Any, trade_type: str):
    profile = get_letterhead(trade_type)
    names = {
        "BUSINESS_NAME": profile["business_name"],
        "BUSINESS_EMAIL": profile["business_email"],
        "BUSINESS_PHONE": profile["business_phone"],
        "BUSINESS_ABN": profile["business_abn"],
        "BUSINESS_ADDRESS": profile["business_address"],
        "BUSINESS_LOGO_PATH": profile["logo_path"],
        "BANK_ACCOUNT_NAME": profile["bank_account_name"],
        "BANK_BSB": profile["bank_bsb"],
        "BANK_ACCOUNT_NUMBER": profile["bank_account_number"],
    }
    original = {key: getattr(invoice_routes_module, key) for key in names}
    try:
        for key, value in names.items():
            setattr(invoice_routes_module, key, value)
        yield profile
    finally:
        for key, value in original.items():
            setattr(invoice_routes_module, key, value)


def install_letterhead_routing() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    import invoice_routes
    import telegram_routes

    original_create_pdf = invoice_routes.create_pdf
    original_create_quote_pdf = invoice_routes.create_quote_pdf

    def routed_create_pdf(row: Any):
        trade_type = detect_document_trade(row)
        with _LOCK:
            with apply_letterhead(invoice_routes, trade_type):
                return original_create_pdf(row)

    def routed_create_quote_pdf(quote: Any):
        trade_type = detect_document_trade(quote)
        with _LOCK:
            with apply_letterhead(invoice_routes, trade_type):
                return original_create_quote_pdf(quote)

    invoice_routes.create_pdf = routed_create_pdf
    invoice_routes.create_quote_pdf = routed_create_quote_pdf
    telegram_routes.create_pdf = routed_create_pdf
    telegram_routes.create_quote_pdf = routed_create_quote_pdf
    telegram_routes._trade_letterheads_installed = True
    _INSTALLED = True


def configured_letterheads() -> dict[str, dict[str, str]]:
    safe: dict[str, dict[str, str]] = {}
    for trade_type in LETTERHEADS:
        profile = get_letterhead(trade_type)
        safe[trade_type] = {
            "business_name": profile["business_name"],
            "business_email": profile["business_email"],
            "business_phone": profile["business_phone"],
            "business_abn": profile["business_abn"],
            "business_address": profile["business_address"],
            "logo_path": Path(profile["logo_path"]).name,
            "bank_configured": bool(profile["bank_account_name"] or profile["bank_bsb"] or profile["bank_account_number"]),
        }
    return safe

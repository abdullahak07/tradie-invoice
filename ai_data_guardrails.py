from __future__ import annotations

import re

import telegram_routes

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_REMOVE_EMAIL_RE = re.compile(
    r"\b(?:remove|delete|clear|erase|drop|no)\b.{0,30}\bemail\b|\bemail\b.{0,30}\b(?:remove|delete|clear|erase)\b",
    re.I | re.S,
)
_PLACEHOLDER_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "test.com",
    "placeholder.com",
    "invalid.com",
}
_INSTALLED = False


def _normalise(value: str) -> str:
    return value.strip().lower().rstrip(".,;:")


def _emails_in(text: str) -> set[str]:
    return {_normalise(value) for value in _EMAIL_RE.findall(text or "")}


def _safe_email(candidate: str, source_text: str) -> str:
    value = _normalise(candidate)
    if not value:
        return ""
    if "@" not in value:
        return ""
    domain = value.rsplit("@", 1)[1]
    if domain in _PLACEHOLDER_DOMAINS:
        return ""
    if value not in _emails_in(source_text):
        return ""
    return candidate.strip()


def install_guardrails() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    original_parse = telegram_routes.ai_parse_sync
    original_edit = telegram_routes.ai_edit_sync

    def guarded_parse(message: str):
        parsed = original_parse(message)
        parsed.customer_email = _safe_email(parsed.customer_email, message)
        return parsed

    def guarded_edit(invoice, instruction: str, prior_instruction: str = ""):
        parsed = original_edit(invoice, instruction, prior_instruction)
        combined = f"{prior_instruction}\n{instruction}".strip()
        if _REMOVE_EMAIL_RE.search(combined):
            parsed.customer_email = ""
            return parsed

        existing = str(invoice.customer.email or "").strip()
        candidate = str(parsed.customer_email or "").strip()
        supplied = _emails_in(combined)

        if candidate and _normalise(candidate) in supplied:
            domain = _normalise(candidate).rsplit("@", 1)[1]
            parsed.customer_email = "" if domain in _PLACEHOLDER_DOMAINS else candidate
        elif candidate != existing:
            parsed.customer_email = existing

        return parsed

    telegram_routes.ai_parse_sync = guarded_parse
    telegram_routes.ai_edit_sync = guarded_edit
    telegram_routes._ai_data_guardrails_installed = True
    _INSTALLED = True

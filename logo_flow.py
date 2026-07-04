from __future__ import annotations


def wants_logo(command: str, state: str) -> bool:
    return command.strip().upper() == "LOGO" and state == "awaiting_confirmation"


def accepts_logo(message_type: str, state: str) -> bool:
    return state == "awaiting_logo" and message_type in {"image", "document"}


def logo_error(mime: str, size: int, max_size: int) -> str:
    if mime not in {"image/png", "image/jpeg"}:
        return "Please send the logo as a PNG or JPG image."
    if size <= 0 or size > max_size:
        return "Please send a non-empty logo image up to 12 MB."
    return ""

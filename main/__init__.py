from __future__ import annotations

import importlib.util
import os
from pathlib import Path

os.environ.setdefault("TRIAL_DAYS", "14")
os.environ.setdefault("TRIAL_CREDITS", "5")
os.environ.setdefault("TRIAL_VOICE_LIMIT", "3")

import billing

billing.TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
billing.TRIAL_CREDITS = int(os.getenv("TRIAL_CREDITS", "5"))

import self_onboarding


def _no_invoice_header_crop(content: bytes, mime: str, folder: Path, visible: bool) -> str:
    return ""


def _safe_summary(data: dict, logo_path: str) -> str:
    def line(label: str, key: str) -> str:
        value = str(data.get(key) or "").strip() or "Not found"
        return f"{label}: {value}"

    if logo_path:
        logo_line = "Logo: Ready to use"
    elif bool(data.get("logo_visible")):
        logo_line = "Logo: Detected, but not clear enough to use"
    else:
        logo_line = "Logo: Not found"

    return "\n".join(
        [
            "I found these business details:",
            "",
            line("Business", "business_name"),
            line("Owner", "owner_name"),
            line("Trade", "trade_type"),
            line("ABN", "abn"),
            line("Licence", "licence_number"),
            line("Phone", "phone"),
            line("Email", "email"),
            line("Address", "address"),
            line("Account name", "bank_account_name"),
            line("BSB", "bank_bsb"),
            line("Account number", "bank_account_number"),
            logo_line,
            "",
            "Please confirm these are your business details, not your customer’s or supplier’s details.",
            "Reply CONFIRM to continue without a logo, EDIT to correct details, or REUPLOAD to send another document.",
        ]
    )


self_onboarding.create_logo_candidate = _no_invoice_header_crop
self_onboarding.summary_text = _safe_summary
self_onboarding.install()

import trade_letterheads

_original_letterhead_install = trade_letterheads.install_letterhead_routing


def _install_letterheads_and_user_profiles() -> None:
    _original_letterhead_install()
    import profile_branding_runtime

    profile_branding_runtime.install()


trade_letterheads.install_letterhead_routing = _install_letterheads_and_user_profiles

_main_file = Path(__file__).resolve().parent.parent / "main.py"
_spec = importlib.util.spec_from_file_location("tradie_invoice_main_file", _main_file)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Could not load the Tradie Invoice application")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
app = _module.app

__all__ = ["app"]

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

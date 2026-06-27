from __future__ import annotations

import os

# Public trial policy: five completed documents or fourteen days, whichever comes first.
os.environ.setdefault("TRIAL_DAYS", "14")
os.environ.setdefault("TRIAL_CREDITS", "5")
os.environ.setdefault("TRIAL_VOICE_LIMIT", "3")

import billing

billing.TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "14"))
billing.TRIAL_CREDITS = int(os.getenv("TRIAL_CREDITS", "5"))

# Install the document-first chat onboarding before FastAPI captures webhook handlers.
import self_onboarding

self_onboarding.install()

# The existing trade-letterhead installer runs while main.py imports. Wrap it so
# confirmed per-user branding is applied after the existing trade routing.
import trade_letterheads

_original_letterhead_install = trade_letterheads.install_letterhead_routing


def _install_letterheads_and_user_profiles() -> None:
    _original_letterhead_install()
    import profile_branding_runtime

    profile_branding_runtime.install()


trade_letterheads.install_letterhead_routing = _install_letterheads_and_user_profiles

from main import app

__all__ = ["app"]

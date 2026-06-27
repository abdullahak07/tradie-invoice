from __future__ import annotations

import importlib.util
from pathlib import Path

import billing_plan_runtime

billing_plan_runtime.install()

_source = Path(__file__).resolve().parent.parent / "voice_confirm_routes.py"
_spec = importlib.util.spec_from_file_location("tradie_voice_confirm_routes_file", _source)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Could not load voice confirmation routes")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

telegram_webhook = _module.telegram_webhook
whatsapp_webhook = _module.whatsapp_webhook

__all__ = ["telegram_webhook", "whatsapp_webhook"]

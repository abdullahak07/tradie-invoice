from __future__ import annotations

import os
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


def _env_int(name: str, default: int, *, minimum: int = 0, fallback: str | None = None) -> int:
    raw = os.getenv(name, "").strip()
    if not raw and fallback:
        raw = os.getenv(fallback, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class PlanLimits:
    name: str
    document_credits: int
    ai_per_minute: int
    ai_per_hour: int
    ai_per_day: int
    enabled_features: frozenset[str]
    feature_limits: Mapping[str, int]

    def feature_enabled(self, feature: str) -> bool:
        return feature.strip().lower() in self.enabled_features

    def feature_limit(self, feature: str) -> int | None:
        key = feature.strip().lower()
        if key not in self.enabled_features:
            return 0
        return self.feature_limits.get(key)


_BASE_FEATURES = frozenset(
    {
        "invoices",
        "quotes",
        "ai_edits",
        "pdf_generation",
        "telegram",
        "whatsapp",
        "gst_calculation",
        "quote_to_invoice",
        "voice_transcription",
    }
)

_STANDARD_FEATURES = _BASE_FEATURES | {
    "email_delivery",
}

_PREMIUM_FEATURES = _STANDARD_FEATURES | {
    "sms_delivery",
}


PLAN_LIMITS: Mapping[str, PlanLimits] = MappingProxyType(
    {
        "trial": PlanLimits(
            name="trial",
            document_credits=_env_int("TRIAL_CREDITS", 30),
            ai_per_minute=_env_int("TRIAL_AI_PER_MINUTE", 3, minimum=1, fallback="AI_LIMIT_PER_MINUTE"),
            ai_per_hour=_env_int("TRIAL_AI_PER_HOUR", 10, minimum=1, fallback="AI_LIMIT_PER_HOUR"),
            ai_per_day=_env_int("TRIAL_AI_PER_DAY", 30, minimum=1, fallback="AI_LIMIT_PER_DAY"),
            enabled_features=_BASE_FEATURES,
            feature_limits=MappingProxyType(
                {
                    "voice_transcription": _env_int("TRIAL_VOICE_LIMIT", 5),
                }
            ),
        ),
        "standard": PlanLimits(
            name="standard",
            document_credits=_env_int("STANDARD_CREDITS", 150),
            ai_per_minute=_env_int("STANDARD_AI_PER_MINUTE", 10, minimum=1, fallback="AI_LIMIT_PER_MINUTE"),
            ai_per_hour=_env_int("STANDARD_AI_PER_HOUR", 30, minimum=1, fallback="AI_LIMIT_PER_HOUR"),
            ai_per_day=_env_int("STANDARD_AI_PER_DAY", 100, minimum=1, fallback="AI_LIMIT_PER_DAY"),
            enabled_features=_STANDARD_FEATURES,
            feature_limits=MappingProxyType(
                {
                    "voice_transcription": _env_int("STANDARD_VOICE_LIMIT", 50),
                    "email_delivery": _env_int("STANDARD_EMAIL_LIMIT", 150),
                }
            ),
        ),
        "premium": PlanLimits(
            name="premium",
            document_credits=_env_int("PREMIUM_CREDITS", 500),
            ai_per_minute=_env_int("PREMIUM_AI_PER_MINUTE", 20, minimum=1, fallback="AI_LIMIT_PER_MINUTE"),
            ai_per_hour=_env_int("PREMIUM_AI_PER_HOUR", 120, minimum=1, fallback="AI_LIMIT_PER_HOUR"),
            ai_per_day=_env_int("PREMIUM_AI_PER_DAY", 500, minimum=1, fallback="AI_LIMIT_PER_DAY"),
            enabled_features=_PREMIUM_FEATURES,
            feature_limits=MappingProxyType(
                {
                    "voice_transcription": _env_int("PREMIUM_VOICE_LIMIT", 250),
                    "email_delivery": _env_int("PREMIUM_EMAIL_LIMIT", 500),
                    "sms_delivery": _env_int("PREMIUM_SMS_LIMIT", 500),
                }
            ),
        ),
    }
)

VALID_PLANS = frozenset(PLAN_LIMITS)


def normalise_plan(plan: str) -> str:
    value = (plan or "").strip().lower()
    if value not in PLAN_LIMITS:
        raise ValueError(
            f"Unsupported plan {plan!r}. Expected one of: "
            + ", ".join(sorted(PLAN_LIMITS))
        )
    return value


def get_plan_limits(plan: str) -> PlanLimits:
    return PLAN_LIMITS[normalise_plan(plan)]


def plan_credit_limit(plan: str) -> int:
    return get_plan_limits(plan).document_credits

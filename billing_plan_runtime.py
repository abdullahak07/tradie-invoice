from __future__ import annotations

import json
import sys
import uuid
from datetime import timedelta
from typing import Any

import billing
from plan_limits import get_plan_limits, normalise_plan


_INSTALLED = False
_ORIGINAL_ISSUE_ACTIVATION_CODE = billing.issue_activation_code


class PlanFeatureError(billing.BillingError):
    pass


class UsageLimitError(billing.BillingError):
    pass


def _rowcount(cursor: Any) -> int:
    raw = getattr(cursor, "_cursor", cursor)
    return int(getattr(raw, "rowcount", 0) or 0)


def ensure_plan_schema() -> None:
    billing.ensure_billing_schema()
    with billing.db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_events (
                event_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                feature TEXT NOT NULL,
                reference TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                UNIQUE(user_id, feature, reference)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_usage_events_user_feature_time
            ON usage_events(user_id, feature, created_at)
            """
        )


def get_user_by_id(user_id: str):
    ensure_plan_schema()
    with billing.db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return billing._refresh_paid_period(conn, user)


def _assert_active(user: Any) -> None:
    billing._assert_active(user)


def _policy_for_user(user: Any):
    _assert_active(user)
    try:
        return get_plan_limits(str(user["plan"]))
    except ValueError as exc:
        raise billing.BillingError(str(exc)) from exc


def _period_start(user: Any):
    if str(user["plan"]) == "trial":
        start = billing.parse_iso(user["trial_started_at"])
    else:
        start = billing.parse_iso(user["billing_period_start"])
    return start or billing.parse_iso(user["created_at"]) or billing.utc_now()


def assert_user_feature(user_id: str, feature: str) -> dict[str, Any]:
    user = get_user_by_id(user_id)
    _assert_active(user)
    policy = _policy_for_user(user)
    key = feature.strip().lower()
    if not policy.feature_enabled(key):
        raise PlanFeatureError(
            f"{key.replace('_', ' ').title()} is not included in the "
            f"{policy.name.title()} plan."
        )
    return {
        "user": user,
        "policy": policy,
        "feature": key,
        "limit": policy.feature_limit(key),
    }


def assert_plan_feature(channel: str, external_id: str, feature: str) -> dict[str, Any]:
    user = billing.get_or_create_user(channel, external_id)
    return assert_user_feature(str(user["user_id"]), feature)


def _feature_usage_count(conn: Any, user_id: str, feature: str, start: Any) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total
        FROM usage_events
        WHERE user_id = ?
          AND feature = ?
          AND created_at >= ?
        """,
        (user_id, feature, billing.iso(start)),
    ).fetchone()
    return int(row["total"])


def get_user_feature_usage(user_id: str, feature: str) -> dict[str, Any]:
    entitlement = assert_user_feature(user_id, feature)
    user = entitlement["user"]
    policy = entitlement["policy"]
    key = entitlement["feature"]
    limit = entitlement["limit"]
    with billing.db() as conn:
        used = _feature_usage_count(conn, user_id, key, _period_start(user))
    return {
        "plan": policy.name,
        "feature": key,
        "used": used,
        "limit": limit,
        "remaining": None if limit is None else max(0, int(limit) - used),
    }


def consume_user_feature_usage(
    user_id: str,
    feature: str,
    *,
    reference: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entitlement = assert_user_feature(user_id, feature)
    user = entitlement["user"]
    policy = entitlement["policy"]
    key = entitlement["feature"]
    limit = entitlement["limit"]
    ref = reference.strip() or uuid.uuid4().hex
    now = billing.utc_now()

    with billing.db() as conn:
        existing = conn.execute(
            """
            SELECT event_id
            FROM usage_events
            WHERE user_id = ? AND feature = ? AND reference = ?
            """,
            (user_id, key, ref),
        ).fetchone()
        if existing:
            used = _feature_usage_count(conn, user_id, key, _period_start(user))
            return {
                "event_id": str(existing["event_id"]),
                "charged": False,
                "already_charged": True,
                "plan": policy.name,
                "feature": key,
                "used": used,
                "limit": limit,
                "remaining": None if limit is None else max(0, int(limit) - used),
            }

        used = _feature_usage_count(conn, user_id, key, _period_start(user))
        if limit is not None and used >= int(limit):
            raise UsageLimitError(
                f"Your {policy.name.title()} plan includes {limit} "
                f"{key.replace('_', ' ')} uses per billing period. "
                "The limit has been reached."
            )

        event_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO usage_events (
                event_id, user_id, feature, reference, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                user_id,
                key,
                ref,
                json.dumps(metadata or {}, default=str, sort_keys=True),
                billing.iso(now),
            ),
        )

    new_used = used + 1
    return {
        "event_id": event_id,
        "charged": True,
        "already_charged": False,
        "plan": policy.name,
        "feature": key,
        "used": new_used,
        "limit": limit,
        "remaining": None if limit is None else max(0, int(limit) - new_used),
    }


def consume_feature_usage(
    channel: str,
    external_id: str,
    feature: str,
    *,
    reference: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    user = billing.get_or_create_user(channel, external_id)
    return consume_user_feature_usage(
        str(user["user_id"]),
        feature,
        reference=reference,
        metadata=metadata,
    )


def refund_feature_usage(event_id: str | None) -> bool:
    if not event_id:
        return False
    ensure_plan_schema()
    with billing.db() as conn:
        deleted = conn.execute(
            "DELETE FROM usage_events WHERE event_id = ?",
            (event_id,),
        )
        return _rowcount(deleted) == 1


def get_document_owner_user_id(document_type: str, document_id: int) -> str | None:
    ensure_plan_schema()
    with billing.db() as conn:
        row = conn.execute(
            """
            SELECT user_id
            FROM credit_events
            WHERE document_type = ?
              AND document_id = ?
              AND event_type = 'pdf_generated'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (document_type.strip().lower(), int(document_id)),
        ).fetchone()
    return str(row["user_id"]) if row else None


def consume_document_feature_usage(
    document_type: str,
    document_id: int,
    feature: str,
    *,
    reference: str = "",
) -> dict[str, Any] | None:
    user_id = get_document_owner_user_id(document_type, document_id)
    if not user_id:
        return None
    return consume_user_feature_usage(
        user_id,
        feature,
        reference=reference or f"{document_type}:{document_id}:{feature}",
        metadata={"document_type": document_type, "document_id": document_id},
    )


def check_ai_rate_limit(channel: str, external_id: str) -> None:
    user = billing.get_or_create_user(channel, external_id)
    _assert_active(user)
    policy = _policy_for_user(user)
    user_id = str(user["user_id"])
    now = billing.utc_now()

    windows = [
        (timedelta(minutes=1), policy.ai_per_minute, "minute"),
        (timedelta(hours=1), policy.ai_per_hour, "hour"),
        (timedelta(days=1), policy.ai_per_day, "day"),
    ]

    with billing.db() as conn:
        for delta, limit, label in windows:
            count = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM rate_events
                WHERE user_id = ?
                  AND operation = 'ai'
                  AND created_at >= ?
                """,
                (user_id, billing.iso(now - delta)),
            ).fetchone()
            if int(count["total"]) >= limit:
                raise billing.RateLimitError(
                    f"Your {policy.name.title()} plan allows {limit} AI requests per {label}. "
                    "Please wait before sending another draft or edit request."
                )

        conn.execute(
            """
            INSERT INTO rate_events (
                event_id, user_id, operation, created_at
            ) VALUES (?, ?, 'ai', ?)
            """,
            (uuid.uuid4().hex, user_id, billing.iso(now)),
        )
        conn.execute(
            "DELETE FROM rate_events WHERE created_at < ?",
            (billing.iso(now - timedelta(days=2)),),
        )


def get_account_status(channel: str, external_id: str) -> dict[str, Any]:
    original = billing._plan_original_get_account_status(channel, external_id)
    user = get_user_by_id(str(original["user_id"]))
    policy = _policy_for_user(user)
    voice_limit = policy.feature_limit("voice_transcription")
    with billing.db() as conn:
        voice_used = _feature_usage_count(
            conn,
            str(user["user_id"]),
            "voice_transcription",
            _period_start(user),
        )
    return {
        **original,
        "voice_used": voice_used,
        "voice_limit": voice_limit,
        "features": sorted(policy.enabled_features),
        "ai_limits": {
            "per_minute": policy.ai_per_minute,
            "per_hour": policy.ai_per_hour,
            "per_day": policy.ai_per_day,
        },
    }


def format_account_status(channel: str, external_id: str) -> str:
    status = get_account_status(channel, external_id)
    voice_limit = status["voice_limit"]
    voice_line = (
        f"Voice transcriptions used: {status['voice_used']} / {voice_limit}\n"
        if voice_limit is not None
        else "Voice transcriptions: Included\n"
    )
    if status["plan"] == "trial":
        expiry = str(status["trial_expires_at"] or "")[:10]
        return (
            "Plan: Trial\n"
            f"Documents remaining: {status['credit_balance']} / {status['credit_limit']}\n"
            f"{voice_line}"
            f"Trial expires: {expiry}\n\n"
            "One document credit is used only when a new invoice or quote PDF is generated."
        )
    period_end = str(status["billing_period_end"] or "")[:10]
    return (
        f"Plan: {status['plan'].title()}\n"
        f"Documents remaining: {status['credit_balance']} / {status['credit_limit']}\n"
        f"{voice_line}"
        f"Limits reset: {period_end}\n\n"
        "Edits, cancellations and viewing existing drafts do not use document credits."
    )


def issue_activation_code(
    plan: str = "standard",
    credits: int | None = None,
    days: int = 30,
    max_uses: int = 2,
) -> str:
    try:
        plan_name = normalise_plan(plan)
    except ValueError as exc:
        raise billing.ActivationError(str(exc)) from exc
    credit_limit = (
        get_plan_limits(plan_name).document_credits
        if credits is None
        else int(credits)
    )
    return _ORIGINAL_ISSUE_ACTIVATION_CODE(
        plan=plan_name,
        credits=credit_limit,
        days=days,
        max_uses=max_uses,
    )


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    billing._plan_original_get_account_status = billing.get_account_status
    billing._plan_original_format_account_status = billing.format_account_status
    billing._plan_original_check_ai_rate_limit = billing.check_ai_rate_limit
    billing._plan_original_issue_activation_code = billing.issue_activation_code

    billing.PlanFeatureError = PlanFeatureError
    billing.UsageLimitError = UsageLimitError
    billing.ensure_plan_schema = ensure_plan_schema
    billing.get_user_by_id = get_user_by_id
    billing.assert_user_feature = assert_user_feature
    billing.assert_plan_feature = assert_plan_feature
    billing.get_user_feature_usage = get_user_feature_usage
    billing.consume_user_feature_usage = consume_user_feature_usage
    billing.consume_feature_usage = consume_feature_usage
    billing.refund_feature_usage = refund_feature_usage
    billing.get_document_owner_user_id = get_document_owner_user_id
    billing.consume_document_feature_usage = consume_document_feature_usage
    billing.check_ai_rate_limit = check_ai_rate_limit
    billing.get_account_status = get_account_status
    billing.format_account_status = format_account_status
    billing.issue_activation_code = issue_activation_code

    # sitecustomize may import channel modules before FastAPI imports main.py.
    # Replace any early-bound aliases as well as the billing module functions.
    for module_name in ("telegram_routes", "whatsapp_routes"):
        module = sys.modules.get(module_name)
        if module is None:
            continue
        if hasattr(module, "check_ai_rate_limit"):
            module.check_ai_rate_limit = check_ai_rate_limit
        if hasattr(module, "format_account_status"):
            module.format_account_status = format_account_status

    ensure_plan_schema()
    billing._plan_limits_installed = True
    _INSTALLED = True

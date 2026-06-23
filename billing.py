from __future__ import annotations

import hashlib
import os
import secrets
import string
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from invoice_routes import db


TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "30"))
TRIAL_CREDITS = int(os.getenv("TRIAL_CREDITS", "30"))
PAID_DEFAULT_CREDITS = int(os.getenv("PAID_DEFAULT_CREDITS", "150"))
AI_LIMIT_PER_MINUTE = int(os.getenv("AI_LIMIT_PER_MINUTE", "10"))
AI_LIMIT_PER_HOUR = int(os.getenv("AI_LIMIT_PER_HOUR", "30"))
AI_LIMIT_PER_DAY = int(os.getenv("AI_LIMIT_PER_DAY", "100"))


class BillingError(RuntimeError):
    pass


class TrialExpiredError(BillingError):
    pass


class NoCreditsError(BillingError):
    pass


class RateLimitError(BillingError):
    pass


class ActivationError(BillingError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def ensure_billing_schema() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                plan TEXT NOT NULL,
                status TEXT NOT NULL,
                trial_started_at TEXT,
                trial_expires_at TEXT,
                credit_balance INTEGER NOT NULL,
                credit_limit INTEGER NOT NULL,
                billing_period_start TEXT,
                billing_period_end TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS channel_identities (
                channel TEXT NOT NULL,
                external_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                verified_at TEXT NOT NULL,
                PRIMARY KEY (channel, external_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS activation_codes (
                code_hash TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                max_uses INTEGER NOT NULL DEFAULT 2,
                uses_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS credit_events (
                user_id TEXT NOT NULL,
                document_type TEXT NOT NULL,
                document_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (
                    user_id,
                    document_type,
                    document_id,
                    event_type
                )
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rate_events (
                event_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )


def _rowcount(cursor: Any) -> int:
    raw = getattr(cursor, "_cursor", cursor)
    return int(getattr(raw, "rowcount", 0) or 0)


def _refresh_paid_period(conn: Any, user: Any):
    if not user or str(user["plan"]) == "trial":
        return user

    period_end = parse_iso(user["billing_period_end"])
    now = utc_now()
    if period_end and now >= period_end:
        next_end = now + timedelta(days=30)
        conn.execute(
            """
            UPDATE users
            SET credit_balance = credit_limit,
                billing_period_start = ?,
                billing_period_end = ?,
                updated_at = ?
            WHERE user_id = ?
            """,
            (iso(now), iso(next_end), iso(now), user["user_id"]),
        )
        user = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user["user_id"],),
        ).fetchone()
    return user


def get_or_create_user(channel: str, external_id: str):
    ensure_billing_schema()
    channel = channel.strip().lower()
    external_id = external_id.strip()
    now = utc_now()

    with db() as conn:
        identity = conn.execute(
            """
            SELECT user_id
            FROM channel_identities
            WHERE channel = ? AND external_id = ?
            """,
            (channel, external_id),
        ).fetchone()

        if identity:
            user = conn.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (identity["user_id"],),
            ).fetchone()
            return _refresh_paid_period(conn, user)

        user_id = uuid.uuid4().hex
        trial_end = now + timedelta(days=TRIAL_DAYS)

        conn.execute(
            """
            INSERT INTO users (
                user_id,
                plan,
                status,
                trial_started_at,
                trial_expires_at,
                credit_balance,
                credit_limit,
                billing_period_start,
                billing_period_end,
                created_at,
                updated_at
            ) VALUES (?, 'trial', 'active', ?, ?, ?, ?, NULL, NULL, ?, ?)
            """,
            (
                user_id,
                iso(now),
                iso(trial_end),
                TRIAL_CREDITS,
                TRIAL_CREDITS,
                iso(now),
                iso(now),
            ),
        )
        conn.execute(
            """
            INSERT INTO channel_identities (
                channel,
                external_id,
                user_id,
                verified_at
            ) VALUES (?, ?, ?, ?)
            """,
            (channel, external_id, user_id, iso(now)),
        )
        return conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def _assert_active(user: Any) -> None:
    if not user:
        raise BillingError("User account was not found.")

    if str(user["status"]) != "active":
        raise BillingError("This account is not active.")

    if str(user["plan"]) == "trial":
        expires = parse_iso(user["trial_expires_at"])
        if expires and utc_now() >= expires:
            raise TrialExpiredError(
                "Your 30-day trial has expired. Activate a paid plan to generate more PDFs."
            )


def get_account_status(channel: str, external_id: str) -> dict[str, Any]:
    user = get_or_create_user(channel, external_id)
    _assert_active(user)

    return {
        "user_id": str(user["user_id"]),
        "plan": str(user["plan"]),
        "status": str(user["status"]),
        "credit_balance": int(user["credit_balance"]),
        "credit_limit": int(user["credit_limit"]),
        "trial_expires_at": user["trial_expires_at"],
        "billing_period_end": user["billing_period_end"],
    }


def format_account_status(channel: str, external_id: str) -> str:
    status = get_account_status(channel, external_id)

    if status["plan"] == "trial":
        expiry = str(status["trial_expires_at"] or "")[:10]
        return (
            f"Plan: Trial\n"
            f"Credits remaining: {status['credit_balance']} / {status['credit_limit']}\n"
            f"Trial expires: {expiry}\n\n"
            "One credit is used only when a new invoice or quote PDF is generated."
        )

    period_end = str(status["billing_period_end"] or "")[:10]
    return (
        f"Plan: {status['plan'].title()}\n"
        f"Credits remaining: {status['credit_balance']} / {status['credit_limit']}\n"
        f"Credits reset: {period_end}\n\n"
        "Edits, cancellations and viewing drafts do not use credits."
    )


def consume_document_credit(
    channel: str,
    external_id: str,
    document_type: str,
    document_id: int,
) -> dict[str, Any]:
    user = get_or_create_user(channel, external_id)
    _assert_active(user)
    user_id = str(user["user_id"])
    now = iso(utc_now())

    with db() as conn:
        existing = conn.execute(
            """
            SELECT 1
            FROM credit_events
            WHERE user_id = ?
              AND document_type = ?
              AND document_id = ?
              AND event_type = 'pdf_generated'
            """,
            (user_id, document_type, document_id),
        ).fetchone()

        if existing:
            current = conn.execute(
                "SELECT credit_balance FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            return {
                "charged": False,
                "already_charged": True,
                "credit_balance": int(current["credit_balance"]),
            }

        debit = conn.execute(
            """
            UPDATE users
            SET credit_balance = credit_balance - 1,
                updated_at = ?
            WHERE user_id = ?
              AND credit_balance > 0
            RETURNING credit_balance
            """,
            (now, user_id),
        ).fetchone()

        if debit is None:
            raise NoCreditsError(
                "You have used all available document credits. "
                "You can still edit or cancel drafts, but generating a new PDF requires more credits."
            )

        conn.execute(
            """
            INSERT INTO credit_events (
                user_id,
                document_type,
                document_id,
                amount,
                event_type,
                created_at
            ) VALUES (?, ?, ?, 1, 'pdf_generated', ?)
            """,
            (user_id, document_type, document_id, now),
        )

        return {
            "charged": True,
            "already_charged": False,
            "credit_balance": int(debit["credit_balance"]),
        }


def refund_document_credit(
    channel: str,
    external_id: str,
    document_type: str,
    document_id: int,
) -> bool:
    user = get_or_create_user(channel, external_id)
    user_id = str(user["user_id"])

    with db() as conn:
        deleted = conn.execute(
            """
            DELETE FROM credit_events
            WHERE user_id = ?
              AND document_type = ?
              AND document_id = ?
              AND event_type = 'pdf_generated'
            """,
            (user_id, document_type, document_id),
        )

        if _rowcount(deleted) != 1:
            return False

        conn.execute(
            """
            UPDATE users
            SET credit_balance = credit_balance + 1,
                updated_at = ?
            WHERE user_id = ?
            """,
            (iso(utc_now()), user_id),
        )
        return True


def check_ai_rate_limit(channel: str, external_id: str) -> None:
    user = get_or_create_user(channel, external_id)
    _assert_active(user)
    user_id = str(user["user_id"])
    now = utc_now()

    windows = [
        (timedelta(minutes=1), AI_LIMIT_PER_MINUTE, "minute"),
        (timedelta(hours=1), AI_LIMIT_PER_HOUR, "hour"),
        (timedelta(days=1), AI_LIMIT_PER_DAY, "day"),
    ]

    with db() as conn:
        for delta, limit, label in windows:
            count = conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM rate_events
                WHERE user_id = ?
                  AND operation = 'ai'
                  AND created_at >= ?
                """,
                (user_id, iso(now - delta)),
            ).fetchone()

            if int(count["total"]) >= limit:
                raise RateLimitError(
                    f"AI request limit reached for this {label}. "
                    "Please wait before sending another draft or edit request."
                )

        conn.execute(
            """
            INSERT INTO rate_events (
                event_id,
                user_id,
                operation,
                created_at
            ) VALUES (?, ?, 'ai', ?)
            """,
            (uuid.uuid4().hex, user_id, iso(now)),
        )
        conn.execute(
            "DELETE FROM rate_events WHERE created_at < ?",
            (iso(now - timedelta(days=2)),),
        )


def _clean_code(code: str) -> str:
    return "".join(ch for ch in code.strip() if ch.isalnum()).upper()


def hash_activation_code(code: str) -> str:
    return hashlib.sha256(_clean_code(code).encode("utf-8")).hexdigest()


def activate_code(channel: str, external_id: str, code: str) -> dict[str, Any]:
    ensure_billing_schema()
    code_hash = hash_activation_code(code)
    now = utc_now()

    with db() as conn:
        record = conn.execute(
            "SELECT * FROM activation_codes WHERE code_hash = ?",
            (code_hash,),
        ).fetchone()

        if not record:
            raise ActivationError("That activation code is invalid.")

        expires_at = parse_iso(record["expires_at"])
        if expires_at and now >= expires_at:
            raise ActivationError("That activation code has expired.")

        if int(record["uses_count"]) >= int(record["max_uses"]):
            raise ActivationError("That activation code has reached its usage limit.")

        target_user_id = str(record["user_id"])

        conn.execute(
            """
            DELETE FROM channel_identities
            WHERE channel = ? AND external_id = ?
            """,
            (channel.strip().lower(), external_id.strip()),
        )
        conn.execute(
            """
            INSERT INTO channel_identities (
                channel,
                external_id,
                user_id,
                verified_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                channel.strip().lower(),
                external_id.strip(),
                target_user_id,
                iso(now),
            ),
        )
        conn.execute(
            """
            UPDATE activation_codes
            SET uses_count = uses_count + 1
            WHERE code_hash = ?
            """,
            (code_hash,),
        )

    return get_account_status(channel, external_id)


def issue_activation_code(
    plan: str = "standard",
    credits: int = PAID_DEFAULT_CREDITS,
    days: int = 30,
    max_uses: int = 2,
) -> str:
    ensure_billing_schema()
    now = utc_now()
    user_id = uuid.uuid4().hex
    period_end = now + timedelta(days=days)

    alphabet = string.ascii_uppercase + string.digits
    raw = "".join(secrets.choice(alphabet) for _ in range(12))
    code = f"TRD-{raw[:4]}-{raw[4:8]}-{raw[8:12]}"

    with db() as conn:
        conn.execute(
            """
            INSERT INTO users (
                user_id,
                plan,
                status,
                trial_started_at,
                trial_expires_at,
                credit_balance,
                credit_limit,
                billing_period_start,
                billing_period_end,
                created_at,
                updated_at
            ) VALUES (?, ?, 'active', NULL, NULL, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                plan.strip().lower(),
                credits,
                credits,
                iso(now),
                iso(period_end),
                iso(now),
                iso(now),
            ),
        )
        conn.execute(
            """
            INSERT INTO activation_codes (
                code_hash,
                user_id,
                expires_at,
                max_uses,
                uses_count,
                created_at
            ) VALUES (?, ?, ?, ?, 0, ?)
            """,
            (
                hash_activation_code(code),
                user_id,
                iso(now + timedelta(days=7)),
                max_uses,
                iso(now),
            ),
        )

    return code

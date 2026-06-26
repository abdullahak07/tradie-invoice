from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from invoice_routes import db


def ensure_gemini_usage_table() -> None:
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gemini_usage (
                event_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                model TEXT NOT NULL,
                operation TEXT NOT NULL,
                success INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                thinking_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT ''
            )
        """)


def record_gemini_usage(
    model: str,
    operation: str,
    success: bool,
    latency_ms: int,
    usage: Any = None,
    error: str = "",
) -> None:
    ensure_gemini_usage_table()

    def token_value(name: str) -> int:
        return int(getattr(usage, name, 0) or 0) if usage else 0

    with db() as conn:
        conn.execute("""
            INSERT INTO gemini_usage (
                event_id,
                created_at,
                model,
                operation,
                success,
                latency_ms,
                prompt_tokens,
                output_tokens,
                thinking_tokens,
                total_tokens,
                error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            uuid.uuid4().hex,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            model,
            operation,
            1 if success else 0,
            int(latency_ms),
            token_value("prompt_token_count"),
            token_value("candidates_token_count"),
            token_value("thoughts_token_count"),
            token_value("total_token_count"),
            str(error)[:500],
        ))

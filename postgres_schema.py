from __future__ import annotations

import os

import psycopg


def init_postgres_schema() -> None:
    database_url = os.getenv("DATABASE_URL", "").strip()

    if not database_url:
        print("DATABASE_URL not configured; skipping PostgreSQL schema setup")
        return

    statements = [
        """
        CREATE TABLE IF NOT EXISTS invoices (
            id BIGSERIAL PRIMARY KEY,
            invoice_number TEXT UNIQUE NOT NULL,
            source_message TEXT NOT NULL,
            customer_json TEXT NOT NULL,
            items_json TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            due_date TEXT NOT NULL,
            subtotal DOUBLE PRECISION NOT NULL,
            gst DOUBLE PRECISION NOT NULL,
            total DOUBLE PRECISION NOT NULL,
            gst_included BOOLEAN NOT NULL DEFAULT FALSE,
            status TEXT NOT NULL DEFAULT 'draft',
            delivery_json TEXT NOT NULL DEFAULT '[]',
            pdf_path TEXT,
            created_at TEXT NOT NULL,
            sent_at TEXT,
            paid_at TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS reminder_log (
            id BIGSERIAL PRIMARY KEY,
            invoice_id BIGINT NOT NULL,
            reminder_key TEXT NOT NULL,
            sent_at TEXT NOT NULL,
            UNIQUE(invoice_id, reminder_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS telegram_sessions (
            chat_id TEXT PRIMARY KEY,
            invoice_id BIGINT,
            state TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            pending_text TEXT NOT NULL DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS telegram_messages (
            id BIGSERIAL PRIMARY KEY,
            chat_id TEXT NOT NULL,
            invoice_id BIGINT,
            direction TEXT NOT NULL,
            body TEXT NOT NULL,
            telegram_message_id TEXT,
            created_at TEXT NOT NULL
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_invoices_status
        ON invoices(status)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_invoices_created_at
        ON invoices(created_at)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_telegram_messages_chat_id
        ON telegram_messages(chat_id)
        """
    ]

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)

    print("PostgreSQL schema ready")


if __name__ == "__main__":
    init_postgres_schema()

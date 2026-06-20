from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import psycopg
from psycopg.rows import dict_row


BASE_DIR = Path(__file__).resolve().parent
SQLITE_PATH = BASE_DIR / "data" / "message_invoices.db"


def sqlite_table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def migrate_sqlite_to_postgres() -> dict[str, int]:
    database_url = os.getenv("DATABASE_URL", "").strip()

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured")

    if not SQLITE_PATH.exists():
        print(f"SQLite database not found: {SQLITE_PATH}")
        return {
            "invoices": 0,
            "reminder_log": 0,
            "telegram_sessions": 0,
            "telegram_messages": 0,
        }

    counts = {
        "invoices": 0,
        "reminder_log": 0,
        "telegram_sessions": 0,
        "telegram_messages": 0,
    }

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    sqlite_conn.row_factory = sqlite3.Row

    try:
        with psycopg.connect(database_url, row_factory=dict_row) as pg_conn:
            with pg_conn.cursor() as cursor:

                if sqlite_table_exists(sqlite_conn, "invoices"):
                    rows = sqlite_conn.execute(
                        "SELECT * FROM invoices ORDER BY id"
                    ).fetchall()

                    for row in rows:
                        cursor.execute(
                            """
                            INSERT INTO invoices (
                                id, invoice_number, source_message,
                                customer_json, items_json, notes,
                                due_date, subtotal, gst, total,
                                gst_included, status, delivery_json,
                                pdf_path, created_at, sent_at, paid_at
                            )
                            VALUES (
                                %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s, %s, %s, %s, %s,
                                %s, %s, %s
                            )
                            ON CONFLICT (id) DO UPDATE SET
                                invoice_number = EXCLUDED.invoice_number,
                                source_message = EXCLUDED.source_message,
                                customer_json = EXCLUDED.customer_json,
                                items_json = EXCLUDED.items_json,
                                notes = EXCLUDED.notes,
                                due_date = EXCLUDED.due_date,
                                subtotal = EXCLUDED.subtotal,
                                gst = EXCLUDED.gst,
                                total = EXCLUDED.total,
                                gst_included = EXCLUDED.gst_included,
                                status = EXCLUDED.status,
                                delivery_json = EXCLUDED.delivery_json,
                                pdf_path = EXCLUDED.pdf_path,
                                created_at = EXCLUDED.created_at,
                                sent_at = EXCLUDED.sent_at,
                                paid_at = EXCLUDED.paid_at
                            """,
                            (
                                row["id"],
                                row["invoice_number"],
                                row["source_message"],
                                row["customer_json"],
                                row["items_json"],
                                row["notes"],
                                row["due_date"],
                                row["subtotal"],
                                row["gst"],
                                row["total"],
                                bool(row["gst_included"]),
                                row["status"],
                                row["delivery_json"],
                                row["pdf_path"],
                                row["created_at"],
                                row["sent_at"],
                                row["paid_at"],
                            ),
                        )

                    counts["invoices"] = len(rows)

                if sqlite_table_exists(sqlite_conn, "reminder_log"):
                    rows = sqlite_conn.execute(
                        "SELECT * FROM reminder_log ORDER BY id"
                    ).fetchall()

                    for row in rows:
                        cursor.execute(
                            """
                            INSERT INTO reminder_log (
                                id, invoice_id, reminder_key, sent_at
                            )
                            VALUES (%s, %s, %s, %s)
                            ON CONFLICT (invoice_id, reminder_key)
                            DO UPDATE SET sent_at = EXCLUDED.sent_at
                            """,
                            (
                                row["id"],
                                row["invoice_id"],
                                row["reminder_key"],
                                row["sent_at"],
                            ),
                        )

                    counts["reminder_log"] = len(rows)

                if sqlite_table_exists(sqlite_conn, "telegram_sessions"):
                    columns = {
                        row["name"]
                        for row in sqlite_conn.execute(
                            "PRAGMA table_info(telegram_sessions)"
                        ).fetchall()
                    }

                    has_pending_text = "pending_text" in columns

                    rows = sqlite_conn.execute(
                        "SELECT * FROM telegram_sessions"
                    ).fetchall()

                    for row in rows:
                        pending_text = (
                            row["pending_text"]
                            if has_pending_text
                            else ""
                        )

                        cursor.execute(
                            """
                            INSERT INTO telegram_sessions (
                                chat_id, invoice_id, state,
                                updated_at, pending_text
                            )
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (chat_id) DO UPDATE SET
                                invoice_id = EXCLUDED.invoice_id,
                                state = EXCLUDED.state,
                                updated_at = EXCLUDED.updated_at,
                                pending_text = EXCLUDED.pending_text
                            """,
                            (
                                row["chat_id"],
                                row["invoice_id"],
                                row["state"],
                                row["updated_at"],
                                pending_text,
                            ),
                        )

                    counts["telegram_sessions"] = len(rows)

                if sqlite_table_exists(sqlite_conn, "telegram_messages"):
                    rows = sqlite_conn.execute(
                        "SELECT * FROM telegram_messages ORDER BY id"
                    ).fetchall()

                    for row in rows:
                        cursor.execute(
                            """
                            INSERT INTO telegram_messages (
                                id, chat_id, invoice_id, direction,
                                body, telegram_message_id, created_at
                            )
                            VALUES (%s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT (id) DO UPDATE SET
                                chat_id = EXCLUDED.chat_id,
                                invoice_id = EXCLUDED.invoice_id,
                                direction = EXCLUDED.direction,
                                body = EXCLUDED.body,
                                telegram_message_id =
                                    EXCLUDED.telegram_message_id,
                                created_at = EXCLUDED.created_at
                            """,
                            (
                                row["id"],
                                row["chat_id"],
                                row["invoice_id"],
                                row["direction"],
                                row["body"],
                                row["telegram_message_id"],
                                row["created_at"],
                            ),
                        )

                    counts["telegram_messages"] = len(rows)

                for table in (
                    "invoices",
                    "reminder_log",
                    "telegram_messages",
                ):
                    cursor.execute(
                        f"""
                        SELECT setval(
                            pg_get_serial_sequence('{table}', 'id'),
                            COALESCE((SELECT MAX(id) FROM {table}), 1),
                            EXISTS(SELECT 1 FROM {table})
                        )
                        """
                    )

        print("SQLite to PostgreSQL migration completed")
        for table, count in counts.items():
            print(f"{table}: {count}")

        return counts

    finally:
        sqlite_conn.close()


if __name__ == "__main__":
    migrate_sqlite_to_postgres()

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row


def using_postgres() -> bool:
    return (
        os.getenv("USE_POSTGRES", "").strip().lower() == "true"
        and bool(os.getenv("DATABASE_URL", "").strip())
    )


class PostgresCursor:
    def __init__(self, cursor, lastrowid: int | None = None):
        self._cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


class PostgresConnection:
    def __init__(self):
        database_url = os.getenv("DATABASE_URL", "").strip()
        if not database_url:
            raise RuntimeError("DATABASE_URL is not configured")

        self._conn = psycopg.connect(
            database_url,
            row_factory=dict_row,
        )

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()

    @staticmethod
    def _translate_sql(sql: str) -> str:
        translated = sql.replace("?", "%s")

        if "INSERT OR IGNORE INTO" in translated.upper():
            translated = translated.replace(
                "INSERT OR IGNORE INTO",
                "INSERT INTO",
            )
            translated = translated.rstrip().rstrip(";")
            translated += " ON CONFLICT DO NOTHING"

        return translated

    def execute(
        self,
        sql: str,
        params: tuple[Any, ...] | list[Any] = (),
    ) -> PostgresCursor:
        translated = self._translate_sql(sql)
        normalised = " ".join(translated.split()).upper()

        cursor = self._conn.cursor()

        if (
            normalised.startswith("INSERT INTO INVOICES")
            and "RETURNING ID" not in normalised
        ):
            translated = translated.rstrip().rstrip(";") + " RETURNING id"
            cursor.execute(translated, params)
            row = cursor.fetchone()
            inserted_id = row["id"] if isinstance(row, dict) else row[0]
            return PostgresCursor(cursor, lastrowid=inserted_id)

        cursor.execute(translated, params)
        return PostgresCursor(cursor)


def open_app_db(sqlite_path: Path):
    if using_postgres():
        return PostgresConnection()

    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    return connection

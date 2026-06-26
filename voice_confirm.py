from __future__ import annotations

from datetime import datetime
from typing import Any

import telegram_routes
import whatsapp_routes


def ensure_table() -> None:
    with telegram_routes.db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_voice_transcriptions (
                channel TEXT NOT NULL,
                external_id TEXT NOT NULL,
                transcript TEXT NOT NULL,
                state TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (channel, external_id)
            )
            """
        )


def save(channel: str, external_id: str, transcript: str, state: str = "confirm") -> None:
    ensure_table()
    with telegram_routes.db() as conn:
        conn.execute(
            """
            INSERT INTO pending_voice_transcriptions
                (channel, external_id, transcript, state, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel, external_id) DO UPDATE SET
                transcript=excluded.transcript,
                state=excluded.state,
                updated_at=excluded.updated_at
            """,
            (channel, external_id, transcript.strip(), state, datetime.now().isoformat(timespec="seconds")),
        )


def get(channel: str, external_id: str):
    ensure_table()
    with telegram_routes.db() as conn:
        return conn.execute(
            "SELECT * FROM pending_voice_transcriptions WHERE channel=? AND external_id=?",
            (channel, external_id),
        ).fetchone()


def clear(channel: str, external_id: str) -> None:
    ensure_table()
    with telegram_routes.db() as conn:
        conn.execute(
            "DELETE FROM pending_voice_transcriptions WHERE channel=? AND external_id=?",
            (channel, external_id),
        )


def telegram_buttons() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ ACCEPT", "callback_data": "voice_accept:0"},
                {"text": "✏️ EDIT", "callback_data": "voice_edit:0"},
            ],
            [{"text": "❌ CANCEL", "callback_data": "voice_cancel:0"}],
        ]
    }


async def send_telegram_preview(chat_id: str, transcript: str) -> None:
    await telegram_routes.send_telegram(
        chat_id,
        f"📝 I heard:\n\n{transcript}\n\nCheck this before I create anything.",
        telegram_buttons(),
    )


async def send_whatsapp_preview(sender: str, transcript: str) -> None:
    await whatsapp_routes.send_whatsapp_text(sender, f"📝 I heard:\n\n{transcript}")
    await whatsapp_routes.send_whatsapp_interactive(
        sender,
        {
            "type": "button",
            "body": {"text": "Is this transcription correct?"},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": "voice_accept:0", "title": "Accept"}},
                    {"type": "reply", "reply": {"id": "voice_edit:0", "title": "Edit"}},
                    {"type": "reply", "reply": {"id": "voice_cancel:0", "title": "Cancel"}},
                ]
            },
        },
    )

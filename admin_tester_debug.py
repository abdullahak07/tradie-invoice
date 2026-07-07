from __future__ import annotations

import html
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse

from admin_dashboard import admin_login
from invoice_routes import db

router = APIRouter(prefix="/admin/tester-debug", tags=["admin-tester-debug"])


def _row(row) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return {key: row[key] for key in row.keys()}


def _rows(cursor) -> list[dict[str, Any]]:
    return [_row(row) for row in cursor.fetchall()]


def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


def _exists(conn, table: str) -> bool:
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return True
    except Exception:
        pass
    try:
        return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", (table,)).fetchone())
    except Exception:
        return False


def _load(limit: int) -> dict[str, list[dict[str, Any]]]:
    with db() as conn:
        users = _rows(conn.execute(
            """
            SELECT bp.user_id, bp.business_name, bp.trade_type, ci.channel, ci.external_id,
                   u.plan, u.status, bb.logo_path, bb.letterhead_path
            FROM business_profiles bp
            LEFT JOIN channel_identities ci ON ci.user_id = bp.user_id
            LEFT JOIN users u ON u.user_id = bp.user_id
            LEFT JOIN business_branding bb ON bb.user_id = bp.user_id
            ORDER BY bp.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )) if _exists(conn, "business_profiles") else []
        invoices = _rows(conn.execute(
            """
            SELECT id, invoice_number, source_message, customer_json, items_json,
                   total, status, pdf_path, created_at
            FROM invoices
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )) if _exists(conn, "invoices") else []
        quotes = _rows(conn.execute(
            """
            SELECT id, quote_number, source_message, customer_json, items_json,
                   total, status, created_at
            FROM quotes
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        )) if _exists(conn, "quotes") else []
        onboarding = _rows(conn.execute(
            """
            SELECT channel, external_id, user_id, state, source_path, logo_path,
                   extracted_json, updated_at
            FROM onboarding_sessions
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )) if _exists(conn, "onboarding_sessions") else []
        telegram = _rows(conn.execute(
            """
            SELECT chat_id, direction, body, invoice_id, telegram_message_id, created_at
            FROM telegram_messages
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )) if _exists(conn, "telegram_messages") else []
    return {"users": users, "invoices": invoices, "quotes": quotes, "onboarding": onboarding, "telegram": telegram}


@router.get("/api")
def api(limit: int = Query(default=100, ge=1, le=500), _: str = Depends(admin_login)) -> JSONResponse:
    return JSONResponse(_load(limit))


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def page(limit: int = Query(default=100, ge=1, le=500), _: str = Depends(admin_login)) -> HTMLResponse:
    data = _load(limit)
    users = "".join(f"<tr><td>{_esc(x.get('business_name'))}</td><td>{_esc(x.get('trade_type'))}</td><td>{_esc(x.get('channel'))}</td><td>{_esc(x.get('external_id'))}</td><td>{_esc(x.get('plan'))}/{_esc(x.get('status'))}</td><td><pre>{_esc(x.get('logo_path'))}\n{_esc(x.get('letterhead_path'))}</pre></td></tr>" for x in data['users'])
    invoices = "".join(f"<tr><td>{_esc(x.get('created_at'))}</td><td>{_esc(x.get('invoice_number'))}</td><td>{_esc(x.get('status'))}</td><td>${float(x.get('total') or 0):,.2f}</td><td><pre>{_esc(x.get('source_message'))}</pre></td><td><pre>{_esc(x.get('pdf_path'))}</pre></td></tr>" for x in data['invoices'])
    quotes = "".join(f"<tr><td>{_esc(x.get('created_at'))}</td><td>{_esc(x.get('quote_number'))}</td><td>{_esc(x.get('status'))}</td><td>${float(x.get('total') or 0):,.2f}</td><td><pre>{_esc(x.get('source_message'))}</pre></td></tr>" for x in data['quotes'])
    onboarding = "".join(f"<tr><td>{_esc(x.get('updated_at'))}</td><td>{_esc(x.get('channel'))}</td><td>{_esc(x.get('external_id'))}</td><td>{_esc(x.get('state'))}</td><td><pre>{_esc(x.get('source_path'))}\n{_esc(x.get('logo_path'))}</pre></td><td><pre>{_esc(x.get('extracted_json'))}</pre></td></tr>" for x in data['onboarding'])
    telegram = "".join(f"<tr><td>{_esc(x.get('created_at'))}</td><td>{_esc(x.get('chat_id'))}</td><td>{_esc(x.get('direction'))}</td><td><pre>{_esc(x.get('body'))}</pre></td></tr>" for x in data['telegram'])
    body = f"""
<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Tester Debug</title>
<style>body{{margin:0;background:#07111f;color:#fff;font-family:Arial,sans-serif}}.page{{max-width:1500px;margin:auto;padding:20px}}a{{color:#7cc7ff}}.nav a{{display:inline-block;background:#14263d;border:1px solid #274260;border-radius:9px;padding:9px 12px;margin-right:8px;text-decoration:none;color:#fff}}.panel{{background:#102238;border:1px solid #23364e;border-radius:14px;padding:16px;margin-top:16px}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;vertical-align:top;padding:8px;border-bottom:1px solid #23364e}}pre{{white-space:pre-wrap;max-width:720px;margin:0;color:#d9ecff}}</style></head>
<body><div class='page'><h1>Tester Debug</h1><div class='nav'><a href='/admin'>Home</a><a href='/admin/tester-debug/api' target='_blank'>Raw JSON</a></div>
<div class='panel'><h2>Users</h2><table><thead><tr><th>Business</th><th>Trade</th><th>Channel</th><th>Identity</th><th>Plan</th><th>Stored file paths</th></tr></thead><tbody>{users or '<tr><td colspan="6">No users found.</td></tr>'}</tbody></table></div>
<div class='panel'><h2>Invoice prompts</h2><table><thead><tr><th>Created</th><th>Invoice</th><th>Status</th><th>Total</th><th>Original prompt</th><th>PDF path</th></tr></thead><tbody>{invoices or '<tr><td colspan="6">No invoices found.</td></tr>'}</tbody></table></div>
<div class='panel'><h2>Quote prompts</h2><table><thead><tr><th>Created</th><th>Quote</th><th>Status</th><th>Total</th><th>Original prompt</th></tr></thead><tbody>{quotes or '<tr><td colspan="5">No quotes found.</td></tr>'}</tbody></table></div>
<div class='panel'><h2>Onboarding uploads and extraction</h2><table><thead><tr><th>Updated</th><th>Channel</th><th>Identity</th><th>State</th><th>Stored file paths</th><th>Extracted JSON</th></tr></thead><tbody>{onboarding or '<tr><td colspan="6">No onboarding records found.</td></tr>'}</tbody></table></div>
<div class='panel'><h2>Telegram messages</h2><table><thead><tr><th>Time</th><th>Chat</th><th>Direction</th><th>Body</th></tr></thead><tbody>{telegram or '<tr><td colspan="4">No Telegram messages found.</td></tr>'}</tbody></table></div>
</div></body></html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from admin_dashboard import admin_login
from invoice_routes import db

router = APIRouter(prefix="/admin/controls", tags=["admin-controls"])


class CreditAdjustment(BaseModel):
    amount: int = Field(ge=-100000, le=100000)
    reason: str = Field(min_length=3, max_length=300)


class TrialExtension(BaseModel):
    days: int = Field(ge=1, le=3650)
    reason: str = Field(min_length=3, max_length=300)


class PlanUpdate(BaseModel):
    plan: Literal["trial", "standard", "premium"]
    credit_limit: int | None = Field(default=None, ge=0, le=1000000)
    reason: str = Field(min_length=3, max_length=300)


class UserStatusUpdate(BaseModel):
    status: Literal["active", "disabled"]
    reason: str = Field(min_length=3, max_length=300)


class DocumentStatusUpdate(BaseModel):
    status: str = Field(min_length=2, max_length=50)
    reason: str = Field(min_length=3, max_length=300)


INVOICE_STATUSES = {
    "awaiting_confirmation",
    "draft",
    "pdf_generated",
    "sent",
    "paid",
    "cancelled",
}

QUOTE_STATUSES = {
    "awaiting_confirmation",
    "draft",
    "pdf_generated",
    "sent",
    "accepted",
    "rejected",
    "converted",
    "cancelled",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def rows(query: str, params: tuple[Any, ...] = ()) -> list[Any]:
    try:
        with db() as conn:
            return list(conn.execute(query, params).fetchall())
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database error: {str(exc)[:200]}") from exc


def ensure_audit_table() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                audit_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                admin_username TEXT NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                before_json TEXT NOT NULL DEFAULT '{}',
                after_json TEXT NOT NULL DEFAULT '{}',
                reason TEXT NOT NULL DEFAULT '',
                success INTEGER NOT NULL DEFAULT 1,
                error TEXT NOT NULL DEFAULT ''
            )
            """
        )


def row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def get_user(user_id: str) -> dict[str, Any]:
    result = rows("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if not result:
        raise HTTPException(status_code=404, detail="User not found")
    return row_to_dict(result[0])


def get_document(kind: str, document_id: int) -> dict[str, Any]:
    table = "invoices" if kind == "invoice" else "quotes"
    result = rows(f"SELECT * FROM {table} WHERE id = ?", (document_id,))
    if not result:
        raise HTTPException(status_code=404, detail=f"{kind.title()} not found")
    return row_to_dict(result[0])


def audit(
    admin: str,
    action: str,
    target_type: str,
    target_id: str,
    before: dict[str, Any],
    after: dict[str, Any],
    reason: str,
    success: bool = True,
    error: str = "",
) -> None:
    ensure_audit_table()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO admin_audit_log (
                audit_id, created_at, admin_username, action,
                target_type, target_id, before_json, after_json,
                reason, success, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                iso(utc_now()),
                admin,
                action,
                target_type,
                str(target_id),
                json.dumps(before, default=str, sort_keys=True),
                json.dumps(after, default=str, sort_keys=True),
                reason,
                1 if success else 0,
                error[:500],
            ),
        )


def parse_datetime(value: Any) -> datetime:
    if not value:
        return utc_now()
    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@router.on_event("startup")
def startup() -> None:
    ensure_audit_table()


@router.get("/api/users")
def list_users(
    search: str = Query(default=""),
    limit: int = Query(default=100, ge=1, le=500),
    _: str = Depends(admin_login),
) -> JSONResponse:
    term = f"%{search.strip().upper()}%"
    result = rows(
        """
        SELECT u.*, ci.channel, ci.external_id
        FROM users u
        LEFT JOIN channel_identities ci ON ci.user_id = u.user_id
        WHERE ? = '%%'
           OR UPPER(u.user_id) LIKE ?
           OR UPPER(COALESCE(ci.external_id, '')) LIKE ?
        ORDER BY u.updated_at DESC
        LIMIT ?
        """,
        (term, term, term, limit),
    )
    payload = []
    for row in result:
        data = row_to_dict(row)
        data["external_id"] = str(data.get("external_id") or "")
        payload.append(data)
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.post("/api/users/{user_id}/credits")
def adjust_credits(
    user_id: str,
    request: CreditAdjustment,
    admin: str = Depends(admin_login),
) -> JSONResponse:
    before = get_user(user_id)
    old_balance = int(before["credit_balance"])
    new_balance = old_balance + request.amount
    if new_balance < 0:
        raise HTTPException(status_code=400, detail="Credit balance cannot become negative")

    with db() as conn:
        conn.execute(
            "UPDATE users SET credit_balance = ?, updated_at = ? WHERE user_id = ?",
            (new_balance, iso(utc_now()), user_id),
        )

    after = get_user(user_id)
    audit(admin, "adjust_credits", "user", user_id, before, after, request.reason)
    return JSONResponse({"ok": True, "user": after})


@router.post("/api/users/{user_id}/trial")
def extend_trial(
    user_id: str,
    request: TrialExtension,
    admin: str = Depends(admin_login),
) -> JSONResponse:
    before = get_user(user_id)
    current_expiry = parse_datetime(before.get("trial_expires_at"))
    base = max(current_expiry, utc_now())
    new_expiry = base + timedelta(days=request.days)

    with db() as conn:
        conn.execute(
            "UPDATE users SET trial_expires_at = ?, updated_at = ? WHERE user_id = ?",
            (iso(new_expiry), iso(utc_now()), user_id),
        )

    after = get_user(user_id)
    audit(admin, "extend_trial", "user", user_id, before, after, request.reason)
    return JSONResponse({"ok": True, "user": after})


@router.post("/api/users/{user_id}/plan")
def update_plan(
    user_id: str,
    request: PlanUpdate,
    admin: str = Depends(admin_login),
) -> JSONResponse:
    before = get_user(user_id)
    credit_limit = request.credit_limit
    if credit_limit is None:
        defaults = {"trial": 30, "standard": 150, "premium": 500}
        credit_limit = defaults[request.plan]

    new_balance = min(int(before["credit_balance"]), credit_limit)
    with db() as conn:
        conn.execute(
            """
            UPDATE users
            SET plan = ?, credit_limit = ?, credit_balance = ?, updated_at = ?
            WHERE user_id = ?
            """,
            (request.plan, credit_limit, new_balance, iso(utc_now()), user_id),
        )

    after = get_user(user_id)
    audit(admin, "update_plan", "user", user_id, before, after, request.reason)
    return JSONResponse({"ok": True, "user": after})


@router.post("/api/users/{user_id}/status")
def update_user_status(
    user_id: str,
    request: UserStatusUpdate,
    admin: str = Depends(admin_login),
) -> JSONResponse:
    before = get_user(user_id)
    with db() as conn:
        conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE user_id = ?",
            (request.status, iso(utc_now()), user_id),
        )
    after = get_user(user_id)
    audit(admin, "update_user_status", "user", user_id, before, after, request.reason)
    return JSONResponse({"ok": True, "user": after})


@router.post("/api/documents/{kind}/{document_id}/status")
def update_document_status(
    kind: Literal["invoice", "quote"],
    document_id: int,
    request: DocumentStatusUpdate,
    admin: str = Depends(admin_login),
) -> JSONResponse:
    allowed = INVOICE_STATUSES if kind == "invoice" else QUOTE_STATUSES
    if request.status not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status. Allowed: {', '.join(sorted(allowed))}",
        )

    before = get_document(kind, document_id)
    table = "invoices" if kind == "invoice" else "quotes"
    with db() as conn:
        conn.execute(
            f"UPDATE {table} SET status = ? WHERE id = ?",
            (request.status, document_id),
        )
    after = get_document(kind, document_id)
    audit(
        admin,
        "update_document_status",
        kind,
        str(document_id),
        before,
        after,
        request.reason,
    )
    return JSONResponse({"ok": True, "document": after})


@router.get("/api/audit")
def audit_log(
    limit: int = Query(default=100, ge=1, le=1000),
    _: str = Depends(admin_login),
) -> JSONResponse:
    ensure_audit_table()
    result = rows(
        """
        SELECT audit_id, created_at, admin_username, action,
               target_type, target_id, reason, success, error
        FROM admin_audit_log
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    return JSONResponse([row_to_dict(row) for row in result])


def export_query(kind: str) -> tuple[str, tuple[Any, ...]]:
    if kind == "users":
        return (
            """
            SELECT u.user_id, ci.channel, ci.external_id, u.plan, u.status,
                   u.credit_balance, u.credit_limit, u.trial_expires_at,
                   u.billing_period_end, u.created_at, u.updated_at
            FROM users u
            LEFT JOIN channel_identities ci ON ci.user_id = u.user_id
            ORDER BY u.updated_at DESC
            """,
            (),
        )
    if kind == "invoices":
        return (
            """
            SELECT id, invoice_number, customer_json, subtotal, gst, total,
                   status, created_at, pdf_path
            FROM invoices
            ORDER BY created_at DESC
            """,
            (),
        )
    if kind == "quotes":
        return (
            """
            SELECT id, quote_number, customer_json, subtotal, gst, total,
                   status, created_at, pdf_path, converted_invoice_id
            FROM quotes
            ORDER BY created_at DESC
            """,
            (),
        )
    raise HTTPException(status_code=400, detail="Unsupported export type")


@router.get("/export/{kind}.csv")
def export_csv(
    kind: Literal["users", "invoices", "quotes"],
    _: str = Depends(admin_login),
) -> StreamingResponse:
    query, params = export_query(kind)
    result = rows(query, params)
    output = io.StringIO()
    writer = csv.writer(output)

    if result:
        headers = list(result[0].keys())
        writer.writerow(headers)
        for row in result:
            writer.writerow([row[key] for key in headers])

    data = output.getvalue().encode("utf-8-sig")
    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="tradie-{kind}-{utc_now().date().isoformat()}.csv"'
        },
    )


CONTROLS_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Admin Controls</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--panel:#102238;--muted:#9fb1c5;--green:#43df87;--blue:#43a5ff;--red:#ff6b6b;--border:#23364e}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#fff;font-family:Arial,sans-serif}.page{max-width:1700px;margin:auto;padding:20px}.head{display:flex;justify-content:space-between;gap:15px;align-items:center}a{color:var(--blue)}.panel{background:var(--panel);border:1px solid var(--border);border-radius:15px;padding:18px;margin-top:16px}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}button,input,select{background:#152b46;color:#fff;border:1px solid var(--border);border-radius:9px;padding:9px 11px}button{cursor:pointer}.danger{background:#6d1f28}.success{background:#14532d}.muted{color:var(--muted)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--border)}th{color:var(--muted)}.scroll{overflow:auto}.message{margin-top:12px;padding:10px;border-radius:9px;display:none}.message.ok{display:block;background:#123d27;color:var(--green)}.message.bad{display:block;background:#4a2025;color:#ff9da5}.audit{max-height:420px;overflow:auto}@media(max-width:900px){.head{align-items:flex-start;flex-direction:column}}
</style>
</head>
<body><div class="page">
<div class="head"><div><h1>Admin Controls</h1><div class="muted"><a href="/admin">Business dashboard</a> | <a href="/admin/railway">Railway monitoring</a></div></div><div id="live" class="muted">Ready</div></div>
<div class="panel"><h2>User management</h2><div class="toolbar"><input id="userSearch" placeholder="Search phone or user ID"><button onclick="loadUsers()">Search</button><a href="/admin/controls/export/users.csv"><button>Export users CSV</button></a></div><div class="scroll"><table><thead><tr><th>Channel</th><th>User</th><th>Plan</th><th>Status</th><th>Credits</th><th>Trial expiry</th><th>Actions</th></tr></thead><tbody id="users"></tbody></table></div><div id="message" class="message"></div></div>
<div class="panel"><h2>Exports</h2><div class="toolbar"><a href="/admin/controls/export/invoices.csv"><button>Export invoices CSV</button></a><a href="/admin/controls/export/quotes.csv"><button>Export quotes CSV</button></a></div></div>
<div class="panel"><h2>Admin audit log</h2><button onclick="loadAudit()">Refresh audit</button><div class="scroll audit"><table><thead><tr><th>Time</th><th>Admin</th><th>Action</th><th>Target</th><th>Reason</th></tr></thead><tbody id="audit"></tbody></table></div></div>
</div>
<script>
function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function show(text,ok=true){const m=document.getElementById('message');m.textContent=text;m.className='message '+(ok?'ok':'bad')}
async function api(url,options={}){const r=await fetch(url,{cache:'no-store',headers:{'Content-Type':'application/json',...(options.headers||{})},...options});const body=await r.json().catch(()=>({detail:'Request failed'}));if(!r.ok)throw new Error(body.detail||'Request failed');return body}
async function loadUsers(){try{const search=document.getElementById('userSearch').value;const data=await api('/admin/controls/api/users?search='+encodeURIComponent(search));document.getElementById('users').innerHTML=data.map(u=>`<tr><td>${esc(u.channel||'')}</td><td>${esc(u.external_id||u.user_id)}</td><td>${esc(u.plan)}</td><td>${esc(u.status)}</td><td>${u.credit_balance}/${u.credit_limit}</td><td>${esc(u.trial_expires_at||'')}</td><td><button onclick="credits('${u.user_id}')">Credits</button><button onclick="trial('${u.user_id}')">Extend trial</button><button onclick="plan('${u.user_id}')">Plan</button><button class="${u.status==='active'?'danger':'success'}" onclick="statusChange('${u.user_id}','${u.status==='active'?'disabled':'active'}')">${u.status==='active'?'Disable':'Enable'}</button></td></tr>`).join('');document.getElementById('live').textContent='Loaded '+data.length+' users'}catch(e){show(e.message,false)}}
async function credits(id){const amount=Number(prompt('Credit adjustment. Use positive to add or negative to remove:','10'));if(!Number.isInteger(amount))return;const reason=prompt('Reason:','Manual admin credit adjustment');if(!reason)return;if(!confirm(`Apply ${amount} credits?`))return;try{await api(`/admin/controls/api/users/${id}/credits`,{method:'POST',body:JSON.stringify({amount,reason})});show('Credits updated');loadUsers();loadAudit()}catch(e){show(e.message,false)}}
async function trial(id){const days=Number(prompt('Days to extend:','30'));if(!Number.isInteger(days))return;const reason=prompt('Reason:','Manual trial extension');if(!reason)return;if(!confirm(`Extend trial by ${days} days?`))return;try{await api(`/admin/controls/api/users/${id}/trial`,{method:'POST',body:JSON.stringify({days,reason})});show('Trial extended');loadUsers();loadAudit()}catch(e){show(e.message,false)}}
async function plan(id){const value=prompt('Plan: trial, standard, or premium','standard');if(!value)return;const reason=prompt('Reason:','Manual plan change');if(!reason)return;if(!confirm(`Change plan to ${value}?`))return;try{await api(`/admin/controls/api/users/${id}/plan`,{method:'POST',body:JSON.stringify({plan:value,reason})});show('Plan updated');loadUsers();loadAudit()}catch(e){show(e.message,false)}}
async function statusChange(id,status){const reason=prompt('Reason:',status==='disabled'?'Account disabled by admin':'Account enabled by admin');if(!reason)return;if(!confirm(`Set account status to ${status}?`))return;try{await api(`/admin/controls/api/users/${id}/status`,{method:'POST',body:JSON.stringify({status,reason})});show('Account status updated');loadUsers();loadAudit()}catch(e){show(e.message,false)}}
async function loadAudit(){try{const data=await api('/admin/controls/api/audit');document.getElementById('audit').innerHTML=data.map(x=>`<tr><td>${esc(x.created_at)}</td><td>${esc(x.admin_username)}</td><td>${esc(x.action)}</td><td>${esc(x.target_type)} ${esc(x.target_id)}</td><td>${esc(x.reason)}</td></tr>`).join('')}catch(e){show(e.message,false)}}
loadUsers();loadAudit();setInterval(loadAudit,15000);
</script></body></html>
"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def controls_page(_: str = Depends(admin_login)) -> HTMLResponse:
    ensure_audit_table()
    return HTMLResponse(
        CONTROLS_HTML,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, max-age=0", "X-Frame-Options": "DENY"},
    )

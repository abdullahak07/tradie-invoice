from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from invoice_routes import db

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def admin_login(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "").strip()
    if not username or not password:
        raise HTTPException(status_code=503, detail="Admin dashboard is not configured")
    if not (
        secrets.compare_digest(credentials.username, username)
        and secrets.compare_digest(credentials.password, password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin login",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def rows(query: str, params: tuple[Any, ...] = ()) -> list[Any]:
    try:
        with db() as conn:
            return list(conn.execute(query, params).fetchall())
    except Exception:
        return []


def scalar(query: str, params: tuple[Any, ...] = (), default: Any = 0) -> Any:
    result = rows(query, params)
    if not result:
        return default
    row = result[0]
    try:
        return row[0]
    except Exception:
        keys = list(row.keys())
        return row[keys[0]] if keys else default


def table_exists(name: str) -> bool:
    try:
        with db() as conn:
            conn.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone()
        return True
    except Exception:
        return False


def mask(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    if len(text) <= 6:
        return "****" + text[-2:]
    return text[:3] + "****" + text[-3:]


def customer_name(raw: Any) -> str:
    try:
        return str(json.loads(str(raw or "{}")).get("name") or "Customer")
    except Exception:
        return "Customer"


@router.get("/api/summary")
def api_summary(_: str = Depends(admin_login)) -> JSONResponse:
    now = now_utc()
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    payload = {
        "generated_at": iso(now),
        "users": {
            "total": int(scalar("SELECT COUNT(*) FROM users")),
            "active": int(scalar("SELECT COUNT(*) FROM users WHERE status='active'")),
            "trial": int(scalar("SELECT COUNT(*) FROM users WHERE plan='trial'")),
            "paid": int(scalar("SELECT COUNT(*) FROM users WHERE plan<>'trial'")),
        },
        "invoices": {
            "total": int(scalar("SELECT COUNT(*) FROM invoices")),
            "today": int(scalar("SELECT COUNT(*) FROM invoices WHERE created_at>=?", (iso(today),))),
            "value_total": float(scalar("SELECT COALESCE(SUM(total),0) FROM invoices") or 0),
            "value_today": float(scalar("SELECT COALESCE(SUM(total),0) FROM invoices WHERE created_at>=?", (iso(today),)) or 0),
        },
        "quotes": {
            "total": int(scalar("SELECT COUNT(*) FROM quotes")),
            "today": int(scalar("SELECT COUNT(*) FROM quotes WHERE created_at>=?", (iso(today),))),
            "converted": int(scalar("SELECT COUNT(*) FROM quotes WHERE status='converted' OR converted_invoice_id IS NOT NULL")),
        },
        "credits": {
            "today": int(scalar("SELECT COALESCE(SUM(amount),0) FROM credit_events WHERE event_type='pdf_generated' AND created_at>=?", (iso(today),)) or 0),
            "remaining": int(scalar("SELECT COALESCE(SUM(credit_balance),0) FROM users") or 0),
        },
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/api/gemini")
def api_gemini(_: str = Depends(admin_login)) -> JSONResponse:
    if not table_exists("gemini_usage"):
        return JSONResponse({"enabled": False, "calls_today": 0, "failures_today": 0, "prompt_tokens_today": 0, "output_tokens_today": 0, "thinking_tokens_today": 0, "total_tokens_today": 0, "average_latency_ms": 0}, headers={"Cache-Control": "no-store"})
    now = now_utc()
    today = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    start = iso(today)
    payload = {
        "enabled": True,
        "calls_today": int(scalar("SELECT COUNT(*) FROM gemini_usage WHERE created_at>=?", (start,))),
        "failures_today": int(scalar("SELECT COUNT(*) FROM gemini_usage WHERE created_at>=? AND success=0", (start,))),
        "prompt_tokens_today": int(scalar("SELECT COALESCE(SUM(prompt_tokens),0) FROM gemini_usage WHERE created_at>=?", (start,)) or 0),
        "output_tokens_today": int(scalar("SELECT COALESCE(SUM(output_tokens),0) FROM gemini_usage WHERE created_at>=?", (start,)) or 0),
        "thinking_tokens_today": int(scalar("SELECT COALESCE(SUM(thinking_tokens),0) FROM gemini_usage WHERE created_at>=?", (start,)) or 0),
        "total_tokens_today": int(scalar("SELECT COALESCE(SUM(total_tokens),0) FROM gemini_usage WHERE created_at>=?", (start,)) or 0),
        "average_latency_ms": int(float(scalar("SELECT COALESCE(AVG(latency_ms),0) FROM gemini_usage WHERE created_at>=?", (start,)) or 0)),
    }
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/api/users")
def api_users(_: str = Depends(admin_login)) -> JSONResponse:
    result = rows("""
        SELECT u.user_id,u.plan,u.status,u.credit_balance,u.credit_limit,u.updated_at,
               ci.channel,ci.external_id,
               (SELECT COUNT(*) FROM credit_events ce WHERE ce.user_id=u.user_id AND ce.document_type='invoice' AND ce.event_type='pdf_generated') invoice_pdfs,
               (SELECT COUNT(*) FROM credit_events ce WHERE ce.user_id=u.user_id AND ce.document_type='quote' AND ce.event_type='pdf_generated') quote_pdfs,
               (SELECT COUNT(*) FROM rate_events re WHERE re.user_id=u.user_id AND re.operation='ai') ai_requests
        FROM users u
        LEFT JOIN channel_identities ci ON ci.user_id=u.user_id
        ORDER BY u.updated_at DESC
        LIMIT 100
    """)
    payload = [{
        "user_id": str(r["user_id"]),
        "channel": str(r["channel"] or "unknown"),
        "external_id": mask(str(r["external_id"] or "")),
        "plan": str(r["plan"]),
        "status": str(r["status"]),
        "credits": int(r["credit_balance"]),
        "credit_limit": int(r["credit_limit"]),
        "invoice_pdfs": int(r["invoice_pdfs"] or 0),
        "quote_pdfs": int(r["quote_pdfs"] or 0),
        "ai_requests": int(r["ai_requests"] or 0),
        "updated_at": str(r["updated_at"]),
    } for r in result]
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/api/documents")
def api_documents(
    kind: str = Query(default="all", pattern="^(all|invoice|quote)$"),
    search: str = Query(default=""),
    _: str = Depends(admin_login),
) -> JSONResponse:
    term = f"%{search.strip().upper()}%"
    docs: list[dict[str, Any]] = []
    if kind in {"all", "invoice"}:
        for r in rows("SELECT id,invoice_number reference,customer_json,total,status,created_at FROM invoices WHERE ?='%%' OR UPPER(invoice_number) LIKE ? OR UPPER(customer_json) LIKE ? ORDER BY created_at DESC LIMIT 100", (term, term, term)):
            docs.append({"kind": "invoice", "id": int(r["id"]), "reference": str(r["reference"]), "customer": customer_name(r["customer_json"]), "total": float(r["total"] or 0), "status": str(r["status"]), "created_at": str(r["created_at"])})
    if kind in {"all", "quote"}:
        for r in rows("SELECT id,quote_number reference,customer_json,total,status,created_at FROM quotes WHERE ?='%%' OR UPPER(quote_number) LIKE ? OR UPPER(customer_json) LIKE ? ORDER BY created_at DESC LIMIT 100", (term, term, term)):
            docs.append({"kind": "quote", "id": int(r["id"]), "reference": str(r["reference"]), "customer": customer_name(r["customer_json"]), "total": float(r["total"] or 0), "status": str(r["status"]), "created_at": str(r["created_at"])})
    docs.sort(key=lambda x: x["created_at"], reverse=True)
    return JSONResponse(docs[:100], headers={"Cache-Control": "no-store"})


@router.get("/api/trends")
def api_trends(days: int = Query(default=30, ge=7, le=90), _: str = Depends(admin_login)) -> JSONResponse:
    start = now_utc() - timedelta(days=days - 1)
    result = rows("SELECT SUBSTR(created_at,1,10) day,COUNT(*) count,COALESCE(SUM(total),0) value FROM invoices WHERE created_at>=? GROUP BY SUBSTR(created_at,1,10) ORDER BY day", (iso(start),))
    found = {str(r["day"]): {"count": int(r["count"]), "value": float(r["value"] or 0)} for r in result}
    points = []
    current = start.date()
    end = now_utc().date()
    while current <= end:
        key = current.isoformat()
        value = found.get(key, {"count": 0, "value": 0})
        points.append({"day": key, "invoices": value["count"], "invoice_value": value["value"]})
        current += timedelta(days=1)
    return JSONResponse({"points": points}, headers={"Cache-Control": "no-store"})


@router.get("/api/activity")
def api_activity(_: str = Depends(admin_login)) -> JSONResponse:
    events: list[dict[str, Any]] = []
    for r in rows("SELECT created_at,invoice_number,total,status,customer_json FROM invoices ORDER BY created_at DESC LIMIT 30"):
        events.append({"time": str(r["created_at"]), "title": f"Invoice {r['invoice_number']}", "detail": f"{customer_name(r['customer_json'])} - ${float(r['total'] or 0):,.2f} - {str(r['status']).replace('_',' ').title()}"})
    for r in rows("SELECT created_at,quote_number,total,status,customer_json FROM quotes ORDER BY created_at DESC LIMIT 30"):
        events.append({"time": str(r["created_at"]), "title": f"Quote {r['quote_number']}", "detail": f"{customer_name(r['customer_json'])} - ${float(r['total'] or 0):,.2f} - {str(r['status']).replace('_',' ').title()}"})
    if table_exists("gemini_usage"):
        for r in rows("SELECT created_at,model,operation,success,latency_ms,total_tokens,error FROM gemini_usage ORDER BY created_at DESC LIMIT 30"):
            ok = int(r["success"]) == 1
            detail = f"{r['model']} - {r['operation']} - {int(r['total_tokens'] or 0)} tokens - {int(r['latency_ms'] or 0)}ms"
            if not ok:
                detail += f" - FAILED: {str(r['error'] or '')[:100]}"
            events.append({"time": str(r["created_at"]), "title": "Gemini request succeeded" if ok else "Gemini request failed", "detail": detail})
    events.sort(key=lambda x: x["time"], reverse=True)
    return JSONResponse(events[:50], headers={"Cache-Control": "no-store"})


DASHBOARD_HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Tradie Invoice Admin</title><style>
:root{color-scheme:dark;--bg:#07111f;--panel:#102238;--muted:#9fb1c5;--green:#43df87;--blue:#43a5ff;--border:#23364e}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#fff;font-family:Arial,sans-serif}.page{max-width:1800px;margin:auto;padding:20px}.head{display:flex;justify-content:space-between;gap:15px;align-items:center}h1{margin:0}.live{color:var(--green);font-weight:700}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-top:18px}.card,.panel{background:var(--panel);border:1px solid var(--border);border-radius:15px}.card{padding:18px}.label,.hint{color:var(--muted);font-size:13px}.value{font-size:31px;font-weight:800;margin-top:7px}.tabs{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}button,input,select{background:#152b46;color:#fff;border:1px solid var(--border);border-radius:9px;padding:9px 11px}.active{background:var(--blue);color:#00152b;font-weight:700}.panel{padding:18px;margin-top:14px}.layout{display:grid;grid-template-columns:1.2fr .8fr;gap:14px}.chart{height:220px;display:flex;align-items:flex-end;gap:6px;overflow:auto}.barwrap{min-width:28px;flex:1;text-align:center}.bar{background:linear-gradient(180deg,var(--blue),#2671ff);border-radius:7px 7px 2px 2px;min-height:3px}.barlabel{font-size:10px;color:var(--muted);margin-top:5px}.feed{max-height:560px;overflow:auto}.event{padding:10px 0;border-bottom:1px solid var(--border)}.eventdetail{font-size:13px;color:var(--muted);margin-top:4px}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--border)}th{color:var(--muted)}.scroll{overflow:auto}.hidden{display:none}.toolbar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}@media(max-width:950px){.layout{grid-template-columns:1fr}.head{align-items:flex-start;flex-direction:column}}
</style></head><body><div class="page"><div class="head"><div><h1>Tradie Invoice Admin</h1><div class="hint">Live business, user and Gemini monitoring</div></div><div id="live" class="live">LIVE: Connecting...</div></div>
<div class="grid"><div class="card"><div class="label">Active users</div><div id="users" class="value">-</div><div id="usersHint" class="hint"></div></div><div class="card"><div class="label">Invoices today</div><div id="invToday" class="value">-</div><div id="invHint" class="hint"></div></div><div class="card"><div class="label">Quotes today</div><div id="quoteToday" class="value">-</div><div id="quoteHint" class="hint"></div></div><div class="card"><div class="label">PDF credits today</div><div id="creditToday" class="value">-</div><div id="creditHint" class="hint"></div></div><div class="card"><div class="label">Gemini calls today</div><div id="geminiCalls" class="value">-</div><div id="geminiHint" class="hint"></div></div><div class="card"><div class="label">Total invoice value</div><div id="invTotal" class="value">-</div></div></div>
<div class="tabs"><button id="overviewBtn" class="active" onclick="tab('overview')">Overview</button><button id="usersBtn" onclick="tab('usersTab')">Users</button><button id="docsBtn" onclick="tab('docs')">Invoices and quotes</button><button id="geminiBtn" onclick="tab('gemini')">Gemini</button></div>
<section id="overview"><div class="layout"><div class="panel"><div class="toolbar"><button onclick="trends(7)">7 days</button><button onclick="trends(30)">30 days</button><button onclick="trends(90)">90 days</button></div><h2>Invoice volume</h2><div id="chart" class="chart"></div></div><div class="panel"><h2>Live activity</h2><div id="activity" class="feed"></div></div></div></section>
<section id="usersTab" class="hidden"><div class="panel"><h2>Users</h2><div class="scroll"><table><thead><tr><th>Channel</th><th>User</th><th>Plan</th><th>Status</th><th>Credits</th><th>Invoice PDFs</th><th>Quote PDFs</th><th>AI</th><th>Last active</th></tr></thead><tbody id="userRows"></tbody></table></div></div></section>
<section id="docs" class="hidden"><div class="panel"><div class="toolbar"><select id="kind"><option value="all">All</option><option value="invoice">Invoices</option><option value="quote">Quotes</option></select><input id="search" placeholder="Search ID or customer"><button onclick="documents()">Search</button></div><div class="scroll"><table><thead><tr><th>Type</th><th>Reference</th><th>Customer</th><th>Total</th><th>Status</th><th>Created</th></tr></thead><tbody id="docRows"></tbody></table></div></div></section>
<section id="gemini" class="hidden"><div class="grid"><div class="card"><div class="label">Prompt tokens</div><div id="promptTokens" class="value">-</div></div><div class="card"><div class="label">Output tokens</div><div id="outputTokens" class="value">-</div></div><div class="card"><div class="label">Thinking tokens</div><div id="thinkingTokens" class="value">-</div></div><div class="card"><div class="label">Average latency</div><div id="latency" class="value">-</div></div><div class="card"><div class="label">Failures today</div><div id="failures" class="value">-</div></div></div></section>
</div><script>
const money=new Intl.NumberFormat('en-AU',{style:'currency',currency:'AUD'});const num=new Intl.NumberFormat('en-AU');function txt(id,v){document.getElementById(id).textContent=v}function esc(v){return String(v??'').replace(/[&<>\"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c]))}async function get(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw new Error(url+' '+r.status);return r.json()}function tab(id){for(const x of ['overview','usersTab','docs','gemini'])document.getElementById(x).classList.toggle('hidden',x!==id)}
async function refresh(){try{const[s,g,u,a]=await Promise.all([get('/admin/api/summary'),get('/admin/api/gemini'),get('/admin/api/users'),get('/admin/api/activity')]);txt('users',s.users.active);txt('usersHint',`${s.users.trial} trial - ${s.users.paid} paid`);txt('invToday',s.invoices.today);txt('invHint',money.format(s.invoices.value_today)+' today');txt('quoteToday',s.quotes.today);txt('quoteHint',`${s.quotes.converted} converted`);txt('creditToday',s.credits.today);txt('creditHint',`${s.credits.remaining} remaining`);txt('geminiCalls',g.calls_today);txt('geminiHint',`${num.format(g.total_tokens_today)} tokens - ${g.failures_today} failures - ${g.average_latency_ms}ms average`);txt('invTotal',money.format(s.invoices.value_total));txt('promptTokens',num.format(g.prompt_tokens_today));txt('outputTokens',num.format(g.output_tokens_today));txt('thinkingTokens',num.format(g.thinking_tokens_today));txt('latency',g.average_latency_ms+'ms');txt('failures',g.failures_today);document.getElementById('userRows').innerHTML=u.map(x=>`<tr><td>${esc(x.channel)}</td><td>${esc(x.external_id)}</td><td>${esc(x.plan)}</td><td>${esc(x.status)}</td><td>${x.credits}/${x.credit_limit}</td><td>${x.invoice_pdfs}</td><td>${x.quote_pdfs}</td><td>${x.ai_requests}</td><td>${esc(x.updated_at)}</td></tr>`).join('');document.getElementById('activity').innerHTML=a.map(x=>`<div class="event"><b>${esc(x.title)}</b><div class="eventdetail">${esc(x.detail)}<br>${esc(x.time)}</div></div>`).join('');txt('live','LIVE: Connected - '+new Date().toLocaleTimeString())}catch(e){console.error(e);txt('live','LIVE: Connection error')}}
async function trends(days=30){const d=await get('/admin/api/trends?days='+days);const max=Math.max(...d.points.map(x=>x.invoices),1);document.getElementById('chart').innerHTML=d.points.map(x=>`<div class="barwrap"><div class="bar" title="${x.invoices} invoices - ${money.format(x.invoice_value)}" style="height:${Math.max(3,x.invoices/max*180)}px"></div><div class="barlabel">${x.day.slice(5)}</div></div>`).join('')}
async function documents(){const k=document.getElementById('kind').value;const s=document.getElementById('search').value;const d=await get(`/admin/api/documents?kind=${encodeURIComponent(k)}&search=${encodeURIComponent(s)}`);document.getElementById('docRows').innerHTML=d.map(x=>`<tr><td>${esc(x.kind)}</td><td>${esc(x.reference)}</td><td>${esc(x.customer)}</td><td>${money.format(x.total)}</td><td>${esc(x.status)}</td><td>${esc(x.created_at)}</td></tr>`).join('')}
refresh();trends(30);setInterval(refresh,5000);
</script></body></html>'''


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(_: str = Depends(admin_login)) -> HTMLResponse:
    return HTMLResponse(
        DASHBOARD_HTML,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, max-age=0", "X-Frame-Options": "DENY"},
    )


@router.get("/health")
def health(_: str = Depends(admin_login)) -> dict[str, Any]:
    return {
        "ok": True,
        "database": "connected",
        "gemini_tracking": table_exists("gemini_usage"),
        "generated_at": iso(now_utc()),
    }

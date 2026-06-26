from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from invoice_routes import db

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBasic()


def admin_login(
    credentials: HTTPBasicCredentials = Depends(security),
):
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "").strip()

    valid = (
        username
        and password
        and secrets.compare_digest(credentials.username, username)
        and secrets.compare_digest(credentials.password, password)
    )

    if not valid:
        raise HTTPException(
            status_code=401,
            detail="Invalid admin login",
            headers={"WWW-Authenticate": "Basic"},
        )


def scalar(query: str, params: tuple = (), default=0):
    try:
        with db() as conn:
            row = conn.execute(query, params).fetchone()
        if not row:
            return default
        try:
            return row[0]
        except Exception:
            return row[list(row.keys())[0]]
    except Exception:
        return default


def query_rows(query: str, params: tuple = []):
    try:
        with db() as conn:
            return conn.execute(query, params).fetchall()
    except Exception:
        return []


def mask(value: str) -> str:
    value = str(value or "")
    if len(value) < 7:
        return "****" + value[-2:]
    return value[:3] + "****" + value[-3:]


@router.get("/api/summary")
def summary(_: None = Depends(admin_login)):
    today = datetime.now(timezone.utc).date().isoformat()

    return {
        "users": scalar("SELECT COUNT(*) FROM users"),
        "active_users": scalar(
            "SELECT COUNT(*) FROM users WHERE status='active'"
        ),
        "trial_users": scalar(
            "SELECT COUNT(*) FROM users WHERE plan='trial'"
        ),
        "paid_users": scalar(
            "SELECT COUNT(*) FROM users WHERE plan<>'trial'"
        ),
        "invoices": scalar("SELECT COUNT(*) FROM invoices"),
        "invoices_today": scalar(
            "SELECT COUNT(*) FROM invoices WHERE created_at>=?",
            (today,),
        ),
        "invoice_value": float(
            scalar(
                "SELECT COALESCE(SUM(total),0) FROM invoices",
                default=0,
            )
            or 0
        ),
        "invoice_value_today": float(
            scalar(
                "SELECT COALESCE(SUM(total),0) FROM invoices "
                "WHERE created_at>=?",
                (today,),
                0,
            )
            or 0
        ),
        "quotes": scalar("SELECT COUNT(*) FROM quotes"),
        "quotes_today": scalar(
            "SELECT COUNT(*) FROM quotes WHERE created_at>=?",
            (today,),
        ),
        "pdfs": scalar(
            "SELECT COUNT(*) FROM credit_events "
            "WHERE event_type='pdf_generated'"
        ),
        "pdfs_today": scalar(
            "SELECT COUNT(*) FROM credit_events "
            "WHERE event_type='pdf_generated' AND created_at>=?",
            (today,),
        ),
        "ai_requests": scalar(
            "SELECT COUNT(*) FROM rate_events WHERE operation='ai'"
        ),
        "ai_today": scalar(
            "SELECT COUNT(*) FROM rate_events "
            "WHERE operation='ai' AND created_at>=?",
            (today,),
        ),
    }


@router.get("/api/users")
def users(_: None = Depends(admin_login)):
    rows = query_rows(
        """
        SELECT
            u.user_id,
            u.plan,
            u.status,
            u.credit_balance,
            u.credit_limit,
            u.updated_at,
            ci.channel,
            ci.external_id,
            (
                SELECT COUNT(*)
                FROM credit_events ce
                WHERE ce.user_id=u.user_id
                  AND ce.document_type='invoice'
                  AND ce.event_type='pdf_generated'
            ) invoice_pdfs,
            (
                SELECT COUNT(*)
                FROM credit_events ce
                WHERE ce.user_id=u.user_id
                  AND ce.document_type='quote'
                  AND ce.event_type='pdf_generated'
            ) quote_pdfs,
            (
                SELECT COUNT(*)
                FROM rate_events re
                WHERE re.user_id=u.user_id
                  AND re.operation='ai'
            ) ai_requests
        FROM users u
        LEFT JOIN channel_identities ci
          ON ci.user_id=u.user_id
        ORDER BY u.updated_at DESC
        LIMIT 30
        """
    )

    return [
        {
            "channel": str(row["channel"] or "unknown"),
            "external_id": mask(row["external_id"]),
            "plan": str(row["plan"]),
            "status": str(row["status"]),
            "credits": int(row["credit_balance"]),
            "credit_limit": int(row["credit_limit"]),
            "invoice_pdfs": int(row["invoice_pdfs"] or 0),
            "quote_pdfs": int(row["quote_pdfs"] or 0),
            "ai_requests": int(row["ai_requests"] or 0),
            "updated_at": str(row["updated_at"]),
        }
        for row in rows
    ]


@router.get("/api/activity")
def activity(_: None = Depends(admin_login)):
    events = []

    for row in query_rows(
        """
        SELECT created_at, invoice_number, total, status
        FROM invoices
        ORDER BY created_at DESC
        LIMIT 15
        """
    ):
        events.append(
            {
                "time": str(row["created_at"]),
                "title": f"Invoice {row['invoice_number']}",
                "detail": (
                    f"${float(row['total']):,.2f} Â -  "
                    f"{str(row['status']).replace('_',' ').title()}"
                ),
            }
        )

    for row in query_rows(
        """
        SELECT created_at, quote_number, total, status
        FROM quotes
        ORDER BY created_at DESC
        LIMIT 15
        """
    ):
        events.append(
            {
                "time": str(row["created_at"]),
                "title": f"Quote {row['quote_number']}",
                "detail": (
                    f"${float(row['total']):,.2f} Â -  "
                    f"{str(row['status']).replace('_',' ').title()}"
                ),
            }
        )

    for row in query_rows(
        """
        SELECT created_at, document_type, document_id
        FROM credit_events
        WHERE event_type='pdf_generated'
        ORDER BY created_at DESC
        LIMIT 15
        """
    ):
        events.append(
            {
                "time": str(row["created_at"]),
                "title": f"{str(row['document_type']).title()} PDF generated",
                "detail": f"Document #{row['document_id']}",
            }
        )

    events.sort(key=lambda item: item["time"], reverse=True)
    return events[:30]




@router.get("/api/gemini")
def gemini_metrics(_: None = Depends(admin_login)):
    today = datetime.now(timezone.utc).date().isoformat()

    return {
        "calls": scalar(
            "SELECT COUNT(*) FROM gemini_usage"
        ),
        "calls_today": scalar(
            "SELECT COUNT(*) FROM gemini_usage WHERE created_at>=?",
            (today,),
        ),
        "failures_today": scalar(
            "SELECT COUNT(*) FROM gemini_usage "
            "WHERE created_at>=? AND success=0",
            (today,),
        ),
        "tokens_today": scalar(
            "SELECT COALESCE(SUM(total_tokens),0) "
            "FROM gemini_usage WHERE created_at>=?",
            (today,),
        ),
        "prompt_tokens_today": scalar(
            "SELECT COALESCE(SUM(prompt_tokens),0) "
            "FROM gemini_usage WHERE created_at>=?",
            (today,),
        ),
        "output_tokens_today": scalar(
            "SELECT COALESCE(SUM(output_tokens),0) "
            "FROM gemini_usage WHERE created_at>=?",
            (today,),
        ),
        "average_latency_ms": scalar(
            "SELECT COALESCE(AVG(latency_ms),0) "
            "FROM gemini_usage WHERE created_at>=?",
            (today,),
        ),
    }


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def dashboard(_: None = Depends(admin_login)):
    return HTMLResponse(r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tradie Invoice Admin</title>
<style>
body{font-family:Arial;background:#07111f;color:#fff;margin:0;padding:24px}
h1{margin:0}.muted{color:#9fb1c5}.live{color:#43df87}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-top:22px}
.card,.panel{background:#102238;border-radius:15px;padding:18px}
.value{font-size:30px;font-weight:700;margin-top:7px}
.label{color:#9fb1c5;font-size:13px}
.layout{display:grid;grid-template-columns:1.3fr .7fr;gap:14px;margin-top:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:10px;border-bottom:1px solid #23364e}
th{color:#9fb1c5}.feed{max-height:490px;overflow:auto}
.event{padding:11px 0;border-bottom:1px solid #23364e}
.detail{color:#9fb1c5;font-size:13px;margin-top:4px}
@media(max-width:900px){.layout{grid-template-columns:1fr}.scroll{overflow:auto}}
</style>
</head>
<body>
<h1>Tradie Invoice Admin</h1>
<div class="live" id="status">LIVE: Connecting...</div>

<div class="grid">
<div class="card"><div class="label">Active users</div><div id="active_users" class="value">-</div><div id="user_hint" class="muted"></div></div>
<div class="card"><div class="label">Invoices today</div><div id="invoices_today" class="value">-</div><div id="invoice_today_value" class="muted"></div></div>
<div class="card"><div class="label">Quotes today</div><div id="quotes_today" class="value">-</div></div>
<div class="card"><div class="label">PDFs today</div><div id="pdfs_today" class="value">-</div></div>
<div class="card"><div class="label">Gemini calls today</div><div id="ai_today" class="value">-</div><div id="gemini_hint" class="muted"></div></div>
<div class="card"><div class="label">Total invoice value</div><div id="invoice_value" class="value">-</div></div>
</div>

<div class="layout">
<div class="panel">
<h2>Recent users</h2>
<div class="scroll">
<table>
<thead><tr><th>Channel</th><th>User</th><th>Plan</th><th>Credits</th><th>Invoices</th><th>Quotes</th><th>AI</th></tr></thead>
<tbody id="users"></tbody>
</table>
</div>
</div>

<div class="panel">
<h2>Live activity</h2>
<div id="activity" class="feed"></div>
</div>
</div>

<script>
const money=new Intl.NumberFormat('en-AU',{style:'currency',currency:'AUD'});

async function refresh(){
 try{
  const [s,u,a,g]=await Promise.all([
   fetch('/admin/api/summary',{cache:'no-store'}).then(r=>r.json()),
   fetch('/admin/api/users',{cache:'no-store'}).then(r=>r.json()),
   fetch('/admin/api/activity',{cache:'no-store'}).then(r=>r.json()),
   fetch('/admin/api/gemini',{cache:'no-store'}).then(r=>r.json())
  ]);

  active_users.textContent=s.active_users;
  invoices_today.textContent=s.invoices_today;
  quotes_today.textContent=s.quotes_today;
  pdfs_today.textContent=s.pdfs_today;
  ai_today.textContent=g.calls_today;
  gemini_hint.textContent=
   `${g.tokens_today} tokens Â -  ${g.failures_today} failures Â -  `+
   `${Math.round(g.average_latency_ms)}ms average`;
  invoice_value.textContent=money.format(s.invoice_value);
  user_hint.textContent=`${s.trial_users} trial Â -  ${s.paid_users} paid`;
  invoice_today_value.textContent=money.format(s.invoice_value_today)+' today';

  users.innerHTML=u.map(x=>`
   <tr>
    <td>${x.channel}</td><td>${x.external_id}</td>
    <td>${x.plan}</td><td>${x.credits}/${x.credit_limit}</td>
    <td>${x.invoice_pdfs}</td><td>${x.quote_pdfs}</td>
    <td>${x.ai_requests}</td>
   </tr>`).join('');

  activity.innerHTML=a.map(x=>`
   <div class="event">
    <b>${x.title}</b>
    <div class="detail">${x.detail}<br>${x.time}</div>
   </div>`).join('');

  document.getElementById('status').textContent='LIVE: Live â€” '+new Date().toLocaleTimeString();
 }catch(e){
  document.getElementById('status').textContent='LIVE: Connection error';
 }
}
refresh();
setInterval(refresh,5000);
</script>
</body>
</html>
""")



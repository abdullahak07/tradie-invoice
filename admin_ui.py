from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from admin_dashboard import admin_login
from invoice_routes import db

router = APIRouter(prefix="/admin", tags=["admin-ui"])
GRAPHQL_ENDPOINT = "https://backboard.railway.com/graphql/v2"


def rows(query: str, params: tuple[Any, ...] = ()) -> list[Any]:
    try:
        with db() as conn:
            return list(conn.execute(query, params).fetchall())
    except Exception:
        return []


def env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.getenv(name, str(default)).strip())
    except Exception:
        return default


def railway_graphql(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    token = os.getenv("RAILWAY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("RAILWAY_API_TOKEN is not configured")

    headers = {
        "Project-Access-Token": token,
        "Content-Type": "application/json",
        "User-Agent": "tradie-invoice-cost-monitor/1.0",
    }
    request = urllib.request.Request(
        GRAPHQL_ENDPOINT,
        data=json.dumps({"query": query, "variables": variables}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Railway API HTTP {exc.code}: {body[:240]}") from exc

    if payload.get("errors"):
        message = "; ".join(str(item.get("message", item)) for item in payload["errors"])
        raise RuntimeError(message[:300])
    return payload.get("data", {})


def find_cost(value: Any) -> float | None:
    preferred = {
        "currentcost",
        "totalcost",
        "cost",
        "amount",
        "usagecost",
        "estimatedcost",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower().replace("_", "") in preferred and isinstance(item, (int, float)):
                return float(item)
        for item in value.values():
            found = find_cost(item)
            if found is not None:
                return found
    elif isinstance(value, list):
        values = [find_cost(item) for item in value]
        numbers = [item for item in values if item is not None]
        if numbers:
            return float(sum(numbers))
    return None


def railway_cost() -> dict[str, Any]:
    project_id = os.getenv("RAILWAY_PROJECT_ID", "").strip()
    if not project_id:
        return {"available": False, "spent_usd": None, "source": "missing_project_id", "error": "RAILWAY_PROJECT_ID is missing"}

    candidates = [
        (
            "query usage($projectId: String!) { usage(projectId: $projectId) }",
            {"projectId": project_id},
            "usage",
        ),
        (
            "query estimatedUsage($projectId: String!) { estimatedUsage(projectId: $projectId) }",
            {"projectId": project_id},
            "estimatedUsage",
        ),
    ]

    errors: list[str] = []
    for query, variables, source in candidates:
        try:
            data = railway_graphql(query, variables)
            cost = find_cost(data)
            if cost is not None:
                return {"available": True, "spent_usd": round(cost, 4), "source": source, "raw": data}
            errors.append(f"{source}: no cost field found")
        except Exception as exc:
            errors.append(f"{source}: {str(exc)[:120]}")

    starting_credit = env_float("RAILWAY_STARTING_CREDIT_USD", 5.0)
    remaining_credit = os.getenv("RAILWAY_REMAINING_CREDIT_USD", "").strip()
    if remaining_credit:
        remaining = env_float("RAILWAY_REMAINING_CREDIT_USD", 0.0)
        return {
            "available": True,
            "spent_usd": round(max(0.0, starting_credit - remaining), 4),
            "remaining_usd": round(remaining, 4),
            "source": "configured_balance_fallback",
            "error": "; ".join(errors),
        }

    return {"available": False, "spent_usd": None, "source": "railway_api_unavailable", "error": "; ".join(errors)}


MODEL_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.1-pro-preview": (2.00, 12.00),
}


def price_for_model(model: str) -> tuple[float, float]:
    model_lower = model.lower()
    for key, price in MODEL_PRICES.items():
        if key in model_lower:
            return price
    return (
        env_float("GEMINI_INPUT_PRICE_PER_MILLION_USD", 0.30),
        env_float("GEMINI_OUTPUT_PRICE_PER_MILLION_USD", 2.50),
    )


def gemini_cost() -> dict[str, Any]:
    result = rows(
        """
        SELECT model,
               COALESCE(SUM(prompt_tokens),0) prompt_tokens,
               COALESCE(SUM(output_tokens),0) output_tokens,
               COALESCE(SUM(thinking_tokens),0) thinking_tokens,
               COUNT(*) calls
        FROM gemini_usage
        WHERE success = 1
        GROUP BY model
        ORDER BY model
        """
    )

    total = 0.0
    models = []
    for row in result:
        model = str(row["model"])
        input_rate, output_rate = price_for_model(model)
        prompt = int(row["prompt_tokens"] or 0)
        output = int(row["output_tokens"] or 0)
        thinking = int(row["thinking_tokens"] or 0)
        cost = (prompt / 1_000_000 * input_rate) + ((output + thinking) / 1_000_000 * output_rate)
        total += cost
        models.append(
            {
                "model": model,
                "calls": int(row["calls"] or 0),
                "prompt_tokens": prompt,
                "output_tokens": output,
                "thinking_tokens": thinking,
                "input_rate": input_rate,
                "output_rate": output_rate,
                "estimated_cost_usd": round(cost, 6),
            }
        )

    return {
        "estimated_spent_usd": round(total, 6),
        "models": models,
        "source": "local_token_telemetry_estimate",
        "note": "Google does not expose the live AI Studio prepaid balance through this app. This estimate uses recorded tokens and configured model prices.",
    }


@router.get("/api/costs")
def api_costs(_: str = Depends(admin_login)) -> JSONResponse:
    return JSONResponse(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "railway": railway_cost(),
            "gemini": gemini_cost(),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/api/home-bars")
def api_home_bars(_: str = Depends(admin_login)) -> JSONResponse:
    start = datetime.now(timezone.utc) - timedelta(days=13)
    invoice_rows = rows(
        """
        SELECT SUBSTR(created_at,1,10) day, COUNT(*) count,
               COALESCE(SUM(total),0) value
        FROM invoices
        WHERE created_at >= ?
        GROUP BY SUBSTR(created_at,1,10)
        ORDER BY day
        """,
        (start.isoformat(timespec="seconds"),),
    )
    quote_rows = rows(
        """
        SELECT SUBSTR(created_at,1,10) day, COUNT(*) count
        FROM quotes
        WHERE created_at >= ?
        GROUP BY SUBSTR(created_at,1,10)
        ORDER BY day
        """,
        (start.isoformat(timespec="seconds"),),
    )
    ai_rows = rows(
        """
        SELECT SUBSTR(created_at,1,10) day, COUNT(*) count
        FROM gemini_usage
        WHERE created_at >= ?
        GROUP BY SUBSTR(created_at,1,10)
        ORDER BY day
        """,
        (start.isoformat(timespec="seconds"),),
    )

    invoice_map = {str(r["day"]): {"count": int(r["count"]), "value": float(r["value"] or 0)} for r in invoice_rows}
    quote_map = {str(r["day"]): int(r["count"]) for r in quote_rows}
    ai_map = {str(r["day"]): int(r["count"]) for r in ai_rows}

    points = []
    current = start.date()
    today = datetime.now(timezone.utc).date()
    while current <= today:
        key = current.isoformat()
        inv = invoice_map.get(key, {"count": 0, "value": 0.0})
        points.append(
            {
                "day": key,
                "invoices": inv["count"],
                "invoice_value": inv["value"],
                "quotes": quote_map.get(key, 0),
                "gemini_calls": ai_map.get(key, 0),
            }
        )
        current += timedelta(days=1)

    return JSONResponse({"points": points}, headers={"Cache-Control": "no-store"})


ADMIN_UI_JS = r"""
(function(){
 const path=location.pathname.replace(/\/$/,'');
 const style=document.createElement('style');
 style.textContent=`
  .shared-admin-nav{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:12px 0}
  .shared-admin-nav a{display:inline-block;text-decoration:none;background:#152b46;color:#fff;border:1px solid #23364e;border-radius:9px;padding:10px 14px;font-weight:700}
  .shared-admin-nav a.primary{background:#43a5ff;color:#00152b}
  .extra-cost-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-top:14px}
  .extra-cost-card,.extra-chart-panel{background:#102238;border:1px solid #23364e;border-radius:15px;padding:18px}
  .extra-cost-label{color:#9fb1c5;font-size:13px}.extra-cost-value{font-size:30px;font-weight:800;margin-top:7px}.extra-cost-note{color:#9fb1c5;font-size:12px;margin-top:6px}
  .extra-chart-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:14px}.extra-bars{height:220px;display:flex;align-items:flex-end;gap:6px;overflow-x:auto}.extra-bar-wrap{min-width:24px;flex:1;text-align:center}.extra-bar{background:linear-gradient(180deg,#43a5ff,#2671ff);border-radius:7px 7px 2px 2px;min-height:3px}.extra-bar-label{font-size:9px;color:#9fb1c5;margin-top:5px}
  @media(max-width:1000px){.extra-chart-grid{grid-template-columns:1fr}}
 `;
 document.head.appendChild(style);

 const nav=document.createElement('div');nav.className='shared-admin-nav';
 if(path==='/admin') nav.innerHTML='<a href="/admin/railway">Railway Monitoring</a><a class="primary" href="/admin/controls">Admin Controls</a>';
 else if(path==='/admin/railway') nav.innerHTML='<a href="/admin">Home</a><a class="primary" href="/admin/controls">Admin Controls</a>';
 else if(path==='/admin/controls') nav.innerHTML='<a href="/admin">Home</a><a class="primary" href="/admin/railway">Railway Monitoring</a>';
 if(nav.innerHTML){const h=document.querySelector('h1');if(h&&h.parentElement)h.parentElement.appendChild(nav)}

 if(path!=='/admin') return;
 const page=document.querySelector('.page')||document.body;
 const costs=document.createElement('div');costs.className='extra-cost-grid';costs.innerHTML=`
  <div class="extra-cost-card"><div class="extra-cost-label">Railway spent this cycle</div><div id="railwayCost" class="extra-cost-value">-</div><div id="railwayCostNote" class="extra-cost-note"></div></div>
  <div class="extra-cost-card"><div class="extra-cost-label">Gemini estimated spend</div><div id="geminiCost" class="extra-cost-value">-</div><div id="geminiCostNote" class="extra-cost-note"></div></div>`;
 const charts=document.createElement('div');charts.className='extra-chart-grid';charts.innerHTML=`
  <div class="extra-chart-panel"><h2>Invoices - 14 days</h2><div id="extraInvoiceBars" class="extra-bars"></div></div>
  <div class="extra-chart-panel"><h2>Quotes - 14 days</h2><div id="extraQuoteBars" class="extra-bars"></div></div>
  <div class="extra-chart-panel"><h2>Gemini calls - 14 days</h2><div id="extraGeminiBars" class="extra-bars"></div></div>`;
 const firstGrid=page.querySelector('.grid');if(firstGrid){firstGrid.after(costs);costs.after(charts)}else{page.append(costs,charts)}

 const usd=v=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:4}).format(v||0);
 function bars(id,points,key){const max=Math.max(...points.map(x=>Number(x[key]||0)),1);document.getElementById(id).innerHTML=points.map(x=>`<div class="extra-bar-wrap"><div class="extra-bar" title="${x[key]}" style="height:${Math.max(3,Number(x[key]||0)/max*175)}px"></div><div class="extra-bar-label">${x.day.slice(5)}</div></div>`).join('')}
 async function loadExtras(){
  try{
   const [c,b]=await Promise.all([fetch('/admin/api/costs',{cache:'no-store'}).then(r=>r.json()),fetch('/admin/api/home-bars',{cache:'no-store'}).then(r=>r.json())]);
   document.getElementById('railwayCost').textContent=c.railway.available?usd(c.railway.spent_usd):'Unavailable';
   document.getElementById('railwayCostNote').textContent=c.railway.available?`Source: ${c.railway.source}`:(c.railway.error||'Railway API did not return billing cost');
   document.getElementById('geminiCost').textContent=usd(c.gemini.estimated_spent_usd);
   document.getElementById('geminiCostNote').textContent='Estimated from recorded Gemini tokens';
   bars('extraInvoiceBars',b.points,'invoices');bars('extraQuoteBars',b.points,'quotes');bars('extraGeminiBars',b.points,'gemini_calls');
  }catch(e){console.error(e)}
 }
 loadExtras();setInterval(loadExtras,30000);
})();
"""


@router.get("/ui.js")
def admin_ui_script() -> Response:
    return Response(ADMIN_UI_JS, media_type="application/javascript; charset=utf-8", headers={"Cache-Control": "no-store"})


class AdminUIInjectionMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/admin") or request.url.path.startswith("/admin/api") or request.url.path.endswith(".js"):
            return response
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        text = body.decode("utf-8", errors="replace")
        if "/admin/ui.js" not in text:
            text = text.replace("</body>", '<script src="/admin/ui.js"></script></body>')
        headers = dict(response.headers)
        headers.pop("content-length", None)
        return Response(content=text, status_code=response.status_code, headers=headers, media_type="text/html; charset=utf-8")

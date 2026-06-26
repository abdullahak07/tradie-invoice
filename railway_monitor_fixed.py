from __future__ import annotations

import json
import os
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse

from admin_dashboard import admin_login

router = APIRouter(prefix="/admin/railway", tags=["admin-railway"])

GRAPHQL_ENDPOINT = "https://backboard.railway.com/graphql/v2"
PROCESS_STARTED = time.monotonic()
_SAMPLE_LOCK = Lock()
_LAST_SAMPLE: dict[str, float] = {}


def env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def read_number(path: str) -> float | None:
    try:
        raw = Path(path).read_text(encoding="utf-8").strip()
        if raw == "max":
            return None
        return float(raw)
    except Exception:
        return None


def format_bytes(value: float | int | None) -> str:
    if value is None:
        return "unlimited"
    size = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while size >= 1024 and index < len(units) - 1:
        size /= 1024
        index += 1
    return f"{size:.1f} {units[index]}"


def runtime_metrics() -> dict[str, Any]:
    now = time.monotonic()
    process_cpu = time.process_time()

    memory_current = read_number("/sys/fs/cgroup/memory.current")
    memory_limit = read_number("/sys/fs/cgroup/memory.max")

    cpu_usage_usec = None
    try:
        for line in Path("/sys/fs/cgroup/cpu.stat").read_text().splitlines():
            key, value = line.split(maxsplit=1)
            if key == "usage_usec":
                cpu_usage_usec = float(value)
                break
    except Exception:
        pass

    cpu_quota = None
    try:
        quota_raw, period_raw = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota_raw != "max":
            cpu_quota = float(quota_raw) / float(period_raw)
    except Exception:
        pass

    network_rx = 0
    network_tx = 0
    try:
        for line in Path("/proc/net/dev").read_text().splitlines()[2:]:
            _, values = line.split(":", 1)
            fields = values.split()
            network_rx += int(fields[0])
            network_tx += int(fields[8])
    except Exception:
        pass

    disk = shutil.disk_usage("/")

    with _SAMPLE_LOCK:
        previous_time = _LAST_SAMPLE.get("time")
        previous_process_cpu = _LAST_SAMPLE.get("process_cpu")
        previous_cgroup_cpu = _LAST_SAMPLE.get("cgroup_cpu")
        previous_rx = _LAST_SAMPLE.get("rx")
        previous_tx = _LAST_SAMPLE.get("tx")

        elapsed = now - previous_time if previous_time else 0
        process_cpu_percent = 0.0
        container_cpu_percent = 0.0
        rx_per_second = 0.0
        tx_per_second = 0.0

        if elapsed > 0:
            if previous_process_cpu is not None:
                process_cpu_percent = max(
                    0.0,
                    ((process_cpu - previous_process_cpu) / elapsed) * 100,
                )
            if cpu_usage_usec is not None and previous_cgroup_cpu is not None:
                container_cpu_percent = max(
                    0.0,
                    ((cpu_usage_usec - previous_cgroup_cpu) / 1_000_000 / elapsed)
                    * 100,
                )
            if previous_rx is not None:
                rx_per_second = max(0.0, (network_rx - previous_rx) / elapsed)
            if previous_tx is not None:
                tx_per_second = max(0.0, (network_tx - previous_tx) / elapsed)

        _LAST_SAMPLE.update(
            {
                "time": now,
                "process_cpu": process_cpu,
                "cgroup_cpu": cpu_usage_usec or 0,
                "rx": float(network_rx),
                "tx": float(network_tx),
            }
        )

    memory_percent = 0.0
    if memory_current is not None and memory_limit not in (None, 0):
        memory_percent = memory_current / memory_limit * 100

    return {
        "uptime_seconds": int(now - PROCESS_STARTED),
        "process_cpu_percent": round(process_cpu_percent, 2),
        "container_cpu_percent": round(container_cpu_percent, 2),
        "cpu_limit_cores": cpu_quota,
        "memory_current_bytes": int(memory_current or 0),
        "memory_limit_bytes": int(memory_limit) if memory_limit else None,
        "memory_percent": round(memory_percent, 2),
        "disk_used_bytes": int(disk.used),
        "disk_total_bytes": int(disk.total),
        "disk_percent": round(disk.used / disk.total * 100, 2),
        "network_received_bytes": network_rx,
        "network_sent_bytes": network_tx,
        "network_received_per_second": round(rx_per_second, 2),
        "network_sent_per_second": round(tx_per_second, 2),
        "formatted": {
            "memory_current": format_bytes(memory_current),
            "memory_limit": format_bytes(memory_limit),
            "disk_used": format_bytes(disk.used),
            "disk_total": format_bytes(disk.total),
            "network_received": format_bytes(network_rx),
            "network_sent": format_bytes(network_tx),
            "network_received_per_second": format_bytes(rx_per_second) + "/s",
            "network_sent_per_second": format_bytes(tx_per_second) + "/s",
        },
    }


def graphql_request(
    query: str,
    variables: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, str]]:
    token = env_value("RAILWAY_API_TOKEN", "RAILWAY_TOKEN")
    if not token:
        raise RuntimeError("RAILWAY_API_TOKEN is not configured")

    token_type = env_value("RAILWAY_TOKEN_TYPE").lower()
    if token_type in {"account", "workspace", "bearer"}:
        auth_headers = {"Authorization": f"Bearer {token}"}
        auth_mode = "Bearer"
    else:
        auth_headers = {"Project-Access-Token": token}
        auth_mode = "Project-Access-Token"

    headers = {
        **auth_headers,
        "Content-Type": "application/json",
        "User-Agent": "tradie-invoice-admin/2.0",
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
            rate_headers = {
                "limit": response.headers.get("X-RateLimit-Limit", ""),
                "remaining": response.headers.get("X-RateLimit-Remaining", ""),
                "reset": response.headers.get("X-RateLimit-Reset", ""),
                "auth_mode": auth_mode,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Railway API HTTP {exc.code}: {body[:300]}") from exc
    except Exception as exc:
        raise RuntimeError(f"Railway API request failed: {exc}") from exc

    if payload.get("errors"):
        message = "; ".join(
            str(item.get("message", item)) for item in payload["errors"]
        )
        raise RuntimeError(message[:500])

    return payload.get("data", {}), rate_headers


def deployment_history() -> dict[str, Any]:
    project_id = env_value("RAILWAY_PROJECT_ID")
    service_id = env_value("RAILWAY_SERVICE_ID")
    environment_id = env_value("RAILWAY_ENVIRONMENT_ID")

    if not project_id or not service_id or not environment_id:
        return {
            "configured": False,
            "error": "Railway project, service or environment ID is missing",
            "deployments": [],
            "rate_limit": {},
        }

    query = """
    query deployments($input: DeploymentListInput!) {
      deployments(input: $input, first: 10) {
        edges {
          node {
            id
            status
            createdAt
          }
        }
      }
    }
    """

    variables = {
        "input": {
            "projectId": project_id,
            "serviceId": service_id,
            "environmentId": environment_id,
        }
    }

    try:
        data, rate_headers = graphql_request(query, variables)
        deployments = [
            edge["node"]
            for edge in data.get("deployments", {}).get("edges", [])
        ]
        return {
            "configured": True,
            "error": "",
            "deployments": deployments,
            "rate_limit": rate_headers,
        }
    except Exception as exc:
        return {
            "configured": True,
            "error": str(exc),
            "deployments": [],
            "rate_limit": {},
        }


def public_health() -> dict[str, Any]:
    domain = env_value("RAILWAY_PUBLIC_DOMAIN")
    if not domain:
        return {
            "configured": False,
            "ok": False,
            "latency_ms": 0,
            "status": None,
        }

    url = f"https://{domain}/whatsapp/health"
    started = time.perf_counter()
    try:
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "tradie-invoice-monitor/2.0"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read(256)
            return {
                "configured": True,
                "ok": 200 <= response.status < 400,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "status": response.status,
                "url": url,
            }
    except Exception as exc:
        return {
            "configured": True,
            "ok": False,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "status": None,
            "url": url,
            "error": str(exc)[:250],
        }


@router.get("/api/status")
def api_status(_: str = Depends(admin_login)) -> JSONResponse:
    deployments = deployment_history()
    latest = deployments["deployments"][0] if deployments["deployments"] else None
    runtime = runtime_metrics()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "railway": {
            "project_id": env_value("RAILWAY_PROJECT_ID"),
            "service_id": env_value("RAILWAY_SERVICE_ID"),
            "environment_id": env_value("RAILWAY_ENVIRONMENT_ID"),
            "deployment_id": env_value("RAILWAY_DEPLOYMENT_ID"),
            "environment_name": env_value("RAILWAY_ENVIRONMENT_NAME"),
            "service_name": env_value("RAILWAY_SERVICE_NAME"),
            "public_domain": env_value("RAILWAY_PUBLIC_DOMAIN"),
            "token_configured": bool(
                env_value("RAILWAY_API_TOKEN", "RAILWAY_TOKEN")
            ),
            "token_type": env_value("RAILWAY_TOKEN_TYPE") or "project",
        },
        "runtime": runtime,
        "public_health": public_health(),
        "latest_deployment": latest,
        "deployments": deployments["deployments"],
        "deployment_error": deployments.get("error", ""),
        "rate_limit": deployments.get("rate_limit", {}),
        "resource_limits": {
            "available": True,
            "source": "container-cgroup",
            "limits": {
                "cpu_cores": runtime["cpu_limit_cores"],
                "memory_bytes": runtime["memory_limit_bytes"],
            },
            "error": "",
        },
    }

    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


RAILWAY_HTML = r"""
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Railway Monitoring</title>
<style>
:root{color-scheme:dark;--bg:#07111f;--panel:#102238;--muted:#9fb1c5;--green:#43df87;--blue:#43a5ff;--red:#ff6b6b;--border:#23364e}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#fff;font-family:Arial,sans-serif}.page{max-width:1650px;margin:auto;padding:20px}.head{display:flex;justify-content:space-between;gap:15px;align-items:center}a{color:var(--blue)}.live{color:var(--green);font-weight:700}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-top:18px}.card,.panel{background:var(--panel);border:1px solid var(--border);border-radius:15px}.card{padding:18px}.label,.hint{color:var(--muted);font-size:13px}.value{font-size:29px;font-weight:800;margin-top:7px}.panel{padding:18px;margin-top:14px}.layout{display:grid;grid-template-columns:1fr 1fr;gap:14px}.barbg{height:11px;background:#07111f;border-radius:999px;overflow:hidden;margin-top:10px}.bar{height:100%;background:var(--blue);width:0}.ok{color:var(--green)}.bad{color:var(--red)}table{width:100%;border-collapse:collapse;font-size:13px}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--border)}th{color:var(--muted)}pre{white-space:pre-wrap;color:var(--muted)}@media(max-width:900px){.layout{grid-template-columns:1fr}.head{align-items:flex-start;flex-direction:column}}
</style>
</head>
<body>
<div class="page">
<div class="head">
  <div><h1>Railway Monitoring</h1><div class="hint"><a href="/admin">Back to business dashboard</a></div></div>
  <div id="live" class="live">LIVE: Connecting...</div>
</div>
<div class="grid">
<div class="card"><div class="label">Deployment status</div><div id="deployStatus" class="value">-</div><div id="deployTime" class="hint"></div></div>
<div class="card"><div class="label">Public endpoint</div><div id="health" class="value">-</div><div id="latency" class="hint"></div></div>
<div class="card"><div class="label">Container CPU</div><div id="cpu" class="value">-</div><div id="cpuLimit" class="hint"></div></div>
<div class="card"><div class="label">Container memory</div><div id="memory" class="value">-</div><div id="memoryDetail" class="hint"></div><div class="barbg"><div id="memoryBar" class="bar"></div></div></div>
<div class="card"><div class="label">Filesystem used</div><div id="disk" class="value">-</div><div id="diskDetail" class="hint"></div><div class="barbg"><div id="diskBar" class="bar"></div></div></div>
<div class="card"><div class="label">Service uptime</div><div id="uptime" class="value">-</div><div id="deploymentId" class="hint"></div></div>
</div>
<div class="layout">
<div class="panel"><h2>Network</h2><div class="grid"><div><div class="label">Received total</div><div id="rx" class="value">-</div><div id="rxRate" class="hint"></div></div><div><div class="label">Sent total</div><div id="tx" class="value">-</div><div id="txRate" class="hint"></div></div></div><h2>Railway configuration</h2><pre id="config"></pre></div>
<div class="panel"><h2>Recent deployments</h2><div id="deployError" class="bad"></div><table><thead><tr><th>Status</th><th>Created</th><th>Deployment ID</th></tr></thead><tbody id="deployments"></tbody></table></div>
</div>
<div class="panel"><h2>API and resource limits</h2><pre id="limits"></pre></div>
</div>
<script>
function text(id,v){document.getElementById(id).textContent=v}
function timefmt(s){const d=new Date(s);return Number.isNaN(d.getTime())?s:d.toLocaleString('en-AU')}
function duration(sec){let s=Math.max(0,Math.floor(sec));const d=Math.floor(s/86400);s%=86400;const h=Math.floor(s/3600);s%=3600;const m=Math.floor(s/60);return `${d}d ${h}h ${m}m`}
async function refresh(){
 try{
  const r=await fetch('/admin/railway/api/status',{cache:'no-store'});
  if(!r.ok)throw new Error(r.status);
  const d=await r.json();const rt=d.runtime;
  text('deployStatus',d.latest_deployment?.status||'Unknown');
  text('deployTime',d.latest_deployment?timefmt(d.latest_deployment.createdAt):'No deployment data');
  text('health',d.public_health.ok?'Healthy':'Unavailable');
  document.getElementById('health').className='value '+(d.public_health.ok?'ok':'bad');
  text('latency',`${d.public_health.latency_ms}ms - HTTP ${d.public_health.status??'n/a'}`);
  text('cpu',`${rt.container_cpu_percent.toFixed(2)}%`);
  text('cpuLimit',rt.cpu_limit_cores?`${rt.cpu_limit_cores} CPU cores limit`:'CPU limit not exposed');
  text('memory',`${rt.memory_percent.toFixed(1)}%`);
  text('memoryDetail',`${rt.formatted.memory_current} / ${rt.formatted.memory_limit}`);
  document.getElementById('memoryBar').style.width=Math.min(rt.memory_percent,100)+'%';
  text('disk',`${rt.disk_percent.toFixed(1)}%`);
  text('diskDetail',`${rt.formatted.disk_used} / ${rt.formatted.disk_total}`);
  document.getElementById('diskBar').style.width=Math.min(rt.disk_percent,100)+'%';
  text('uptime',duration(rt.uptime_seconds));
  text('deploymentId',d.railway.deployment_id||'Deployment ID unavailable');
  text('rx',rt.formatted.network_received);text('tx',rt.formatted.network_sent);
  text('rxRate',rt.formatted.network_received_per_second);text('txRate',rt.formatted.network_sent_per_second);
  text('config',JSON.stringify(d.railway,null,2));
  text('limits',JSON.stringify({resource_limits:d.resource_limits,api_rate_limit:d.rate_limit},null,2));
  document.getElementById('deployments').innerHTML=d.deployments.map(x=>`<tr><td>${x.status}</td><td>${timefmt(x.createdAt)}</td><td>${x.id}</td></tr>`).join('');
  text('deployError',d.deployment_error||'');
  text('live','LIVE: Connected - '+new Date().toLocaleTimeString());
 }catch(e){console.error(e);text('live','LIVE: Connection error')}
}
refresh();setInterval(refresh,5000);
</script>
</body>
</html>
"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def railway_dashboard(_: str = Depends(admin_login)) -> HTMLResponse:
    return HTMLResponse(
        RAILWAY_HTML,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store, max-age=0", "X-Frame-Options": "DENY"},
    )

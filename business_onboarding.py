from __future__ import annotations

import json
import mimetypes
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from admin_dashboard import admin_login
from invoice_routes import DATA_DIR, db

router = APIRouter(prefix="/admin/onboarding", tags=["admin-onboarding"])
UPLOAD_DIR = DATA_DIR / "business_branding"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

TRADE_OPTIONS = [
    ("electrician", "Electrician / Sparky"),
    ("carpenter", "Carpenter / Chippy"),
    ("plumber", "Plumber"),
    ("hvac", "HVAC / Air-conditioning Technician"),
    ("painter", "Painter"),
    ("landscaper", "Landscaper"),
    ("roofer", "Roofer"),
    ("tiler", "Tiler"),
    ("concreter", "Concreter"),
    ("handyman", "Handyman"),
    ("cleaner", "Cleaner"),
    ("builder", "Builder"),
    ("locksmith", "Locksmith"),
    ("mechanic", "Mechanic"),
    ("pest_control", "Pest Control"),
    ("solar_installer", "Solar Installer"),
    ("other", "Other"),
]
TRADE_KEYS = {key for key, _ in TRADE_OPTIONS}

TRADE_PROMPTS: dict[str, str] = {
    "electrician": "Use Australian electrical terminology: power points, RCDs, switchboards, downlights, wiring, testing and certification. Never invent licence or compliance details.",
    "carpenter": "Use Australian carpentry terminology: doors, timber, framing, decking, skirting, cabinetry, hardware and waste removal. Never invent dimensions or timber species.",
    "plumber": "Use Australian plumbing terminology: call-outs, taps, toilets, drains, hot-water systems, pipework, leak detection and fixtures.",
    "hvac": "Use HVAC terminology: split systems, ducting, refrigerant, servicing, filters, diagnostics, commissioning and labour.",
    "painter": "Use painting terminology: preparation, patching, undercoat, coats, walls, ceilings, trim, labour and materials.",
    "landscaper": "Use landscaping terminology: turf, mulch, soil, planting, irrigation, retaining, paving and green-waste removal.",
    "roofer": "Use roofing terminology: tiles, sheets, flashing, gutters, leaks, ridge capping, access and waste removal.",
    "tiler": "Use tiling terminology: square metres, preparation, waterproofing, adhesive, grout, cutting and trims.",
    "concreter": "Use concreting terminology: excavation, formwork, reinforcement, cubic metres, pumping, finishing and curing.",
    "handyman": "Use clear maintenance terminology and separate labour, materials, hardware and disposal where applicable.",
    "cleaner": "Use cleaning terminology: hours, rooms, end-of-lease, deep clean, windows, carpet, consumables and travel.",
    "builder": "Use building terminology: labour, materials, subcontractors, demolition, framing, fit-off, compliance and variations.",
    "locksmith": "Use locksmith terminology: call-out, locks, cylinders, keys, rekeying, access control and emergency work.",
    "mechanic": "Use automotive terminology: diagnostics, labour hours, parts, service items, fluids and workshop supplies.",
    "pest_control": "Use pest-control terminology: inspection, treatment, barriers, baiting, follow-up and warranty conditions.",
    "solar_installer": "Use solar terminology: panels, inverter, mounting, cabling, commissioning, monitoring and compliance testing.",
    "other": "Use concise professional trade terminology. Do not invent quantities, prices, credentials or compliance details.",
}


class ExtractedLetterhead(BaseModel):
    business_name: str = ""
    owner_name: str = ""
    abn: str = ""
    licence_number: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    bank_account_name: str = ""
    bank_bsb: str = ""
    bank_account_number: str = ""
    footer_text: str = ""


class ConfirmProfile(BaseModel):
    user_id: str
    channel: Literal["whatsapp", "telegram"]
    external_id: str = Field(min_length=2, max_length=100)
    trade_type: str
    business_name: str = Field(min_length=2, max_length=150)
    owner_name: str = ""
    abn: str = ""
    licence_number: str = ""
    phone: str = ""
    email: str = ""
    address: str = ""
    gst_enabled: bool = True
    default_terms: str = "Payment due within 7 days"
    bank_account_name: str = ""
    bank_bsb: str = ""
    bank_account_number: str = ""
    payment_reference: str = "Invoice number"
    footer_text: str = ""
    plan: Literal["trial", "standard", "premium"] = "trial"
    starting_credits: int = Field(default=30, ge=0, le=100000)
    logo_path: str = ""
    letterhead_path: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    with db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_profiles (
                profile_id TEXT PRIMARY KEY,
                user_id TEXT UNIQUE NOT NULL,
                trade_type TEXT NOT NULL,
                business_name TEXT NOT NULL,
                owner_name TEXT NOT NULL DEFAULT '',
                abn TEXT NOT NULL DEFAULT '',
                licence_number TEXT NOT NULL DEFAULT '',
                phone TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                gst_enabled INTEGER NOT NULL DEFAULT 1,
                default_terms TEXT NOT NULL DEFAULT '',
                trade_prompt TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_payment_details (
                user_id TEXT PRIMARY KEY,
                account_name TEXT NOT NULL DEFAULT '',
                bsb TEXT NOT NULL DEFAULT '',
                account_number TEXT NOT NULL DEFAULT '',
                payment_reference TEXT NOT NULL DEFAULT 'Invoice number',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS business_branding (
                user_id TEXT PRIMARY KEY,
                logo_path TEXT NOT NULL DEFAULT '',
                letterhead_path TEXT NOT NULL DEFAULT '',
                primary_colour TEXT NOT NULL DEFAULT '#1f2937',
                secondary_colour TEXT NOT NULL DEFAULT '#ecfdf3',
                footer_text TEXT NOT NULL DEFAULT '',
                extraction_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            )
            """
        )


def safe_filename(filename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name).strip("._")
    return stem[:120] or "upload.bin"


def get_profile_for_user(user_id: str) -> dict[str, Any] | None:
    ensure_schema()
    with db() as conn:
        row = conn.execute(
            """
            SELECT bp.*, bpd.account_name, bpd.bsb, bpd.account_number,
                   bpd.payment_reference, bb.logo_path, bb.letterhead_path,
                   bb.primary_colour, bb.secondary_colour, bb.footer_text
            FROM business_profiles bp
            LEFT JOIN business_payment_details bpd ON bpd.user_id = bp.user_id
            LEFT JOIN business_branding bb ON bb.user_id = bp.user_id
            WHERE bp.user_id = ?
            """,
            (user_id,),
        ).fetchone()
    return {key: row[key] for key in row.keys()} if row else None


def user_id_for_channel(channel: str, external_id: str) -> str | None:
    with db() as conn:
        row = conn.execute(
            "SELECT user_id FROM channel_identities WHERE channel = ? AND external_id = ?",
            (channel.strip().lower(), external_id.strip()),
        ).fetchone()
    return str(row["user_id"]) if row else None


def profile_for_channel(channel: str, external_id: str) -> dict[str, Any] | None:
    user_id = user_id_for_channel(channel, external_id)
    return get_profile_for_user(user_id) if user_id else None


def profile_for_document(document_type: str, document_id: int) -> dict[str, Any] | None:
    ensure_schema()
    with db() as conn:
        row = conn.execute(
            """
            SELECT user_id FROM credit_events
            WHERE document_type = ? AND document_id = ?
              AND event_type = 'pdf_generated'
            ORDER BY created_at DESC LIMIT 1
            """,
            (document_type, document_id),
        ).fetchone()
    return get_profile_for_user(str(row["user_id"])) if row else None


def trade_prompt_for_profile(profile: dict[str, Any] | None) -> str:
    if not profile:
        return ""
    trade_type = str(profile.get("trade_type") or "other")
    custom = str(profile.get("trade_prompt") or "").strip()
    base = TRADE_PROMPTS.get(trade_type, TRADE_PROMPTS["other"])
    business = str(profile.get("business_name") or "the tradie business")
    return (
        f"\nPERMANENT USER BUSINESS PROFILE\n"
        f"Business: {business}\nTrade: {trade_type}\n"
        f"Trade instructions: {custom or base}\n"
        "Use this stored trade profile instead of guessing the user's trade from the current job. "
        "Do not copy ABN, licence, bank details or letterhead text into service line items.\n"
    )


@router.on_event("startup")
def startup() -> None:
    ensure_schema()


@router.get("/api/trades")
def trades(_: str = Depends(admin_login)) -> JSONResponse:
    return JSONResponse([{"value": key, "label": label} for key, label in TRADE_OPTIONS])


@router.post("/api/upload")
async def upload_branding(
    file: UploadFile = File(...),
    user_id: str = Form(default="new"),
    file_type: Literal["logo", "letterhead"] = Form(default="letterhead"),
    _: str = Depends(admin_login),
) -> JSONResponse:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(content) > 12 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File must be 12 MB or smaller")

    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    allowed = {"image/png", "image/jpeg", "application/pdf"}
    if mime not in allowed:
        raise HTTPException(status_code=400, detail="Only PNG, JPEG or PDF files are supported")
    if file_type == "logo" and mime == "application/pdf":
        raise HTTPException(status_code=400, detail="Logo must be PNG or JPEG")

    folder = UPLOAD_DIR / re.sub(r"[^A-Za-z0-9_-]+", "_", user_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{file_type}_{uuid.uuid4().hex[:8]}_{safe_filename(file.filename or 'upload')}"
    path.write_bytes(content)
    return JSONResponse({"ok": True, "path": str(path), "mime_type": mime, "size": len(content)})


@router.post("/api/extract")
async def extract_letterhead(
    file: UploadFile = File(...),
    _: str = Depends(admin_login),
) -> JSONResponse:
    content = await file.read()
    if not content or len(content) > 12 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Provide a non-empty file up to 12 MB")
    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    if mime not in {"image/png", "image/jpeg", "application/pdf"}:
        raise HTTPException(status_code=400, detail="Only PNG, JPEG or PDF files are supported")

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise HTTPException(status_code=503, detail="GEMINI_API_KEY is not configured")

    prompt = (
        "Extract visible business identity details from this Australian tradie letterhead. "
        "Return only details clearly present. Never guess missing values. "
        "ABN, licence and bank details must be copied exactly."
    )
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=[prompt, types.Part.from_bytes(data=content, mime_type=mime)],
            config={
                "response_mime_type": "application/json",
                "response_schema": ExtractedLetterhead,
                "temperature": 0,
            },
        )
        extracted = response.parsed or ExtractedLetterhead.model_validate_json(response.text)
        return JSONResponse({"ok": True, "extracted": extracted.model_dump(), "requires_admin_confirmation": True})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gemini could not read the letterhead: {str(exc)[:300]}") from exc


@router.post("/api/confirm")
def confirm_profile(request: ConfirmProfile, admin: str = Depends(admin_login)) -> JSONResponse:
    ensure_schema()
    if request.trade_type not in TRADE_KEYS:
        raise HTTPException(status_code=400, detail="Unsupported trade type")

    now = now_iso()
    user_id = request.user_id.strip() or uuid.uuid4().hex
    profile_id = uuid.uuid4().hex
    trial_end = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(timespec="seconds")
    credit_limit = request.starting_credits

    with db() as conn:
        existing_identity = conn.execute(
            "SELECT user_id FROM channel_identities WHERE channel=? AND external_id=?",
            (request.channel, request.external_id.strip()),
        ).fetchone()
        if existing_identity and str(existing_identity["user_id"]) != user_id:
            raise HTTPException(status_code=409, detail="That channel identity already belongs to another user")

        existing_user = conn.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not existing_user:
            conn.execute(
                """
                INSERT INTO users (user_id, plan, status, trial_started_at, trial_expires_at,
                    credit_balance, credit_limit, billing_period_start, billing_period_end,
                    created_at, updated_at)
                VALUES (?, ?, 'active', ?, ?, ?, ?, NULL, NULL, ?, ?)
                """,
                (user_id, request.plan, now, trial_end, request.starting_credits, credit_limit, now, now),
            )
        else:
            conn.execute(
                "UPDATE users SET plan=?, status='active', credit_balance=?, credit_limit=?, updated_at=? WHERE user_id=?",
                (request.plan, request.starting_credits, credit_limit, now, user_id),
            )

        conn.execute(
            """
            INSERT INTO channel_identities (channel, external_id, user_id, verified_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel, external_id) DO UPDATE SET user_id=excluded.user_id, verified_at=excluded.verified_at
            """,
            (request.channel, request.external_id.strip(), user_id, now),
        )
        conn.execute(
            """
            INSERT INTO business_profiles (profile_id, user_id, trade_type, business_name, owner_name,
                abn, licence_number, phone, email, address, gst_enabled, default_terms,
                trade_prompt, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET trade_type=excluded.trade_type,
                business_name=excluded.business_name, owner_name=excluded.owner_name,
                abn=excluded.abn, licence_number=excluded.licence_number,
                phone=excluded.phone, email=excluded.email, address=excluded.address,
                gst_enabled=excluded.gst_enabled, default_terms=excluded.default_terms,
                trade_prompt=excluded.trade_prompt, updated_at=excluded.updated_at
            """,
            (profile_id, user_id, request.trade_type, request.business_name, request.owner_name,
             request.abn, request.licence_number, request.phone, request.email, request.address,
             1 if request.gst_enabled else 0, request.default_terms,
             TRADE_PROMPTS.get(request.trade_type, TRADE_PROMPTS["other"]), now, now),
        )
        conn.execute(
            """
            INSERT INTO business_payment_details (user_id, account_name, bsb, account_number, payment_reference, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET account_name=excluded.account_name,
                bsb=excluded.bsb, account_number=excluded.account_number,
                payment_reference=excluded.payment_reference, updated_at=excluded.updated_at
            """,
            (user_id, request.bank_account_name, request.bank_bsb,
             request.bank_account_number, request.payment_reference, now),
        )
        conn.execute(
            """
            INSERT INTO business_branding (user_id, logo_path, letterhead_path, footer_text, extraction_json, updated_at)
            VALUES (?, ?, ?, ?, '{}', ?)
            ON CONFLICT(user_id) DO UPDATE SET logo_path=excluded.logo_path,
                letterhead_path=excluded.letterhead_path, footer_text=excluded.footer_text,
                updated_at=excluded.updated_at
            """,
            (user_id, request.logo_path, request.letterhead_path, request.footer_text, now),
        )

    return JSONResponse({"ok": True, "user_id": user_id, "profile": get_profile_for_user(user_id), "admin": admin})


@router.get("/api/users")
def onboarded_users(_: str = Depends(admin_login)) -> JSONResponse:
    ensure_schema()
    with db() as conn:
        result = conn.execute(
            """
            SELECT bp.user_id, bp.business_name, bp.trade_type, bp.abn,
                   bp.licence_number, bp.updated_at, u.plan, u.status,
                   ci.channel, ci.external_id, bb.logo_path, bb.letterhead_path
            FROM business_profiles bp
            JOIN users u ON u.user_id = bp.user_id
            LEFT JOIN channel_identities ci ON ci.user_id = bp.user_id
            LEFT JOIN business_branding bb ON bb.user_id = bp.user_id
            ORDER BY bp.updated_at DESC
            """
        ).fetchall()
    return JSONResponse([{key: row[key] for key in row.keys()} for row in result])


ONBOARDING_HTML = r"""
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Onboard Tradie</title>
<style>:root{color-scheme:dark;--bg:#07111f;--panel:#102238;--muted:#9fb1c5;--blue:#43a5ff;--border:#23364e}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#fff;font-family:Arial,sans-serif}.page{max-width:1450px;margin:auto;padding:20px}.panel{background:var(--panel);border:1px solid var(--border);border-radius:15px;padding:18px;margin-top:16px}.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}.full{grid-column:1/-1}label{font-size:12px;color:var(--muted)}input,select,textarea,button{width:100%;background:#07111f;color:#fff;border:1px solid var(--border);border-radius:9px;padding:11px;margin-top:5px}button{background:var(--blue);color:#00152b;font-weight:800;cursor:pointer}.nav{display:flex;gap:8px;flex-wrap:wrap}.nav a{background:#152b46;color:#fff;border:1px solid var(--border);border-radius:9px;padding:10px 14px;text-decoration:none}.status{white-space:pre-wrap;background:#07111f;padding:12px;border-radius:9px;margin-top:12px}.users{overflow:auto}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:9px;border-bottom:1px solid var(--border)}@media(max-width:800px){.grid{grid-template-columns:1fr}.full{grid-column:auto}}</style></head>
<body><div class="page"><h1>Onboard New Tradie</h1><div class="nav"><a href="/admin">Home</a><a href="/admin/controls">Admin Controls</a><a href="/admin/railway">Railway Monitoring</a></div>
<div class="panel"><h2>1. Upload and read letterhead</h2><input id="letterheadFile" type="file" accept="image/png,image/jpeg,application/pdf"><button onclick="extract()">Read letterhead with Gemini</button><div id="extractStatus" class="status">Upload a PNG, JPEG or PDF. Extracted values must be confirmed before activation.</div></div>
<div class="panel"><h2>2. Confirm account and business profile</h2><div class="grid">
<div><label>Channel</label><select id="channel"><option>whatsapp</option><option>telegram</option></select></div><div><label>WhatsApp number or Telegram chat ID</label><input id="external_id"></div>
<div><label>Trade</label><select id="trade_type"></select></div><div><label>Business name</label><input id="business_name"></div>
<div><label>Owner name</label><input id="owner_name"></div><div><label>ABN</label><input id="abn"></div>
<div><label>Licence number</label><input id="licence_number"></div><div><label>Phone</label><input id="phone"></div>
<div><label>Email</label><input id="email"></div><div><label>Address</label><input id="address"></div>
<div><label>Plan</label><select id="plan"><option>trial</option><option>standard</option><option>premium</option></select></div><div><label>Starting credits</label><input id="starting_credits" type="number" value="30"></div>
<div><label>Bank account name</label><input id="bank_account_name"></div><div><label>BSB</label><input id="bank_bsb"></div>
<div><label>Account number</label><input id="bank_account_number"></div><div><label>Payment reference</label><input id="payment_reference" value="Invoice number"></div>
<div class="full"><label>Default terms</label><input id="default_terms" value="Payment due within 7 days"></div><div class="full"><label>Footer text</label><textarea id="footer_text"></textarea></div>
<div><label>Logo (PNG/JPEG)</label><input id="logoFile" type="file" accept="image/png,image/jpeg"><button onclick="uploadFile('logo')">Upload logo</button></div><div><label>Letterhead file</label><input id="finalLetterheadFile" type="file" accept="image/png,image/jpeg,application/pdf"><button onclick="uploadFile('letterhead')">Upload letterhead</button></div>
<div class="full"><button onclick="confirmUser()">Confirm and Activate User</button></div></div><div id="confirmStatus" class="status">Not activated</div></div>
<div class="panel"><h2>Onboarded users</h2><button onclick="loadUsers()">Refresh users</button><div class="users"><table><thead><tr><th>Business</th><th>Trade</th><th>Channel</th><th>Identity</th><th>Plan</th><th>Branding</th></tr></thead><tbody id="users"></tbody></table></div></div></div>
<script>
let logo_path='',letterhead_path='';const ids=['business_name','owner_name','abn','licence_number','phone','email','address','bank_account_name','bank_bsb','bank_account_number','footer_text'];
async function init(){const trades=await fetch('/admin/onboarding/api/trades').then(r=>r.json());document.getElementById('trade_type').innerHTML=trades.map(x=>`<option value="${x.value}">${x.label}</option>`).join('');loadUsers()}
async function extract(){const f=document.getElementById('letterheadFile').files[0];if(!f)return;const fd=new FormData();fd.append('file',f);document.getElementById('extractStatus').textContent='Gemini is reading the letterhead...';const r=await fetch('/admin/onboarding/api/extract',{method:'POST',body:fd});const d=await r.json();if(!r.ok){document.getElementById('extractStatus').textContent=d.detail;return}for(const id of ids)if(d.extracted[id]!==undefined)document.getElementById(id).value=d.extracted[id]||'';document.getElementById('extractStatus').textContent=JSON.stringify(d,null,2)}
async function uploadFile(type){const input=type==='logo'?'logoFile':'finalLetterheadFile';const f=document.getElementById(input).files[0];if(!f)return;const fd=new FormData();fd.append('file',f);fd.append('user_id',document.getElementById('external_id').value||'pending');fd.append('file_type',type);const r=await fetch('/admin/onboarding/api/upload',{method:'POST',body:fd});const d=await r.json();if(!r.ok){alert(d.detail);return}if(type==='logo')logo_path=d.path;else letterhead_path=d.path;alert(type+' uploaded')}
async function confirmUser(){const val=id=>document.getElementById(id).value;const body={user_id:crypto.randomUUID().replaceAll('-',''),channel:val('channel'),external_id:val('external_id'),trade_type:val('trade_type'),business_name:val('business_name'),owner_name:val('owner_name'),abn:val('abn'),licence_number:val('licence_number'),phone:val('phone'),email:val('email'),address:val('address'),gst_enabled:true,default_terms:val('default_terms'),bank_account_name:val('bank_account_name'),bank_bsb:val('bank_bsb'),bank_account_number:val('bank_account_number'),payment_reference:val('payment_reference'),footer_text:val('footer_text'),plan:val('plan'),starting_credits:Number(val('starting_credits')),logo_path,letterhead_path};const r=await fetch('/admin/onboarding/api/confirm',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();document.getElementById('confirmStatus').textContent=JSON.stringify(d,null,2);if(r.ok)loadUsers()}
async function loadUsers(){const d=await fetch('/admin/onboarding/api/users').then(r=>r.json());document.getElementById('users').innerHTML=d.map(x=>`<tr><td>${x.business_name}</td><td>${x.trade_type}</td><td>${x.channel||''}</td><td>${x.external_id||''}</td><td>${x.plan}</td><td>${x.logo_path?'Logo ':''}${x.letterhead_path?'Letterhead':''}</td></tr>`).join('')}
init();
</script></body></html>
"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def onboarding_page(_: str = Depends(admin_login)) -> HTMLResponse:
    ensure_schema()
    return HTMLResponse(ONBOARDING_HTML, headers={"Cache-Control": "no-store"})

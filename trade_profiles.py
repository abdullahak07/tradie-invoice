from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from admin_dashboard import admin_login

router = APIRouter(prefix="/admin/trades", tags=["admin-trades"])

TRADE_PROFILES: dict[str, dict[str, Any]] = {
    "electrician": {
        "display_name": "Electrician",
        "keywords": [
            "electrician", "sparky", "electrical", "downlight", "power point",
            "powerpoint", "switchboard", "rcd", "circuit", "wiring", "wire",
            "ceiling fan", "light switch", "socket", "fault finding", "led bulb",
        ],
        "prompt": """
TRADE PROFILE: ELECTRICIAN / SPARKY
Interpret the job using Australian electrical-trade language.
Commercially natural units include each, hour, metre and call-out.
Common services include call-out fee, fault finding, power point replacement,
downlight installation, ceiling fan installation, switchboard work, RCD work,
wiring, testing and certification.
Do not invent licence numbers, compliance certificates, quantities or prices.
When the message is genuinely ambiguous, ask whether materials are supplied by
the tradie or customer and whether testing/certification is included.
Use clear invoice descriptions such as "LED downlight installation",
"Double power point replacement", "Electrical fault finding" and
"Electrical testing".
""".strip(),
        "examples": [
            "Invoice John Smith, install 6 downlights at $95 each and replace 2 power points at $140 each",
            "Quote Sarah, fault finding 2 hours at $120 and call-out $90",
        ],
    },
    "carpenter": {
        "display_name": "Carpenter",
        "keywords": [
            "carpenter", "carpentry", "door", "doors", "timber", "deck",
            "decking", "skirting", "cabinet", "cabinetry", "pergola", "frame",
            "framing", "fence", "hinge", "handle", "architrave", "shelf",
            "shelving", "wood", "plywood",
        ],
        "prompt": """
TRADE PROFILE: CARPENTER
Interpret the job using Australian carpentry and building-maintenance language.
Commercially natural units include each, hour, linear metre, square metre and day.
Common services include site visit, door installation, door adjustment, handle
installation, timber framing, deck repair, skirting boards, cabinet repair,
fence repair, pergola work, hardware, materials and waste removal.
Do not invent timber species, dimensions, finishes, quantities or prices.
When the message is genuinely ambiguous, ask whether materials are supplied,
what dimensions/timber type apply, and whether painting, staining or waste
removal is included.
Use clear invoice descriptions such as "Internal door installation",
"Door handle installation", "Timber deck repair" and "Skirting board installation".
""".strip(),
        "examples": [
            "Invoice Mark Lee, replace 3 internal doors at $280 each and fit handles at $45 each",
            "Quote Lisa, repair 8 square metres of timber decking at $165 per square metre",
        ],
    },
}


class TradeTestRequest(BaseModel):
    message: str = Field(min_length=3, max_length=3000)
    forced_trade: str | None = None


def normalise_trade(value: str | None) -> str | None:
    text = (value or "").strip().lower()
    aliases = {
        "sparky": "electrician",
        "electrical": "electrician",
        "electrician": "electrician",
        "carpentry": "carpenter",
        "chippy": "carpenter",
        "carpenter": "carpenter",
    }
    return aliases.get(text)


def explicit_trade_from_message(message: str) -> str | None:
    match = re.search(
        r"\b(?:trade|trade type|profile)\s*(?:is|=|:)?\s*"
        r"(electrician|electrical|sparky|carpenter|carpentry|chippy)\b",
        message,
        re.I,
    )
    return normalise_trade(match.group(1)) if match else None


def detect_trade(message: str, forced_trade: str | None = None) -> dict[str, Any]:
    forced = normalise_trade(forced_trade)
    if forced:
        return {"trade_type": forced, "confidence": 1.0, "reason": "forced trade profile"}

    explicit = explicit_trade_from_message(message)
    if explicit:
        return {"trade_type": explicit, "confidence": 1.0, "reason": "explicit trade in message"}

    lowered = message.lower()
    scores: dict[str, int] = {}
    matched: dict[str, list[str]] = {}
    for trade, profile in TRADE_PROFILES.items():
        hits = [keyword for keyword in profile["keywords"] if keyword in lowered]
        scores[trade] = len(hits)
        matched[trade] = hits

    best = max(scores, key=scores.get)
    best_score = scores[best]
    other_score = max(value for key, value in scores.items() if key != best)

    if best_score == 0:
        return {
            "trade_type": "electrician",
            "confidence": 0.25,
            "reason": "no trade keywords; default pilot profile",
            "matched_keywords": [],
        }

    confidence = min(0.98, 0.60 + (best_score - other_score) * 0.12)
    return {
        "trade_type": best,
        "confidence": round(confidence, 2),
        "reason": "keyword routing",
        "matched_keywords": matched[best],
    }


def trade_prompt_for_message(message: str, forced_trade: str | None = None) -> tuple[str, dict[str, Any]]:
    detection = detect_trade(message, forced_trade)
    profile = TRADE_PROFILES[detection["trade_type"]]
    context = (
        f"\n\n{profile['prompt']}\n"
        f"DETECTED TRADE: {profile['display_name']}\n"
        "Apply this trade profile while preserving all general invoice rules.\n"
    )
    return context, detection


def install_trade_prompt_routing() -> None:
    """Patch the shared Gemini parser used by both Telegram and WhatsApp."""
    import telegram_routes

    if getattr(telegram_routes, "_trade_profiles_installed", False):
        return

    original_parse_sync = telegram_routes.ai_parse_sync
    original_edit_sync = telegram_routes.ai_edit_sync

    def routed_parse_sync(message: str):
        context, _ = trade_prompt_for_message(message)
        return original_parse_sync(context + "\nUSER JOB MESSAGE:\n" + message)

    def routed_edit_sync(invoice, instruction: str, prior_instruction: str = ""):
        combined = " ".join(
            [
                getattr(invoice, "notes", "") or "",
                " ".join(getattr(item, "description", "") for item in getattr(invoice, "items", [])),
                instruction,
            ]
        )
        context, detection = trade_prompt_for_message(combined)
        routed_instruction = (
            context
            + f"\nUse the {detection['trade_type']} profile for this edit.\n"
            + instruction
        )
        return original_edit_sync(invoice, routed_instruction, prior_instruction)

    telegram_routes.ai_parse_sync = routed_parse_sync
    telegram_routes.ai_edit_sync = routed_edit_sync
    telegram_routes._trade_profiles_installed = True


@router.get("/api/profiles")
def profiles(_: str = Depends(admin_login)) -> JSONResponse:
    return JSONResponse(
        {
            key: {
                "display_name": value["display_name"],
                "keywords": value["keywords"],
                "examples": value["examples"],
            }
            for key, value in TRADE_PROFILES.items()
        }
    )


@router.post("/api/test")
def test_trade(request: TradeTestRequest, _: str = Depends(admin_login)) -> JSONResponse:
    context, detection = trade_prompt_for_message(request.message, request.forced_trade)
    return JSONResponse(
        {
            "ok": True,
            "detection": detection,
            "profile": TRADE_PROFILES[detection["trade_type"]]["display_name"],
            "prompt_context": context,
            "original_message": request.message,
        }
    )


TRADES_HTML = r"""
<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Trade Profiles</title>
<style>:root{color-scheme:dark;--bg:#07111f;--panel:#102238;--muted:#9fb1c5;--blue:#43a5ff;--border:#23364e}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#fff;font-family:Arial,sans-serif}.page{max-width:1200px;margin:auto;padding:20px}.panel{background:var(--panel);border:1px solid var(--border);border-radius:15px;padding:18px;margin-top:16px}.nav{display:flex;gap:8px;flex-wrap:wrap}.nav a,button{background:#152b46;color:#fff;border:1px solid var(--border);border-radius:9px;padding:10px 14px;text-decoration:none;cursor:pointer}textarea,select{width:100%;background:#07111f;color:#fff;border:1px solid var(--border);border-radius:9px;padding:12px;margin:8px 0}textarea{min-height:130px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}.muted{color:var(--muted)}pre{white-space:pre-wrap;background:#07111f;padding:12px;border-radius:9px;overflow:auto}@media(max-width:800px){.grid{grid-template-columns:1fr}}</style></head>
<body><div class="page"><h1>Trade Profiles</h1><div class="nav"><a href="/admin">Home</a><a href="/admin/railway">Railway Monitoring</a><a href="/admin/controls">Admin Controls</a></div>
<div class="grid"><div class="panel"><h2>Electrician</h2><p class="muted">Downlights, power points, switchboards, RCDs, wiring, fans and electrical testing.</p></div><div class="panel"><h2>Carpenter</h2><p class="muted">Doors, timber, decking, framing, skirting, cabinetry, pergolas and hardware.</p></div></div>
<div class="panel"><h2>Test automatic routing</h2><select id="forced"><option value="">Automatic detection</option><option value="electrician">Force electrician</option><option value="carpenter">Force carpenter</option></select><textarea id="message">Invoice John Smith, replace 3 internal doors at $280 each and fit handles at $45 each</textarea><button onclick="runTest()">Test trade routing</button><pre id="result">Ready</pre></div></div>
<script>async function runTest(){const message=document.getElementById('message').value;const forced_trade=document.getElementById('forced').value||null;const r=await fetch('/admin/trades/api/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message,forced_trade})});document.getElementById('result').textContent=JSON.stringify(await r.json(),null,2)}</script></body></html>
"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def trade_page(_: str = Depends(admin_login)) -> HTMLResponse:
    return HTMLResponse(TRADES_HTML, headers={"Cache-Control": "no-store"})

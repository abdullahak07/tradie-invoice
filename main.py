from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from database import add_customer, get_pricing, init_db, list_customers, save_quote
from llm_client import generate_quote, recalc
from models import Customer, CustomerIn, DemoSample, PricingSettings, Quote, QuoteRequest, TranscriptionResponse
from pdf_generator import build_pdf
from postgres_schema import init_postgres_schema
from migrate_sqlite_to_postgres import migrate_sqlite_to_postgres

app = FastAPI(title="Perth Tradie Quote AI", version="0.2.0")


@app.get("/health/migration")
def migration_health() -> dict:
    import sqlite3
    from pathlib import Path

    import psycopg

    tables = [
        "invoices",
        "reminder_log",
        "telegram_sessions",
        "telegram_messages",
    ]

    sqlite_path = Path(__file__).resolve().parent / "data" / "message_invoices.db"
    database_url = os.getenv("DATABASE_URL", "").strip()

    result = {
        "ok": True,
        "sqlite_path_exists": sqlite_path.exists(),
        "sqlite": {},
        "postgresql": {},
        "matches": {},
    }

    if sqlite_path.exists():
        with sqlite3.connect(sqlite_path) as sqlite_conn:
            for table in tables:
                exists = sqlite_conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()

                if exists:
                    count = sqlite_conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    result["sqlite"][table] = count
                else:
                    result["sqlite"][table] = None

    if not database_url:
        result["ok"] = False
        result["error"] = "DATABASE_URL is not configured"
        return result

    with psycopg.connect(database_url) as pg_conn:
        with pg_conn.cursor() as cursor:
            for table in tables:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                result["postgresql"][table] = cursor.fetchone()[0]

    for table in tables:
        sqlite_count = result["sqlite"].get(table)
        postgres_count = result["postgresql"].get(table)

        result["matches"][table] = (
            sqlite_count is not None
            and sqlite_count == postgres_count
        )

    result["ok"] = all(result["matches"].values())
    return result


@app.get("/health/database")
def database_health() -> dict:
    database_url = os.getenv("DATABASE_URL", "").strip()

    if not database_url:
        return {
            "ok": False,
            "database": "postgresql",
            "error": "DATABASE_URL is not configured",
        }

    try:
        import psycopg

        with psycopg.connect(database_url, connect_timeout=10) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT current_database(), version()")
                database_name, version = cursor.fetchone()

        return {
            "ok": True,
            "database": "postgresql",
            "database_name": database_name,
            "server": version.split(",")[0],
        }

    except Exception as exc:
        return {
            "ok": False,
            "database": "postgresql",
            "error": str(exc)[:300],
        }


# Private admin dashboard
from admin_dashboard import router as admin_router
app.include_router(admin_router)

# Production onboarding with personal trade and branding profiles
from business_onboarding import router as onboarding_router
app.include_router(onboarding_router)

# Railway infrastructure monitoring
from railway_monitor_fixed import router as railway_monitor_router
app.include_router(railway_monitor_router)

# Stage 3 admin controls
from admin_controls import router as admin_controls_router
app.include_router(admin_controls_router)

# Shared navigation, home charts and cost monitoring
from admin_ui import AdminUIInjectionMiddleware, router as admin_ui_router
app.include_router(admin_ui_router)
app.add_middleware(AdminUIInjectionMiddleware)

# Telegram Message-to-Invoice routes
from telegram_routes import router as telegram_router
app.include_router(telegram_router)

# Electrician and carpenter trade routing
from trade_profiles import install_trade_prompt_routing, router as trade_profiles_router
install_trade_prompt_routing()
app.include_router(trade_profiles_router)

# Separate electrician and carpenter PDF letterheads
from trade_letterheads import install_letterhead_routing
install_letterhead_routing()

# WhatsApp Cloud API routes
from whatsapp_routes import router as whatsapp_router
app.include_router(whatsapp_router)

# Message-to-Invoice routes
from invoice_routes import router as invoice_router
app.include_router(invoice_router)
quote_queue = asyncio.Lock()

DEMO_SAMPLES = [
    DemoSample(id="downlights-fan", title="2 downlights and a fan", text="Install 2 downlights in the lounge room and replace one ceiling fan in the main bedroom."),
    DemoSample(id="rewire", title="Full rewire 3-bedroom house", text="Full rewire of a three bedroom house in Perth, include new power points, light switches and RCD safety switches."),
    DemoSample(id="emergency", title="Emergency callout switchboard", text="Emergency callout for switchboard fault, replace one RCD safety switch and test power points."),
]


@app.on_event("startup")
def startup() -> None:
    init_db()
    init_postgres_schema()

    if os.getenv("MIGRATE_SQLITE_TO_POSTGRES", "").lower() == "true":
        migrate_sqlite_to_postgres()


@app.post("/transcribe", response_model=TranscriptionResponse)
async def transcribe(audio: UploadFile = File(...)) -> TranscriptionResponse:
    suffix = Path(audio.filename or "memo.webm").suffix or ".webm"
    src = ""
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as uploaded:
            uploaded.write(await audio.read())
            src = uploaded.name

        try:
            import whisper

            model_name = os.getenv("WHISPER_MODEL", "base")
            model = whisper.load_model(model_name)
            result = model.transcribe(src, fp16=False)
            return TranscriptionResponse(text=result.get("text", "").strip(), source=f"local-whisper:{model_name}")
        except Exception:
            model_name = os.getenv("WHISPER_MODEL", "base")
            output_dir = tempfile.gettempdir()
            subprocess.check_output(
                ["whisper", src, "--model", model_name, "--fp16", "False", "--output_format", "txt", "--output_dir", output_dir],
                timeout=25,
                text=True,
            )
            transcript_path = Path(output_dir) / f"{Path(src).stem}.txt"
            return TranscriptionResponse(text=transcript_path.read_text().strip(), source=f"whisper-cli:{model_name}")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Could not understand that - try speaking slower or type it in") from exc
    finally:
        if src:
            Path(src).unlink(missing_ok=True)


@app.post("/generate-quote", response_model=Quote)
async def gen(req: QuoteRequest) -> Quote:
    async with quote_queue:
        try:
            quote = await asyncio.wait_for(generate_quote(req), timeout=30)
        except Exception as exc:
            raise HTTPException(status_code=503, detail="Could not generate quote - please try again or enter manually") from exc
        return save_quote(quote)


@app.get("/pricing-defaults", response_model=PricingSettings)
def pricing() -> PricingSettings:
    return get_pricing()


@app.post("/update-quote", response_model=Quote)
def update_quote(quote: Quote) -> Quote:
    return recalc(quote)


@app.post("/generate-pdf")
def generate_pdf(quote: Quote) -> Response:
    quote = recalc(quote)
    pdf = build_pdf(quote)
    filename = f"quote-{quote.quote_number or 'draft'}.pdf"
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": f"inline; filename={filename}"})


@app.get("/customers", response_model=list[Customer])
def customers() -> list[Customer]:
    return list_customers()


@app.post("/customers", response_model=Customer)
def create_customer(customer: CustomerIn) -> Customer:
    return add_customer(customer)


@app.get("/demo-samples", response_model=list[DemoSample])
def demo_samples() -> list[DemoSample]:
    return DEMO_SAMPLES


app.mount("/", StaticFiles(directory=".", html=True), name="static")


@app.get("/")
def home():
    return FileResponse("index.html")

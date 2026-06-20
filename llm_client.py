from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass

import httpx

from database import get_customer, get_pricing
from models import LineItem, Quote, QuoteRequest

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")


NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


LOCATIONS = [
    "living room",
    "lounge room",
    "lounge",
    "kitchen",
    "bedroom",
    "main bedroom",
    "bathroom",
    "garage",
    "laundry",
    "toilet",
    "ensuite",
    "hallway",
    "dining room",
    "outdoor",
    "outside",
    "patio",
    "alfresco",
    "office",
    "shop",
    "warehouse",
    "switchboard",
]


@dataclass(frozen=True)
class ServiceItem:
    id: str
    display_name: str
    keywords: list[str]
    material_name: str | None
    default_material_unit_cost: float
    default_labor_hours_per_unit: float
    default_markup_percent: float
    category: str
    pattern: str


SERVICE_CATALOGUE: list[ServiceItem] = [
    ServiceItem(
        id="downlight",
        display_name="LED downlight supply and install",
        keywords=["downlight", "led downlight", "lights"],
        material_name="LED downlight",
        default_material_unit_cost=22,
        default_labor_hours_per_unit=0.5,
        default_markup_percent=20,
        category="Electrical",
        pattern=r"(?P<qty>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(?:led\s+)?downlights?",
    ),
    ServiceItem(
        id="power_point",
        display_name="Power point (double) supply and install",
        keywords=["power point", "powerpoint", "gpo", "double power point"],
        material_name="Power point (double)",
        default_material_unit_cost=25,
        default_labor_hours_per_unit=1.0,
        default_markup_percent=20,
        category="Electrical",
        pattern=r"(?P<qty>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s+(?:old\s+|new\s+|double\s+)?(?:power\s*points?|powerpoints?|gpos?)",
    ),
    ServiceItem(
        id="ceiling_fan",
        display_name="Ceiling fan supply and install",
        keywords=["ceiling fan", "fan"],
        material_name="Ceiling fan",
        default_material_unit_cost=180,
        default_labor_hours_per_unit=2.0,
        default_markup_percent=20,
        category="Electrical",
        pattern=r"(?P<qty>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)?\s*ceiling\s+fans?",
    ),
    ServiceItem(
        id="exhaust_fan",
        display_name="Exhaust fan supply and install",
        keywords=["exhaust fan", "bathroom fan"],
        material_name="Exhaust fan",
        default_material_unit_cost=90,
        default_labor_hours_per_unit=1.5,
        default_markup_percent=20,
        category="Electrical",
        pattern=r"(?P<qty>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)?\s*exhaust\s+fans?",
    ),
    ServiceItem(
        id="light_switch",
        display_name="Light switch replacement",
        keywords=["switch", "light switch"],
        material_name="Light switch",
        default_material_unit_cost=15,
        default_labor_hours_per_unit=0.5,
        default_markup_percent=20,
        category="Electrical",
        pattern=r"(?P<qty>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)?\s*(?:light\s+)?switch(?:es)?",
    ),
    ServiceItem(
        id="rcd",
        display_name="RCD safety switch replacement",
        keywords=["rcd", "safety switch"],
        material_name="RCD safety switch",
        default_material_unit_cost=65,
        default_labor_hours_per_unit=1.5,
        default_markup_percent=20,
        category="Electrical",
        pattern=r"(?P<qty>\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)?\s*(?:rcds?|safety\s+switch(?:es)?)",
    ),
    ServiceItem(
        id="switchboard_callout",
        display_name="Switchboard fault finding callout",
        keywords=["switchboard", "fault", "emergency", "callout"],
        material_name=None,
        default_material_unit_cost=0,
        default_labor_hours_per_unit=1.0,
        default_markup_percent=20,
        category="Electrical",
        pattern=r"(?:emergency|callout|switchboard|fault\s+finding)",
    ),
    ServiceItem(
        id="rewire",
        display_name="Full house rewire",
        keywords=["rewire", "full rewire", "house rewire"],
        material_name=None,
        default_material_unit_cost=0,
        default_labor_hours_per_unit=24.0,
        default_markup_percent=20,
        category="Electrical",
        pattern=r"(?:full\s+)?rewire",
    ),
    ServiceItem(
        id="split_ac_service",
        display_name="Split system AC service",
        keywords=["split system service", "aircon service", "ac service"],
        material_name=None,
        default_material_unit_cost=0,
        default_labor_hours_per_unit=1.5,
        default_markup_percent=20,
        category="HVAC",
        pattern=r"(?:split\s+system|aircon|air\s*con|ac)\s+service",
    ),
    ServiceItem(
        id="split_ac_install",
        display_name="Split system AC install",
        keywords=["split system install", "aircon install", "ac install"],
        material_name=None,
        default_material_unit_cost=650,
        default_labor_hours_per_unit=5.0,
        default_markup_percent=20,
        category="HVAC",
        pattern=r"(?:install|supply\s+and\s+install).{0,30}(?:split\s+system|aircon|air\s*con|ac)",
    ),
]


def _quantity(match: re.Match[str]) -> float:
    value = match.groupdict().get("qty")
    if not value:
        return 1.0
    value = value.lower().strip()
    return float(NUMBER_WORDS.get(value, value))


def _find_location(text: str, start: int, end: int) -> str:
    window = text[end : end + 90].lower()

    for loc in LOCATIONS:
        pattern = rf"\b(?:in|inside|at|for|to|into)\s+(?:the\s+)?{re.escape(loc)}\b"
        if re.search(pattern, window):
            return loc

    before = text[max(0, start - 50) : start].lower()
    for loc in LOCATIONS:
        pattern = rf"\b{re.escape(loc)}\b"
        if re.search(pattern, before):
            return loc

    return ""


def _material_cost(service: ServiceItem) -> float:
    pricing = get_pricing()
    if service.material_name:
        return float(pricing.materials.get(service.material_name, service.default_material_unit_cost))
    return float(service.default_material_unit_cost)


def recalc(quote: Quote) -> Quote:
    subtotal = 0.0
    for item in quote.line_items:
        materials = item.quantity * item.material_unit_cost * (1 + item.material_markup_percent / 100)
        labor = item.labor_hours * item.hourly_rate
        subtotal += materials + labor

    quote.subtotal = round(subtotal, 2)
    quote.gst = round(subtotal * 0.10, 2)
    quote.total = round(quote.subtotal + quote.gst, 2)
    return quote


def catalogue_quote(req: QuoteRequest) -> Quote:
    pricing = get_pricing()
    hourly_rate = pricing.hourly_rates.get(req.profile, pricing.hourly_rates["Residential"])
    text = req.text.lower()

    line_items: list[LineItem] = []

    for service in SERVICE_CATALOGUE:
        for match in re.finditer(service.pattern, text):
            qty = _quantity(match)
            location = _find_location(text, match.start(), match.end())

            line_items.append(
                LineItem(
                    description=service.display_name,
                    quantity=qty,
                    location=location,
                    material_unit_cost=_material_cost(service),
                    labor_hours=round(service.default_labor_hours_per_unit * qty, 2),
                    hourly_rate=hourly_rate,
                    material_markup_percent=pricing.markup_percent or service.default_markup_percent,
                )
            )

    if not line_items:
        line_items.append(
            LineItem(
                description=req.text[:90] or "Custom electrical/HVAC work",
                quantity=1,
                location="",
                material_unit_cost=0,
                labor_hours=2,
                hourly_rate=hourly_rate,
                material_markup_percent=pricing.markup_percent,
            )
        )

    customer = get_customer(req.customer_id) if req.customer_id else req.customer

    return recalc(
        Quote(
            customer=customer,
            customer_id=req.customer_id,
            profile=req.profile,
            line_items=line_items,
            terms=pricing.default_terms,
        )
    )


async def generate_quote(req: QuoteRequest) -> Quote:
    deterministic_quote = catalogue_quote(req)

    # If catalogue matching found useful items, trust it.
    # The local LLM can help only when no catalogue match was found.
    if len(deterministic_quote.line_items) > 1:
        return deterministic_quote

    schema = '{"line_items":[{"description":"string","quantity":1,"location":"string","material_unit_cost":0,"labor_hours":0}]}'
    prompt = f"""You are an Australian electrical and HVAC quoting assistant.

Extract every separate job item from the tradie's job note.
Do not omit items.
Return ONLY valid JSON matching this schema:
{schema}

Job note:
{req.text}
"""

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(18.0, connect=2.0)) as client:
            response = await client.post(
                OLLAMA_URL,
                json={
                    "model": OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {"num_predict": 700},
                },
            )
            response.raise_for_status()
            payload = json.loads(response.json().get("response", "{}"))

        generated_items = payload.get("line_items") or []

        if generated_items:
            pricing = get_pricing()
            rate = pricing.hourly_rates.get(req.profile, pricing.hourly_rates["Residential"])

            deterministic_quote.line_items = [
                LineItem(
                    description=str(item.get("description", "Custom item")),
                    quantity=float(item.get("quantity", 1) or 1),
                    location=str(item.get("location", "") or ""),
                    material_unit_cost=float(item.get("material_unit_cost", 0) or 0),
                    labor_hours=float(item.get("labor_hours", 1) or 1),
                    hourly_rate=rate,
                    material_markup_percent=pricing.markup_percent,
                )
                for item in generated_items
                if isinstance(item, dict)
            ]

            return recalc(deterministic_quote)

    except Exception:
        await asyncio.sleep(0.1)

    return deterministic_quote

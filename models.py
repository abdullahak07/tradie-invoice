from __future__ import annotations
from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field

GST_RATE = 0.10

class CustomerIn(BaseModel):
    name: str = Field(..., min_length=1)
    phone: str = ""
    email: str = ""
    address: str = ""

class Customer(CustomerIn):
    id: int

class LineItem(BaseModel):
    description: str
    quantity: float = 1
    location: str = ""
    material_unit_cost: float = 0
    labor_hours: float = 0
    hourly_rate: float = 95
    material_markup_percent: float = 20

class QuoteRequest(BaseModel):
    text: str
    customer: CustomerIn | None = None
    customer_id: int | None = None
    profile: str = "Residential"
    demo: bool = False

class Quote(BaseModel):
    id: int | None = None
    quote_number: str = ""
    customer: CustomerIn | Customer | None = None
    customer_id: int | None = None
    business_name: str = "Perth Spark & Air"
    abn: str = "12 345 678 901"
    status: str = "draft"
    profile: str = "Residential"
    line_items: list[LineItem] = Field(default_factory=list)
    subtotal: float = 0
    gst: float = 0
    total: float = 0
    terms: str = "14 days payment. 50% deposit required for jobs over $2,000. Quote valid for 30 days."
    created_at: datetime = Field(default_factory=datetime.utcnow)

class PricingSettings(BaseModel):
    hourly_rates: dict[str, float] = Field(default_factory=lambda: {
        "Residential": 95, "Commercial": 120, "FIFO": 150, "Emergency": 200
    })
    markup_percent: float = 20
    gst_percent: float = 10
    default_terms: str = "14 days payment. 50% deposit required for jobs over $2,000. Quote valid for 30 days."
    materials: dict[str, float] = Field(default_factory=lambda: {
        "LED downlight": 22, "Power point (single)": 15, "Power point (double)": 25,
        "Light switch": 12, "RCD safety switch": 85, "Ceiling fan": 180, "Exhaust fan": 95
    })

class TranscriptionResponse(BaseModel):
    text: str
    source: str

class DemoSample(BaseModel):
    id: str
    title: str
    text: str

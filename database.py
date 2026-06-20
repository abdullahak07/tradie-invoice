from __future__ import annotations
import json, sqlite3
from pathlib import Path
from models import CustomerIn, Quote, PricingSettings
DB_PATH = Path("quotes.db")

def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, phone TEXT, email TEXT, address TEXT);
        CREATE TABLE IF NOT EXISTS quotes (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER, line_items_json TEXT NOT NULL, total REAL NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(customer_id) REFERENCES customers(id));
        CREATE TABLE IF NOT EXISTS pricing_settings (id INTEGER PRIMARY KEY CHECK(id=1), hourly_rates_json TEXT NOT NULL, markup_percent REAL NOT NULL, default_terms TEXT NOT NULL);
        ''')
        if not db.execute("SELECT 1 FROM pricing_settings WHERE id=1").fetchone():
            p = PricingSettings()
            db.execute("INSERT INTO pricing_settings VALUES (1,?,?,?)", (json.dumps(p.hourly_rates), p.markup_percent, p.default_terms))

def list_customers():
    with connect() as db:
        return [dict(r) for r in db.execute("SELECT * FROM customers ORDER BY id DESC")]

def add_customer(c: CustomerIn):
    with connect() as db:
        cur = db.execute("INSERT INTO customers(name,phone,email,address) VALUES (?,?,?,?)", (c.name,c.phone,c.email,c.address))
        return {"id": cur.lastrowid, **c.model_dump()}

def get_customer(cid: int):
    with connect() as db:
        r = db.execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None

def get_pricing():
    with connect() as db:
        r = db.execute("SELECT * FROM pricing_settings WHERE id=1").fetchone()
    p = PricingSettings()
    if r:
        p.hourly_rates = json.loads(r["hourly_rates_json"]); p.markup_percent = r["markup_percent"]; p.default_terms = r["default_terms"]
    return p

def save_quote(q: Quote):
    with connect() as db:
        cur = db.execute("INSERT INTO quotes(customer_id,line_items_json,total,status,created_at) VALUES (?,?,?,?,?)", (q.customer_id, json.dumps([i.model_dump() for i in q.line_items]), q.total, q.status, q.created_at.isoformat()))
        q.id = cur.lastrowid; q.quote_number = f"Q-{cur.lastrowid:05d}"
        return q

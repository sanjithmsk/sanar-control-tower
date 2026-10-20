"""SQLite storage: schema and sample seed data.

In production this module is replaced by connectors to the ERP / WMS / POS systems.
"""
import os
import sqlite3
from datetime import date, timedelta

SCHEMA = """
CREATE TABLE branches(id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE skus(id TEXT PRIMARY KEY, name TEXT NOT NULL, cold INT, controlled INT, seasonal INT,
                  impact INT, pack INT, price REAL);
CREATE TABLE batches(id INTEGER PRIMARY KEY, sku TEXT, branch TEXT, batch_no TEXT, qty INT, expiry TEXT);
CREATE TABLE positions(sku TEXT, branch TEXT, in_transit INT DEFAULT 0, forecast_14d INT, safety INT,
                       PRIMARY KEY(sku, branch));
CREATE TABLE sales(id INTEGER PRIMARY KEY, sku TEXT, branch TEXT, day TEXT, qty INT);
CREATE TABLE routes(a TEXT, b TEXT, hours INT, reefer INT, PRIMARY KEY(a, b));
CREATE TABLE suppliers(id INTEGER PRIMARY KEY, name TEXT, lead_days INT, price_mult REAL, compliance INT,
                       is_primary INT, available INT DEFAULT 1);
CREATE TABLE actions(id INTEGER PRIMARY KEY, kind TEXT, sku TEXT, from_branch TEXT, to_branch TEXT,
                     supplier TEXT, qty INT, value REAL, status TEXT, approved_by TEXT, created_at TEXT,
                     detail TEXT);
CREATE TABLE holds(sku TEXT, branch TEXT, PRIMARY KEY(sku, branch));
CREATE TABLE scenarios(key TEXT PRIMARY KEY, label TEXT, active INT DEFAULT 0);
CREATE TABLE events(id INTEGER PRIMARY KEY, ts TEXT DEFAULT (datetime('now','localtime')), msg TEXT);
"""

BRANCHES = {"A": "Central Retail", "B": "North Branch", "C": "East Branch", "D": "Distribution Hub"}
# id: name, cold, controlled, seasonal, clinical impact (1-10), pack size, unit price (SAR)
SKUS = {
    "INS": ("Insulin glargine 100 IU/mL", 1, 0, 0, 9, 10, 85),
    "ADA": ("Adalimumab 40 mg pen", 1, 0, 0, 8, 2, 2400),
    "TRM": ("Tramadol 50 mg", 0, 1, 0, 7, 10, 12),
    "OSE": ("Oseltamivir 75 mg", 0, 0, 1, 7, 10, 28),
    "AMX": ("Amoxicillin 500 mg", 0, 0, 0, 6, 20, 4),
    "PCM": ("Paracetamol 500 mg", 0, 0, 1, 4, 50, 1),
}
# sku -> branch -> (batches [(qty, days to expiry)], 14-day forecast, safety buffer)
STOCK = {
    "INS": {"A": ([(40, 200)], 120, 30), "B": ([(160, 90), (100, 240)], 90, 30),
            "C": ([(110, 150)], 80, 25), "D": ([(150, 300)], 60, 60)},
    "ADA": {"A": ([(2, 120)], 8, 2), "B": ([(6, 200)], 5, 1), "C": ([(4, 160)], 3, 1), "D": ([(14, 270)], 4, 4)},
    "TRM": {"A": ([(70, 300)], 50, 20), "B": ([(20, 150)], 60, 20), "C": ([(55, 200)], 40, 15),
            "D": ([(100, 365)], 60, 40)},
    "OSE": {"A": ([(150, 220)], 100, 30), "B": ([(120, 240)], 80, 25), "C": ([(100, 200)], 70, 20),
            "D": ([(400, 300)], 150, 80)},
    "AMX": {"A": ([(420, 180)], 280, 60), "B": ([(80, 45), (300, 120)], 250, 50),
            "C": ([(150, 210)], 300, 60), "D": ([(900, 330)], 300, 100)},
    "PCM": {"A": ([(900, 300)], 600, 150), "B": ([(700, 250)], 500, 120),
            "C": ([(300, 20), (520, 280)], 400, 100), "D": ([(3000, 400)], 1000, 500)},
}
# (a, b, transit hours, reefer vehicle available)
ROUTES = [("A", "B", 2, 1), ("A", "C", 3, 1), ("A", "D", 4, 1), ("B", "C", 3, 0), ("B", "D", 5, 1), ("C", "D", 2, 1)]
SCENARIOS = {
    "cold_fail": "Cold-chain failure on routes to Central Retail",
    "supplier_short": "Primary supplier shortage",
    "surge": "Seasonal flu surge, demand +60% on seasonal medicines",
}


def connect():
    conn = sqlite3.connect(os.environ.get("SANAR_DB", "sanar.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def log(conn, msg):
    conn.execute("INSERT INTO events(msg) VALUES (?)", (msg,))


def reset(conn):
    """Drop everything and load the sample data set."""
    for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        conn.execute(f"DROP TABLE IF EXISTS {r['name']}")
    conn.executescript(SCHEMA)
    today = date.today()
    conn.executemany("INSERT INTO branches VALUES (?,?)", BRANCHES.items())
    conn.executemany("INSERT INTO skus VALUES (?,?,?,?,?,?,?,?)", [(k, *v) for k, v in SKUS.items()])
    conn.executemany("INSERT INTO routes VALUES (?,?,?,?)", ROUTES)
    conn.executemany("INSERT INTO scenarios(key,label) VALUES (?,?)", SCENARIOS.items())
    conn.executemany(
        "INSERT INTO suppliers(name,lead_days,price_mult,compliance,is_primary) VALUES (?,?,?,?,?)",
        [("Supplier A (primary)", 3, 1.0, 98, 1), ("Supplier B (secondary)", 5, 1.08, 94, 0)],
    )
    for sku, per_branch in STOCK.items():
        for branch, (batches, forecast, safety) in per_branch.items():
            conn.execute("INSERT INTO positions VALUES (?,?,0,?,?)", (sku, branch, forecast, safety))
            for i, (qty, days) in enumerate(batches, 1):
                conn.execute(
                    "INSERT INTO batches(sku,branch,batch_no,qty,expiry) VALUES (?,?,?,?,?)",
                    (sku, branch, f"{sku}-{branch}{i}", qty, (today + timedelta(days=days)).isoformat()),
                )
            # 28 days of POS history whose run rate matches the forecast
            base, extra = divmod(forecast, 14)
            for d in range(28):
                qty = base + (1 if d % 14 < extra else 0)
                conn.execute("INSERT INTO sales(sku,branch,day,qty) VALUES (?,?,?,?)",
                             (sku, branch, (today - timedelta(days=d + 1)).isoformat(), qty))
    log(conn, "Control tower started. Monitoring 4 sites, 6 medicines.")
    conn.commit()


def init():
    conn = connect()
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='skus'").fetchone():
        reset(conn)
    conn.close()

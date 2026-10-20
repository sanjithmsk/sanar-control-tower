"""Semantic layer: turns raw tables into pharma business metrics and enforces guardrails.

Every number the planner or the LLM sees comes from here, so the rules live in one place.
"""
from datetime import date, timedelta

MIN_SHELF_DAYS = 30      # batches at or below this are excluded from planning and transfers
SIGNOFF_VALUE = 10_000   # SAR; actions above this need human sign-off
SURGE_FACTOR = 1.6
NEAR_EXPIRY_DAYS = 60


def scenario(conn, key):
    r = conn.execute("SELECT active FROM scenarios WHERE key=?", (key,)).fetchone()
    return bool(r and r["active"])


def _cutoff():
    return (date.today() + timedelta(days=MIN_SHELF_DAYS)).isoformat()


def eligible_batches(conn, sku, branch):
    """Batches usable for planning, earliest expiry first (FEFO)."""
    return conn.execute(
        "SELECT * FROM batches WHERE sku=? AND branch=? AND qty>0 AND expiry>? ORDER BY expiry",
        (sku, branch, _cutoff())).fetchall()


def days_left(expiry):
    return (date.fromisoformat(expiry) - date.today()).days


def metrics(conn, sku, branch):
    s = conn.execute("SELECT * FROM skus WHERE id=?", (sku,)).fetchone()
    p = conn.execute("SELECT * FROM positions WHERE sku=? AND branch=?", (sku, branch)).fetchone()
    fefo = sum(b["qty"] for b in eligible_batches(conn, sku, branch))
    forecast = p["forecast_14d"]
    if s["seasonal"] and scenario(conn, "surge"):
        forecast = int(forecast * SURGE_FACTOR + 0.5)
    allb = conn.execute("SELECT MIN(expiry) e FROM batches WHERE sku=? AND branch=? AND qty>0",
                        (sku, branch)).fetchone()["e"]
    return {
        "fefo_stock": fefo,
        "in_transit": p["in_transit"],
        "forecast": forecast,
        "safety": p["safety"],
        # Available FEFO Stock + In-Transit - (Forecasted Demand + Safety Buffer); negative = deficit
        "balance": fefo + p["in_transit"] - forecast - p["safety"],
        "days_on_hand": round((fefo + p["in_transit"]) / (forecast / 14), 1) if forecast else None,
        "earliest_days": days_left(allb) if allb else None,
    }


def route(conn, a, b):
    x, y = sorted((a, b))
    return conn.execute("SELECT * FROM routes WHERE a=? AND b=?", (x, y)).fetchone()


def route_ok(conn, sku, a, b):
    """Guardrail: cold-chain medicines only move on working reefer routes."""
    s = conn.execute("SELECT cold FROM skus WHERE id=?", (sku,)).fetchone()
    if not s["cold"]:
        return True
    r = route(conn, a, b)
    if not r or not r["reefer"]:
        return False
    if scenario(conn, "cold_fail") and "A" in (a, b):
        return False
    return True


def signoff_reasons(sku_row, value):
    """Guardrail: which actions must go to a human."""
    why = []
    if sku_row["controlled"]:
        why.append("controlled substance")
    if value > SIGNOFF_VALUE:
        why.append(f"value above SAR {SIGNOFF_VALUE:,}")
    return why


def refresh_forecast(conn, sku, branch):
    """Demand sensing baseline: 14-day forecast = run rate of the last 28 days of POS sales."""
    since = (date.today() - timedelta(days=28)).isoformat()
    total = conn.execute("SELECT COALESCE(SUM(qty),0) q FROM sales WHERE sku=? AND branch=? AND day>=?",
                         (sku, branch, since)).fetchone()["q"]
    f = int(total / 2 + 0.5)
    conn.execute("UPDATE positions SET forecast_14d=? WHERE sku=? AND branch=?", (f, sku, branch))
    return f

"""Deficit detection and resolution planning (Phase 1 and Phase 2 of the workflow).

Quantities and compliance decisions are computed here deterministically.
The LLM explains and answers questions; it never invents stock figures.
"""
import json
import math
from datetime import date, datetime, timedelta

from . import semantic as S
from .db import log


def _names(conn):
    return ({r["id"]: r["name"] for r in conn.execute("SELECT * FROM branches")},
            {r["id"]: r for r in conn.execute("SELECT * FROM skus")})


def alerts(conn):
    branches, skus = _names(conn)
    held = {(r["sku"], r["branch"]) for r in conn.execute("SELECT * FROM holds")}
    out = []
    for sid, s in skus.items():
        for bid, bname in branches.items():
            m = S.metrics(conn, sid, bid)
            if m["balance"] >= 0 or (sid, bid) in held:
                continue
            cov = (m["fefo_stock"] + m["in_transit"]) / max(m["forecast"], 1)
            sev = "Critical" if cov < 0.5 or (s["impact"] >= 8 and cov < 0.8) else "High" if cov < 0.8 else "Medium"
            need = -m["balance"]
            out.append({
                "sku": sid, "branch": bid, "sku_name": s["name"], "branch_name": bname, "need": need,
                "severity": sev, "impact": s["impact"], "cold": bool(s["cold"]), "controlled": bool(s["controlled"]),
                "signoff": S.signoff_reasons(s, need * s["price"]),
                "_score": s["impact"] * (1 - min(cov, 1)),
            })
    out.sort(key=lambda a: -a["_score"])
    return out


def pick_supplier(conn):
    rows = conn.execute("SELECT * FROM suppliers WHERE available=1 ORDER BY lead_days, price_mult").fetchall()
    if S.scenario(conn, "supplier_short"):
        rows = [r for r in rows if not r["is_primary"]]
    return rows[0] if rows else None


def plan(conn, sku, branch):
    """Step 1 network FEFO audit, Step 2 compliance check, Step 3 transfer and/or PO."""
    branches, skus = _names(conn)
    s = skus[sku]
    m = S.metrics(conn, sku, branch)
    if m["balance"] >= 0:
        return None
    need = remaining = -m["balance"]
    audit, transfers = [], []
    others = sorted((b for b in branches if b != branch), key=lambda b: S.route(conn, b, branch)["hours"])
    for o in others:
        om = S.metrics(conn, sku, o)
        elig = S.eligible_batches(conn, sku, o)
        surplus = max(min(om["balance"], om["fefo_stock"]), 0)
        ok = S.route_ok(conn, sku, o, branch)
        hours = S.route(conn, o, branch)["hours"]
        qty, result = 0, "No surplus"
        if surplus and not ok:
            result = "Blocked: no cold-chain route"
        elif surplus and remaining:
            qty = min(surplus, remaining)
            remaining -= qty
            transfers.append({"from": o, "from_name": branches[o], "qty": qty, "hours": hours})
            result = f"Selected: {qty:,} units"
        elif surplus:
            result = "Surplus not needed"
        audit.append({"branch": o, "branch_name": branches[o], "surplus": surplus,
                      "shelf_days": S.days_left(elig[0]["expiry"]) if elig else 0,
                      "hours": hours, "route_ok": ok, "result": result})
    po = None
    sup = pick_supplier(conn)
    if remaining and sup:
        qty = math.ceil(remaining / s["pack"]) * s["pack"]
        po = {"supplier": sup["name"], "qty": qty, "lead_days": sup["lead_days"],
              "compliance": sup["compliance"], "value": round(qty * s["price"] * sup["price_mult"], 2)}
    return {
        "sku": sku, "branch": branch, "sku_name": s["name"], "branch_name": branches[branch],
        "cold": bool(s["cold"]), "controlled": bool(s["controlled"]),
        "metrics": m, "need": need, "audit": audit, "transfers": transfers, "po": po,
        "vendor_lead_days": sup["lead_days"] if sup else None,
        "vendor_name": sup["name"] if sup else None,
        "signoff": S.signoff_reasons(s, need * s["price"]),
        "min_shelf_days": S.MIN_SHELF_DAYS,
    }


def execute(conn, sku, branch, approver):
    """Turn the current plan into transfer tickets and purchase orders."""
    p = plan(conn, sku, branch)
    if not p:
        return []
    now = datetime.now().isoformat(timespec="seconds")
    price = conn.execute("SELECT price FROM skus WHERE id=?", (sku,)).fetchone()["price"]
    ids = []
    for t in p["transfers"]:
        left, lines = t["qty"], []
        for b in S.eligible_batches(conn, sku, t["from"]):  # FEFO: earliest expiry leaves first
            take = min(b["qty"], left)
            if not take:
                break
            conn.execute("UPDATE batches SET qty=qty-? WHERE id=?", (take, b["id"]))
            lines.append({"batch_no": b["batch_no"], "qty": take, "expiry": b["expiry"]})
            left -= take
        cur = conn.execute(
            "INSERT INTO actions(kind,sku,from_branch,to_branch,qty,value,status,approved_by,created_at,detail)"
            " VALUES ('transfer',?,?,?,?,?,'in_transit',?,?,?)",
            (sku, t["from"], branch, t["qty"], t["qty"] * price, approver, now, json.dumps(lines)))
        ids.append(cur.lastrowid)
        conn.execute("UPDATE positions SET in_transit=in_transit+? WHERE sku=? AND branch=?", (t["qty"], sku, branch))
        log(conn, f"TR-{cur.lastrowid} {approver}: {t['qty']:,} × {p['sku_name']}, {t['from_name']} → "
                  f"{p['branch_name']}, ETA {t['hours']} h" + (", reefer 2–8 °C" if p["cold"] else ""))
    if p["po"]:
        po = p["po"]
        cur = conn.execute(
            "INSERT INTO actions(kind,sku,to_branch,supplier,qty,value,status,approved_by,created_at,detail)"
            " VALUES ('po',?,?,?,?,?,'in_transit',?,?,'[]')",
            (sku, branch, po["supplier"], po["qty"], po["value"], approver, now))
        ids.append(cur.lastrowid)
        conn.execute("UPDATE positions SET in_transit=in_transit+? WHERE sku=? AND branch=?", (po["qty"], sku, branch))
        log(conn, f"PO-{cur.lastrowid} {approver}: {po['qty']:,} × {p['sku_name']} to {po['supplier']} for "
                  f"{p['branch_name']}, lead {po['lead_days']} d, SAR {po['value']:,.0f}")
    conn.commit()
    return ids


def auto_run(conn):
    """Execute every routine action (no sign-off needed). Sign-off items stay in the queue."""
    count = 0
    for _ in range(100):
        routine = [a for a in alerts(conn) if not a["signoff"]]
        if not routine:
            break
        count += len(execute(conn, routine[0]["sku"], routine[0]["branch"], "auto-executed"))
    return count


def receive(conn, action_id):
    """Goods received: in-transit stock becomes batches at the destination."""
    a = conn.execute("SELECT * FROM actions WHERE id=? AND status='in_transit'", (action_id,)).fetchone()
    if not a:
        return False
    lines = json.loads(a["detail"]) or [{
        "batch_no": f"PO{a['id']}", "qty": a["qty"], "expiry": (date.today() + timedelta(days=365)).isoformat()}]
    for l in lines:
        conn.execute("INSERT INTO batches(sku,branch,batch_no,qty,expiry) VALUES (?,?,?,?,?)",
                     (a["sku"], a["to_branch"], l["batch_no"], l["qty"], l["expiry"]))
    conn.execute("UPDATE positions SET in_transit=MAX(in_transit-?,0) WHERE sku=? AND branch=?",
                 (a["qty"], a["sku"], a["to_branch"]))
    conn.execute("UPDATE actions SET status='received' WHERE id=?", (action_id,))
    log(conn, f"{'TR' if a['kind'] == 'transfer' else 'PO'}-{a['id']} received at {a['to_branch']}: {a['qty']:,} units")
    conn.commit()
    return True

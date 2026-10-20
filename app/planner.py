"""Planning engine: sense deficits, plan their resolution, execute approved actions, and run the
operations around them (receipts, recalls, quarantine, expiry rebalancing, load sharing, reports).

Quantities and compliance are decided here, deterministically, from the semantic layer and the
guardrails. The language model explains and answers questions; it never moves stock.
"""
import csv
import io
import json
import math
from datetime import date, datetime, timedelta

from . import guardrails as G
from .db import log
from .semantic import HORIZON, REF, Network, refresh_forecast


def _now():
    return datetime.now().isoformat(timespec="seconds")


def duration(hours):
    return f"{hours:g} h" if hours < 48 else f"{hours / 24:.0f} d"


def _spare(m):
    """What a site can give away: FEFO stock beyond its own forecast and safety buffer."""
    return max(min(m["balance"], m["fefo_stock"]), 0)


def _handling(net, item):
    labels = [r.label for t in sorted(item["tags"]) if (r := net.domain.requirements.get(t)) and (r.lane or r.temp)]
    return f", {'; '.join(labels)}" if labels else ""


def _badges(net, item):
    return [{"tag": t, "badge": r.badge, "label": r.label}
            for t in sorted(item["tags"]) if (r := net.domain.requirements.get(t)) and r.badge]


def _outcome(conn, kind, sku, site, units, value, ref):
    """Record what an action achieved or cost; reports are built from these."""
    conn.execute("INSERT INTO outcomes(day,kind,sku,site,units,value,ref) VALUES (?,?,?,?,?,?,?)",
                 (date.today().isoformat(), kind, sku, site, units, round(value, 2), ref))


def sense(conn):
    """Housekeeping before planning: lots past expiry are quarantined and their write-off recorded."""
    net = Network(conn)
    expired = [lot for lots in net.lots.values() for lot in lots
               if lot["status"] == "available" and lot["expiry"] and net.days_left(lot["expiry"]) <= 0]
    for lot in expired:
        conn.execute("UPDATE lots SET status='expired', note=? WHERE id=?", (f"Expired {lot['expiry']}", lot["id"]))
        item = net.items[lot["sku"]]
        value = lot["qty"] * item["price"]
        _outcome(conn, "write_off", lot["sku"], lot["site"], lot["qty"], value, lot["lot_no"])
        log(conn, f"Expired: {lot['lot_no']} {item['name']} at {net.sites[lot['site']]['name']}, {lot['qty']:,} units "
                  f"quarantined (write-off {net.domain.currency} {value:,.0f})")
    if expired:
        conn.commit()
        net = Network(conn)
    return net


# ---- planning -----------------------------------------------------------------------------------
def _donors(net, sku, site):
    """Sites with a lane to `site`, nearest first. A donor whose stock would otherwise expire unsold
    goes first when it can still arrive before the site runs out (waste prevention)."""
    window = (net.metrics(sku, site)["stockout_days"] or 0) * 24
    ranked = []
    for src in net.sites:
        if src != site and net.lane(src, site) and (sku, src) in net.positions:
            hours = G.transit_hours(net, src, site)
            at_risk = any(G.plannable(net, w) for w in net.waste(sku, src))
            ranked.append((not (at_risk and hours <= window), hours, src))
    return [src for *_, src in sorted(ranked)]


def _pick(net, sku, src, dst, want, transit_days, arriving):
    """FEFO lots from `src` up to `want`, each capped at what `dst` can sell before it expires."""
    lines = []
    for lot in net.lots.get((sku, src), []):
        if want <= 0:
            break
        if not G.plannable(net, lot):
            continue
        take = min(lot["qty"], want)
        if lot["expiry"]:
            planned = arriving + [(x["qty"], net.days_left(x["expiry"])) for x in lines if x["expiry"]]
            take = min(take, net.absorb(sku, dst, net.days_left(lot["expiry"]) - transit_days, planned))
        if take > 0:
            lines.append({"lot_id": lot["id"], "lot_no": lot["lot_no"], "qty": take, "expiry": lot["expiry"]})
            want -= take
    return lines


def _severity(item, m, risk, unmet):
    cov, impact = (m["fefo_stock"] + m["in_transit"] - m["late"]) / max(m["forecast"], 1), item["impact"]
    if (cov < 0.5 and impact >= 5) or (impact >= 8 and cov < 0.8) or ((risk or unmet) and impact >= 7):
        return "Critical"
    return "High" if cov < 0.8 or risk or unmet else "Medium"


def _alternatives(net, sku, site):
    """Substitutes that could bridge a gap. Advisory only: substitution is a professional decision."""
    out = []
    for sub in net.substitutes.get(sku, []):
        alt = sub["alt"]
        if alt not in net.items:
            continue
        here = net.metrics(alt, site)["fefo_stock"] if (alt, site) in net.positions else 0
        sources = []
        for src in net.sites:
            if src == site or (alt, src) not in net.positions or not net.lane(src, site):
                continue
            spare = _spare(net.metrics(alt, src))
            if spare:
                failed = [c for c in G.transfer_checks(net, alt, src, site) if not c["ok"]]
                sources.append({"site": src, "site_name": net.sites[src]["name"], "spare": spare, "ok": not failed,
                                "blocked_by": failed[0]["detail"] if failed else None})
        out.append({"sku": alt, "name": net.items[alt]["name"], "note": sub["note"], "at_site": here, "sources": sources})
    return out


def _late_for(net, sku, site):
    return [{"ref": f"{REF[a['kind']]}-{a['id']}", "source": a["supplier"] or net.sites[a["from_site"]]["name"],
             "qty": a["qty"], "late_days": round(a["late_hours"] / 24, 1),
             "excluded": a["late_hours"] > net.policy["late_cover_hours"]}
            for a in net.open if a["sku"] == sku and a["to_site"] == site and a["late_hours"] > 0]


def plan(net, sku, site):
    """Step 1 network audit, Step 2 guardrails, Step 3 transfers and/or a purchase order or import."""
    item, dest = net.items[sku], net.sites[site]
    m = net.metrics(sku, site)
    if m["balance"] >= 0:
        return None
    need = remaining = -m["balance"]
    room = net.room(site)
    audit, transfers, arriving = [], [], []
    for src in _donors(net, sku, site):
        om, checks = net.metrics(sku, src), G.transfer_checks(net, sku, src, site)
        spare, failed = _spare(om), [c for c in checks if not c["ok"]]
        hours = G.transit_hours(net, src, site)
        qty, result = 0, "No surplus"
        if spare and failed:
            result = "Blocked: " + failed[0]["detail"]
        elif spare and remaining and room > 0:
            lines = _pick(net, sku, src, site, min(spare, remaining, room), math.ceil(hours / 24), arriving)
            qty = sum(x["qty"] for x in lines)
            if qty:
                remaining, room = remaining - qty, room - qty
                arriving += [(x["qty"], net.days_left(x["expiry"])) for x in lines if x["expiry"]]
                at_risk = {w["id"]: w["unsold"] for w in net.waste(sku, src)}
                transfers.append({"from": src, "from_name": net.sites[src]["name"], "qty": qty, "hours": hours,
                                  "value": round(qty * item["price"], 2),
                                  "cross_border": net.sites[src]["country"] != dest["country"],
                                  "rescued": sum(min(x["qty"], at_risk.get(x["lot_id"], 0)) for x in lines),
                                  "transport": net.transport(src, site, qty), "lines": lines, "checks": checks})
                result = f"Selected: {qty:,} units"
            else:
                result = "Blocked: would expire before it can be used here"
        elif spare:
            result = "Surplus not needed" if not remaining else "Blocked: no storage room at destination"
        first = next((lot for lot in net.lots.get((sku, src), []) if G.plannable(net, lot)), None)
        audit.append({"site": src, "site_name": net.sites[src]["name"], "surplus": spare, "hours": hours,
                      "shelf_days": net.days_left(first["expiry"]) if first and first["expiry"] else None,
                      "ok": not failed, "blocked_by": failed[0]["code"] if failed else None, "result": result})

    coverage = net.days_until(sku, site, m["on_hand"] + m["in_transit"] - m["late"] + sum(t["qty"] for t in transfers))
    qty = math.ceil(remaining / item["pack"]) * item["pack"]
    inbound = sum(t["qty"] for t in transfers) + qty
    options = [G.sourcing_option(net, sku, s, site, inbound) for s in net.suppliers
               if item["category"] in s["categories"]]
    usable, po, urgent = [o for o in options if o["ok"]], None, False
    if remaining and usable:
        fastest = min(usable, key=lambda o: (o["lead_days"], o["unit_cost"]))
        cheapest = min(usable, key=lambda o: (o["unit_cost"], o["lead_days"]))
        urgent = coverage is not None and coverage < cheapest["lead_days"] + net.policy["lead_time_buffer_days"]
        best = fastest if urgent else cheapest
        po = {k: best[k] for k in ("supplier", "supplier_id", "country", "country_name", "incoterm", "lead_days",
                                   "customs_days", "duty_pct", "freight_pct", "unit_cost", "premium_pct", "compliance",
                                   "otif_pct")}
        po.update(kind="import" if best["import"] else "po", qty=qty, value=round(qty * best["unit_cost"], 2),
                  cover_days=net.days_until(sku, site, qty),
                  why="fastest compliant source: cover is too short to wait for the cheapest"
                      if urgent and best is not cheapest else "lowest landed cost among compliant sources")
        remaining = 0
    for o in options:
        failed = [c for c in o["checks"] if not c["ok"]]
        o["selected"] = bool(po) and o["supplier_id"] == po["supplier_id"]
        o["note"] = (failed[0]["detail"] if failed else "Selected" if o["selected"] else
                     "Not needed" if not po else "Arrives later" if urgent else "Higher landed cost")
    unmet = remaining
    risk = bool(po and coverage is not None and coverage < po["lead_days"]) or bool(unmet and coverage is not None)
    actions = ([{"value": t["value"], "cross_border": t["cross_border"]} for t in transfers]
               + ([{"value": po["value"], "premium_pct": po["premium_pct"], "cover_days": po["cover_days"],
                    "import": po["kind"] == "import"}] if po else []))
    reasons = G.signoff(net, item, actions)
    rules = [G.check("FEFO", True, "Earliest-expiry lots leave first", "SOP-INV-02"),
             G.check("SHELF_LIFE", True, f"Only lots with more than {net.policy['min_shelf_days']:g} days left are "
                                         "planned or moved", "SOP-INV-02"),
             G.check("DONOR_SAFE", True, "Donors keep their own forecast and safety buffer", "SOP-INV-02")]
    if m["late"]:
        rules.append(G.check("LATE_SHIPMENTS", True, f"{m['late']:,} units more than "
                             f"{net.policy['late_cover_hours']:g} h past ETA are not counted as cover", "POL-PL-01"))
    for t in transfers:
        rules += [{**c, "detail": f"{t['from_name']}: {c['detail']}"} for c in t.pop("checks")]
    if po:
        best_checks = next(o["checks"] for o in options if o["selected"])
        rules += [{**c, "detail": f"{po['supplier']}: {c['detail']}"} for c in best_checks]
    first_arrival = min([t["hours"] / 24 for t in transfers] + ([po["lead_days"]] if po else []), default=None)
    return {
        "sku": sku, "site": site, "sku_name": item["name"], "site_name": dest["name"], "category": item["category"],
        "badges": _badges(net, item), "metrics": m, "need": need, "severity": _severity(item, m, risk, unmet),
        "audit": audit, "transfers": transfers, "sourcing": options, "po": po, "unmet": unmet,
        "late_shipments": _late_for(net, sku, site),
        "timeline": {"stockout_days": m["stockout_days"], "coverage_days": coverage,
                     "first_arrival_days": round(first_arrival, 1) if first_arrival is not None else None,
                     "po_lead_days": po["lead_days"] if po else None,
                     "gap_days": round(po["lead_days"] - coverage, 1) if risk and po and coverage is not None else None},
        "stockout_risk": risk, "checks": rules,
        "alternatives": _alternatives(net, sku, site) if risk or unmet else [],
        "signoff": [r["reason"] for r in reasons], "signoff_detail": reasons,
        "signoff_roles": G.allowed_roles(net, reasons) if reasons else [],
        "value": round(sum(a["value"] for a in actions), 2),
        "transport_cost": sum(t["transport"]["cost"] for t in transfers),
        "min_shelf_days": net.policy["min_shelf_days"], "currency": net.domain.currency,
    }


def resolution(p):
    parts = [f"Transfer {t['qty']:,} from {t['from_name']} ({duration(t['hours'])})" for t in p["transfers"]]
    if p["po"]:
        parts.append(f"{'Import' if p['po']['kind'] == 'import' else 'Order'} {p['po']['qty']:,} from "
                     f"{p['po']['supplier']} ({p['po']['lead_days']} d)")
    if p["unmet"]:
        parts.append(f"No compliant source for {p['unmet']:,}")
    return " + ".join(parts)


def alerts(net):
    out = []
    for sku, item in net.items.items():
        for site in net.sites:
            if (sku, site) not in net.positions or (sku, site) in net.holds:
                continue
            p = plan(net, sku, site)
            if not p:
                continue
            m = p["metrics"]
            cov = (m["fefo_stock"] + m["in_transit"] - m["late"]) / max(m["forecast"], 1)
            out.append({
                "sku": sku, "site": site, "sku_name": item["name"], "site_name": p["site_name"], "need": p["need"],
                "severity": p["severity"], "impact": item["impact"], "badges": p["badges"],
                "signoff": p["signoff"], "signoff_roles": p["signoff_roles"], "stockout_days": m["stockout_days"],
                "stockout_risk": p["stockout_risk"], "unmet": p["unmet"], "resolution": resolution(p),
                "value": p["value"], "late": m["late"], "uplift": [u["name"] for u in m["uplifts"]],
                "transfers": [{"from": t["from"], "from_name": t["from_name"], "qty": t["qty"]} for t in p["transfers"]],
                "_score": item["impact"] * (1 - min(cov, 1)) + item["impact"] * p["stockout_risk"] + 0.5 * bool(p["unmet"]),
            })
    out.sort(key=lambda a: -a["_score"])
    return out


# ---- execution ------------------------------------------------------------------------------------
def _take(conn, lines):
    for x in lines:
        if conn.execute("UPDATE lots SET qty=qty-? WHERE id=? AND qty>=?", (x["qty"], x["lot_id"], x["qty"])).rowcount != 1:
            raise G.Rejected("Stock changed while planning; refresh and try again")


def _book_trucks(conn, net, src, dst, qty):
    """Put `qty` on today's trucks for the lane, filling booked trucks before opening new ones."""
    t, left, ids = net.transport(src, dst, qty), qty, []
    for truck in net.trucks.get((src, dst), []):
        if (take := min(truck["capacity"] - truck["used"], left)) > 0:
            conn.execute("UPDATE loads SET used=used+? WHERE id=?", (take, truck["id"]))
            truck["used"] += take
            left -= take
            ids.append(truck["id"])
    while left > 0:
        take = min(left, t["vehicle_units"])
        cur = conn.execute("INSERT INTO loads(a,b,day,capacity,used,cost) VALUES (?,?,?,?,?,?)",
                           (src, dst, net.today.isoformat(), t["vehicle_units"], take, t["trip_cost"]))
        net.trucks.setdefault((src, dst), []).append({"id": cur.lastrowid, "capacity": t["vehicle_units"], "used": take})
        left -= take
        ids.append(cur.lastrowid)
    return t, ids


def _ship(conn, net, sku, src, dst, qty, value, lines, approver, role, hours, purpose=None):
    eta = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="minutes")
    cross = net.sites[src]["country"] != net.sites[dst]["country"]
    t, trucks = _book_trucks(conn, net, src, dst, qty)
    cur = conn.execute(
        "INSERT INTO actions(kind,sku,from_site,to_site,qty,value,status,approved_by,role,created_at,eta,detail)"
        " VALUES ('transfer',?,?,?,?,?,'in_transit',?,?,?,?,?)",
        (sku, src, dst, qty, value, approver, role, _now(), eta,
         json.dumps({"lines": lines, "cross_border": cross, "purpose": purpose, "trucks": trucks,
                     "transport_cost": t["cost"], "transport_saved": t["saved"]})))
    conn.execute("UPDATE positions SET in_transit=in_transit+? WHERE sku=? AND site=?", (qty, sku, dst))
    ref = f"TR-{cur.lastrowid}"
    if t["cost"]:
        _outcome(conn, "transport", sku, dst, qty, t["cost"], ref)
    if t["saved"]:
        _outcome(conn, "transport_saved", sku, dst, qty, t["saved"], ref)
    truck = (f", shares a booked truck (saves {net.domain.currency} {t['saved']:,.0f})" if t["shares_truck"] else
             f", {t['trips']} truck{'s' if t['trips'] > 1 else ''} {net.domain.currency} {t['cost']:,.0f}" if t["cost"] else "")
    return cur.lastrowid, cross, truck


def execute(conn, sku, site, approver, role, net=None):
    """Turn the current plan into transfer tickets and a purchase order. Guardrails and sign-off are
    re-evaluated here, so a stale screen cannot push through an action that is now blocked."""
    net = net or Network(conn)
    p = plan(net, sku, site)
    if not p or not (p["transfers"] or p["po"]):
        return []
    G.authorize(net, p["signoff_detail"], role)
    item, ids = net.items[sku], []
    try:
        for t in p["transfers"]:
            _take(conn, t["lines"])
            aid, cross, truck = _ship(conn, net, sku, t["from"], site, t["qty"], t["value"], t["lines"], approver,
                                      role, t["hours"])
            ids.append(aid)
            if t["rescued"]:
                _outcome(conn, "waste_saved", sku, t["from"], t["rescued"], t["rescued"] * item["price"], f"TR-{aid}")
            log(conn, f"TR-{aid} {approver}: {t['qty']:,} × {item['name']}, {t['from_name']} → {p['site_name']}, "
                      f"ETA {duration(t['hours'])}{_handling(net, item)}" + (", cross-border" if cross else "") + truck)
        if po := p["po"]:
            eta = (datetime.now() + timedelta(days=po["lead_days"])).isoformat(timespec="minutes")
            cur = conn.execute(
                "INSERT INTO actions(kind,sku,to_site,supplier,qty,value,status,approved_by,role,created_at,eta,detail)"
                " VALUES (?,?,?,?,?,?,'in_transit',?,?,?,?,?)",
                (po["kind"], sku, site, po["supplier"], po["qty"], po["value"], approver, role, _now(), eta,
                 json.dumps({"lines": [], "incoterm": po["incoterm"], "country": po["country"]})))
            ids.append(cur.lastrowid)
            conn.execute("UPDATE positions SET in_transit=in_transit+? WHERE sku=? AND site=?", (po["qty"], sku, site))
            ref = f"{REF[po['kind']]}-{cur.lastrowid}"
            _outcome(conn, "spend", sku, site, po["qty"], po["value"], ref)
            trade = (f", {po['incoterm']} from {po['country_name']}, customs {po['customs_days']} d, duty "
                     f"{po['duty_pct']:g}%" if po["kind"] == "import" else "")
            log(conn, f"{ref} {approver}: {po['qty']:,} × {item['name']} from {po['supplier']} for "
                      f"{p['site_name']}, lead {po['lead_days']} d{trade}, {net.domain.currency} {po['value']:,.0f}")
        m = p["metrics"]
        if m["stockout_days"] is not None and m["stockout_days"] <= HORIZON and not p["stockout_risk"]:
            _outcome(conn, "stockout_avoided", sku, site, p["need"], p["need"] * item["price"],
                     ",".join(f"#{i}" for i in ids))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return ids


def auto_run(conn):
    """Execute every routine action (no sign-off needed). Sign-off items stay in the queue; alerts
    with nothing executable are skipped instead of retried."""
    done, stuck = 0, set()
    sense(conn)
    for _ in range(500):
        net = Network(conn)
        todo = [a for a in alerts(net) if not a["signoff"] and (a["sku"], a["site"]) not in stuck]
        if not todo:
            break
        a = todo[0]
        ids = execute(conn, a["sku"], a["site"], "auto-executed", "system", net)
        if not ids or a["unmet"]:
            stuck.add((a["sku"], a["site"]))
        done += len(ids)
    left = alerts(Network(conn))
    return {"executed": done, "awaiting_signoff": sum(1 for a in left if a["signoff"]), "unresolved": len(stuck)}


# ---- receipts, recalls and quarantine ---------------------------------------------------------------
def receive(conn, action_id, min_temp=None, max_temp=None, minutes=0, by="receiving", qty=None):
    """Goods received: in-transit stock becomes lots at the destination. Recalled lots and goods with a
    temperature excursion go straight into quarantine. A short delivery closes the order; the receipt
    date and quantity feed the supplier's scorecard."""
    net = Network(conn)
    a = conn.execute("SELECT * FROM actions WHERE id=? AND status='in_transit'", (action_id,)).fetchone()
    if not a:
        return None
    item = net.items[a["sku"]]
    detail = json.loads(a["detail"] or "{}")
    lines = detail.get("lines") or [{"lot_no": f"{REF[a['kind']]}{a['id']}", "qty": a["qty"],
                                     "expiry": (net.today + timedelta(days=item["shelf_days"])).isoformat()
                                     if item["shelf_days"] else None}]
    pending = sum(x["qty"] for x in lines if not x.get("recalled"))  # still counted as in transit
    got = a["qty"] if qty is None else max(0, min(int(qty), a["qty"]))
    excursion = G.receipt_check(net, item, min_temp, max_temp, minutes)
    left, held = got, 0
    for x in lines:
        take = min(x["qty"], left)
        if take <= 0:
            break
        left -= take
        status, note = "available", None
        if x.get("recalled") or x["lot_no"] in net.recalled:
            status, note = "recalled", "Recalled while in transit"
        elif excursion:
            status, note = "quarantined", excursion
        conn.execute("INSERT INTO lots(sku,site,lot_no,qty,expiry,status,note) VALUES (?,?,?,?,?,?,?)",
                     (a["sku"], a["to_site"], x["lot_no"], take, x["expiry"], status, note))
        held += take if status != "available" else 0
    conn.execute("UPDATE positions SET in_transit=MAX(in_transit-?,0) WHERE sku=? AND site=?",
                 (pending, a["sku"], a["to_site"]))
    conn.execute("UPDATE actions SET status='received', received_at=?, received_qty=? WHERE id=?",
                 (_now(), got, action_id))
    late = max(0.0, (datetime.now() - datetime.fromisoformat(a["eta"])).total_seconds() / 86400)
    log(conn, f"{REF[a['kind']]}-{a['id']} received at {net.sites[a['to_site']]['name']} by {by}: {got:,} of "
              f"{a['qty']:,} units" + (f" (short {a['qty'] - got:,})" if got < a["qty"] else "")
              + (f", {late:.1f} days late" if late > 0.5 else "")
              + (f"; {held:,} quarantined ({excursion or 'recalled lot'})" if held else ""))
    conn.commit()
    return {"ok": True, "received": got, "short": a["qty"] - got, "late_days": round(late, 1), "quarantined": held,
            "note": excursion}


def cancel(conn, action_id, by, role):
    """Cancel an open supplier order (typically a late one); the shortfall is re-planned."""
    net = Network(conn)
    G.require_role(net, "cancel", role)
    a = next((x for x in net.open if x["id"] == action_id), None)
    if not a:
        raise G.NotFound("No open order with that number")
    if a["kind"] == "transfer":
        raise G.Rejected("A transfer already on the road cannot be cancelled; receive it instead")
    conn.execute("UPDATE actions SET status='cancelled' WHERE id=?", (action_id,))
    conn.execute("UPDATE positions SET in_transit=MAX(in_transit-?,0) WHERE sku=? AND site=?",
                 (a["qty"], a["sku"], a["to_site"]))
    ref = f"{REF[a['kind']]}-{a['id']}"
    if not a["detail"].get("sample"):
        _outcome(conn, "spend", a["sku"], a["to_site"], -a["qty"], -a["value"], ref)
    log(conn, f"{ref} cancelled by {by}: {a['qty']:,} × {net.items[a['sku']]['name']} from {a['supplier']}"
              + (f", {a['late_hours'] / 24:.1f} days late" if a["late_hours"] else ""))
    conn.commit()


def recall(conn, lot_no, reason, by, role):
    """Quarantine every unit of a lot across the network, including shipments still on the road."""
    net = Network(conn)
    G.require_role(net, "recall", role)
    lot_no = lot_no.strip()
    stock = conn.execute("SELECT * FROM lots WHERE lot_no=? AND qty>0 AND status!='disposed'", (lot_no,)).fetchall()
    moving = 0
    for a in conn.execute("SELECT * FROM actions WHERE status='in_transit' AND kind='transfer'").fetchall():
        detail = json.loads(a["detail"] or "{}")
        hit = [x for x in detail.get("lines", []) if x["lot_no"] == lot_no and not x.get("recalled")]
        if hit:
            for x in hit:
                x["recalled"] = True
            qty = sum(x["qty"] for x in hit)
            moving += qty
            conn.execute("UPDATE actions SET detail=? WHERE id=?", (json.dumps(detail), a["id"]))
            conn.execute("UPDATE positions SET in_transit=MAX(in_transit-?,0) WHERE sku=? AND site=?",
                         (qty, a["sku"], a["to_site"]))
    if not stock and not moving:
        raise G.NotFound(f"No stock or shipment found for lot {lot_no}")
    conn.execute("UPDATE lots SET status='recalled', note=? WHERE lot_no=? AND status!='disposed'", (reason, lot_no))
    conn.execute("INSERT OR REPLACE INTO recalls VALUES (?,?,?,?)", (lot_no, reason, _now(), by))
    held = sum(r["qty"] for r in stock)
    where = ", ".join(sorted({net.sites[r["site"]]["name"] for r in stock}))
    log(conn, f"Recall {lot_no} by {by}: {held:,} units quarantined" + (f" at {where}" if where else "")
              + (f", {moving:,} units in transit flagged" if moving else "") + f" ({reason})")
    conn.commit()
    return {"lot_no": lot_no, "quarantined": held, "in_transit": moving}


def _lot(conn, lot_id):
    lot = conn.execute("SELECT * FROM lots WHERE id=?", (lot_id,)).fetchone()
    if not lot:
        raise G.NotFound("No such lot")
    return lot


def release(conn, lot_id, by, role):
    """QA releases a quarantined lot back to stock. Recalled or expired stock can never be released."""
    net = Network(conn)
    G.require_role(net, "release", role)
    lot = _lot(conn, lot_id)
    if lot["status"] != "quarantined":
        raise G.Rejected(f"Only quarantined lots can be released; this lot is {lot['status']}")
    conn.execute("UPDATE lots SET status='available', note=? WHERE id=?",
                 (f"{lot['note'] or ''} · released by {by}".strip(" ·"), lot_id))
    log(conn, f"Released {lot['lot_no']} at {net.sites[lot['site']]['name']} by {by}: {lot['qty']:,} units back in stock")
    conn.commit()


def dispose(conn, lot_id, by, role, method="destroy"):
    """Destroy or return held stock to the supplier and record the write-off (expired stock was already
    written off when it expired)."""
    net = Network(conn)
    G.require_role(net, "dispose", role)
    lot = _lot(conn, lot_id)
    if lot["status"] not in ("quarantined", "recalled", "expired"):
        raise G.Rejected("Only quarantined, recalled or expired lots can be disposed of")
    conn.execute("UPDATE lots SET status='disposed', note=? WHERE id=?", (f"{method} by {by}", lot_id))
    value = lot["qty"] * net.items[lot["sku"]]["price"]
    if lot["status"] != "expired":
        _outcome(conn, "write_off", lot["sku"], lot["site"], lot["qty"], value, lot["lot_no"])
    log(conn, f"{'Returned to supplier' if method == 'return' else 'Destroyed'}: {lot['lot_no']} "
              f"{lot['qty']:,} units by {by} (write-off {net.domain.currency} {value:,.0f})")
    conn.commit()


def hold(conn, sku, site, by):
    conn.execute("INSERT OR REPLACE INTO holds VALUES (?,?,?,?)", (sku, site, by, _now()))
    log(conn, f"Held for manual review by {by}: {sku} at {site}")
    conn.commit()


def release_hold(conn, sku, site, by):
    if conn.execute("DELETE FROM holds WHERE sku=? AND site=?", (sku, site)).rowcount:
        log(conn, f"Hold released by {by}: {sku} at {site}")
    conn.commit()


def record_demand(conn, sku, site, qty, day, by="POS"):
    """Demand feed (POS, orders): store it, refresh the demand-sensing forecast, flag spikes."""
    conn.execute("INSERT INTO demand(sku,site,day,qty) VALUES (?,?,?,?)", (sku, site, day, qty))
    f = refresh_forecast(conn, sku, site)
    net = Network(conn)
    d = net.demand.get((sku, site), {})
    avg = (d.get("base") or 0) / 28
    spike = bool(avg and d["q0"] >= 3 and d["q0"] >= net.policy["spike_factor"] * avg)
    if spike:
        log(conn, f"Demand spike via {by}: {net.items[sku]['name']} at {net.sites[site]['name']}, {d['q0']:,} units "
                  f"today vs {avg:.1f}/day ({d['q0'] / avg:.1f}×)")
    conn.commit()
    return {"forecast_14d": f, "spike": spike}


# ---- waste prevention ------------------------------------------------------------------------------
def rebalance(net):
    """Transfers that save lots projected to expire unsold, best value first. A destination only gets
    what it can sell before expiry without putting its own lots at risk."""
    out = []
    for (sku, src) in list(net.lots):
        waste = net.waste(sku, src)
        if not waste:
            continue
        item, spare = net.items[sku], _spare(net.metrics(sku, src))
        for w in waste:
            if spare <= 0 or not G.plannable(net, w):
                continue
            best = None
            for dst in net.sites:
                if dst == src or not net.lane(src, dst) or (sku, dst) not in net.positions:
                    continue
                if not all(c["ok"] for c in G.transfer_checks(net, sku, src, dst)):
                    continue
                hours = G.transit_hours(net, src, dst)
                qty = min(w["unsold"], spare, net.absorb(sku, dst, w["days"] - math.ceil(hours / 24)), net.room(dst))
                key = (net.metrics(sku, dst)["balance"] < 0, qty, -hours)
                if qty > 0 and (best is None or key > best[0]):
                    best = (key, dst, int(qty), hours)
            if not best:
                continue
            _, dst, qty, hours = best
            spare -= qty
            value = round(qty * item["price"], 2)
            cross = net.sites[src]["country"] != net.sites[dst]["country"]
            reasons = G.signoff(net, item, [{"value": value, "cross_border": cross}])
            out.append({"lot_id": w["id"], "lot_no": w["lot_no"], "sku": sku, "sku_name": item["name"],
                        "from": src, "from_name": net.sites[src]["name"], "to": dst, "to_name": net.sites[dst]["name"],
                        "qty": qty, "days_left": w["days"], "unsold": w["unsold"], "hours": hours, "value_saved": value,
                        "transport": net.transport(src, dst, qty),
                        "signoff": [r["reason"] for r in reasons], "signoff_detail": reasons,
                        "signoff_roles": G.allowed_roles(net, reasons) if reasons else []})
    return sorted(out, key=lambda r: -r["value_saved"])


def execute_rebalance(conn, lot_id, dst, approver, role):
    net = Network(conn)
    prop = next((r for r in rebalance(net) if r["lot_id"] == lot_id and r["to"] == dst), None)
    if not prop:
        raise G.NotFound("No rebalancing proposal for that lot and destination")
    G.authorize(net, prop["signoff_detail"], role)
    lot = next(x for x in net.lots[(prop["sku"], prop["from"])] if x["id"] == lot_id)
    lines = [{"lot_id": lot_id, "lot_no": lot["lot_no"], "qty": prop["qty"], "expiry": lot["expiry"]}]
    try:
        _take(conn, lines)
        aid, _, truck = _ship(conn, net, prop["sku"], prop["from"], dst, prop["qty"], prop["value_saved"], lines,
                              approver, role, prop["hours"], "rebalance")
        _outcome(conn, "waste_saved", prop["sku"], prop["from"], prop["qty"], prop["value_saved"], f"TR-{aid}")
        log(conn, f"TR-{aid} {approver}: rebalance {prop['qty']:,} × {prop['sku_name']} ({lot['lot_no']}) "
                  f"{prop['from_name']} → {prop['to_name']} before expiry, saves {net.domain.currency} "
                  f"{prop['value_saved']:,.0f}{truck}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return aid


def load_plan(net, al):
    """Pending transfers grouped by lane: trucks and cost if they travel together versus one by one."""
    lanes = {}
    for a in al:
        for t in a["transfers"]:
            lanes.setdefault((t["from"], a["site"]), []).append({"sku_name": a["sku_name"], "qty": t["qty"],
                                                                 "source": "deficit"})
    for r in rebalance(net):
        lanes.setdefault((r["from"], r["to"]), []).append({"sku_name": r["sku_name"], "qty": r["qty"],
                                                           "source": "rebalance"})
    out = []
    for (src, dst), loads in lanes.items():
        units, lane = sum(x["qty"] for x in loads), net.lane(src, dst)
        cap, trip = lane["vehicle_units"] or units, lane["trip_cost"] or 0
        together, separate = net.transport(src, dst, units), sum(math.ceil(x["qty"] / cap) * trip for x in loads)
        out.append({"from": src, "from_name": net.sites[src]["name"], "to": dst, "to_name": net.sites[dst]["name"],
                    "items": loads, "units": units, "trucks": together["trips"], "fill_pct": together["fill_pct"],
                    "cost": together["cost"], "separate_cost": separate, "saved": separate - together["cost"],
                    "trip_cost": trip, "vehicle_units": cap})
    return sorted(out, key=lambda x: (-x["saved"], -x["units"]))


# ---- read models for the console and the agent ----------------------------------------------------
def check_transfer(net, sku, src, dst, qty=None):
    """What-if: may `sku` move from `src` to `dst` now, and how much? Read-only."""
    item, a, b = net.items[sku], net.sites[src], net.sites[dst]
    checks = G.transfer_checks(net, sku, src, dst, qty or 0)
    spare = _spare(net.metrics(sku, src)) if (sku, src) in net.positions else 0
    checks.append(G.check("DONOR_SURPLUS", spare, f"{a['name']} can spare {spare:,} units without going into deficit"
                          if spare else f"{a['name']} has no surplus to give", "SOP-INV-02"))
    movable, want = 0, min(spare, qty or spare)
    if net.lane(src, dst) and want:
        lines = _pick(net, sku, src, dst, want, math.ceil(G.transit_hours(net, src, dst) / 24), [])
        movable = sum(x["qty"] for x in lines)
        if movable < want:
            checks.append(G.check("SELL_THROUGH", movable, f"Only {movable:,} units can be used at {b['name']} before "
                                  "they expire" if movable else f"{b['name']} could not use this stock before it "
                                  "expires", "SOP-INV-05"))
    ok = all(c["ok"] for c in checks)
    cross = a["country"] != b["country"]
    reasons = G.signoff(net, item, [{"value": movable * item["price"], "cross_border": cross}]) if movable else []
    return {"sku": sku, "item": item["name"], "from": src, "from_name": a["name"], "to": dst, "to_name": b["name"],
            "allowed": ok and movable > 0, "max_qty": movable if ok else 0, "requested": qty, "checks": checks,
            "signoff": [r["reason"] for r in reasons],
            "transport": net.transport(src, dst, movable) if net.lane(src, dst) and movable else None}


def kpis(net, al):
    waste = sum(net.metrics(sku, site)["waste_value"] for sku, site in net.positions)
    held = sum(lot["qty"] for lots in net.lots.values() for lot in lots if lot["status"] != "available")
    today = net.conn.execute("SELECT COUNT(*) n FROM actions WHERE created_at>=? AND role!='history'",
                             (net.today.isoformat(),)).fetchone()["n"]
    return {"open_deficits": len(al), "stockout_risks": sum(1 for a in al if a["stockout_risk"]),
            "need_signoff": sum(1 for a in al if a["signoff"]), "waste_value": round(waste), "held_units": held,
            "late_shipments": sum(1 for a in net.open if a["late_hours"] > 0), "actions_today": today,
            "auto_spend": round(net.auto_spend), "auto_budget": net.policy["auto_daily_budget"]}


def expiry_watch(net):
    limit, floor, out = net.policy["near_expiry_days"], net.policy["min_shelf_days"], []
    for (sku, site), lots in net.lots.items():
        unsold = {w["id"]: w["unsold"] for w in net.waste(sku, site)}
        for lot in lots:
            if lot["status"] != "available" or not lot["expiry"]:
                continue
            days = net.days_left(lot["expiry"])
            if days <= limit or lot["id"] in unsold:
                out.append({"lot_id": lot["id"], "sku": sku, "sku_name": net.items[sku]["name"], "site": site,
                            "site_name": net.sites[site]["name"], "lot_no": lot["lot_no"], "qty": lot["qty"],
                            "days": days, "excluded": days <= floor, "unsold": unsold.get(lot["id"], 0)})
    return sorted(out, key=lambda e: e["days"])


def quarantine(net):
    return [{"lot_id": lot["id"], "sku": lot["sku"], "sku_name": net.items[lot["sku"]]["name"],
             "site_name": net.sites[lot["site"]]["name"], "lot_no": lot["lot_no"], "qty": lot["qty"],
             "status": lot["status"], "note": lot["note"],
             "value": round(lot["qty"] * net.items[lot["sku"]]["price"], 2)}
            for lots in net.lots.values() for lot in lots if lot["status"] != "available"]


def lots_view(net, sku, site=None):
    return [{**lot, "site_name": net.sites[lot["site"]]["name"],
             "days": net.days_left(lot["expiry"]) if lot["expiry"] else None, "plannable": G.plannable(net, lot)}
            for (s, st), lots in net.lots.items() if s == sku and (site is None or st == site) for lot in lots]


def in_transit(net):
    out = []
    for a in net.open:
        item = net.items[a["sku"]]
        temp = next((r.temp for t in sorted(item["tags"]) if (r := net.domain.requirements.get(t)) and r.temp), None)
        out.append({"id": a["id"], "kind": a["kind"], "ref": f"{REF[a['kind']]}-{a['id']}",
                    "sku": a["sku"], "sku_name": item["name"], "qty": a["qty"],
                    "from_name": net.sites[a["from_site"]]["name"] if a["from_site"] else a["supplier"],
                    "to_name": net.sites[a["to_site"]]["name"], "eta": a["eta"], "temp": temp,
                    "late_days": round(a["late_hours"] / 24, 1),
                    "excluded": a["late_hours"] > net.policy["late_cover_hours"],
                    "purpose": a["detail"].get("purpose"),
                    "cross_border": a["detail"].get("cross_border", a["kind"] == "import"),
                    "recalled": a["recalled"], "can_cancel": a["kind"] != "transfer"})
    return out


KINDS = ("season", "holiday", "promotion", "event")


def calendar_view(net):
    out = []
    for e in net.calendar:
        start, end = (e["first"] - net.today).days, (e["last"] - net.today).days
        hits = [p for p in net.positions if any(x["id"] == e["id"] for x in net.events_for(*p))]
        out.append({"id": e["id"], "name": e["name"], "kind": e["kind"], "start": e["start"], "end": e["end"],
                    "factor": e["factor"], "scope": sorted(e["scope"]), "sites": sorted(e["sites"]),
                    "status": "past" if end < 0 else "active" if start <= 0 else "upcoming", "starts_in": max(start, 0),
                    "in_horizon": end >= 0 and start < HORIZON, "positions": len(hits)})
    return out


def add_event(conn, name, kind, start, end, factor, scope, sites, by, role):
    """Add a season, holiday or promotion to the demand calendar; forecasts pick it up immediately."""
    net = Network(conn)
    G.require_role(net, "calendar", role)
    scope, sites = [s.strip() for s in scope if s.strip()], [s.strip() for s in sites if s.strip()]
    if kind not in KINDS:
        raise G.Rejected(f"Kind must be one of {', '.join(KINDS)}")
    if end < start:
        raise G.Rejected("The event must end on or after its start")
    if not 0.1 <= factor <= 10:
        raise G.Rejected("Demand factor must be between 0.1 and 10")
    categories = {i["category"] for i in net.items.values()}
    tags = {t for i in net.items.values() for t in i["tags"]}
    for sel in scope:
        kind_, _, value = sel.partition(":")
        if not ((sel == "seasonal") or (kind_ == "category" and value in categories) or
                (kind_ == "sku" and value in net.items) or (kind_ == "tag" and value in tags)):
            raise G.Rejected(f"Unknown item selector '{sel}' (use seasonal, category:<name>, sku:<id> or tag:<tag>)")
    if unknown := [s for s in sites if s not in net.sites]:
        raise G.Rejected(f"Unknown site {', '.join(unknown)}")
    cur = conn.execute("INSERT INTO calendar(name,kind,start,end,factor,scope,sites) VALUES (?,?,?,?,?,?,?)",
                       (name.strip(), kind, start.isoformat(), end.isoformat(), factor, ",".join(scope), ",".join(sites)))
    log(conn, f"Calendar: {name} ({kind}) {start} to {end}, demand ×{factor:g} for {', '.join(scope) or 'all items'}"
              + (f" at {', '.join(sites)}" if sites else "") + f", added by {by}")
    conn.commit()
    return cur.lastrowid


def delete_event(conn, event_id, by, role):
    net = Network(conn)
    G.require_role(net, "calendar", role)
    row = conn.execute("SELECT name FROM calendar WHERE id=?", (event_id,)).fetchone()
    if not row:
        raise G.NotFound("No such calendar entry")
    conn.execute("DELETE FROM calendar WHERE id=?", (event_id,))
    log(conn, f"Calendar: {row['name']} removed by {by}")
    conn.commit()


def scorecards(net):
    """Supplier scorecards with their on-time-in-full trend by month."""
    out = []
    for sup in net.suppliers:
        months = {}
        for r in net.deliveries.get(sup["name"], []):
            late = (datetime.fromisoformat(r["received_at"]) - datetime.fromisoformat(r["eta"])).total_seconds() / 86400
            ok, n = months.get(r["received_at"][:7], (0, 0))
            months[r["received_at"][:7]] = (ok + (late <= 0.5 and (r["received_qty"] or 0) >= r["qty"]), n + 1)
        out.append({**net.scorecard(sup), "minimum": net.policy["min_supplier_compliance"],
                    "trend": [{"month": k, "otif_pct": round(100 * ok / n, 1), "deliveries": n}
                              for k, (ok, n) in sorted(months.items())]})
    return out


SERIES = ("stockouts_avoided", "units_protected", "waste_saved", "write_off", "spend", "transport", "transport_saved")


def buckets(series, key):
    """(day, value) pairs, summed by week (starting Monday) when the period is longer than a month."""
    if len(series) <= 31:
        return [(s["day"], s[key]) for s in series]
    out = {}
    for s in series:
        d = date.fromisoformat(s["day"])
        week = (d - timedelta(days=d.weekday())).isoformat()
        out[week] = out.get(week, 0) + s[key]
    return sorted(out.items())


def report(net, days=90):
    """What the tower avoided, saved and spent over the last `days`, per day and in total, with supplier
    scorecards. Sample history loaded with the demo is flagged and left out of 'since_start'."""
    first = net.today - timedelta(days=days - 1)
    series = {(first + timedelta(days=i)).isoformat(): dict.fromkeys(SERIES, 0) for i in range(days)}
    for r in net.conn.execute("SELECT day, kind, COUNT(*) n, SUM(units) u, SUM(value) v FROM outcomes WHERE day>=? "
                              "GROUP BY day, kind", (first.isoformat(),)):
        row = series.get(r["day"])
        if row is None:
            continue
        if r["kind"] == "stockout_avoided":
            row["stockouts_avoided"] += r["n"]
            row["units_protected"] += r["u"]
        elif r["kind"] in row:
            row[r["kind"]] += r["v"]
    since = {r["kind"]: {"count": r["n"], "units": r["u"], "value": round(r["v"], 2)} for r in net.conn.execute(
        "SELECT kind, COUNT(*) n, SUM(units) u, SUM(value) v FROM outcomes WHERE sample=0 GROUP BY kind")}
    roles = {r["role"]: r["n"] for r in net.conn.execute(
        "SELECT role, COUNT(*) n FROM actions WHERE created_at>=? AND role!='history' GROUP BY role", (first.isoformat(),))}
    actions = sum(roles.values())
    return {"days": days, "from": first.isoformat(), "to": net.today.isoformat(), "currency": net.domain.currency,
            "network": net.domain.title, "totals": {k: round(sum(s[k] for s in series.values()), 2) for k in SERIES},
            "since_start": since, "actions": actions, "auto_actions": roles.get("system", 0),
            "auto_share_pct": round(100 * roles.get("system", 0) / actions, 1) if actions else 0.0,
            "series": [{"day": d, **{k: round(v, 2) for k, v in s.items()}} for d, s in series.items()],
            "suppliers": scorecards(net)}


def report_csv(net, which, days=90):
    """CSV export: 'daily' outcomes, 'suppliers' scorecards or the 'actions' audit trail."""
    buf, rep = io.StringIO(), report(net, days)
    w = csv.writer(buf)
    if which == "daily":
        w.writerow(["day", *SERIES])
        w.writerows([s["day"], *(s[k] for k in SERIES)] for s in rep["series"])
    elif which == "suppliers":
        cols = ("supplier", "country", "deliveries", "on_time_pct", "in_full_pct", "otif_pct", "avg_delay_days",
                "lead_sd_days", "contract_compliance", "compliance", "spend")
        w.writerow(cols)
        w.writerows([s[k] for k in cols] for s in rep["suppliers"])
    elif which == "actions":
        cols = ("id", "kind", "sku", "from_site", "to_site", "supplier", "qty", "value", "status", "approved_by", "role",
                "created_at", "eta", "received_at", "received_qty")
        w.writerow(cols)
        w.writerows(tuple(r) for r in net.conn.execute(
            f"SELECT {', '.join(cols)} FROM actions WHERE created_at>=? ORDER BY id", (rep["from"],)))
    else:
        raise G.NotFound(f"Unknown export '{which}'")
    return buf.getvalue()


def briefing_facts(net):
    al = alerts(net)
    keep = ("sku_name", "site_name", "severity", "need", "resolution", "signoff", "stockout_risk", "stockout_days")
    signals = net.signals()
    return {"network": net.domain.title, "kpis": kpis(net, al), "currency": net.domain.currency,
            "top_alerts": [{k: a[k] for k in keep} for a in al[:6]],
            "signals": [{"kind": s["kind"], "title": s["title"], "detail": s["detail"]} for s in signals][:12],
            "rebalance": [{k: r[k] for k in ("sku_name", "from_name", "to_name", "qty", "value_saved")}
                          for r in rebalance(net)[:3]],
            "held": [{k: q[k] for k in ("sku_name", "site_name", "lot_no", "qty", "status")} for q in quarantine(net)][:6],
            "savings_since_start": report(net, 30)["since_start"]}

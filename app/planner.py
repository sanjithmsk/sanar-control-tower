"""Planning engine: sense deficits, plan their resolution, execute approved actions, and run the
lot-level operations around them (receipts, recalls, quarantine, expiry rebalancing).

Quantities and compliance are decided here, deterministically, from the semantic layer and the
guardrails. The language model explains and answers questions; it never moves stock.
"""
import json
import math
from datetime import datetime, timedelta

from . import guardrails as G
from .db import log
from .semantic import Network, refresh_forecast

REF = {"transfer": "TR", "po": "PO", "import": "IMP"}


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


def sense(conn):
    """Housekeeping before planning: lots past expiry are quarantined and their write-off logged."""
    net = Network(conn)
    expired = [lot for lots in net.lots.values() for lot in lots
               if lot["status"] == "available" and lot["expiry"] and net.days_left(lot["expiry"]) <= 0]
    for lot in expired:
        conn.execute("UPDATE lots SET status='expired', note=? WHERE id=?", (f"Expired {lot['expiry']}", lot["id"]))
        item = net.items[lot["sku"]]
        log(conn, f"Expired: {lot['lot_no']} {item['name']} at {net.sites[lot['site']]['name']}, {lot['qty']:,} units "
                  f"quarantined (write-off {net.domain.currency} {lot['qty'] * item['price']:,.0f})")
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
    cov, impact = (m["fefo_stock"] + m["in_transit"]) / max(m["forecast"], 1), item["impact"]
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
                transfers.append({"from": src, "from_name": net.sites[src]["name"], "qty": qty, "hours": hours,
                                  "value": round(qty * item["price"], 2),
                                  "cross_border": net.sites[src]["country"] != dest["country"],
                                  "lines": lines, "checks": checks})
                result = f"Selected: {qty:,} units"
            else:
                result = "Blocked: would expire before it can be used here"
        elif spare:
            result = "Surplus not needed" if not remaining else "Blocked: no storage room at destination"
        first = next((lot for lot in net.lots.get((sku, src), []) if G.plannable(net, lot)), None)
        audit.append({"site": src, "site_name": net.sites[src]["name"], "surplus": spare, "hours": hours,
                      "shelf_days": net.days_left(first["expiry"]) if first and first["expiry"] else None,
                      "ok": not failed, "blocked_by": failed[0]["code"] if failed else None, "result": result})

    rate = m["forecast"] / 14
    coverage = round((m["on_hand"] + m["in_transit"] + sum(t["qty"] for t in transfers)) / rate, 1) if rate else None
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
                                   "customs_days", "duty_pct", "freight_pct", "unit_cost", "premium_pct", "compliance")}
        po.update(kind="import" if best["import"] else "po", qty=qty, value=round(qty * best["unit_cost"], 2),
                  cover_days=round(qty / rate, 1) if rate else None,
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
        "timeline": {"stockout_days": m["stockout_days"], "coverage_days": coverage,
                     "first_arrival_days": round(first_arrival, 1) if first_arrival is not None else None,
                     "po_lead_days": po["lead_days"] if po else None,
                     "gap_days": round(po["lead_days"] - coverage, 1) if risk and po and coverage is not None else None},
        "stockout_risk": risk, "checks": rules,
        "alternatives": _alternatives(net, sku, site) if risk or unmet else [],
        "signoff": [r["reason"] for r in reasons], "signoff_detail": reasons,
        "signoff_roles": G.allowed_roles(net, reasons) if reasons else [],
        "value": round(sum(a["value"] for a in actions), 2), "min_shelf_days": net.policy["min_shelf_days"],
        "currency": net.domain.currency,
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
            cov = (m["fefo_stock"] + m["in_transit"]) / max(m["forecast"], 1)
            out.append({
                "sku": sku, "site": site, "sku_name": item["name"], "site_name": p["site_name"], "need": p["need"],
                "severity": p["severity"], "impact": item["impact"], "badges": p["badges"],
                "signoff": p["signoff"], "signoff_roles": p["signoff_roles"], "stockout_days": m["stockout_days"],
                "stockout_risk": p["stockout_risk"], "unmet": p["unmet"], "resolution": resolution(p),
                "value": p["value"],
                "_score": item["impact"] * (1 - min(cov, 1)) + item["impact"] * p["stockout_risk"] + 0.5 * bool(p["unmet"]),
            })
    out.sort(key=lambda a: -a["_score"])
    return out


# ---- execution ------------------------------------------------------------------------------------
def _take(conn, lines):
    for x in lines:
        if conn.execute("UPDATE lots SET qty=qty-? WHERE id=? AND qty>=?", (x["qty"], x["lot_id"], x["qty"])).rowcount != 1:
            raise G.Rejected("Stock changed while planning; refresh and try again")


def _ship(conn, net, sku, src, dst, qty, value, lines, approver, role, hours, purpose=None):
    eta = (datetime.now() + timedelta(hours=hours)).isoformat(timespec="minutes")
    cross = net.sites[src]["country"] != net.sites[dst]["country"]
    cur = conn.execute(
        "INSERT INTO actions(kind,sku,from_site,to_site,qty,value,status,approved_by,role,created_at,eta,detail)"
        " VALUES ('transfer',?,?,?,?,?,'in_transit',?,?,?,?,?)",
        (sku, src, dst, qty, value, approver, role, _now(), eta,
         json.dumps({"lines": lines, "cross_border": cross, "purpose": purpose})))
    conn.execute("UPDATE positions SET in_transit=in_transit+? WHERE sku=? AND site=?", (qty, sku, dst))
    return cur.lastrowid, cross


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
            aid, cross = _ship(conn, net, sku, t["from"], site, t["qty"], t["value"], t["lines"], approver, role,
                               t["hours"])
            ids.append(aid)
            log(conn, f"TR-{aid} {approver}: {t['qty']:,} × {item['name']}, {t['from_name']} → {p['site_name']}, "
                      f"ETA {duration(t['hours'])}{_handling(net, item)}" + (", cross-border" if cross else ""))
        if po := p["po"]:
            eta = (datetime.now() + timedelta(days=po["lead_days"])).isoformat(timespec="minutes")
            cur = conn.execute(
                "INSERT INTO actions(kind,sku,to_site,supplier,qty,value,status,approved_by,role,created_at,eta,detail)"
                " VALUES (?,?,?,?,?,?,'in_transit',?,?,?,?,?)",
                (po["kind"], sku, site, po["supplier"], po["qty"], po["value"], approver, role, _now(), eta,
                 json.dumps({"lines": [], "incoterm": po["incoterm"], "country": po["country"]})))
            ids.append(cur.lastrowid)
            conn.execute("UPDATE positions SET in_transit=in_transit+? WHERE sku=? AND site=?", (po["qty"], sku, site))
            tag = "IMP" if po["kind"] == "import" else "PO"
            trade = (f", {po['incoterm']} from {po['country_name']}, customs {po['customs_days']} d, duty "
                     f"{po['duty_pct']:g}%" if po["kind"] == "import" else "")
            log(conn, f"{tag}-{cur.lastrowid} {approver}: {po['qty']:,} × {item['name']} from {po['supplier']} for "
                      f"{p['site_name']}, lead {po['lead_days']} d{trade}, {net.domain.currency} {po['value']:,.0f}")
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
def receive(conn, action_id, min_temp=None, max_temp=None, minutes=0, by="receiving"):
    """Goods received: in-transit stock becomes lots at the destination. Recalled lots and goods with a
    temperature excursion are booked straight into quarantine."""
    net = Network(conn)
    a = conn.execute("SELECT * FROM actions WHERE id=? AND status='in_transit'", (action_id,)).fetchone()
    if not a:
        return None
    item = net.items[a["sku"]]
    detail = json.loads(a["detail"] or "{}")
    lines = detail.get("lines") or [{"lot_no": f"{REF[a['kind']]}{a['id']}", "qty": a["qty"],
                                     "expiry": (net.today + timedelta(days=item["shelf_days"])).isoformat()
                                     if item["shelf_days"] else None}]
    excursion = G.receipt_check(net, item, min_temp, max_temp, minutes)
    arrived, held = 0, 0
    for x in lines:
        status, note = "available", None
        if x.get("recalled") or x["lot_no"] in net.recalled:
            status, note = "recalled", "Recalled while in transit"
        elif excursion:
            status, note = "quarantined", excursion
        conn.execute("INSERT INTO lots(sku,site,lot_no,qty,expiry,status,note) VALUES (?,?,?,?,?,?,?)",
                     (a["sku"], a["to_site"], x["lot_no"], x["qty"], x["expiry"], status, note))
        arrived += 0 if x.get("recalled") else x["qty"]
        held += x["qty"] if status != "available" else 0
    conn.execute("UPDATE positions SET in_transit=MAX(in_transit-?,0) WHERE sku=? AND site=?",
                 (arrived, a["sku"], a["to_site"]))
    conn.execute("UPDATE actions SET status='received' WHERE id=?", (action_id,))
    log(conn, f"{REF[a['kind']]}-{a['id']} received at {net.sites[a['to_site']]['name']} by {by}: {a['qty']:,} units"
              + (f"; {held:,} quarantined ({excursion or 'recalled lot'})" if held else ""))
    conn.commit()
    return {"ok": True, "quarantined": held, "note": excursion}


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
    """Destroy or return held stock to the supplier and record the write-off."""
    net = Network(conn)
    G.require_role(net, "dispose", role)
    lot = _lot(conn, lot_id)
    if lot["status"] not in ("quarantined", "recalled", "expired"):
        raise G.Rejected("Only quarantined, recalled or expired lots can be disposed of")
    conn.execute("UPDATE lots SET status='disposed', note=? WHERE id=?", (f"{method} by {by}", lot_id))
    value = lot["qty"] * net.items[lot["sku"]]["price"]
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
    net = Network(conn)
    f = refresh_forecast(conn, sku, site, net.policy["forecast_recent_weight"])
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
        aid, _ = _ship(conn, net, prop["sku"], prop["from"], dst, prop["qty"], prop["value_saved"], lines, approver,
                       role, prop["hours"], "rebalance")
        log(conn, f"TR-{aid} {approver}: rebalance {prop['qty']:,} × {prop['sku_name']} ({lot['lot_no']}) "
                  f"{prop['from_name']} → {prop['to_name']} before expiry, saves {net.domain.currency} "
                  f"{prop['value_saved']:,.0f}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return aid


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
            "signoff": [r["reason"] for r in reasons]}


def kpis(net, al):
    waste = sum(net.metrics(sku, site)["waste_value"] for sku, site in net.positions)
    held = sum(lot["qty"] for lots in net.lots.values() for lot in lots if lot["status"] != "available")
    executed = net.conn.execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]
    return {"open_deficits": len(al), "stockout_risks": sum(1 for a in al if a["stockout_risk"]),
            "need_signoff": sum(1 for a in al if a["signoff"]), "waste_value": round(waste), "held_units": held,
            "actions_executed": executed, "auto_spend": round(net.auto_spend), "auto_budget": net.policy["auto_daily_budget"]}


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
    for a in net.conn.execute("SELECT * FROM actions WHERE status='in_transit' ORDER BY id").fetchall():
        item, detail = net.items[a["sku"]], json.loads(a["detail"] or "{}")
        temp = next((r.temp for t in sorted(item["tags"]) if (r := net.domain.requirements.get(t)) and r.temp), None)
        out.append({"id": a["id"], "kind": a["kind"], "ref": f"{REF[a['kind']]}-{a['id']}",
                    "sku": a["sku"], "sku_name": item["name"], "qty": a["qty"],
                    "from_name": net.sites[a["from_site"]]["name"] if a["from_site"] else a["supplier"],
                    "to_name": net.sites[a["to_site"]]["name"], "eta": a["eta"], "temp": temp,
                    "purpose": detail.get("purpose"), "cross_border": detail.get("cross_border", a["kind"] == "import"),
                    "recalled": sum(x["qty"] for x in detail.get("lines", []) if x.get("recalled"))})
    return out


def briefing_facts(net):
    al = alerts(net)
    keep = ("sku_name", "site_name", "severity", "need", "resolution", "signoff", "stockout_risk", "stockout_days")
    return {"network": net.domain.title, "kpis": kpis(net, al), "currency": net.domain.currency,
            "top_alerts": [{k: a[k] for k in keep} for a in al[:6]],
            "signals": [{"title": s["title"], "detail": s["detail"]} for s in net.signals()][:10],
            "rebalance": [{k: r[k] for k in ("sku_name", "from_name", "to_name", "qty", "value_saved")}
                          for r in rebalance(net)[:3]],
            "held": [{k: q[k] for k in ("sku_name", "site_name", "lot_no", "qty", "status")} for q in quarantine(net)][:6]}

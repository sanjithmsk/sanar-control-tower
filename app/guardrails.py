"""Guardrails: every rule a plan or action must pass, and the policy thresholds behind them.

Checks return structured results (code, ok, detail, ref) so the console, the API and the agent can
show exactly which rule allowed or blocked an action. Handling rules come from the active domain
pack: a tag on an item (cold, hazmat, frozen, halal...) names the capability that sites, lanes and
suppliers must carry, so a new industry rule is data rather than code.
"""
from datetime import timedelta


class Blocked(Exception):
    """The acting role may not do this."""


class NotFound(Exception):
    pass


class Rejected(Exception):
    """The request breaks a rule or a policy bound."""


# key: (default, min, max, unit, description, governing document; "@x" = the domain's refs["x"])
POLICY = {
    "min_shelf_days": (30, 0, 365, "days", "Lots at or below this remaining shelf life are not planned or moved",
                       "SOP-INV-02"),
    "near_expiry_days": (60, 7, 365, "days", "Expiry watch window", "SOP-INV-05"),
    "signoff_value": (10000, 0, 1e8, "currency", "Any single action above this value needs senior sign-off",
                      "@approvals"),
    "auto_daily_budget": (25000, 0, 1e9, "currency", "Most the platform may auto-execute per day", "POL-PR-02"),
    "min_supplier_compliance": (90, 0, 100, "%", "Suppliers below this compliance score are not used", "POL-PR-02"),
    "price_premium_pct": (15, 0, 500, "%", "Landed cost this far above list price needs sign-off", "POL-PR-02"),
    "max_cover_days": (60, 7, 730, "days", "Orders covering more days of demand need sign-off", "POL-PR-02"),
    "lead_time_buffer_days": (3, 0, 60, "days", "A cheaper, slower supplier is used only with this much cover to spare",
                              "POL-PR-02"),
    "temp_max_transit_hours": (6, 1, 240, "h", "Longest transit allowed for temperature-controlled transfers",
                               "@temperature"),
    "surge_factor": (1.6, 1, 5, "×", "Demand multiplier for seasonal items during a surge", ""),
    "overstock_days": (60, 7, 730, "days", "Days on hand above which a site is flagged overstocked", "SOP-INV-05"),
    "spike_factor": (3, 1.5, 50, "×", "A day's demand above this multiple of the 28-day average is a spike", ""),
    "forecast_recent_weight": (0.5, 0, 1, "", "Weight of the last 7 days in the demand-sensing forecast", ""),
}


def defaults(domain):
    return {k: domain.policy.get(k, v[0]) for k, v in POLICY.items()}


def policy_rows(net):
    d = net.domain
    return [{"key": k, "value": net.policy[k], "default": d.policy.get(k, default), "min": lo, "max": hi,
             "unit": d.currency if unit == "currency" else unit, "label": label,
             "ref": d.refs.get(ref[1:], "") if ref.startswith("@") else ref}
            for k, (default, lo, hi, unit, label, ref) in POLICY.items()]


def validate_policy(key, value):
    if key not in POLICY:
        raise NotFound(f"Unknown policy '{key}'")
    lo, hi = POLICY[key][1:3]
    if not lo <= value <= hi:
        raise Rejected(f"{key} must be between {lo:g} and {hi:g}")
    return value


def check(code, ok, detail, ref=""):
    return {"code": code, "ok": bool(ok), "detail": detail, "ref": ref}


def plannable(net, lot):
    """Usable for planning and transfers: available, not recalled, enough shelf life left (FEFO rule)."""
    return (lot["status"] == "available" and lot["lot_no"] not in net.recalled
            and (lot["expiry"] is None or net.days_left(lot["expiry"]) > net.policy["min_shelf_days"]))


def customs_days(net, origin, dest):
    """Clearance days for goods entering `dest` from `origin`; 0 when domestic."""
    return 0 if origin == dest else net.customs_days(dest)


def transit_hours(net, src, dst):
    a, b = net.sites[src]["country"], net.sites[dst]["country"]
    return net.lane(src, dst)["hours"] + 24 * customs_days(net, a, b)


def border_checks(net, item, origin, dest, arrival):
    """Import / export rules for goods crossing from `origin` into `dest`."""
    name = net.country_name
    out = [check("CUSTOMS", True, f"Customs clearance into {name(dest)}: {net.customs_days(dest)} d", "POL-TR-01")]
    hits = [f"{name(c)}: {net.restricted(c)}" for c in (origin, dest) if net.restricted(c)]
    out.append(check("TRADE_RESTRICTION", not hits, "; ".join(hits) or f"No restriction on {name(origin)} → {name(dest)}",
                     "POL-TR-01"))
    for tag in sorted(item["tags"]):
        req = net.domain.requirements.get(tag)
        if req and req.permit:
            p = net.permit(item["category"], dest, arrival)
            out.append(check("PERMIT", p, f"{req.label}: " + (f"{p['ref']} valid to {p['valid_until']}" if p else
                             f"no valid permit for {item['category']} into {name(dest)}"), req.ref))
    return out


def capacity_checks(net, site, qty=0):
    cap = net.sites[site]["capacity"]
    if not cap:
        return []
    load = net.load.get(site, 0) + qty
    return [check("CAPACITY", load <= cap, f"{net.sites[site]['name']} storage {load:,} of {cap:,} units", "SOP-INV-02")]


def transfer_checks(net, sku, src, dst, qty=0):
    """Every rule an inter-site transfer must pass, before quantities are considered."""
    item, a, b, lane = net.items[sku], net.sites[src], net.sites[dst], net.lane(src, dst)
    out = [check("LANE", lane, f"Lane {a['name']} – {b['name']}" +
                 (f" ({lane['mode']}, {lane['hours']} h)" if lane else " does not exist"))]
    if not lane:
        return out
    closed = net.lane_closed(src, dst)
    out.append(check("LANE_OPEN", not closed, closed or "Lane open"))
    down = [s["name"] for s in (a, b) if not net.site_up(s["id"])]
    out.append(check("SITE_UP", not down, " and ".join(down) + " not operational" if down else "Both sites operational"))
    caps, hours = net.lane_caps(src, dst), transit_hours(net, src, dst)
    for tag in sorted(item["tags"]):
        req = net.domain.requirements.get(tag)
        if not req:
            continue
        need = req.cap or tag
        if req.lane:
            out.append(check("LANE_" + tag.upper(), need in caps,
                             f"{req.label}: lane {'equipped' if need in caps else 'not equipped'}", req.ref))
        if req.site:
            missing = [s["name"] for s in (a, b) if need not in s["caps"]]
            out.append(check("SITE_" + tag.upper(), not missing, f"{req.label}: " +
                             (" and ".join(missing) + " not equipped" if missing else "both sites equipped"), req.ref))
        if req.temp:
            limit = net.policy["temp_max_transit_hours"]
            out.append(check("TEMP_TRANSIT", hours <= limit,
                             f"Transit {hours:g} h {'within' if hours <= limit else 'exceeds'} the {limit:g} h limit",
                             req.ref))
    if a["country"] != b["country"]:
        out += border_checks(net, item, a["country"], b["country"], net.today + timedelta(hours=hours))
    return out + capacity_checks(net, dst, qty)


def sourcing_option(net, sku, sup, dst, qty=0):
    """A supplier as a source for `dst`: landed cost, total lead time and every rule it must pass.
    `qty` is everything planned to arrive at `dst`, for the storage capacity check."""
    item, site = net.items[sku], net.sites[dst]
    imported = sup["country"] != site["country"]
    customs = customs_days(net, sup["country"], site["country"])
    lead = sup["lead_days"] + customs
    duty = item["duty_pct"] if imported else 0
    unit = item["price"] * sup["price_mult"] * (1 + sup["freight_pct"] / 100) * (1 + duty / 100)
    least = net.policy["min_supplier_compliance"]
    out = [check("SUPPLIER_AVAILABLE", sup["available"] and not net.supplier_out(sup),
                 "Available" if sup["available"] and not net.supplier_out(sup) else "Reporting a shortage"),
           check("COMPLIANCE", sup["compliance"] >= least, f"Compliance {sup['compliance']}% (minimum {least:g}%)",
                 "POL-PR-02")]
    for tag in sorted(item["tags"]):
        req = net.domain.requirements.get(tag)
        need = req and (req.cap or tag)
        if req and req.supplier:
            out.append(check("CERT_" + tag.upper(), need in sup["caps"],
                             f"{req.label}: {'certified' if need in sup['caps'] else 'not certified'}", req.ref))
        if req and req.site and need not in site["caps"]:
            out.append(check("SITE_" + tag.upper(), False, f"{req.label}: {site['name']} not equipped", req.ref))
    if imported:
        out += border_checks(net, item, sup["country"], site["country"], net.today + timedelta(days=lead))
    out += capacity_checks(net, dst, qty) if qty else []
    return {"supplier": sup["name"], "supplier_id": sup["id"], "country": sup["country"],
            "country_name": net.country_name(sup["country"]), "import": imported, "incoterm": sup["incoterm"],
            "lead_days": lead, "customs_days": customs, "duty_pct": duty, "freight_pct": sup["freight_pct"],
            "unit_cost": round(unit, 2), "premium_pct": round((unit / item["price"] - 1) * 100, 1),
            "compliance": sup["compliance"], "checks": out, "ok": all(c["ok"] for c in out)}


def _reason(code, reason, roles, ref):
    return {"code": code, "reason": reason, "roles": list(roles), "ref": ref}


def signoff(net, item, actions):
    """Why a set of actions needs a human, each reason with the roles allowed to approve it.

    `actions` are dicts with value and optionally premium_pct, cover_days, import, cross_border.
    """
    d, cur, p = net.domain, net.domain.currency, net.policy
    out = [_reason(tag, req.label.lower(), req.signoff, req.ref)
           for tag in sorted(item["tags"]) if (req := d.requirements.get(tag)) and req.signoff]
    if any(a["value"] > p["signoff_value"] for a in actions):
        out.append(_reason("value", f"value above {cur} {p['signoff_value']:,.0f}", d.approvals["value"],
                           d.refs.get("approvals", "")))
    premium = max((a.get("premium_pct") or 0 for a in actions), default=0)
    if premium > p["price_premium_pct"]:
        out.append(_reason("price_premium", f"price {premium:g}% above list", d.approvals["price_premium"], "POL-PR-02"))
    cover = max((a.get("cover_days") or 0 for a in actions), default=0)
    if cover > p["max_cover_days"]:
        out.append(_reason("max_cover", f"order covers {cover:g} days of demand", d.approvals["max_cover"], "POL-PR-02"))
    if "import" in d.approvals and any(a.get("import") for a in actions):
        out.append(_reason("import", "import (trade compliance review)", d.approvals["import"], "POL-TR-01"))
    if "cross_border" in d.approvals and any(a.get("cross_border") for a in actions):
        out.append(_reason("cross_border", "cross-border transfer", d.approvals["cross_border"], "POL-TR-01"))
    if actions and net.auto_spend + sum(a["value"] for a in actions) > p["auto_daily_budget"]:
        out.append(_reason("budget", f"auto-execution budget of {cur} {p['auto_daily_budget']:,.0f} reached",
                           d.approvals["budget"], "POL-PR-02"))
    return out


def allowed_roles(net, reasons):
    """Roles that may approve every reason at once, in the domain's order."""
    if not reasons:
        return list(net.domain.roles)
    allowed = set.intersection(*(set(r["roles"]) for r in reasons))
    return [r for r in net.domain.roles if r in allowed]


def authorize(net, reasons, role):
    """Raise Blocked unless `role` may approve every reason. 'system' is the auto-executor."""
    roles = net.domain.roles
    if role != "system" and role not in roles:
        raise Blocked(f"Unknown role '{role}'")
    if not reasons:
        return
    why = ", ".join(r["reason"] for r in reasons)
    if role == "system":
        raise Blocked(f"Needs human sign-off: {why}")
    allowed = allowed_roles(net, reasons)
    if role not in allowed:
        need = " or ".join(roles[r] for r in allowed) or "separate approvals"
        raise Blocked(f"{roles[role]} cannot approve this ({why}). Needs: {need}.")


def require_role(net, action, role):
    """Lot and policy operations (release, dispose, recall, policy) are limited to named roles."""
    allowed = net.domain.approvals.get(action, ())
    if role not in allowed:
        who = net.domain.roles.get(role, role or "unknown role")
        raise Blocked(f"{who} cannot {action}; needs " + " or ".join(net.domain.roles[r] for r in allowed))


def receipt_check(net, item, min_temp=None, max_temp=None, minutes=0):
    """Why received goods must be quarantined, or None. Temperature rules come from the item's tags."""
    for tag in sorted(item["tags"]):
        req = net.domain.requirements.get(tag)
        if not req or not req.temp:
            continue
        lo, hi = req.temp
        if max_temp is not None and max_temp > hi and minutes > req.tolerance_min:
            return f"Excursion to {max_temp:g} °C for {minutes} min (limit {hi:g} °C, {req.tolerance_min} min) [{req.ref}]"
        if req.freeze_below is not None and min_temp is not None:
            if min_temp < req.freeze_below:
                return f"Exposed to {min_temp:g} °C: freezing damage [{req.ref}]"
            if min_temp < lo and minutes > req.tolerance_min:
                return f"Below {lo:g} °C for {minutes} min [{req.ref}]"
    return None

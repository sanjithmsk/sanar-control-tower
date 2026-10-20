"""Semantic layer: one consistent read of the network and the business metrics derived from it.

Network loads every table it needs in a fixed handful of queries and computes metrics in memory,
so a dashboard refresh costs the same whether the network has ten items or ten thousand alerts.
Every number the planner, the console or the agent sees comes from here.

Demand is a daily curve: the baseline rate, lifted by calendar events (seasons, holidays,
promotions) and disruption scenarios. Forecasts, stockout timing, expiry waste and sell-through
all read the same curve, so they always agree.
"""
import json
import math
from bisect import bisect_left
from datetime import date, datetime, timedelta
from statistics import NormalDist, mean, pstdev

from . import domains
from . import guardrails as G

HORIZON = 14  # forecast horizon in days
REF = {"transfer": "TR", "po": "PO", "import": "IMP"}


def _tags(s):
    return {t.strip() for t in (s or "").split(",") if t.strip()}


def matches(event, item, site):
    """Does a calendar event apply to this item at this site? Scope selectors: seasonal,
    category:<name>, sku:<id>, tag:<tag>; an empty scope means every item."""
    if event["sites"] and site not in event["sites"]:
        return False
    for sel in event["scope"] or {"*"}:
        kind, _, value = sel.partition(":")
        if (kind == "*" or (kind == "seasonal" and item["seasonal"]) or (kind == "category" and item["category"] == value)
                or (kind == "sku" and item["id"] == value) or (kind == "tag" and value in item["tags"])):
            return True
    return False


class Network:
    def __init__(self, conn):
        self.conn, self.today, self.now = conn, date.today(), datetime.now()

        def rows(sql, *args):
            return conn.execute(sql, args).fetchall()

        meta = {r["key"]: r["value"] for r in rows("SELECT key, value FROM meta")}
        self.domain = domains.get(meta.get("domain"))
        self.policy = {**G.defaults(self.domain), **{r["key"]: r["value"] for r in rows("SELECT key, value FROM policy")}}
        self.scenarios = [{**json.loads(r["params"] or "{}"), "key": r["key"], "label": r["label"]}
                          for r in rows("SELECT * FROM scenarios WHERE active=1")]
        self.countries = {r["code"]: dict(r) for r in rows("SELECT * FROM countries")}
        self.sites = {r["id"]: {**dict(r), "caps": _tags(r["caps"])} for r in rows("SELECT * FROM sites ORDER BY rowid")}
        self.items = {r["id"]: {**dict(r), "tags": _tags(r["tags"])} for r in rows("SELECT * FROM items ORDER BY rowid")}
        self.positions = {(r["sku"], r["site"]): dict(r) for r in rows("SELECT * FROM positions")}
        self.lots, self.load = {}, {}
        for r in rows("SELECT * FROM lots WHERE qty>0 AND status!='disposed' ORDER BY expiry IS NULL, expiry, id"):
            self.lots.setdefault((r["sku"], r["site"]), []).append(dict(r))
            self.load[r["site"]] = self.load.get(r["site"], 0) + r["qty"]
        for (_, site), p in self.positions.items():
            self.load[site] = self.load.get(site, 0) + p["in_transit"]
        self.lanes = {}
        for r in rows("SELECT * FROM lanes"):
            self.lanes[(r["a"], r["b"])] = self.lanes[(r["b"], r["a"])] = {**dict(r), "caps": _tags(r["caps"])}
        self.suppliers = [{**dict(r), "caps": _tags(r["caps"]), "categories": _tags(r["categories"])}
                          for r in rows("SELECT * FROM suppliers ORDER BY id")]
        self.permits = [dict(r) for r in rows("SELECT * FROM permits")]
        self.substitutes = {}
        for r in rows("SELECT * FROM substitutes"):
            self.substitutes.setdefault(r["sku"], []).append(dict(r))
        self.calendar = [{**dict(r), "scope": _tags(r["scope"]), "sites": _tags(r["sites"]),
                          "first": date.fromisoformat(r["start"]), "last": date.fromisoformat(r["end"])}
                         for r in rows("SELECT * FROM calendar ORDER BY start, id")]
        self.holds = {(r["sku"], r["site"]): dict(r) for r in rows("SELECT * FROM holds")}
        self.recalled = {r["lot_no"] for r in rows("SELECT lot_no FROM recalls")}
        d = self.today
        # demand windows: 28 days plus today, 7 days plus today, today alone, and the 28 full days before today
        self.demand = {(r["sku"], r["site"]): dict(r) for r in rows(
            "SELECT sku, site, SUM(qty) q28, SUM(CASE WHEN day>=? THEN qty ELSE 0 END) q7,"
            " SUM(CASE WHEN day=? THEN qty ELSE 0 END) q0, SUM(CASE WHEN day<? THEN qty ELSE 0 END) base"
            " FROM demand WHERE day>=? GROUP BY sku, site",
            (d - timedelta(days=7)).isoformat(), d.isoformat(), d.isoformat(), (d - timedelta(days=28)).isoformat())}
        self.daily = {}  # (sku, site) -> demand on each of the 28 days before today, yesterday first
        for r in rows("SELECT sku, site, day, SUM(qty) q FROM demand WHERE day>=? AND day<? GROUP BY sku, site, day",
                      (d - timedelta(days=28)).isoformat(), d.isoformat()):
            self.daily.setdefault((r["sku"], r["site"]), [0] * 28)[(d - date.fromisoformat(r["day"])).days - 1] = r["q"]
        self.open = []
        for r in rows("SELECT * FROM actions WHERE status='in_transit' ORDER BY id"):
            a = {**dict(r), "detail": json.loads(r["detail"] or "{}")}
            a["late_hours"] = max(0.0, (self.now - datetime.fromisoformat(a["eta"])).total_seconds() / 3600)
            a["recalled"] = sum(x["qty"] for x in a["detail"].get("lines", []) if x.get("recalled"))
            self.open.append(a)
        self.late = {}  # (sku, site) -> units so far past ETA that they no longer count as cover
        for a in self.open:
            if a["late_hours"] > self.policy["late_cover_hours"]:
                key = (a["sku"], a["to_site"])
                self.late[key] = self.late.get(key, 0) + a["qty"] - a["recalled"]
        self.deliveries = {}
        for r in rows("SELECT * FROM actions WHERE kind IN ('po','import') AND status='received' ORDER BY received_at"):
            self.deliveries.setdefault(r["supplier"], []).append(dict(r))
        self.trucks = {}  # (from, to) -> today's trucks, for load sharing
        for r in rows("SELECT * FROM loads WHERE day=?", d.isoformat()):
            self.trucks.setdefault((r["a"], r["b"]), []).append(dict(r))
        self.auto_spend = rows("SELECT COALESCE(SUM(value), 0) v FROM actions WHERE role='system' AND created_at>=?",
                               d.isoformat())[0]["v"]
        self._metrics, self._cum, self._events, self._cards, self._repl = {}, {}, {}, {}, {}

    # ---- scenario-aware views of the network -------------------------------------------------
    def active(self, kind):
        return [s for s in self.scenarios if s.get("type") == kind]

    def lane(self, a, b):
        return self.lanes.get((a, b))

    def lane_closed(self, a, b):
        return next((s["label"] for s in self.active("lane_closed") if {a, b} == set(s["lane"])), "")

    def lane_caps(self, a, b):
        caps = set(self.lanes[(a, b)]["caps"])
        for s in self.active("cap_outage"):
            if s["site"] in (a, b):
                caps.discard(s["cap"])
        return caps

    def site_up(self, site):
        return bool(self.sites[site]["operational"]) and not any(s["site"] == site for s in self.active("site_down"))

    def customs_days(self, country):
        base = self.countries.get(country, {}).get("customs_days", 0)
        return base + sum(s.get("days", 0) for s in self.active("customs_delay"))

    def restricted(self, country):
        if self.countries.get(country, {}).get("restricted"):
            return "restricted country"
        return next((s["label"] for s in self.active("origin_restricted") if s["country"] == country), "")

    def supplier_out(self, sup):
        return bool(sup["is_primary"]) and bool(self.active("primary_supplier_out"))

    def permit(self, category, country, on):
        return next((p for p in self.permits if p["category"] == category and p["country"] == country
                     and p["valid_until"] >= on.isoformat()), None)

    def country_name(self, code):
        return self.countries.get(code, {}).get("name", code)

    def days_left(self, expiry):
        return (date.fromisoformat(expiry) - self.today).days

    def room(self, site):
        """Free storage at a site in units (unlimited when capacity is not tracked)."""
        cap = self.sites[site]["capacity"]
        return max(cap - self.load.get(site, 0), 0) if cap else float("inf")

    # ---- demand curve -------------------------------------------------------------------------
    def events_for(self, sku, site):
        if (sku, site) not in self._events:
            self._events[(sku, site)] = [e for e in self.calendar if matches(e, self.items[sku], site)]
        return self._events[(sku, site)]

    def mult(self, sku, site, offset):
        """Calendar demand factor on today + offset days."""
        day, f = self.today + timedelta(days=offset), 1.0
        for e in self.events_for(sku, site):
            if e["first"] <= day <= e["last"]:
                f *= e["factor"]
        return min(f, 10.0)

    def base_rate(self, sku, site):
        """Daily demand before calendar effects; a surge scenario lifts seasonal items."""
        f = self.positions.get((sku, site), {}).get("forecast_14d", 0) / HORIZON
        return f * self.policy["surge_factor"] if self.items[sku]["seasonal"] and self.active("demand_surge") else f

    def cum(self, sku, site, days):
        """Expected demand over the next `days` (fractional) days."""
        if days <= 0:
            return 0.0
        days, c = min(days, 3650), self._cum.setdefault((sku, site), [0.0])
        whole = int(days)
        while len(c) <= whole + 1:
            c.append(c[-1] + self.base_rate(sku, site) * self.mult(sku, site, len(c) - 1))
        return c[whole] + (days - whole) * (c[whole + 1] - c[whole])

    def days_until(self, sku, site, qty):
        """Days until `qty` units are used up at forecast demand; None when there is no demand."""
        if self.base_rate(sku, site) <= 0:
            return None
        if qty <= 0:
            return 0.0
        self.cum(sku, site, 1)
        c = self._cum[(sku, site)]
        while c[-1] < qty and len(c) < 3651:
            c.append(c[-1] + self.base_rate(sku, site) * self.mult(sku, site, len(c) - 1))
        if c[-1] < qty:
            return 3650.0
        k = bisect_left(c, qty)
        return round(k - 1 + (qty - c[k - 1]) / (c[k] - c[k - 1]), 1)

    def forecast(self, sku, site):
        return int(self.cum(sku, site, HORIZON) + 0.5)

    def uplifts(self, sku, site):
        """Calendar events that change this item's demand at this site within the horizon."""
        out = []
        for e in self.events_for(sku, site):
            start, end = (e["first"] - self.today).days, (e["last"] - self.today).days
            if end >= 0 and start < HORIZON:
                out.append({"name": e["name"], "kind": e["kind"], "factor": e["factor"], "from_day": max(start, 0),
                            "days": min(end, HORIZON - 1) - max(start, 0) + 1})
        return out

    def baseline(self, sku, site):
        """14-day baseline from recent demand with calendar effects taken out (demand sensing)."""
        d, w = self.demand.get((sku, site)), self.policy["forecast_recent_weight"]
        if not d:
            return 0
        m7 = mean(self.mult(sku, site, -o) for o in range(8))
        m28 = mean(self.mult(sku, site, -o) for o in range(29))
        return int(HORIZON * (w * d["q7"] / 7 / m7 + (1 - w) * d["q28"] / 28 / m28) + 0.5)

    # ---- suppliers ----------------------------------------------------------------------------
    def scorecard(self, sup):
        """Delivery performance from receipts. Once there is enough history it moves the supplier's
        compliance score (so a supplier that keeps delivering late gets blocked) and its planned lead time."""
        if sup["name"] in self._cards:
            return self._cards[sup["name"]]
        rows, p = self.deliveries.get(sup["name"], []), self.policy
        n = len(rows)
        delays = [(datetime.fromisoformat(r["received_at"]) - datetime.fromisoformat(r["eta"])).total_seconds() / 86400
                  for r in rows]
        on_time = [x <= 0.5 for x in delays]
        in_full = [(r["received_qty"] or 0) >= r["qty"] for r in rows]
        otif = sum(a and b for a, b in zip(on_time, in_full))
        learned = n >= p["scorecard_min_deliveries"]
        late = mean(max(x, 0) for x in delays) if n else 0.0
        card = {"supplier": sup["name"], "country": sup["country"], "deliveries": n,
                "on_time_pct": round(100 * sum(on_time) / n, 1) if n else None,
                "in_full_pct": round(100 * sum(in_full) / n, 1) if n else None,
                "otif_pct": round(100 * otif / n, 1) if n else None,
                "avg_delay_days": round(late, 1), "lead_sd_days": round(pstdev(delays), 2) if n > 1 else 0.0,
                "expected_delay_days": round(late) if learned else 0, "learned": learned,
                "contract_compliance": sup["compliance"],
                "compliance": round((1 - p["scorecard_weight"]) * sup["compliance"]
                                    + p["scorecard_weight"] * 100 * otif / n, 1) if learned else sup["compliance"],
                "spend": round(sum(r["value"] for r in rows), 2)}
        self._cards[sup["name"]] = card
        return card

    def replenishment(self, sku, site):
        """(lead days, lead-time spread, supplier) of the fastest compliant source for safety stock."""
        if (sku, site) not in self._repl:
            item = self.items[sku]
            ok = [o for s in self.suppliers if item["category"] in s["categories"]
                  if (o := G.sourcing_option(self, sku, s, site))["ok"]]
            if ok:
                best = min(ok, key=lambda o: (o["lead_days"], o["unit_cost"]))
                sup = next(s for s in self.suppliers if s["id"] == best["supplier_id"])
                self._repl[(sku, site)] = (best["lead_days"], self.scorecard(sup)["lead_sd_days"], best["supplier"])
            else:
                self._repl[(sku, site)] = (int(self.policy["default_lead_days"]), 0.0, None)
        return self._repl[(sku, site)]

    # ---- metrics ------------------------------------------------------------------------------
    def safety(self, sku, site):
        """Safety buffer z × sqrt(L·σd² + d²·σL²) for the item's service level, using demand variability
        (calendar effects removed) and the supplier's lead-time record. Falls back to the fixed buffer."""
        fixed, hist = (self.positions.get((sku, site)) or {}).get("safety", 0), self.daily.get((sku, site))
        if not self.policy["dynamic_safety_stock"] or not hist or not any(hist):
            return fixed, {"mode": "fixed", "fixed": fixed}
        level = self.policy["critical_service_level_pct" if self.items[sku]["impact"] >= 8 else "service_level_pct"]
        z = NormalDist().inv_cdf(level / 100)
        base = self.positions[(sku, site)]["forecast_14d"] / HORIZON
        rate = self.cum(sku, site, HORIZON) / HORIZON
        sd = pstdev(q / self.mult(sku, site, -1 - i) for i, q in enumerate(hist)) * (rate / base if base else 1)
        lead, lead_sd, source = self.replenishment(sku, site)
        lead += self.policy["review_period_days"]
        value = max(0, math.ceil(z * math.sqrt(lead * sd ** 2 + rate ** 2 * lead_sd ** 2))) if rate else 0
        return value, {"mode": "smart", "fixed": fixed, "service_level": level, "z": round(z, 2),
                       "demand_sd": round(sd, 2), "lead_days": lead, "lead_sd": lead_sd, "source": source}

    def metrics(self, sku, site):
        if (sku, site) not in self._metrics:
            self._metrics[(sku, site)] = self._compute(sku, site)
        return self._metrics[(sku, site)]

    def _compute(self, sku, site):
        pos = self.positions.get((sku, site)) or {"in_transit": 0, "forecast_14d": 0}
        lots = self.lots.get((sku, site), [])
        avail = [lot for lot in lots if lot["status"] == "available"]
        fefo = sum(lot["qty"] for lot in avail if G.plannable(self, lot))
        on_hand = sum(lot["qty"] for lot in avail)
        forecast, (safety, how) = self.forecast(sku, site), self.safety(sku, site)
        late = self.late.get((sku, site), 0)
        cover = fefo + pos["in_transit"] - late
        dated = [self.days_left(lot["expiry"]) for lot in avail if lot["expiry"]]
        waste = sum(w["unsold"] for w in self.waste(sku, site))
        price = self.items[sku]["price"]
        return {
            "fefo_stock": fefo, "in_transit": pos["in_transit"], "late": late, "forecast": forecast,
            "base_forecast": pos["forecast_14d"], "uplifts": self.uplifts(sku, site), "safety": safety,
            "safety_detail": how,
            # Available FEFO stock + in transit (late shipments excluded) - (forecast demand + safety buffer)
            "balance": cover - forecast - safety,
            "days_on_hand": self.days_until(sku, site, cover), "stockout_days": self.days_until(sku, site, on_hand),
            "earliest_days": min(dated) if dated else None, "on_hand": on_hand, "short_dated": on_hand - fefo,
            "held": sum(lot["qty"] for lot in lots if lot["status"] != "available"),
            "value": round(on_hand * price, 2), "waste_units": waste, "waste_value": round(waste * price, 2),
            "rate": round(forecast / HORIZON, 2),
        }

    def waste(self, sku, site):
        """Lots projected to expire unsold when forecast demand is served earliest-expiry first."""
        used, out = 0.0, []
        for lot in self.lots.get((sku, site), []):
            if lot["status"] != "available" or not lot["expiry"]:
                continue
            days = self.days_left(lot["expiry"])
            sold = min(lot["qty"], max(0.0, self.cum(sku, site, days) - used))
            used += sold
            if (unsold := int(lot["qty"] - sold + 1e-6)) > 0:
                out.append({**lot, "days": days, "unsold": unsold})
        return out

    def absorb(self, sku, site, days, arriving=()):
        """Units of a lot with `days` of shelf life that `site` can sell before expiry without pushing
        any of its own lots past expiry. `arriving` holds (qty, days) lots already planned in."""
        if days <= 0 or (room := self.cum(sku, site, days)) <= 0:
            return 0
        lots = [(lot["qty"], self.days_left(lot["expiry"])) for lot in self.lots.get((sku, site), [])
                if lot["status"] == "available" and lot["expiry"]] + list(arriving)
        used = sum(q for q, d in lots if d <= days)
        room -= used
        for d, q in sorted((d, q) for q, d in lots if d > days):
            used += q
            room = min(room, self.cum(sku, site, d) - used)
        return max(0, int(room + 1e-6))

    # ---- transport ----------------------------------------------------------------------------
    def transport(self, src, dst, qty):
        """Truck cost of moving `qty` from `src` to `dst` today, riding on trucks already booked on the
        same lane when they have room (load sharing)."""
        lane = self.lane(src, dst)
        cap, trip = lane["vehicle_units"] or max(qty, 1), lane["trip_cost"] or 0
        spare = sum(max(t["capacity"] - t["used"], 0) for t in self.trucks.get((src, dst), []))
        ride = min(spare, qty)
        trips, alone = math.ceil((qty - ride) / cap), math.ceil(qty / cap)
        return {"trips": trips, "cost": trips * trip, "standalone_cost": alone * trip, "saved": (alone - trips) * trip,
                "shares_truck": ride > 0, "trip_cost": trip, "vehicle_units": cap, "fill_pct": round(100 * qty / cap, 1)}

    # ---- network signals ------------------------------------------------------------------------
    def signals(self):
        cur, out = self.domain.currency, []
        for s in self.scenarios:
            out.append({"kind": "scenario", "level": "warn", "title": s["label"], "detail": "Disruption scenario active"})
        for a in self.open:
            if a["late_hours"] > 0:
                excluded = a["late_hours"] > self.policy["late_cover_hours"]
                out.append({"kind": "late", "level": "crit" if excluded else "warn", "sku": a["sku"], "site": a["to_site"],
                            "title": f"Late: {REF[a['kind']]}-{a['id']} {self.items[a['sku']]['name']} to "
                                     f"{self.sites[a['to_site']]['name']}",
                            "detail": f"{a['late_hours'] / 24:.1f} days past ETA from "
                                      f"{a['supplier'] or self.sites[a['from_site']]['name']}"
                                      + ("; no longer counted as cover, re-planned" if excluded else "")})
        for e in self.calendar:
            start = (e["first"] - self.today).days
            if -1 < start < HORIZON or (start < 0 <= (e["last"] - self.today).days):
                out.append({"kind": "calendar", "level": "info", "title": f"{e['name']} "
                            + (f"starts in {start} d" if start > 0 else "under way"),
                            "detail": f"Demand ×{e['factor']:g} for {', '.join(sorted(e['scope'])) or 'all items'}"
                                      + (f" at {', '.join(self.sites[s]['name'] for s in sorted(e['sites']))}"
                                         if e["sites"] else "")})
        for sup in self.suppliers:
            card = self.scorecard(sup)
            least = self.policy["min_supplier_compliance"]
            if card["learned"] and card["compliance"] < least <= sup["compliance"]:
                out.append({"kind": "supplier", "level": "crit", "title": f"Supplier blocked by performance: {sup['name']}",
                            "detail": f"Compliance {card['compliance']:g}% after on time in full {card['otif_pct']:g}% "
                                      f"(contract {sup['compliance']}%, minimum {least:g}%)"})
        for sku, item in self.items.items():
            total = 0
            for site, srow in self.sites.items():
                if (sku, site) not in self.positions:
                    continue
                m = self.metrics(sku, site)
                total += m["balance"]
                d = self.demand.get((sku, site))
                if d and d["base"]:
                    avg = d["base"] / 28
                    if d["q0"] >= 3 and d["q0"] >= self.policy["spike_factor"] * avg:
                        out.append({"kind": "spike", "level": "warn", "sku": sku, "site": site,
                                    "title": f"Demand spike: {item['name']} at {srow['name']}",
                                    "detail": f"{d['q0']:,} units today vs {avg:.1f}/day average ({d['q0'] / avg:.1f}×)"})
                if m["days_on_hand"] and m["days_on_hand"] > self.policy["overstock_days"]:
                    out.append({"kind": "overstock", "level": "info", "sku": sku, "site": site,
                                "title": f"Overstock: {item['name']} at {srow['name']}",
                                "detail": f"{m['days_on_hand']:g} days on hand, {cur} {m['value']:,.0f} tied up"})
                if m["waste_units"]:
                    out.append({"kind": "waste", "level": "warn", "sku": sku, "site": site,
                                "title": f"Expiry risk: {item['name']} at {srow['name']}",
                                "detail": f"{m['waste_units']:,} units projected to expire unsold "
                                          f"({cur} {m['waste_value']:,.0f})"})
            if total < 0:
                out.append({"kind": "shortage", "level": "crit", "sku": sku, "title": f"Network shortage: {item['name']}",
                            "detail": f"Whole network short by {-total:,} units; external supply required"})
        return out


def refresh_forecast(conn, sku, site):
    """Recompute the baseline forecast from the latest demand (calendar effects removed)."""
    f = Network(conn).baseline(sku, site)
    conn.execute("UPDATE positions SET forecast_14d=? WHERE sku=? AND site=?", (f, sku, site))
    return f


def metrics(conn, sku, site):
    """Convenience for scripts and tests: metrics from a fresh snapshot."""
    return Network(conn).metrics(sku, site)

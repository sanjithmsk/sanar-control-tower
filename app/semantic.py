"""Semantic layer: one consistent read of the network and the business metrics derived from it.

Network loads every table it needs in a fixed handful of queries and computes metrics in memory,
so a dashboard refresh costs the same whether the network has ten items or ten thousand alerts.
Every number the planner, the console or the agent sees comes from here.
"""
import json
from collections import defaultdict
from datetime import date, timedelta

from . import domains
from . import guardrails as G


def _tags(s):
    return {t.strip() for t in (s or "").split(",") if t.strip()}


class Network:
    def __init__(self, conn):
        self.conn, self.today = conn, date.today()

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
        self.lots, self.load = defaultdict(list), defaultdict(int)
        for r in rows("SELECT * FROM lots WHERE qty>0 AND status!='disposed' ORDER BY expiry IS NULL, expiry, id"):
            self.lots[(r["sku"], r["site"])].append(dict(r))
            self.load[r["site"]] += r["qty"]
        for (_, site), p in self.positions.items():
            self.load[site] += p["in_transit"]
        self.lanes = {}
        for r in rows("SELECT * FROM lanes"):
            self.lanes[(r["a"], r["b"])] = self.lanes[(r["b"], r["a"])] = {**dict(r), "caps": _tags(r["caps"])}
        self.suppliers = [{**dict(r), "caps": _tags(r["caps"]), "categories": _tags(r["categories"])}
                          for r in rows("SELECT * FROM suppliers ORDER BY id")]
        self.permits = [dict(r) for r in rows("SELECT * FROM permits")]
        self.substitutes = defaultdict(list)
        for r in rows("SELECT * FROM substitutes"):
            self.substitutes[r["sku"]].append(dict(r))
        self.holds = {(r["sku"], r["site"]): dict(r) for r in rows("SELECT * FROM holds")}
        self.recalled = {r["lot_no"] for r in rows("SELECT lot_no FROM recalls")}
        d = self.today
        # demand windows: 28 days plus today, 7 days plus today, today alone, and the 28 full days before today
        self.demand = {(r["sku"], r["site"]): dict(r) for r in rows(
            "SELECT sku, site, SUM(qty) q28, SUM(CASE WHEN day>=? THEN qty ELSE 0 END) q7,"
            " SUM(CASE WHEN day=? THEN qty ELSE 0 END) q0, SUM(CASE WHEN day<? THEN qty ELSE 0 END) base"
            " FROM demand WHERE day>=? GROUP BY sku, site",
            (d - timedelta(days=7)).isoformat(), d.isoformat(), d.isoformat(), (d - timedelta(days=28)).isoformat())}
        self.auto_spend = rows("SELECT COALESCE(SUM(value), 0) v FROM actions WHERE role='system' AND created_at>=?",
                               d.isoformat())[0]["v"]
        self._metrics = {}

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

    # ---- metrics ------------------------------------------------------------------------------
    def forecast(self, sku, site):
        f = self.positions.get((sku, site), {}).get("forecast_14d", 0)
        if self.items[sku]["seasonal"] and self.active("demand_surge"):
            f = int(f * self.policy["surge_factor"] + 0.5)
        return f

    def metrics(self, sku, site):
        if (sku, site) not in self._metrics:
            self._metrics[(sku, site)] = self._compute(sku, site)
        return self._metrics[(sku, site)]

    def _compute(self, sku, site):
        pos = self.positions.get((sku, site)) or {"in_transit": 0, "safety": 0}
        lots = self.lots.get((sku, site), [])
        avail = [lot for lot in lots if lot["status"] == "available"]
        fefo = sum(lot["qty"] for lot in avail if G.plannable(self, lot))
        on_hand = sum(lot["qty"] for lot in avail)
        forecast = self.forecast(sku, site)
        rate = forecast / 14
        dated = [self.days_left(lot["expiry"]) for lot in avail if lot["expiry"]]
        waste = sum(w["unsold"] for w in self.waste(sku, site))
        price = self.items[sku]["price"]
        return {
            "fefo_stock": fefo, "in_transit": pos["in_transit"], "forecast": forecast, "safety": pos["safety"],
            # Available FEFO stock + in transit - (forecast demand + safety buffer); negative = deficit
            "balance": fefo + pos["in_transit"] - forecast - pos["safety"],
            "days_on_hand": round((fefo + pos["in_transit"]) / rate, 1) if rate else None,
            "stockout_days": round(on_hand / rate, 1) if rate else None,
            "earliest_days": min(dated) if dated else None,
            "on_hand": on_hand, "short_dated": on_hand - fefo,
            "held": sum(lot["qty"] for lot in lots if lot["status"] != "available"),
            "value": round(on_hand * price, 2), "waste_units": waste, "waste_value": round(waste * price, 2),
            "rate": round(rate, 2),
        }

    def waste(self, sku, site):
        """Lots projected to expire unsold when demand is served earliest-expiry first."""
        rate, t, out = self.forecast(sku, site) / 14, 0.0, []
        for lot in self.lots.get((sku, site), []):
            if lot["status"] != "available" or not lot["expiry"]:
                continue
            days = self.days_left(lot["expiry"])
            sold = min(lot["qty"], max(0.0, (days - t) * rate)) if rate else 0
            if rate:
                t += sold / rate
            if (unsold := int(lot["qty"] - sold)) > 0:
                out.append({**lot, "days": days, "unsold": unsold})
        return out

    def absorb(self, sku, site, days, arriving=()):
        """Units of a lot with `days` of shelf life that `site` can sell before expiry without pushing
        any of its own lots past expiry. `arriving` holds (qty, days) lots already planned in."""
        rate = self.forecast(sku, site) / 14
        if rate <= 0 or days <= 0:
            return 0
        lots = [(lot["qty"], self.days_left(lot["expiry"])) for lot in self.lots.get((sku, site), [])
                if lot["status"] == "available" and lot["expiry"]] + list(arriving)
        ahead = sum(q for q, d in lots if d <= days)
        room, cum = rate * days - ahead, ahead
        for d, q in sorted((d, q) for q, d in lots if d > days):
            cum += q
            room = min(room, rate * d - cum)
        return max(0, int(room))

    # ---- network signals ------------------------------------------------------------------------
    def signals(self):
        cur, out = self.domain.currency, []
        for s in self.scenarios:
            out.append({"kind": "scenario", "level": "warn", "title": s["label"], "detail": "Disruption scenario active"})
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


def forecast_from(q7, q28, weight):
    """Demand sensing: blend the 7-day and 28-day run rates into a 14-day forecast."""
    return int(14 * (weight * q7 / 7 + (1 - weight) * q28 / 28) + 0.5)


def refresh_forecast(conn, sku, site, weight=0.5):
    d = date.today()
    r = conn.execute("SELECT COALESCE(SUM(qty), 0) q28, COALESCE(SUM(CASE WHEN day>=? THEN qty ELSE 0 END), 0) q7"
                     " FROM demand WHERE sku=? AND site=? AND day>=?",
                     ((d - timedelta(days=7)).isoformat(), sku, site, (d - timedelta(days=28)).isoformat())).fetchone()
    f = forecast_from(r["q7"], r["q28"], weight)
    conn.execute("UPDATE positions SET forecast_14d=? WHERE sku=? AND site=?", (f, sku, site))
    return f


def metrics(conn, sku, site):
    """Convenience for scripts and tests: metrics from a fresh snapshot."""
    return Network(conn).metrics(sku, site)

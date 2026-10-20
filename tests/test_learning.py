"""Planning intelligence: smart safety stock, demand calendar, supplier scorecards, late shipments,
load sharing and the reports built from recorded outcomes."""
import math
from datetime import timedelta
from statistics import NormalDist

import pytest

from app import guardrails as G
from app import planner
from app.semantic import Network


def late_order(net):
    return next(a for a in net.open if a["late_hours"] > 0)


def test_smart_safety_stock_follows_variability_service_level_and_lead_time(c):
    net = Network(c)
    safety, how = net.safety("INS", "A")
    assert how["mode"] == "smart" and how["service_level"] == 99  # insulin is critical (impact 9)
    rate = net.cum("INS", "A", 14) / 14
    z = NormalDist().inv_cdf(0.99)
    assert safety == math.ceil(z * math.sqrt(how["lead_days"] * how["demand_sd"] ** 2 + rate ** 2 * how["lead_sd"] ** 2))
    assert net.safety("AMX", "A")[1]["service_level"] == 95
    c.execute("UPDATE policy SET value=99.9 WHERE key='critical_service_level_pct'")
    assert Network(c).safety("INS", "A")[0] > safety
    c.execute("UPDATE policy SET value=0 WHERE key='dynamic_safety_stock'")
    assert Network(c).safety("INS", "A") == (30, {"mode": "fixed", "fixed": 30})


def test_calendar_lifts_the_forecast_and_the_baseline_takes_it_out(c):
    net = Network(c)
    m = net.metrics("PCM", "A")
    assert m["forecast"] > m["base_forecast"] and [u["name"] for u in m["uplifts"]] == ["Flu season"]
    today = net.today
    planner.add_event(c, "Amoxicillin promotion", "promotion", today, today + timedelta(days=6), 2, ["sku:AMX"], ["A"],
                      "x", "pharmacist")
    assert Network(c).metrics("AMX", "A")["forecast"] == 280 + 140  # base 20 a day, doubled for 7 of 14 days
    with pytest.raises(G.Rejected):
        planner.add_event(c, "Bad", "promotion", today, today, 2, ["sku:NOPE"], [], "x", "pharmacist")
    with pytest.raises(G.Blocked):
        planner.add_event(c, "Not mine", "promotion", today, today, 2, [], [], "x", "qa")
    # a past event that doubled demand is divided out when the baseline is re-sensed
    c.execute("DELETE FROM calendar")
    planner.add_event(c, "Last month's promotion", "promotion", today - timedelta(days=40), today, 2, ["sku:AMX"],
                      ["A"], "x", "pharmacist")
    assert planner.record_demand(c, "AMX", "A", 40, today.isoformat())["forecast_14d"] == 165  # 330 / 2


def test_supplier_scorecard_learns_from_receipts(c):
    net = Network(c)
    b = next(s for s in net.suppliers if s["name"].startswith("Supplier B"))
    assert net.scorecard(b)["compliance"] == 92.0  # 94% contract, 87.5% on time in full
    order = late_order(net)
    assert order["supplier"] == b["name"]
    r = planner.receive(c, order["id"], qty=180)  # two days late and 20 short
    assert r["short"] == 20 and r["late_days"] > 1
    net = Network(c)
    card = net.scorecard(b)
    assert (card["deliveries"], card["otif_pct"], card["compliance"]) == (9, 77.8, 89.1)
    option = G.sourcing_option(net, "TRM", b, "B")
    assert [x["code"] for x in option["checks"] if not x["ok"]] == ["COMPLIANCE"]  # blocked until it improves
    assert any(s["kind"] == "supplier" for s in net.signals())


def test_late_shipments_stop_counting_as_cover_and_can_be_cancelled(c):
    net = Network(c)
    m, order = net.metrics("AMX", "C"), late_order(net)
    assert m["late"] == 200 and m["in_transit"] == 200 and m["balance"] < 0
    c.execute("UPDATE policy SET value=720 WHERE key='late_cover_hours'")
    assert Network(c).metrics("AMX", "C")["balance"] == m["balance"] + 200  # within the grace period it counts
    with pytest.raises(G.Blocked):
        planner.cancel(c, order["id"], "x", "pharmacist")
    planner.cancel(c, order["id"], "x", "logistics_manager")
    net = Network(c)
    assert net.metrics("AMX", "C")["in_transit"] == 0 and not net.open
    with pytest.raises(G.NotFound):
        planner.cancel(c, order["id"], "x", "logistics_manager")


def test_transfers_on_the_same_lane_share_a_truck(c):
    plan = planner.load_plan(Network(c), planner.alerts(Network(c)))
    hub_north = next(x for x in plan if (x["from"], x["to"]) == ("D", "B"))
    assert hub_north["trucks"] == 1 and hub_north["saved"] == 300  # TRM and ADA for North Branch, one truck
    planner.execute(c, "ADA", "B", "test", "pharmacist")  # books the first truck on Hub → North
    planner.execute(c, "TRM", "B", "test", "chief_pharmacist")  # its Hub leg rides along
    loads = c.execute("SELECT * FROM loads WHERE a='D' AND b='B'").fetchall()
    assert len(loads) == 1 and loads[0]["used"] == hub_north["units"]
    saved = c.execute("SELECT SUM(value) v FROM outcomes WHERE kind='transport_saved'").fetchone()["v"]
    assert saved == 300


def test_reports_count_what_the_tower_did(c):
    planner.auto_run(c)
    rep = planner.report(Network(c), 30)
    since = rep["since_start"]
    assert since["stockout_avoided"]["count"] == 3 and rep["auto_share_pct"] == 100
    assert rep["totals"]["spend"] > since["spend"]["value"]  # includes the sample delivery history
    assert [s["supplier"] for s in rep["suppliers"]][0] == "Supplier A (primary)"
    daily = planner.report_csv(Network(c), "daily", 30).splitlines()
    assert daily[0].startswith("day,stockouts_avoided") and len(daily) == 31
    with pytest.raises(G.NotFound):
        planner.report_csv(Network(c), "nope")


def test_report_downloads(api):
    assert api.get("/api/reports/report.pdf?days=30").content[:5] == b"%PDF-"
    r = api.get("/api/reports/suppliers.csv")
    assert r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    assert api.get("/api/reports?days=3").status_code == 422  # at least a week

from app import guardrails as G
from app import planner
from app import semantic as S
from app.semantic import Network


def pairs(alerts):
    return {(a["sku"], a["site"]) for a in alerts}


def test_initial_alerts(c):
    assert pairs(planner.alerts(Network(c))) == {("INS", "A"), ("ADA", "A"), ("TRM", "B"), ("AMX", "C"), ("GLM", "C"),
                                                 ("ADA", "B"), ("ADA", "C")}


def test_deficit_formula(c):
    m = S.metrics(c, "INS", "A")
    assert m["balance"] == m["fefo_stock"] + m["in_transit"] - m["late"] - m["forecast"] - m["safety"]
    assert (m["fefo_stock"], m["forecast"]) == (40, 120)


def test_near_expiry_batch_excluded(c):
    assert S.metrics(c, "PCM", "C")["fefo_stock"] == 520  # the 300-unit batch at 20 days is ignored


def test_internal_transfer_preferred(c):
    p = planner.plan(Network(c), "INS", "A")
    (t,) = p["transfers"]
    assert (t["from"], t["qty"], t["hours"]) == ("B", p["need"], 2) and p["po"] is None
    assert [x["lot_no"] for x in t["lines"]] == ["INS-B1"]  # earliest expiry leaves first


def test_cold_chain_guardrail_forces_po(c, on):
    on(c, "cold_fail")
    p = planner.plan(Network(c), "INS", "A")
    assert p["transfers"] == [] and p["po"]["qty"] >= p["need"] and p["po"]["qty"] % 10 == 0  # full packs
    assert any(a["result"].startswith("Blocked: Cold chain") for a in p["audit"])
    failed = [x["code"] for x in G.transfer_checks(Network(c), "INS", "B", "C") if not x["ok"]]
    assert failed == ["LANE_COLD"]  # no reefer on B-C in any scenario


def test_split_transfer_and_po_rounds_to_pack(c):
    p = planner.plan(Network(c), "ADA", "A")
    assert p["transfers"][0]["qty"] == 7 and p["po"]["qty"] == 2
    assert p["signoff"] == ["value above SAR 10,000"]


def test_controlled_needs_signoff_and_secondary_supplier(c, on):
    on(c, "supplier_short", "hub_outage")
    p = planner.plan(Network(c), "TRM", "B")
    assert "controlled substance" in p["signoff"]
    assert p["po"]["supplier"].startswith("Supplier B")


def test_execute_is_fefo_and_never_creates_donor_deficit(c):
    need = planner.plan(Network(c), "INS", "A")["need"]
    (act,) = planner.execute(c, "INS", "A", "test", "pharmacist")
    assert S.metrics(c, "INS", "A")["balance"] == 0
    assert S.metrics(c, "INS", "B")["balance"] >= 0
    assert c.execute("SELECT qty FROM lots WHERE lot_no='INS-B1'").fetchone()["qty"] == 160 - need
    assert planner.receive(c, act)["ok"]
    m = S.metrics(c, "INS", "A")
    assert m["in_transit"] == 0 and m["fefo_stock"] == 40 + need


def test_auto_run_leaves_signoff_items(c):
    assert planner.auto_run(c) == {"executed": 6, "awaiting_signoff": 2, "unresolved": 0}
    assert pairs(planner.alerts(Network(c))) == {("ADA", "A"), ("TRM", "B")}


def test_surge_creates_deficits(c, on):
    on(c, "surge")
    assert ("OSE", "A") in pairs(planner.alerts(Network(c)))


def test_stockout_risk_offers_alternatives(c, on):
    on(c, "cold_fail", "supplier_short")
    p = planner.plan(Network(c), "INS", "A")
    assert p["stockout_risk"] and p["severity"] == "Critical"
    assert p["timeline"]["gap_days"] == 0.3  # runs out 0.3 days before Supplier B delivers
    (alt,) = p["alternatives"]
    assert alt["sku"] == "INB" and not alt["sources"][0]["ok"]  # the cold-chain outage blocks it too


def test_customs_delay_switches_import_to_domestic(c, on):
    p = planner.plan(Network(c), "GLM", "C")
    assert (p["po"]["kind"], p["po"]["lead_days"]) == ("import", 9)  # 7 days air + 2 days customs
    on(c, "customs_delay")
    p = planner.plan(Network(c), "GLM", "C")
    assert p["po"]["kind"] == "po" and p["po"]["supplier"].startswith("Supplier B")


def test_route_closure_reroutes(c, on):
    on(c, "route_closure")
    p = planner.plan(Network(c), "INS", "A")
    assert [t["from"] for t in p["transfers"]] == ["C", "D"] and sum(t["qty"] for t in p["transfers"]) == p["need"]
    assert next(a for a in p["audit"] if a["site"] == "B")["blocked_by"] == "LANE_OPEN"


def test_hub_outage_removes_donor(c, on):
    on(c, "hub_outage")
    p = planner.plan(Network(c), "ADA", "A")
    assert p["transfers"] == [] and p["po"]["qty"] == 10
    assert next(a for a in p["audit"] if a["site"] == "D")["blocked_by"] == "SITE_UP"


def test_alerts_use_a_fixed_number_of_queries(c, on):
    def count():
        queries = []
        c.set_trace_callback(queries.append)
        n = len(planner.alerts(Network(c)))
        c.set_trace_callback(None)
        return n, len(queries)

    few, q1 = count()
    on(c, "surge")
    many, q2 = count()
    assert many > few and q1 == q2 <= 25  # one snapshot; no per-item, per-site or per-alert queries

"""The engine is industry-neutral: the logistics pack and a pack defined right here run unchanged."""
from app import db, domains, planner
from app.domains import Domain, Requirement
from app.semantic import Network


def failed(checks):
    return [x["code"] for x in checks if not x["ok"]]


def test_logistics_alerts(lc):
    net = Network(lc)
    assert net.domain.labels["site"] == "Facility"
    assert {(a["sku"], a["site"]) for a in planner.alerts(net)} == {
        ("PWB", "RUH"), ("TV55", "RUH"), ("TV55", "DMM"), ("CHK", "RUH"), ("OLV", "RUH"), ("TSH", "RUH"), ("TSH", "DMM")}


def test_import_with_customs_duty_and_permit(lc):
    p = planner.plan(Network(lc), "TV55", "RUH")
    po = p["po"]
    assert (po["kind"], po["supplier"], po["lead_days"], po["customs_days"]) == (
        "import", "Gulf Electronics Distribution (UAE)", 6, 2)
    assert po["unit_cost"] == 2024.19  # 1,800 list × 1.05 price × 1.02 freight × 1.05 duty
    china = next(o for o in p["sourcing"] if "China" in o["supplier"])
    assert china["ok"] and china["note"] == "Arrives later"  # cheaper, but 23 days is too slow
    assert p["signoff"] == ["import (trade compliance review)"]
    assert p["signoff_roles"] == ["ops_manager", "trade_compliance"]


def test_cross_border_transfer_rules(lc):
    net = Network(lc)
    tv = planner.check_transfer(net, "TV55", "DXB", "DMM")
    assert tv["allowed"] and "cross-border transfer" in tv["signoff"]
    chicken = planner.check_transfer(net, "CHK", "DMM", "DXB")
    assert not chicken["allowed"]
    assert {"LANE_FROZEN", "SITE_FROZEN", "TEMP_TRANSIT", "PERMIT"} <= set(failed(chicken["checks"]))


def test_origin_restriction_blocks_supplier(lc, on):
    on(lc, "origin_restriction")
    p = planner.plan(Network(lc), "TV55", "RUH")
    china = next(o for o in p["sourcing"] if "China" in o["supplier"])
    assert "TRADE_RESTRICTION" in failed(china["checks"])


def test_no_compliant_supplier_escalates(lc):
    p = planner.plan(Network(lc), "TSH", "RUH")
    moved = sum(t["qty"] for t in p["transfers"])
    assert moved and p["po"] is None and p["unmet"] == p["need"] - moved and p["stockout_risk"]
    assert failed(p["sourcing"][0]["checks"]) == ["COMPLIANCE"]  # 88% contract, 33% on time in full


def test_waste_aware_donor_choice(lc):
    p = planner.plan(Network(lc), "OLV", "RUH")
    assert [t["from"] for t in p["transfers"]] == ["JED"]  # its olive oil would expire unsold; Dammam is nearer


def test_frozen_excursion_on_receipt(lc):
    need = planner.plan(Network(lc), "CHK", "RUH")["need"]
    (act,) = planner.execute(lc, "CHK", "RUH", "test", "planner")
    assert planner.receive(lc, act, max_temp=-12, minutes=90)["quarantined"] == need


def test_dangerous_goods_never_routed_to_unlicensed_site(lc):
    r = planner.check_transfer(Network(lc), "PWB", "JED", "DMM")
    assert not r["allowed"] and "SITE_HAZMAT" in failed(r["checks"])


def test_a_new_industry_is_data_only(monkeypatch):
    """An automotive spare-parts network: new vocabulary, rule and sample data, no engine changes."""
    pack = Domain(
        name="spares", title="Spare Parts", tagline="Automotive aftermarket",
        labels={"item": "Part", "items": "Parts", "site": "Depot", "sites": "Depots", "lot": "Lot", "demand": "Orders"},
        roles={"planner": "Planner", "manager": "Manager"},
        approvals={"value": ("manager",), "budget": ("planner", "manager"), "policy": ("manager",)},
        requirements={"oversize": Requirement("Oversize freight", "XL", lane=True, ref="SOP-X")},
        sample={
            "countries": [("DE", "Germany", 0)],
            "sites": [("N", "North Depot", "dc", "DE", "", None), ("S", "South Depot", "dc", "DE", "", None)],
            "items": [("BMP", "Front bumper", "body", "oversize", 0, 5, 1, 300, None, "8708.10", 0)],
            "stock": {"BMP": {"N": ([(30, None)], 14, 2), "S": ([(2, None)], 14, 4)}},
            "lanes": [("N", "S", "road", 6, "")],
            "suppliers": [("Bumper Works", "DE", 4, 1.0, 0, 95, 1, "", "body", "DAP")],
            "permits": [], "substitutes": [], "scenarios": {},
        })
    monkeypatch.setitem(domains.PACKS, "spares", pack)
    conn = db.connect()
    try:
        db.reset(conn, "spares")
        p = planner.plan(Network(conn), "BMP", "S")
        assert p["transfers"] == [] and p["audit"][0]["blocked_by"] == "LANE_OVERSIZE"  # no oversize truck on the lane
        assert p["po"]["supplier"] == "Bumper Works" and p["po"]["qty"] == p["need"]
    finally:
        conn.close()

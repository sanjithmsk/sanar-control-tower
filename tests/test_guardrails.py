import pytest

from app import guardrails as G
from app import planner
from app import semantic as S
from app.semantic import Network


def failed(checks):
    return [x["code"] for x in checks if not x["ok"]]


def test_value_signoff_names_the_roles(c):
    p = planner.plan(Network(c), "ADA", "A")
    assert p["signoff_roles"] == ["chief_pharmacist", "logistics_manager"]
    with pytest.raises(G.Blocked, match="Pharmacist cannot approve"):
        planner.execute(c, "ADA", "A", "test", "pharmacist")
    assert planner.execute(c, "ADA", "A", "test", "chief_pharmacist")


def test_controlled_and_high_value_needs_chief_pharmacist(c):
    net = Network(c)
    reasons = G.signoff(net, net.items["TRM"], [{"value": 20_000}])
    assert [r["code"] for r in reasons] == ["controlled", "value"]
    assert G.allowed_roles(net, reasons) == ["chief_pharmacist"]


def test_auto_executor_and_unknown_roles_are_refused(c):
    with pytest.raises(G.Blocked, match="Needs human sign-off"):
        planner.execute(c, "TRM", "B", "auto", "system")
    with pytest.raises(G.Blocked, match="Unknown role"):
        planner.execute(c, "INS", "A", "x", "intern")


def test_supplier_compliance_and_certification(c):
    p = planner.plan(Network(c), "TRM", "B")
    spot = next(o for o in p["sourcing"] if o["supplier"].startswith("Supplier C"))
    assert failed(spot["checks"]) == ["COMPLIANCE", "CERT_CONTROLLED"]
    assert all(not o["supplier"].startswith("MedTech") for o in p["sourcing"])  # does not supply medicines


def test_price_premium_and_cover_need_signoff(c):
    net = Network(c)
    codes = [r["code"] for r in G.signoff(net, net.items["AMX"], [{"value": 100, "premium_pct": 25, "cover_days": 90}])]
    assert codes == ["price_premium", "max_cover"]


def test_daily_auto_budget_hands_over_to_humans(c):
    c.execute("UPDATE policy SET value=5000 WHERE key='auto_daily_budget'")
    r = planner.auto_run(c)
    left = planner.alerts(Network(c))
    assert r["executed"] < 6  # 6 run without the cap
    assert any("auto-execution budget" in reason for a in left for reason in a["signoff"])


def test_policy_bounds():
    with pytest.raises(G.Rejected):
        G.validate_policy("min_shelf_days", 999)
    with pytest.raises(G.NotFound):
        G.validate_policy("no_such_rule", 1)


def test_policy_change_takes_effect(c):
    c.execute("UPDATE policy SET value=10 WHERE key='min_shelf_days'")
    assert S.metrics(c, "PCM", "C")["fefo_stock"] == 820  # the 20-day batch is plannable again


def test_temperature_transit_limit(c):
    c.execute("UPDATE policy SET value=3 WHERE key='temp_max_transit_hours'")
    net = Network(c)
    assert failed(G.transfer_checks(net, "INS", "D", "A")) == ["TEMP_TRANSIT"]  # 4 h lane
    assert failed(G.transfer_checks(net, "INS", "B", "A")) == []  # 2 h lane


def test_hazmat_needs_equipped_sites(c):
    assert failed(G.transfer_checks(Network(c), "HSN", "D", "C")) == ["LANE_HAZMAT", "SITE_HAZMAT"]


def test_storage_capacity_limits_inbound_stock(c):
    c.execute("UPDATE sites SET capacity=1912 WHERE id='A'")  # Central Retail holds 1,862 units: room for 50
    p = planner.plan(Network(c), "INS", "A")
    assert sum(t["qty"] for t in p["transfers"]) == 50
    assert p["po"] is None and p["unmet"] == p["need"] - 50  # an order on top would not fit either
    assert all("CAPACITY" in failed(o["checks"]) for o in p["sourcing"])


def test_sell_through_capacity(c):
    net = Network(c)  # North Branch holds oseltamivir dated 240 days out; how much of a 44-day lot can it sell?
    room = net.absorb("OSE", "B", 44)
    assert room == int(net.cum("OSE", "B", 44))  # everything it sells in 44 days, flu season included
    assert net.absorb("OSE", "B", 44, [(room, 44)]) == 0  # nothing more once that is planned in
    assert net.absorb("OSE", "B", 44, [(room, 45)]) > 0  # a lot expiring earlier still sells first (FEFO)


def test_receipt_temperature_rules(c):
    net = Network(c)
    ins, amx = net.items["INS"], net.items["AMX"]
    assert "Excursion" in G.receipt_check(net, ins, max_temp=9.5, minutes=45)
    assert G.receipt_check(net, ins, max_temp=9.5, minutes=20) is None  # within the 30-minute tolerance
    assert "freezing" in G.receipt_check(net, ins, min_temp=-1, minutes=5)
    assert G.receipt_check(net, amx, max_temp=30, minutes=600) is None  # ambient item

import pytest

from app import guardrails as G
from app import planner
from app import semantic as S
from app.semantic import Network


def lot(c, lot_no):
    return c.execute("SELECT * FROM lots WHERE lot_no=? ORDER BY id DESC", (lot_no,)).fetchone()


def test_expired_lot_is_quarantined_and_written_off(c):
    planner.sense(c)
    assert lot(c, "AMX-A2")["status"] == "expired"
    assert "write-off SAR 160" in c.execute("SELECT msg FROM events ORDER BY id DESC").fetchone()["msg"]


def test_recall_reaches_stock_and_shipments(c):
    need = planner.plan(Network(c), "INS", "A")["need"]
    (act,) = planner.execute(c, "INS", "A", "test", "pharmacist")  # INS-B1 leaves North Branch
    with pytest.raises(G.Blocked):
        planner.recall(c, "INS-B1", "Manufacturer recall", "x", "pharmacist")
    assert planner.recall(c, "INS-B1", "Manufacturer recall", "QA", "qa") == {
        "lot_no": "INS-B1", "quarantined": 160 - need, "in_transit": need}
    assert S.metrics(c, "INS", "A")["in_transit"] == 0  # the shipment no longer counts as cover
    assert planner.receive(c, act)["quarantined"] == need
    assert lot(c, "INS-B1")["status"] == "recalled"
    assert ("INS", "A") in {(a["sku"], a["site"]) for a in planner.alerts(Network(c))}  # re-planned at once
    assert S.metrics(c, "INS", "B")["fefo_stock"] == 100  # only the unaffected INS-B2 still counts


def test_excursion_quarantine_release_and_disposal(c):
    need = planner.plan(Network(c), "INS", "A")["need"]
    (act,) = planner.execute(c, "INS", "A", "test", "pharmacist")
    r = planner.receive(c, act, max_temp=9.5, minutes=45)
    assert r["quarantined"] == need and "Excursion" in r["note"]
    held = c.execute("SELECT id FROM lots WHERE site='A' AND status='quarantined'").fetchone()["id"]
    with pytest.raises(G.Blocked):
        planner.release(c, held, "x", "pharmacist")
    planner.release(c, held, "QA", "qa")
    assert S.metrics(c, "INS", "A")["fefo_stock"] == 40 + need

    planner.recall(c, "TRM-B1", "Tamper evidence", "QA", "qa")
    recalled = lot(c, "TRM-B1")["id"]
    with pytest.raises(G.Rejected):
        planner.release(c, recalled, "QA", "qa")  # recalled stock can never be released
    planner.dispose(c, recalled, "QA", "qa", "return")
    assert lot(c, "TRM-B1")["status"] == "disposed"


def test_rebalance_saves_expiring_stock(c):
    net = planner.sense(c)
    (prop,) = planner.rebalance(net)
    assert (prop["lot_no"], prop["from"], prop["to"], prop["qty"], prop["value_saved"]) == ("OSE-C2", "C", "D", 122, 3416)
    planner.execute_rebalance(c, prop["lot_id"], "D", "test", "pharmacist")
    net = Network(c)
    assert net.metrics("OSE", "C")["waste_units"] == 0 and planner.rebalance(net) == []


def test_demand_sensing_forecast_and_spike(c):
    r = planner.record_demand(c, "AMX", "A", 40, Network(c).today.isoformat())
    assert r == {"forecast_14d": 330, "spike": False}  # 7-day and 28-day run rates blended
    r = planner.record_demand(c, "AMX", "A", 100, Network(c).today.isoformat())
    assert r["spike"]  # 140 today against a 20-a-day average
    assert any(s["kind"] == "spike" for s in Network(c).signals())


def test_hold_and_release(c):
    planner.hold(c, "INS", "A", "pharmacist")
    assert ("INS", "A") not in {(a["sku"], a["site"]) for a in planner.alerts(Network(c))}
    planner.release_hold(c, "INS", "A", "pharmacist")
    assert ("INS", "A") in {(a["sku"], a["site"]) for a in planner.alerts(Network(c))}


def test_signals(c):
    kinds = {(s["kind"], s.get("sku")) for s in planner.sense(c).signals()}
    assert {("shortage", "ADA"), ("overstock", "OSE"), ("waste", "OSE"), ("late", "AMX"), ("calendar", None)} <= kinds

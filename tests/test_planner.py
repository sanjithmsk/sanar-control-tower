import os
import tempfile

import pytest

os.environ["SANAR_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.pop("ANTHROPIC_API_KEY", None)

from fastapi.testclient import TestClient  # noqa: E402

from app import db, planner, rag  # noqa: E402
from app import semantic as S  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture
def c():
    conn = db.connect()
    db.reset(conn)
    yield conn
    conn.close()


def on(c, key):
    c.execute("UPDATE scenarios SET active=1 WHERE key=?", (key,))


def test_initial_alerts(c):
    assert {(a["sku"], a["branch"]) for a in planner.alerts(c)} == {("INS", "A"), ("ADA", "A"), ("TRM", "B"), ("AMX", "C")}


def test_deficit_formula(c):
    m = S.metrics(c, "INS", "A")
    assert m["balance"] == 40 + 0 - 120 - 30


def test_near_expiry_batch_excluded(c):
    assert S.metrics(c, "PCM", "C")["fefo_stock"] == 520  # the 300-unit batch at 20 days is ignored


def test_internal_transfer_preferred(c):
    p = planner.plan(c, "INS", "A")
    assert p["transfers"] == [{"from": "B", "from_name": "North Branch", "qty": 110, "hours": 2}]
    assert p["po"] is None


def test_cold_chain_guardrail_forces_po(c):
    on(c, "cold_fail")
    p = planner.plan(c, "INS", "A")
    assert p["transfers"] == [] and p["po"]["qty"] == 110
    assert not S.route_ok(c, "INS", "B", "C")  # no reefer on B-C in any scenario


def test_split_transfer_and_po_rounds_to_pack(c):
    p = planner.plan(c, "ADA", "A")
    assert p["transfers"][0]["qty"] == 6 and p["po"]["qty"] == 2
    assert p["signoff"]  # high value


def test_controlled_needs_signoff_and_secondary_supplier(c):
    on(c, "supplier_short")
    p = planner.plan(c, "TRM", "B")
    assert "controlled substance" in p["signoff"]
    assert p["po"]["supplier"].startswith("Supplier B")


def test_execute_is_fefo_and_never_creates_donor_deficit(c):
    planner.execute(c, "INS", "A", "test")
    assert S.metrics(c, "INS", "A")["balance"] == 0
    assert S.metrics(c, "INS", "B")["balance"] >= 0
    first = c.execute("SELECT qty FROM batches WHERE batch_no='INS-B1'").fetchone()["qty"]
    assert first == 50  # earliest-expiry batch was drawn down first
    act = c.execute("SELECT id FROM actions").fetchone()["id"]
    assert planner.receive(c, act)
    m = S.metrics(c, "INS", "A")
    assert m["in_transit"] == 0 and m["fefo_stock"] == 150


def test_auto_run_leaves_signoff_items(c):
    planner.auto_run(c)
    assert {(a["sku"], a["branch"]) for a in planner.alerts(c)} == {("ADA", "A"), ("TRM", "B")}


def test_surge_creates_deficits(c):
    on(c, "surge")
    assert ("OSE", "A") in {(a["sku"], a["branch"]) for a in planner.alerts(c)}


def test_rag_finds_policy():
    assert rag.search("reefer vehicle for insulin transfer")[0]["source"] == "sop_cold_chain.md"


def test_api_flow(c):
    api = TestClient(app)
    assert api.get("/api/state").json()["kpis"]["open_deficits"] == 4
    assert api.get("/api/alerts/AMX/C/plan").json()["need"] == 210
    assert "rule-based" in api.get("/api/alerts/AMX/C/explain").json()["text"]
    assert api.post("/api/alerts/AMX/C/approve", json={"approver": "QA"}).status_code == 200
    assert api.post("/api/alerts/AMX/C/approve", json={}).status_code == 409
    assert api.post("/api/ask", json={"question": "When is sign-off required?"}).json()["sources"]
    assert api.post("/api/sales", json={"sku": "AMX", "branch": "A", "qty": 40}).json()["forecast_14d"] == 300

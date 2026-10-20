def test_api_flow(api):
    state = api.get("/api/state").json()
    assert state["kpis"]["open_deficits"] == 7 and state["domain"]["name"] == "pharmacy"
    assert api.get("/api/alerts/AMX/C/plan").json()["need"] == 174
    assert "rule-based" in api.get("/api/alerts/AMX/C/explain").json()["text"]
    assert api.post("/api/alerts/AMX/C/approve", json={"approver": "QA"}).status_code == 200
    assert api.post("/api/alerts/AMX/C/approve", json={}).status_code == 409
    assert api.post("/api/ask", json={"question": "When is sign-off required?"}).json()["sources"]
    assert api.post("/api/sales", json={"sku": "AMX", "branch": "A", "qty": 40}).json()["forecast_14d"] == 330
    assert api.get("/api/health").json() == {"ok": True}


def test_api_enforces_signoff_roles(api):
    r = api.post("/api/alerts/ADA/A/approve", json={"role": "pharmacist"})
    assert r.status_code == 403 and "Chief pharmacist" in r.json()["detail"]
    assert api.post("/api/alerts/ADA/A/approve", json={"role": "chief_pharmacist", "approver": "Dr Noor"}).status_code == 200
    assert any("approved by Dr Noor" in e["msg"] for e in api.get("/api/state").json()["log"])


def test_api_policy(api):
    assert api.put("/api/policy/min_shelf_days", json={"value": 20, "role": "pharmacist"}).status_code == 403
    assert api.put("/api/policy/min_shelf_days", json={"value": 999, "role": "chief_pharmacist"}).status_code == 422
    r = api.put("/api/policy/min_shelf_days", json={"value": 20, "role": "chief_pharmacist"})
    assert r.status_code == 200 and r.json()["value"] == 20


def test_api_lot_operations(api):
    need = api.get("/api/alerts/INS/A/plan").json()["need"]
    (aid,) = api.post("/api/alerts/INS/A/approve", json={}).json()["actions"]
    action = next(a for a in api.get("/api/state").json()["open_actions"] if a["id"] == aid)
    assert action["ref"] == f"TR-{aid}" and action["temp"] == [2, 8]
    r = api.post(f"/api/actions/{aid}/receive", json={"max_temp_c": 9.5, "excursion_minutes": 45})
    assert r.json()["quarantined"] == need
    held = next(q for q in api.get("/api/state").json()["quarantine"] if q["status"] == "quarantined")
    assert api.post(f"/api/lots/{held['lot_id']}/release", json={"role": "qa"}).status_code == 200
    assert api.post("/api/recalls", json={"lot_no": "NOPE-1", "role": "qa"}).status_code == 404
    assert api.post("/api/recalls", json={"lot_no": "TRM-B1", "role": "qa"}).json()["quarantined"] == 20


def test_api_validation(api):
    assert api.post("/api/demand", json={"sku": "AMX", "site": "A", "qty": 0}).status_code == 422
    assert api.post("/api/demand", json={"sku": "AMX", "site": "A", "qty": 5, "day": "2999-01-01"}).status_code == 422
    assert api.post("/api/demand", json={"sku": "XXX", "site": "A", "qty": 5}).status_code == 404
    r = api.post("/api/check-transfer", json={"sku": "INS", "from_site": "B", "to_site": "C"}).json()
    assert not r["allowed"]


def test_api_switches_domain(api):
    assert api.post("/api/reset", params={"domain": "nowhere"}).status_code == 404
    assert api.post("/api/reset", params={"domain": "logistics"}).json()["domain"] == "logistics"
    state = api.get("/api/state").json()
    assert state["domain"]["labels"]["item"] == "Product" and state["kpis"]["open_deficits"] == 7
    assert api.post("/api/reset", params={"domain": "pharmacy"}).json()["domain"] == "pharmacy"

"""HTTP API and dashboard. Run with:  uvicorn app.main:app --reload"""
from datetime import date, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import agent, db, planner
from . import semantic as S

app = FastAPI(title="Sanar Control Tower")
STATIC = Path(__file__).parent / "static"
db.init()


def conn():
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


class Approval(BaseModel):
    approver: str = "pharmacist"


class Sale(BaseModel):
    sku: str
    branch: str
    qty: int
    day: str | None = None


class Question(BaseModel):
    question: str


def _check(c, sku, branch):
    if not c.execute("SELECT 1 FROM positions WHERE sku=? AND branch=?", (sku, branch)).fetchone():
        raise HTTPException(404, "Unknown medicine or site")


def _inventory(c, sku):
    rows = []
    for b in c.execute("SELECT * FROM branches"):
        rows.append({"branch": b["id"], "branch_name": b["name"], **S.metrics(c, sku, b["id"])})
    return rows


def _expiry(c):
    limit = (date.today() + timedelta(days=S.NEAR_EXPIRY_DAYS)).isoformat()
    return [{"sku_name": r["sn"], "branch_name": r["bn"], "batch_no": r["batch_no"], "qty": r["qty"],
             "days": S.days_left(r["expiry"]), "excluded": S.days_left(r["expiry"]) <= S.MIN_SHELF_DAYS}
            for r in c.execute(
                "SELECT b.*, s.name sn, br.name bn FROM batches b JOIN skus s ON s.id=b.sku "
                "JOIN branches br ON br.id=b.branch WHERE b.qty>0 AND b.expiry<=? ORDER BY b.expiry", (limit,))]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state")
def state(c=Depends(conn)):
    al = planner.alerts(c)
    exp = _expiry(c)
    open_actions = [dict(r) for r in c.execute(
        "SELECT a.id, a.kind, a.qty, a.supplier, s.name sku_name, t.name to_name, f.name from_name "
        "FROM actions a JOIN skus s ON s.id=a.sku JOIN branches t ON t.id=a.to_branch "
        "LEFT JOIN branches f ON f.id=a.from_branch WHERE a.status='in_transit' ORDER BY a.id")]
    return {
        "kpis": {"open_deficits": len(al), "need_signoff": sum(1 for a in al if a["signoff"]),
                 "near_expiry_units": sum(e["qty"] for e in exp),
                 "actions_executed": c.execute("SELECT COUNT(*) n FROM actions").fetchone()["n"]},
        "alerts": al, "expiry": exp, "open_actions": open_actions,
        "skus": [dict(r) for r in c.execute("SELECT id, name FROM skus")],
        "scenarios": [dict(r) for r in c.execute("SELECT * FROM scenarios")],
        "log": [dict(r) for r in c.execute("SELECT ts, msg FROM events ORDER BY id DESC LIMIT 60")],
    }


@app.get("/api/inventory/{sku}")
def inventory(sku: str, c=Depends(conn)):
    _check(c, sku, "A")
    return _inventory(c, sku)


@app.get("/api/alerts/{sku}/{branch}/plan")
def get_plan(sku: str, branch: str, c=Depends(conn)):
    _check(c, sku, branch)
    p = planner.plan(c, sku, branch)
    if not p:
        raise HTTPException(404, "No deficit at this site")
    return p


@app.get("/api/alerts/{sku}/{branch}/explain")
def explain(sku: str, branch: str, c=Depends(conn)):
    return agent.explain_plan(get_plan(sku, branch, c))


@app.post("/api/alerts/{sku}/{branch}/approve")
def approve(sku: str, branch: str, body: Approval, c=Depends(conn)):
    _check(c, sku, branch)
    ids = planner.execute(c, sku, branch, f"approved by {body.approver}")
    if not ids:
        raise HTTPException(409, "Nothing to execute; the deficit is already resolved")
    return {"actions": ids}


@app.post("/api/alerts/{sku}/{branch}/hold")
def hold(sku: str, branch: str, c=Depends(conn)):
    _check(c, sku, branch)
    c.execute("INSERT OR IGNORE INTO holds VALUES (?,?)", (sku, branch))
    db.log(c, f"Held for manual review: {sku} at site {branch}")
    c.commit()
    return {"ok": True}


@app.post("/api/auto-run")
def auto_run(c=Depends(conn)):
    return {"executed": planner.auto_run(c)}


@app.post("/api/actions/{action_id}/receive")
def receive(action_id: int, c=Depends(conn)):
    if not planner.receive(c, action_id):
        raise HTTPException(404, "No in-transit action with that number")
    return {"ok": True}


@app.post("/api/scenarios/{key}")
def toggle(key: str, c=Depends(conn)):
    r = c.execute("SELECT * FROM scenarios WHERE key=?", (key,)).fetchone()
    if not r:
        raise HTTPException(404, "Unknown scenario")
    c.execute("UPDATE scenarios SET active=? WHERE key=?", (0 if r["active"] else 1, key))
    db.log(c, ("Scenario off: " if r["active"] else "Scenario on: ") + r["label"])
    c.commit()
    return {"active": not r["active"]}


@app.post("/api/sales")
def add_sale(s: Sale, c=Depends(conn)):
    """POS feed: record a dispense and refresh that site's forecast."""
    _check(c, s.sku, s.branch)
    c.execute("INSERT INTO sales(sku,branch,day,qty) VALUES (?,?,?,?)",
              (s.sku, s.branch, s.day or date.today().isoformat(), s.qty))
    f = S.refresh_forecast(c, s.sku, s.branch)
    c.commit()
    return {"forecast_14d": f}


@app.post("/api/ask")
def ask(q: Question, c=Depends(conn)):
    snapshot = {r["name"]: [{k: v for k, v in row.items() if k != "branch"} for row in _inventory(c, r["id"])]
                for r in c.execute("SELECT id, name FROM skus").fetchall()}
    return agent.answer(q.question, snapshot)


@app.post("/api/reset")
def reset(c=Depends(conn)):
    db.reset(c)
    return {"ok": True}

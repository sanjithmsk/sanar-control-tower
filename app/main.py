"""HTTP API and console. Run with:  uvicorn app.main:app --reload"""
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import AliasChoices, BaseModel, Field

from . import agent, db, domains, planner
from . import guardrails as G
from .semantic import Network

app = FastAPI(title="Supply Chain Control Tower")
STATIC = Path(__file__).parent / "static"
db.init()

for _exc, _code in ((G.Blocked, 403), (G.NotFound, 404), (G.Rejected, 422)):
    app.add_exception_handler(_exc, lambda _req, e, code=_code: JSONResponse({"detail": str(e)}, status_code=code))


def conn():
    c = db.connect()
    try:
        yield c
    finally:
        c.close()


class Actor(BaseModel):
    role: str | None = None
    by: str | None = Field(None, max_length=80)


class Approval(Actor):
    approver: str | None = Field(None, max_length=80)


class Receipt(BaseModel):
    min_temp_c: float | None = Field(None, ge=-100, le=100)
    max_temp_c: float | None = Field(None, ge=-100, le=100)
    excursion_minutes: int = Field(0, ge=0, le=100_000)
    by: str | None = Field(None, max_length=80)


class Demand(BaseModel):
    sku: str
    site: str = Field(validation_alias=AliasChoices("site", "branch"))
    qty: int = Field(gt=0, le=1_000_000)
    day: date | None = None
    by: str | None = Field(None, max_length=80)


class RecallIn(Actor):
    lot_no: str = Field(min_length=1, max_length=60)
    reason: str = Field("Manufacturer recall", max_length=200)


class Disposal(Actor):
    method: Literal["destroy", "return"] = "destroy"


class PolicyIn(Actor):
    value: float


class TransferQuery(BaseModel):
    sku: str
    from_site: str
    to_site: str
    qty: int | None = Field(None, gt=0)


class Question(BaseModel):
    question: str = Field(min_length=1, max_length=1000)


def _who(net, body):
    """Acting role (default: the domain's first role) and the name written to the log."""
    role = (body.role if body else None) or net.domain.default_role
    name = (getattr(body, "approver", None) or (body.by if body else None)) or net.domain.roles.get(role, role)
    return role, name


def _known(net, sku, *sites):
    if sku not in net.items:
        raise G.NotFound(f"Unknown {net.domain.labels['item'].lower()} '{sku}'")
    for s in sites:
        if s not in net.sites:
            raise G.NotFound(f"Unknown {net.domain.labels['site'].lower()} '{s}'")


def _domain(d):
    return {"name": d.name, "title": d.title, "tagline": d.tagline, "labels": d.labels, "roles": d.roles,
            "currency": d.currency, "approvals": {k: list(v) for k, v in d.approvals.items()},
            "available": [{"name": p.name, "title": p.title} for p in domains.PACKS.values()]}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/state")
def state(c=Depends(conn)):
    net = planner.sense(c)
    al = planner.alerts(net)
    return {
        "domain": _domain(net.domain), "llm": agent.status(), "kpis": planner.kpis(net, al),
        "alerts": [{k: v for k, v in a.items() if k != "_score"} for a in al],
        "signals": net.signals(), "expiry": planner.expiry_watch(net), "rebalance": planner.rebalance(net),
        "quarantine": planner.quarantine(net), "open_actions": planner.in_transit(net),
        "holds": [{"sku": s, "site": st, "sku_name": net.items[s]["name"], "site_name": net.sites[st]["name"],
                   "by": h["by"]} for (s, st), h in net.holds.items()],
        "items": [{"id": k, "name": v["name"]} for k, v in net.items.items()],
        "sites": [{"id": k, "name": v["name"]} for k, v in net.sites.items()],
        "scenarios": [dict(r) for r in c.execute("SELECT key, label, active FROM scenarios")],
        "policy": G.policy_rows(net),
        "log": [dict(r) for r in c.execute("SELECT ts, msg FROM events ORDER BY id DESC LIMIT 60")],
    }


@app.get("/api/domains")
def list_domains():
    return [_domain(p) for p in domains.PACKS.values()]


@app.get("/api/inventory/{sku}")
def inventory(sku: str, c=Depends(conn)):
    net = Network(c)
    _known(net, sku)
    return [{"site": sid, "site_name": s["name"], **net.metrics(sku, sid)}
            for sid, s in net.sites.items() if (sku, sid) in net.positions]


@app.get("/api/lots/{sku}")
def lots(sku: str, site: str | None = None, c=Depends(conn)):
    net = Network(c)
    _known(net, sku, *([site] if site else []))
    return planner.lots_view(net, sku, site)


@app.get("/api/alerts/{sku}/{site}/plan")
def get_plan(sku: str, site: str, c=Depends(conn)):
    net = Network(c)
    _known(net, sku, site)
    p = planner.plan(net, sku, site)
    if not p:
        raise HTTPException(404, "No deficit at this site")
    return p


@app.get("/api/alerts/{sku}/{site}/explain")
def explain(sku: str, site: str, c=Depends(conn)):
    return agent.explain_plan(Network(c), get_plan(sku, site, c))


@app.post("/api/alerts/{sku}/{site}/approve")
def approve(sku: str, site: str, body: Approval, c=Depends(conn)):
    net = Network(c)
    _known(net, sku, site)
    role, who = _who(net, body)
    ids = planner.execute(c, sku, site, f"approved by {who}", role, net)
    if not ids:
        raise HTTPException(409, "No compliant transfer or supplier is available; escalate or review alternatives"
                            if planner.plan(Network(c), sku, site) else
                            "Nothing to execute; the deficit is already resolved")
    return {"actions": ids}


@app.post("/api/alerts/{sku}/{site}/hold")
def hold(sku: str, site: str, body: Actor | None = None, c=Depends(conn)):
    net = Network(c)
    _known(net, sku, site)
    planner.hold(c, sku, site, _who(net, body)[1])
    return {"ok": True}


@app.post("/api/alerts/{sku}/{site}/release")
def release_hold(sku: str, site: str, body: Actor | None = None, c=Depends(conn)):
    net = Network(c)
    _known(net, sku, site)
    planner.release_hold(c, sku, site, _who(net, body)[1])
    return {"ok": True}


@app.post("/api/auto-run")
def auto_run(c=Depends(conn)):
    return planner.auto_run(c)


@app.post("/api/check-transfer")
def check_transfer(q: TransferQuery, c=Depends(conn)):
    net = Network(c)
    _known(net, q.sku, q.from_site, q.to_site)
    if q.from_site == q.to_site:
        raise G.Rejected("Pick two different sites")
    return planner.check_transfer(net, q.sku, q.from_site, q.to_site, q.qty)


@app.get("/api/rebalance")
def rebalance(c=Depends(conn)):
    return planner.rebalance(Network(c))


@app.post("/api/rebalance/{lot_id}/{site}/approve")
def approve_rebalance(lot_id: int, site: str, body: Approval, c=Depends(conn)):
    role, who = _who(Network(c), body)
    return {"action": planner.execute_rebalance(c, lot_id, site, f"approved by {who}", role)}


@app.post("/api/actions/{action_id}/receive")
def receive(action_id: int, body: Receipt | None = None, c=Depends(conn)):
    body = body or Receipt()
    r = planner.receive(c, action_id, body.min_temp_c, body.max_temp_c, body.excursion_minutes, body.by or "receiving")
    if not r:
        raise HTTPException(404, "No in-transit action with that number")
    return r


@app.post("/api/recalls")
def recall(body: RecallIn, c=Depends(conn)):
    role, who = _who(Network(c), body)
    return planner.recall(c, body.lot_no, body.reason, who, role)


@app.post("/api/lots/{lot_id}/release")
def release_lot(lot_id: int, body: Actor, c=Depends(conn)):
    role, who = _who(Network(c), body)
    planner.release(c, lot_id, who, role)
    return {"ok": True}


@app.post("/api/lots/{lot_id}/dispose")
def dispose_lot(lot_id: int, body: Disposal, c=Depends(conn)):
    role, who = _who(Network(c), body)
    planner.dispose(c, lot_id, who, role, body.method)
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


@app.post("/api/demand")
@app.post("/api/sales")
def add_demand(d: Demand, c=Depends(conn)):
    """Demand feed (POS, orders): records it and refreshes that site's forecast."""
    net = Network(c)
    _known(net, d.sku, d.site)
    day = d.day or date.today()
    if day > date.today():
        raise G.Rejected("Demand cannot be dated in the future")
    return planner.record_demand(c, d.sku, d.site, d.qty, day.isoformat(), d.by or "POS")


@app.get("/api/policy")
def policy(c=Depends(conn)):
    return G.policy_rows(Network(c))


@app.put("/api/policy/{key}")
def set_policy(key: str, body: PolicyIn, c=Depends(conn)):
    net = Network(c)
    role, who = _who(net, body)
    G.require_role(net, "policy", role)
    value = G.validate_policy(key, body.value)
    c.execute("INSERT OR REPLACE INTO policy VALUES (?,?)", (key, value))
    db.log(c, f"Policy {key} changed from {net.policy[key]:g} to {value:g} by {who}")
    c.commit()
    return next(r for r in G.policy_rows(Network(c)) if r["key"] == key)


@app.post("/api/ask")
def ask(q: Question, c=Depends(conn)):
    return agent.answer(c, q.question)


@app.get("/api/briefing")
def briefing(c=Depends(conn)):
    return agent.briefing(c)


@app.post("/api/reset")
def reset(domain: str | None = None, c=Depends(conn)):
    if domain and domain not in domains.PACKS:
        raise G.NotFound(f"Unknown domain '{domain}'")
    db.reset(c, domain)
    return {"ok": True, "domain": db.current_domain(c)}

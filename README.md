# Sanar Control Tower

AI-driven supply chain control tower. It senses deficits across a network of sites, plans compliant
resolutions (inter-site transfers, purchase orders, imports), enforces handling, trade and approval
guardrails, prevents expiry waste, runs recalls and quarantine, and explains every decision.

The engine is industry-neutral. Each industry is a **domain pack** that supplies vocabulary, handling
rules, approval roles, policy defaults, documents and a sample network. Two packs ship today:

| Pack | Network | Shows |
| --- | --- | --- |
| `pharmacy` (default) | Sanar Pharmacy: 4 branches, 9 items | FEFO, cold chain 2–8 °C, controlled substances, biosimilar substitution, flammables, SFDA device imports |
| `logistics` | Gulf Trade & Logistics: port, 2 DCs, a free-zone hub in the UAE | Imports and re-exports, customs and duty, Incoterms, permits, dangerous goods, frozen and halal food, high-value freight |

Switch packs from the console (top right) or with `TOWER_DOMAIN`.

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...                    # optional, enables the Claude agent
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. API docs are at http://127.0.0.1:8000/docs. Run the tests with `pytest`.

Without an API key everything still works: rationale, briefings and questions are answered by the
rules engine and retrieved SOP excerpts (for example "Can insulin move from North to East?" is
answered by running the transfer guardrails). Optional settings are in [.env.example](.env.example).

## What it does

- **Sense**: live balance per item and site (FEFO stock + in transit − 14-day forecast − safety buffer),
  ranked by impact and cover; stockout date versus the earliest possible arrival; network shortages,
  demand spikes, overstock and projected expiry waste as signals. Expired lots are quarantined and
  written off automatically.
- **Plan**: donors are audited nearest first (or first when their stock would otherwise expire), lots
  leave earliest-expiry first and only in the quantity the destination can sell before expiry. The
  rest is sourced from the compliant supplier with the lowest landed cost, or the fastest one when
  cover is too short to wait. Imports add customs days, freight and duty. When nothing compliant
  exists the gap is escalated and substitutes are listed.
- **Act**: routine actions auto-execute up to a daily budget; everything else waits for the right
  role. Guardrails and sign-off are re-checked at execution time. Transfers on the same lane and day
  share trucks, and the cost and saving are recorded.
- **Learn**: receipts build supplier scorecards (on time, in full, delay, lead-time spread). A
  supplier that keeps delivering late or short sees its compliance score fall and is blocked
  automatically; its delays lengthen its planned lead time and the safety stock that depends on it.
- **Operate**: receipts with temperature-logger checks and short deliveries, lot recalls that reach
  stock on the road, QA release or disposal, holds, late-order cancellation, editable policy.
- **Report**: stockouts avoided, waste prevented, procurement and transport spend, load-sharing
  savings, write-offs and supplier performance over time, with CSV and PDF export.
- **Explain**: Claude explains each plan, writes shift briefings and answers questions through
  read-only tools over the live network.

## Planning intelligence

- **Demand calendar and seasonality.** Seasons, holidays and promotions (flu season, Ramadan, Hajj,
  a store promotion) lift demand for the items and sites they apply to. The forecast is a daily
  curve: the baseline from the last 7 and 28 days with past events divided out, times the calendar
  for each of the next 14 days. Stockout timing, expiry waste and sell-through read the same curve.
- **Smart safety stock.** z(service level) × √(lead time × demand variance + demand² × lead-time
  variance), with 99% service for critical items. Demand variance comes from history; lead time and
  its spread come from the fastest compliant supplier's delivery record. Fixed buffers stay available.
- **Late shipments.** Anything past its ETA raises a signal; past the late-cover window it stops
  counting as cover, so the deficit reopens and is re-planned. Late supplier orders can be cancelled.
- **Load sharing.** Each lane has a trip cost and a vehicle capacity. Pending transfers are grouped
  by lane to show trucks and cost together versus one by one, and executed transfers fill trucks
  already booked that day before opening new ones.

## Architecture

| Layer | File | What it does |
| --- | --- | --- |
| Domain packs | `app/domains/` | Vocabulary, handling requirements, roles, approval rules, policy defaults and sample data per industry. |
| Enterprise data | `app/db.py` | Industry-neutral SQLite schema (sites, items, lots, lanes, suppliers, countries, permits). Replace with ERP / WMS / TMS / POS connectors. |
| Semantic layer | `app/semantic.py` | One snapshot of the network in a fixed number of queries; demand curve with calendar, smart safety stock, supplier scorecards, late shipments, truck sharing, FEFO waste simulation, sell-through capacity, signals. |
| Guardrails | `app/guardrails.py` | Every rule as a structured check (code, pass/fail, detail, document), bounded policy thresholds, sign-off roles. |
| Planning engine | `app/planner.py` | Alerts, network audit, sourcing, execution, receipts, recalls, quarantine, rebalancing, load plan, calendar, auto-run, recorded outcomes and reports. |
| Reports | `app/reports_pdf.py` | PDF report (fpdf2): savings tiles, spend and savings charts, supplier scorecards. |
| RAG | `app/rag.py` + `knowledge/` | BM25 retrieval over `knowledge/common/` and the active pack's folder. Swap for a vector DB behind `search()`. |
| Agent | `app/agent.py` | Claude tool-use loop with read-only tools, plan rationale, briefings, rules-engine fallback. |
| API + console | `app/main.py`, `app/static/index.html` | REST API and the approval console. |

Design choice: quantities, compliance and approvals are computed in code, never by the model. The
agent's tools are read-only, so it cannot move stock or approve anything.

Efficiency: the console's state, every alert and every plan come from one in-memory snapshot, so a
refresh costs the same number of queries however many items, sites or alerts there are (a test
asserts this).

## Guardrails

| Rule | Checks | Applies to |
| --- | --- | --- |
| FEFO and shelf life | Earliest-expiry lots leave first; lots at or below the minimum shelf life are not planned or moved | Planning, transfers |
| Donor safety | A donor keeps its own forecast and safety buffer | Transfers |
| Sell-through | A destination receives only what it can sell before expiry without pushing its own lots past expiry | Transfers, rebalancing |
| Handling capabilities | Item tags (cold, frozen, hazmat, controlled, halal, high value...) require the matching capability on both sites, the lane and the supplier | Transfers, sourcing |
| Temperature transit | Temperature-controlled transfers arrive within the transit limit, customs dwell included | Transfers |
| Lane and site status | Lane exists and is open; both sites are operational | Transfers |
| Storage capacity | Inbound stock fits the destination's free space | Transfers, sourcing |
| Supplier compliance | Score (contract blended with on-time-in-full once there are enough deliveries) above the minimum; required certifications held; not reporting a shortage | Sourcing |
| Late shipments | Shipments past the late-cover window are not counted as cover | Planning |
| Trade | Customs clearance added to lead time; duty and freight in landed cost; valid import permit for the arrival date; no restricted origin or destination | Imports, cross-border transfers |
| Sign-off | Tag rules (e.g. controlled substances), value limit, price premium, order cover, imports and cross-border moves where the pack requires it, daily auto-execution budget; each with the roles allowed to approve | Execution |
| Lot status | Recalled, quarantined and expired stock is never planned; recalled or expired lots can never be released | Everywhere |
| Receipt | Temperature excursions or freezing quarantine the received lot | Receipts |

Thresholds are editable in the console or with `PUT /api/policy/{key}` (bounded, role-checked, logged).

## Add an industry

Create a pack in `app/domains/` and register it in `app/domains/__init__.py`:

```python
from .base import Domain, Requirement

FOOD = Domain(
    name="food", title="Fresh Foods", tagline="Chilled and frozen distribution",
    labels={"item": "Product", "items": "Products", "site": "Depot", "sites": "Depots", "lot": "Lot", "demand": "Orders"},
    roles={"planner": "Planner", "manager": "Manager", "qa": "Food safety"},
    approvals={"value": ("manager",), "budget": ("planner", "manager"), "release": ("qa",),
               "dispose": ("qa",), "recall": ("qa",), "policy": ("manager",)},
    requirements={"chilled": Requirement("Chilled 0–4 °C", "0–4 °C", cap="reefer", site=True, lane=True,
                                         supplier=True, temp=(0, 4), tolerance_min=20, ref="SOP-CH-01")},
    policy={"min_shelf_days": 3, "temp_max_transit_hours": 12},
    sample={...},  # countries, sites, items, stock, lanes, suppliers, permits, substitutes, scenarios
)
```

Optional sample data: `volatility` (day-to-day demand variation per item), `calendar` (seasons and
promotions), `deliveries` (supplier history for scorecards) and `late` (orders already overdue);
lanes may add a cost per trip and a vehicle capacity.

Add its documents to `knowledge/food/`. No engine changes are needed; `tests/test_domains.py` builds
a spare-parts pack inline to prove it. Disruption scenarios are data too: `cap_outage`, `lane_closed`,
`site_down`, `primary_supplier_out`, `demand_surge`, `customs_delay` and `origin_restricted`.

## Agent

`app/agent.py` runs a tool-use loop on `claude-opus-5-5` (override with `ANTHROPIC_MODEL` and
`ANTHROPIC_EFFORT`). Tools: network overview, item inventory, resolution plan, transfer what-if,
expiry risk, lots, shipments, supplier scorecards, performance report, demand calendar, policy
search and recent activity. Requests use prompt caching, the server-side
refusal fallback, an append-only history, and a typed error chain; a failed model call falls back to
the rules engine. Plan explanations are cached per plan, so reopening a plan costs nothing.

## Main endpoints

- `GET /api/state` — KPIs, alerts, signals, expiry watch, rebalancing, quarantine, in transit, calendar,
  scorecards, load plan, policy, log
- `GET /api/alerts/{sku}/{site}/plan` and `/explain` — resolution plan and rationale
- `POST /api/alerts/{sku}/{site}/approve` | `/hold` | `/release` — decision with `{"role": ...}`
- `POST /api/auto-run` — execute routine actions within the daily budget
- `POST /api/check-transfer` — what-if guardrail check, moves nothing
- `GET /api/rebalance`, `POST /api/rebalance/{lot_id}/{site}/approve` — waste prevention
- `POST /api/actions/{id}/receive` — book goods in, with optional logger readings and received `qty`
- `POST /api/actions/{id}/cancel` — cancel an open supplier order
- `POST /api/recalls`, `POST /api/lots/{id}/release` | `/dispose` — recalls and quarantine
- `GET /api/calendar`, `POST /api/calendar`, `DELETE /api/calendar/{id}` — demand calendar
- `GET /api/scorecards`, `GET /api/load-plan` — supplier performance and truck sharing
- `GET /api/reports?days=`, `/api/reports/{daily|suppliers|actions}.csv`, `/api/reports/report.pdf` — reports
- `POST /api/demand` (alias `/api/sales`) — demand feed; refreshes the forecast and flags spikes
- `GET /api/policy`, `PUT /api/policy/{key}` — guardrail thresholds
- `POST /api/scenarios/{key}` — toggle a disruption
- `POST /api/ask`, `GET /api/briefing` — agent
- `POST /api/reset?domain=` — reload sample data, optionally switching pack
- `GET /api/health` — health check

Errors: 403 when the acting role may not approve, 404 for unknown items or sites, 409 when there is
nothing to execute, 422 when a request breaks a rule or bound.

## Before production

All data and the documents in `knowledge/` are samples. Still to add: authentication (roles are
currently chosen in the console), real ERP / WMS / TMS / POS and customs connectors, a production
database, a proper forecasting model, carrier and booking integration, and validation of every rule
by the relevant pharmacists, QA, trade compliance and regulatory teams.

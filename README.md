# Sanar Control Tower

AI-driven supply chain control tower for a pharmacy network: senses deficits, checks FEFO, cold-chain
and approval rules, then raises inter-branch transfers or vendor purchase orders.

## Run

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...                    # optional, enables the LLM rationale and Q&A
export ANTHROPIC_MODEL=claude-opus-5-5                 # optional, set to the model your account uses
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. API docs are at http://127.0.0.1:8000/docs. Run the tests with `pytest`.

Without an API key everything still works; the rationale and "Ask the tower" fall back to rule-based
text and retrieved SOP excerpts.

## How it maps to the architecture

| Layer | File | What it does |
| --- | --- | --- |
| Enterprise data | `app/db.py` | SQLite schema and sample data (branches, batches, sales, routes, suppliers). Replace with ERP/WMS/POS connectors. |
| Semantic layer | `app/semantic.py` | FEFO stock, days on hand, deficit formula, forecast, cold-chain and sign-off guardrails. |
| Planning engine | `app/planner.py` | Alerts, network FEFO audit, transfer vs PO decision, execution, goods receipt, auto-run of routine actions. |
| RAG | `app/rag.py` + `knowledge/` | Retrieval over SOPs, supplier agreements and policies. TF-IDF now; swap for a vector DB behind `search()`. |
| LLM orchestration | `app/agent.py` | Claude explains each plan and answers questions, grounded in retrieved policy and live figures. |
| API + console | `app/main.py`, `app/static/index.html` | REST API and the approval dashboard. |

Design choice: quantities and compliance checks are computed in code, not by the model. The model
explains and answers; it cannot move stock that the guardrails would block.

## Main endpoints

- `GET /api/state` — KPIs, alerts, expiry watch, in-transit actions, log
- `GET /api/alerts/{sku}/{branch}/plan` and `/explain` — resolution plan and agent rationale
- `POST /api/alerts/{sku}/{branch}/approve` | `/hold` — human decision
- `POST /api/auto-run` — execute all routine actions
- `POST /api/actions/{id}/receive` — book goods into stock
- `POST /api/scenarios/{cold_fail|supplier_short|surge}` — toggle a disruption
- `POST /api/sales` — POS feed; refreshes the 14-day forecast
- `POST /api/ask` — natural-language question
- `POST /api/reset` — reload sample data

## Before production

All data and the documents in `knowledge/` are samples. Still to add: authentication and roles for
sign-off, real ERP/WMS/POS connectors, a production database, a proper demand forecasting model, and
validation of the rules by Sanar's pharmacists and regulatory team.

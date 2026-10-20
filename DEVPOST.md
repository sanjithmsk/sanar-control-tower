# Sanar Control Tower

An AI-driven supply chain control tower for a pharmacy network. It senses stock
deficits across branches, checks FEFO, cold-chain and approval rules, and then raises
inter-branch transfers or vendor purchase orders — with a human approving anything that matters.

## Inspiration

Pharmacy supply chains fail quietly. A branch runs out of insulin while another branch across
town is sitting on surplus that is weeks from expiry. A cold-chain medicine gets routed on a van
with no reefer. A controlled substance moves without a sign-off. By the time someone notices, a
patient has been turned away or thousands of riyals of stock have expired on a shelf.

These are not forecasting problems — the data to prevent them usually already exists in the ERP,
the WMS and the POS. The problem is that nobody is watching all of it at once, applying the rules,
and proposing a fix fast enough to act on. We wanted to build the thing that watches: a control
tower that senses the deficit, does the FEFO and compliance math, and hands a planner a decision
they can approve in one click — while keeping a human firmly in charge of the actions that carry
clinical or financial risk.

## What it does

Sanar Control Tower monitors a network of pharmacy branches and, for every medicine at every site:

- **Senses deficits.** It computes a live balance — available FEFO stock + in-transit − (14-day
  forecast + safety buffer) — and raises an alert when it goes negative. Alerts are scored and
  ranked by clinical impact and how little coverage is left, so the most dangerous gap is always
  on top.
- **Audits the network first.** Before it spends a riyal, it looks for surplus at other branches,
  earliest-expiry-first (FEFO), and proposes inter-branch transfers along the fastest route.
- **Only then buys.** Whatever the network can't cover becomes a vendor purchase order, rounded to
  pack size, costed at the chosen supplier's price and lead time.
- **Enforces the rules in code.** Cold-chain medicines are blocked from any route without a working
  reefer vehicle. Batches within 30 days of expiry are excluded from planning. Controlled substances
  and any action above the SAR 10,000 threshold are flagged for human sign-off.
- **Keeps a human in the loop.** Routine, low-risk transfers can be auto-executed; anything requiring
  sign-off waits in the approval queue for a pharmacist or logistics manager to approve or hold.
- **Explains itself.** For every plan, Claude writes a short rationale grounded in the live figures
  and the retrieved SOP excerpts, citing the policy it relied on. A planner can also ask the tower
  questions in plain language ("why was this transfer blocked?") and get an answer backed by policy.
- **Handles disruptions.** Built-in scenarios — a cold-chain failure, a primary supplier shortage, a
  seasonal flu surge — let you toggle a shock and watch the tower re-plan around it in real time.

The demo runs on a sample network of 4 sites and 6 medicines (insulin, adalimumab, tramadol,
oseltamivir, amoxicillin, paracetamol), each with realistic batches, expiry dates, forecasts and
28 days of POS history.

## How we built it

A small FastAPI service with a layered architecture that mirrors how a real control tower is built:

- **Enterprise data** — SQLite schema and seed data for branches, batches, sales, routes and
  suppliers. In production this layer is swapped for ERP / WMS / POS connectors.
- **Semantic layer** — the single place where raw tables become pharma business metrics: FEFO
  stock, days on hand, the deficit formula, a demand-sensing forecast (run rate of the last 28 days
  of POS sales), and the cold-chain and sign-off guardrails. Every number the planner and the model
  see comes from here.
- **Planning engine** — deterministic logic that generates and ranks alerts, runs the network FEFO
  audit, makes the transfer-vs-PO decision, executes approved actions as transfer tickets and POs,
  and books goods into stock on receipt.
- **RAG layer** — a dependency-free TF-IDF index over a `knowledge/` folder of SOPs, supplier
  agreements and policies, behind a single `search(query, k)` function that a vector database can
  slot straight into.
- **LLM orchestration** — Claude explains each plan and answers questions, grounded strictly in the
  retrieved policy and the live figures, and told never to invent stock, batches, suppliers or rules.
- **API + console** — a REST API and a single-page approval dashboard to see KPIs, alerts, the
  expiry watch and in-transit actions, and to approve or hold.

The key design decision: **quantities and compliance are computed in code, never by the model.** The
LLM explains and answers; it cannot move stock that the guardrails would block. The whole system also
runs with no API key at all — the rationale and Q&A gracefully fall back to rule-based text and
retrieved SOP excerpts — so it never hard-depends on a model call to function.

## Challenges we ran into

- **Keeping the model out of the math.** It's tempting to let an LLM "figure out" the quantities.
  We deliberately didn't. Drawing a hard line — code owns every number and every compliance check,
  the model only explains — was the central architectural challenge and the thing that makes the
  tower trustworthy.
- **Getting FEFO right end to end.** Earliest-expiry-first has to hold not just when selecting
  surplus, but when actually decrementing batches on execution, and near-expiry stock has to be
  excluded from planning entirely. Getting the audit, the execution and the goods-receipt paths to
  agree took care.
- **Guardrails that interact.** Cold-chain routing, the near-expiry cutoff, sign-off thresholds and
  controlled-substance rules all overlap on a single plan. Encoding them so a blocked cold-chain
  route produces a clear "Blocked: no cold-chain route" instead of a silently wrong transfer was
  fiddly.
- **Grounding the LLM honestly.** Making the model cite real policy by file name and refuse to answer
  when the context doesn't support it — rather than confabulating a plausible rule — meant being
  strict about what we put in the prompt and what we told it not to do.
- **Degrading gracefully.** We wanted a judge to be able to clone and run it with zero setup, so every
  LLM-powered feature has a rule-based fallback when no API key is present.

## Accomplishments that we're proud of

- A genuinely **sense → plan → act** loop: the tower doesn't just flag a shortage, it proposes a
  compliant, ranked, executable resolution and lets a human approve it.
- **Compliance you can trust** because it lives in deterministic code — cold-chain, FEFO, near-expiry,
  controlled substances and value thresholds are enforced, not suggested.
- A **human-in-the-loop** model that auto-runs the routine work but never lets the model push through
  a high-risk action on its own.
- **Explanations grounded in real policy**, with citations, plus a natural-language Q&A over the SOPs.
- **Live disruption scenarios** that show the tower re-planning around a cold-chain failure, a
  supplier shortage or a demand surge.
- It **runs anywhere, with or without an API key**, and ships with tests.

## What we learned

- For high-stakes domains like pharma, the right division of labour is **code for decisions, LLM for
  language.** The model earns trust precisely by not being in the critical path of the numbers.
- A clean **semantic layer** — one place that owns every metric and guardrail — pays for itself the
  moment two features need the same rule to agree.
- **Retrieval beats recall.** Grounding answers in retrieved SOP text, with citations, is far safer
  than hoping a model remembers a policy correctly.
- Designing for **graceful degradation** from the start (rule-based fallbacks everywhere) made the
  system both more robust and far easier to demo.

## What's next for Sanar Control Tower

- **Real connectors** to ERP / WMS / POS in place of the SQLite sample data, and a production database.
- **A proper demand-forecasting model** — seasonality, promotions and lead-time variability — behind
  the same semantic interface, replacing the run-rate baseline.
- **A vector database** behind the existing `search()` function for better policy retrieval at scale.
- **Authentication and roles** so sign-off is tied to real pharmacist and manager identities, with a
  full audit trail.
- **Validation by Sanar's pharmacists and regulatory team** of every rule and threshold before any
  real stock is moved.
- Broader action types (expiry-driven redistribution, recalls) and richer multi-branch optimization.

# Sanar Control Tower

An AI-driven supply chain control tower. It senses stock deficits across a network, plans compliant
transfers, purchase orders and imports, enforces handling, trade and approval guardrails, prevents
expiry waste, and runs recalls, with a human approving anything that matters. It started as a
pharmacy network and is now an industry-neutral engine: pharmacy and import/export logistics run on
the same code today, and a new industry is a data pack.

## Inspiration

Supply chains fail quietly. A pharmacy branch runs out of insulin while another across town sits on
surplus that is weeks from expiry. A cold-chain medicine gets routed on a van with no reefer. A
container of electronics waits at the port because nobody noticed the conformity certificate expires
before it lands. By the time someone notices, a patient has been turned away or stock has been
written off.

These are rarely forecasting problems. The data to prevent them usually exists in the ERP, the WMS
and the POS. What is missing is something that watches all of it at once, applies the rules and
proposes a fix fast enough to act on. We built that: a control tower that senses the gap, does the
FEFO, compliance and trade math, and hands a planner a decision they can approve in one click, while
keeping a human firmly in charge of anything with clinical, financial or regulatory risk.

## What it does

- **Senses risk.** For every item at every site it computes a live balance (FEFO stock + in transit
  − 14-day forecast − safety buffer), how many days the stock lasts against when supply can arrive,
  network-wide shortages, demand spikes, overstock and stock projected to expire unsold.
- **Plans like an expert planner.** It audits the network first, takes earliest-expiry stock and
  only the quantity the destination can sell before it expires, and prefers donors whose stock would
  otherwise go to waste. Whatever the network can't cover goes to the compliant supplier with the
  lowest landed cost, or the fastest one when cover is too short to wait.
- **Handles import and export.** Imports add customs clearance to the lead time and duty and freight
  to the landed cost, need a valid permit on the arrival date, and are screened for trade
  restrictions. Moves between sites in different countries are treated as cross-border movements.
- **Enforces guardrails in code.** Cold-chain, frozen, dangerous-goods, controlled-substance, halal
  and high-value handling; transit-time limits; storage capacity; supplier compliance and
  certification; value, price-premium and order-cover limits; a daily budget for autonomous
  execution. Every check is shown with its result and the SOP behind it.
- **Keeps the right human in the loop.** Each sign-off reason names the roles allowed to approve it,
  and the server re-checks guardrails and roles at execution, so a pharmacist cannot approve what
  needs the chief pharmacist.
- **Runs the operation around the plan.** Lot recalls that reach stock still on the road, temperature
  logger checks on receipt, QA release or disposal with write-off, automatic quarantine of expired
  stock, and rebalancing transfers that save stock before it expires.
- **Learns from what happens.** Every receipt updates a supplier scorecard (on time, in full, delay).
  A supplier that keeps delivering late or short loses compliance and is blocked automatically, and
  its delays lengthen the lead time the planner assumes. Shipments that miss their ETA stop counting
  as cover, so the gap is re-planned instead of silently waited on.
- **Plans for what's coming.** A demand calendar (flu season, Ramadan, Hajj, promotions) shapes a
  day-by-day forecast, and safety stock is calculated from demand variability, the target service
  level and the supplier's real lead-time record instead of fixed numbers.
- **Shares trucks.** Every lane has a trip cost and a vehicle capacity; transfers going the same way
  on the same day ride together, and the saving is recorded.
- **Reports real numbers.** Stockouts avoided, waste prevented, spend, transport cost, load-sharing
  savings, write-offs and supplier performance over time, exportable as CSV or a PDF report.
- **Explains itself.** Claude explains each plan, writes shift handover briefings and answers
  questions through read-only tools over the live network, citing the policy it relied on. Without
  an API key, the rules engine still answers questions such as "Can insulin move from North to
  East?" by running the transfer guardrails.
- **Simulates disruptions.** Cold-chain failures, road and border closures, hub outages, supplier
  shortages, demand surges, customs delays and origin restrictions: switch one on and every open
  deficit is re-planned against it.

Two sample networks ship with it: Sanar Pharmacy (4 branches, 9 items including a biosimilar, an
imported glucose meter and flammable hand sanitizer) and Gulf Trade & Logistics (a bonded port
warehouse, two DCs and a free-zone hub across the border, trading power banks, TVs, frozen chicken,
olive oil, solvents and apparel).

## How we built it

A FastAPI service with a layered architecture:

- **Domain packs.** Vocabulary, handling requirements, approval roles, policy defaults, documents
  and a sample network per industry. A handling rule is a tag on an item plus a capability on
  sites, lanes and suppliers, so "frozen", "hazmat" or "halal" are data, not code.
- **Semantic layer.** One snapshot of the network, loaded in a fixed number of queries, from which
  every metric is computed in memory: a daily demand curve (baseline with past events divided out,
  times the calendar ahead), statistical safety stock, supplier scorecards, late shipments, truck
  sharing, cover, stockout timing, a FEFO waste simulation, sell-through capacity and signals.
- **Outcomes and reports.** Each action records what it achieved or cost (stockout avoided, waste
  prevented, spend, transport, savings), so reports, CSV exports and the PDF come from facts, not
  estimates.
- **Guardrails.** Every rule returns a structured result (code, pass or fail, detail, governing
  document). Thresholds are bounded, editable by authorised roles and logged.
- **Planning engine.** Deterministic alerts, network audit, sourcing, execution, receipts, recalls,
  quarantine, rebalancing and autonomous execution of routine work.
- **Retrieval.** BM25 over shared policies plus the active industry's SOPs.
- **Agent.** A Claude tool-use loop (Claude Opus 5.5) whose tools are read-only: inventory, plans,
  what-if transfer checks, expiry risk, lots, policy search and activity. It uses prompt caching and
  the server-side refusal fallback, and falls back to the rules engine if a call fails.
- **Console.** A single-page approval console that adapts its labels and roles to the active industry.

The key design decision: **quantities, compliance and approvals are computed in code, never by the
model.** The agent explains and answers; it cannot move stock or approve anything.

## Challenges we ran into

- **Keeping the model out of the math.** Drawing a hard line, where code owns every number and every
  rule and the model only explains through read-only tools, was the central architectural decision
  and what makes the tower trustworthy.
- **Making it industry-neutral without making it vague.** Pharmacy rules had to become general
  concepts (capabilities, lots, lanes, permits) without losing their precision. The test was building
  a second, very different network on the same engine with no special cases.
- **Expiry-aware planning.** Moving short-dated stock can simply move the waste to another site. We
  simulate FEFO consumption at the destination so a transfer never exceeds what it can sell in time.
- **Guardrails that interact.** Cold chain, transit time, customs dwell, permits, capacity and
  sign-off all meet in one plan; each check had to say clearly why it passed or failed.
- **Efficiency.** Planning every alert on every refresh had to stay cheap, so all reads come from a
  single snapshot; a test asserts the query count does not grow with the number of alerts.

## Accomplishments that we're proud of

- A real **sense → plan → act → operate** loop, from deficit to approved transfer, receipt, recall
  and write-off.
- **Two industries on one engine**, plus a test that defines a third (automotive spare parts) inline
  and plans it with no engine changes.
- **Import and export done properly**: customs time, duty, freight, Incoterms, permits valid on the
  arrival date, restricted origins and cross-border approvals.
- **Compliance you can audit**: every plan shows each guardrail, its result and its document, and the
  right role must approve.
- **Waste prevention** that proposes rebalancing before stock expires instead of reporting it after.
- **A closed learning loop**: in the demo, receiving one late, short delivery drops a supplier below
  the compliance minimum and the tower stops ordering from it, with the reason on screen.
- It **runs anywhere, with or without an API key**, and ships with 64 tests.

## What we learned

- For high-stakes operations, the right division of labour is **code for decisions, LLM for
  language**. The model earns trust by not being in the critical path of the numbers.
- **Generalising pays off when the concepts are right.** Once a handling rule became "a tag that
  needs a capability", a frozen-food or dangerous-goods network fell out of the same code.
- A single **semantic snapshot** made the system both faster and more consistent: every view agrees
  because every view reads the same numbers.
- **Retrieval beats recall.** Grounding answers in retrieved SOPs, with citations, is far safer than
  hoping a model remembers a policy.

## What's next for Sanar Control Tower

- **Real connectors** to ERP, WMS, TMS, POS and customs systems, and a production database.
- **More industry packs**: chilled food, automotive spare parts, chemicals and e-commerce fulfilment.
- **Learned forecasting**: estimate event uplifts and seasonality from history instead of entering
  factors by hand, behind the same demand-curve interface.
- **Authentication and audit** so roles and sign-offs are tied to real identities.
- **Carrier and booking integration** so approved transfers and imports become real bookings.
- **Validation** of every rule by pharmacists, QA, trade compliance and regulatory teams before any
  real stock moves.

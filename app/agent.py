"""LLM orchestration: Claude explains plans, writes shift briefings and answers questions.

The agent works through read-only tools over the live network (inventory, plans, transfer checks,
expiry risk, lots, policy search), so every figure it quotes comes from the semantic layer and the
guardrails. It cannot execute, approve or move anything. Without an API key, explanations and
answers fall back to the rules engine and retrieved SOP excerpts.
"""
import json
import os
import re
from collections import OrderedDict

from . import planner, rag
from .semantic import Network

_client_instance = None
_explained = OrderedDict()  # plan fingerprint -> rationale, so re-opening a plan costs nothing


def model():
    return os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")


def effort():
    return os.environ.get("ANTHROPIC_EFFORT", "medium")


def llm_enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def status():
    return {"enabled": llm_enabled(), "model": model(), "effort": effort()}


def _client():
    global _client_instance
    if _client_instance is None:
        import anthropic
        _client_instance = anthropic.Anthropic()
    return _client_instance


def _system(d):
    return (f"You are the supply chain control tower agent for {d.title} ({d.tagline}). You answer questions from "
            f"{', '.join(d.roles.values()).lower()} about stock, risks, guardrails and policy.\n\n"
            "Ground every figure in a tool result or in the context of the message; never invent stock levels, lots, "
            "suppliers, prices or rules. If the tools cannot answer, say so.\n"
            "Quantities, compliance checks and approvals are decided by the planning engine. You explain them; you "
            "cannot execute, approve or move anything. When asked to act, say what the engine recommends and which "
            "role must approve it in the console.\n"
            "Cite policies by file name in square brackets, for example [sop_fefo_transfers.md].\n"
            f"Write briefly for an operations audience, using this business's terms: {d.labels['item'].lower()}, "
            f"{d.labels['site'].lower()}, {d.labels['lot'].lower()}.")


TOOLS = [
    {"name": "network_overview",
     "description": "Current KPIs, open deficit alerts (severity, shortfall, planned resolution, sign-off), network "
                    "signals and active disruption scenarios. Use it for broad questions about risks or priorities.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "item_inventory",
     "description": "Per-site stock position for one item: FEFO stock, in transit, 14-day forecast, safety buffer, "
                    "balance (negative = deficit), days on hand, days to stockout, earliest expiry, units held in "
                    "quarantine and projected expiry waste.",
     "input_schema": {"type": "object", "properties": {
         "item": {"type": "string", "description": "Item id or name, e.g. 'INS' or 'insulin'"}}, "required": ["item"]}},
    {"name": "resolution_plan",
     "description": "The planning engine's resolution for a deficit at one site: donor audit, transfers with lots, "
                    "supplier comparison (imports, customs days, duty, landed cost), failed guardrails, stockout "
                    "timeline, alternatives and required sign-off. Returns no_deficit when the site is covered.",
     "input_schema": {"type": "object", "properties": {"item": {"type": "string"}, "site": {"type": "string"}},
                      "required": ["item", "site"]}},
    {"name": "check_transfer",
     "description": "What-if check: may an item move from one site to another right now, and how much? Runs every "
                    "guardrail (lane, handling capabilities, temperature transit time, border and permits, capacity), "
                    "the donor's spare stock and the destination's ability to use it before expiry. Read-only.",
     "input_schema": {"type": "object", "properties": {
         "item": {"type": "string"}, "from_site": {"type": "string"}, "to_site": {"type": "string"},
         "qty": {"type": "integer", "description": "Optional quantity to test"}},
         "required": ["item", "from_site", "to_site"]}},
    {"name": "expiry_risk",
     "description": "Lots projected to expire unsold with their value, and the redistribution transfers proposed to "
                    "save them.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "lots",
     "description": "Lot-level stock for an item, optionally at one site: quantity, expiry, status (available, "
                    "quarantined, recalled, expired) and notes.",
     "input_schema": {"type": "object", "properties": {"item": {"type": "string"}, "site": {"type": "string"}},
                      "required": ["item"]}},
    {"name": "search_policy",
     "description": "Search the SOPs, policies and supplier agreements. Returns passages with their file names for "
                    "citation.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "recent_activity",
     "description": "Latest activity log entries: executed actions, receipts, recalls, holds and scenario changes.",
     "input_schema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "shipments",
     "description": "Open transfers and supplier orders with ETA, how many days late each is, and whether it is "
                    "still counted as cover (very late shipments are not, so their deficits are re-planned).",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "supplier_scorecards",
     "description": "Delivery performance per supplier from actual receipts: on-time, in-full and on-time-in-full "
                    "rates, average delay, lead-time spread, contract versus effective compliance (below the minimum "
                    "means blocked), monthly trend and spend.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "performance_report",
     "description": "What the tower avoided, saved and spent: stockouts avoided, waste prevented, procurement spend, "
                    "transport cost, load-sharing savings, write-offs and the share of auto-executed actions, for a "
                    "look-back window and since the demo started.",
     "input_schema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "Look-back window in days, 7 to 365 (default 30)"}}}},
    {"name": "demand_calendar",
     "description": "Seasons, holidays and promotions in the demand calendar: dates, demand factor, which items and "
                    "sites they lift, and whether they fall inside the 14-day forecast horizon.",
     "input_schema": {"type": "object", "properties": {}}},
]


# ---- resolving names in tool input and in free text -----------------------------------------------
_STOP = {"the", "and", "for", "from", "with", "into", "branch", "site", "facility", "warehouse", "mg", "ml",
         "export", "import"}


def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP and not w.isdigit()]


def _mentions(text, names, by_position=False):
    """Keys whose id (3+ characters, whole word) or name words (word starts, so plurals match) appear
    in `text`: best match first, or in order of first mention when `by_position`."""
    low, found = text.lower(), []
    for key, name in names.items():
        patterns = [rf"\b{re.escape(w)}" for w in set(_words(name))]
        if len(key) > 2:
            patterns.append(rf"\b{re.escape(key.lower())}\b")
        pos = [m.start() for p in patterns for m in re.finditer(p, low)]
        if pos:
            hits = sum(1 for p in patterns if re.search(p, low)) / len(patterns)
            found.append((min(pos), -hits, key) if by_position else (-hits, min(pos), key))
    return [k for *_, k in sorted(found)]


def _plural(word):
    return word + ("es" if word.endswith(("s", "x", "ch", "sh")) else "s")


HOW_IT_WORKS = (
    "The tower first looks for surplus elsewhere in the network: a site can only give what it has beyond its own "
    "forecast and safety buffer, earliest-expiry lots leave first, and only as much as the receiving site can use "
    "before expiry. An internal transfer is preferred over a purchase order because transit takes hours and supplier "
    "lead time takes days [sop_fefo_transfers.md]. Whatever the network cannot cover is ordered from the compliant "
    "supplier with the lowest landed cost, or the fastest one when stock would run out first [policy_procurement.md]. "
    "Every handling, trade and approval rule is checked in code before anything moves, and actions above the limits "
    "wait for the right role to sign off.")


def _resolve(net, text, kind):
    names = ({k: v["name"] for k, v in net.items.items()} if kind == "item"
             else {k: v["name"] for k, v in net.sites.items()})
    text = (text or "").strip()
    found = None
    if text:
        exact = next((k for k in names if text.lower() in (k.lower(), names[k].lower())), None)
        found = exact or next(iter(_mentions(text, names)), None)
    if not found:
        raise ValueError(f"Unknown {kind} '{text}'. Known: " + ", ".join(f"{k} ({v})" for k, v in names.items()))
    return found


# ---- tools ------------------------------------------------------------------------------------------
def _brief(p):
    """A plan without the bulky parts, for the model."""
    out = {k: p[k] for k in ("sku_name", "site_name", "need", "severity", "po", "unmet", "timeline", "stockout_risk",
                             "signoff", "alternatives", "value", "currency")}
    out["metrics"] = {k: p["metrics"][k] for k in ("fefo_stock", "in_transit", "forecast", "safety", "balance",
                                                   "stockout_days", "on_hand")}
    out["transfers"] = [{k: t[k] for k in ("from_name", "qty", "hours", "cross_border", "lines")} for t in p["transfers"]]
    out["donors"] = [{k: a[k] for k in ("site_name", "surplus", "hours", "result")} for a in p["audit"]]
    out["suppliers"] = [{k: o[k] for k in ("supplier", "country_name", "import", "lead_days", "unit_cost", "note")}
                        for o in p["sourcing"]]
    out["failed_checks"] = [c for c in p["checks"] if not c["ok"]]
    return out


def _overview(net):
    al = planner.alerts(net)
    keep = ("sku", "sku_name", "site", "site_name", "severity", "need", "resolution", "signoff", "stockout_risk",
            "stockout_days")
    return {"kpis": planner.kpis(net, al), "alerts": [{k: a[k] for k in keep} for a in al[:15]],
            "signals": [{"title": s["title"], "detail": s["detail"]} for s in net.signals()],
            "active_scenarios": [s["label"] for s in net.scenarios]}


def _report_brief(net, days):
    rep = planner.report(net, max(7, min(int(days or 30), 365)))
    return {k: rep[k] for k in ("days", "from", "to", "currency", "totals", "since_start", "actions", "auto_actions",
                                "auto_share_pct")} | {
        "spend_by_period": planner.buckets(rep["series"], "spend"),
        "saved_by_period": planner.buckets([{"day": s["day"], "v": s["waste_saved"] + s["transport_saved"]}
                                            for s in rep["series"]], "v")}


def run_tool(net, name, args):
    """Execute one read-only tool. Returns (result, is_error)."""
    try:
        if name == "network_overview":
            return _overview(net), False
        if name == "item_inventory":
            sku = _resolve(net, args.get("item"), "item")
            return {"item": net.items[sku]["name"], "sites": [{"site": s["name"], **net.metrics(sku, sid)}
                                                              for sid, s in net.sites.items()]}, False
        if name == "resolution_plan":
            sku, site = _resolve(net, args.get("item"), "item"), _resolve(net, args.get("site"), "site")
            p = planner.plan(net, sku, site)
            return (_brief(p) if p else {"no_deficit": True, "metrics": net.metrics(sku, site)}), False
        if name == "check_transfer":
            sku = _resolve(net, args.get("item"), "item")
            src, dst = _resolve(net, args.get("from_site"), "site"), _resolve(net, args.get("to_site"), "site")
            qty = args.get("qty")
            return planner.check_transfer(net, sku, src, dst, int(qty) if qty else None), False
        if name == "expiry_risk":
            return {"watch": planner.expiry_watch(net), "proposals": planner.rebalance(net)}, False
        if name == "lots":
            sku = _resolve(net, args.get("item"), "item")
            site = _resolve(net, args["site"], "site") if args.get("site") else None
            return planner.lots_view(net, sku, site), False
        if name == "search_policy":
            return rag.search(str(args.get("query", "")), k=4, domain=net.domain.name), False
        if name == "recent_activity":
            limit = max(1, min(int(args.get("limit") or 15), 50))
            return [dict(r) for r in net.conn.execute("SELECT ts, msg FROM events ORDER BY id DESC LIMIT ?",
                                                      (limit,))], False
        if name == "shipments":
            return planner.in_transit(net), False
        if name == "supplier_scorecards":
            return planner.scorecards(net), False
        if name == "performance_report":
            return _report_brief(net, args.get("days")), False
        if name == "demand_calendar":
            return planner.calendar_view(net), False
        return {"error": f"Unknown tool '{name}'"}, True
    except (ValueError, TypeError) as e:
        return {"error": str(e)}, True


# ---- model calls ----------------------------------------------------------------------------------
def _call(**kw):
    """One Messages API call with the server-side refusal fallback. Returns (response, error)."""
    import anthropic
    kw.update(model=model(), output_config={"effort": effort()}, cache_control={"type": "ephemeral"})
    try:
        try:
            return _client().beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                                                  **kw), None
        except anthropic.BadRequestError as e:
            if "fallback" not in str(e).lower():
                raise
            return _client().messages.create(**kw), None  # account without the fallback beta
    except anthropic.AuthenticationError:
        return None, "the API key was rejected"
    except anthropic.PermissionDeniedError:
        return None, "the API key may not use this model"
    except anthropic.NotFoundError:
        return None, f"model {model()} was not found"
    except anthropic.RateLimitError:
        return None, "rate limited, try again shortly"
    except anthropic.APIStatusError as e:
        return None, f"API error {e.status_code}"
    except anthropic.APIConnectionError:
        return None, "could not reach the API"


def _echo(content):
    """Blocks to send back next turn. After a mid-output fallback, the declined model's thinking and
    tool calls before the last fallback marker are dropped; text and everything after it are kept."""
    marks = [i for i, b in enumerate(content) if b.type == "fallback"]
    if not marks:
        return list(content)
    return [b for i, b in enumerate(content) if i > marks[-1] or (i < marks[-1] and b.type == "text")]


def _loop(net, messages, tools=True, max_turns=8):
    """Run the tool loop until Claude answers. Returns (text, tools used, policy passages, error)."""
    used, sources = [], []
    kw = {"max_tokens": 16000, "system": _system(net.domain), "messages": messages}
    if tools:
        kw["tools"] = TOOLS
    for _ in range(max_turns):
        r, err = _call(**kw)
        if err:
            return None, used, sources, err
        content = _echo(r.content)
        if r.stop_reason in ("tool_use", "pause_turn"):
            messages.append({"role": "assistant", "content": content})
            if r.stop_reason == "pause_turn":
                continue
            results = []
            for b in content:
                if b.type == "tool_use":
                    out, bad = run_tool(net, b.name, b.input or {})
                    used.append({"tool": b.name, "input": b.input})
                    if b.name == "search_policy" and not bad:
                        sources += out
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, default=str)[:30000], **({"is_error": True} if bad else {})})
            messages.append({"role": "user", "content": results})
            continue
        text = "".join(b.text for b in content if b.type == "text").strip()
        if r.stop_reason == "refusal":
            text = text or "The model declined to answer this request."
        elif r.stop_reason == "max_tokens":
            text += " …"
        return text, used, sources, None
    return "That needed too many lookups; please ask a narrower question.", used, sources, None


def _context(passages):
    return "\n\n".join(f"[{p['source']}] {p['text']}" for p in passages)


def _unique(passages):
    seen, out = set(), []
    for p in passages:
        if (p["source"], p["text"]) not in seen:
            seen.add((p["source"], p["text"]))
            out.append(p)
    return out


# ---- explain, answer, brief -----------------------------------------------------------------------
def _roles(net, roles):
    return " or ".join(net.domain.roles.get(r, r) for r in roles)


def _rule_explain(net, p):
    cur, parts = p["currency"], []
    if p["transfers"]:
        parts.append("Internal surplus covers " + ", ".join(
            f"{t['qty']:,} units from {t['from_name']} in {planner.duration(t['hours'])}" for t in p["transfers"])
            + ", earliest-expiry lots first.")
    blocked = [a for a in p["audit"] if a["result"].startswith("Blocked")]
    if blocked:
        parts.append("Not used: " + "; ".join(f"{a['site_name']} ({a['result'][9:]})" for a in blocked) + ".")
    if po := p["po"]:
        trade = f", incl. {po['customs_days']} days customs" if po["kind"] == "import" else ""
        parts.append(f"The remaining {po['qty']:,} units are {'imported' if po['kind'] == 'import' else 'ordered'} "
                     f"from {po['supplier']} ({po['lead_days']} days{trade}, {cur} {po['unit_cost']:,.2f} landed per "
                     f"unit): {po['why']}.")
    if p["unmet"]:
        parts.append(f"{p['unmet']:,} units have no compliant source and must be escalated.")
    if p["stockout_risk"]:
        alts = ", ".join(a["name"] for a in p["alternatives"])
        parts.append(f"Stock lasts about {p['timeline']['coverage_days']} days, less than supply needs to arrive"
                     + (f"; alternatives to review: {alts}" if alts else "") + ".")
    if p["signoff"]:
        parts.append(f"Sign-off required ({', '.join(p['signoff'])}): {_roles(net, p['signoff_roles'])}.")
    return " ".join(parts)


def explain_plan(net, p):
    terms = ["transfer FEFO surplus shelf life approval"] + [b["label"] for b in p["badges"]]
    if p["po"]:
        terms.append("supplier lead time compliance" + (" import customs permit duty" if p["po"]["kind"] == "import" else ""))
    passages = rag.search(" ".join(terms), k=4, domain=net.domain.name)
    if not llm_enabled():
        return {"text": _rule_explain(net, p) + " (No API key set, so this summary is rule-based.)",
                "sources": passages, "llm": False}
    key = json.dumps([net.domain.name, _brief(p)], sort_keys=True, default=str)
    if key not in _explained:
        text, _, _, err = _loop(net, [{"role": "user", "content":
                                       "Explain in 3-5 sentences why this resolution plan is right and what the "
                                       "approver should check.\n\nPLAN (computed by the planning engine):\n"
                                       + json.dumps(_brief(p), default=str) + "\n\nPOLICY EXCERPTS:\n"
                                       + _context(passages)}], tools=False)
        if err:
            return {"text": _rule_explain(net, p) + f" (Model call failed: {err}.)", "sources": passages, "llm": False}
        _explained[key] = text
        while len(_explained) > 128:
            _explained.popitem(last=False)
    return {"text": _explained[key], "sources": passages, "llm": True}


def _rule_answer(net, question):
    """Answer common questions from the rules engine when no model is available."""
    low, lab = question.lower(), net.domain.labels
    items = _mentions(question, {k: v["name"] for k, v in net.items.items()})
    sites = _mentions(question, {k: v["name"] for k, v in net.sites.items()}, by_position=True)
    if items and len(sites) >= 2 and re.search(r"\b(move|moved|transfer|send|ship|shift)", low):
        r = planner.check_transfer(net, items[0], sites[0], sites[1])
        failed = [c for c in r["checks"] if not c["ok"]]
        if r["allowed"]:
            return (f"Yes. {r['from_name']} can send up to {r['max_qty']:,} units of {r['item']} to {r['to_name']} now; "
                    "every guardrail passes." + (f" Sign-off needed: {', '.join(r['signoff'])}." if r["signoff"] else ""))
        return (f"Not now. Moving {r['item']} from {r['from_name']} to {r['to_name']} is blocked: "
                + "; ".join(c["detail"] + (f" [{c['ref']}]" if c["ref"] else "") for c in failed) + ".")
    if not items and re.search(r"^(can|may|must|is it|are we|do we|does|what does|what is the (rule|policy))\b|"
                               r"\b(allowed|permitted|rules?|policy|policies)\b", low) \
            and not re.search(r"\b(show|list|which|how many|give)\b", low):
        return None  # a question about the rules: answered from the policy documents
    if not items and re.search(r"how (does|do|is|are) .*(plan|work|decid|choos)|\bwhy\b.*(transfer|order|move)|"
                               r"instead of (order|buy)", low):
        return HOW_IT_WORKS
    if re.search(r"expir|waste|rebalanc", low):
        props = planner.rebalance(net)
        watch = planner.expiry_watch(net)
        lines = [f"{len(watch)} {_plural(lab['lot'].lower())} are on the expiry watch."]
        lines += [f"Move {r['qty']:,} × {r['sku_name']} ({r['lot_no']}) {r['from_name']} → {r['to_name']} to save "
                  f"{net.domain.currency} {r['value_saved']:,.0f}." for r in props[:3]]
        return " ".join(lines)
    if re.search(r"recall|quarantin", low):
        held = planner.quarantine(net)
        return (f"{len(held)} {_plural(lab['lot'].lower())} are held: " + "; ".join(
            f"{q['lot_no']} {q['sku_name']} at {q['site_name']} ({q['qty']:,} units, {q['status']})" for q in held[:6])
            + "." if held else "Nothing is in quarantine.")
    if re.search(r"supplier|scorecard|otif|on.time|vendor|reliab", low):
        cards = planner.scorecards(net)
        return "Supplier performance: " + "; ".join(
            f"{s['supplier']} {s['otif_pct']:g}% on time in full over {s['deliveries']} deliveries, compliance "
            f"{s['compliance']:g}%" + (" (blocked)" if s["compliance"] < s["minimum"] else "")
            for s in cards if s["deliveries"]) + "."
    if re.search(r"\blate\b|overdue|delay|\beta\b", low):
        late = [a for a in planner.in_transit(net) if a["late_days"] > 0]
        return ("Late shipments: " + "; ".join(
            f"{a['ref']} {a['sku_name']} from {a['from_name']} to {a['to_name']}, {a['late_days']:g} days past ETA"
            + (" (not counted as cover, re-planned)" if a["excluded"] else "") for a in late) + "."
            if late else "Nothing is past its ETA.")
    if re.search(r"sav(e|ed|ing)|spend|spent|report|avoid", low):
        rep, cur = planner.report(net, 30), net.domain.currency
        since = rep["since_start"]
        got = lambda k: since.get(k, {"count": 0, "value": 0})  # noqa: E731
        return (f"Since the demo started: {got('stockout_avoided')['count']} stockouts avoided, {cur} "
                f"{got('waste_saved')['value']:,.0f} of waste prevented, {cur} {got('transport_saved')['value']:,.0f} "
                f"saved by sharing trucks, {cur} {got('spend')['value']:,.0f} spent on orders. Last 30 days including "
                f"sample history: {cur} {rep['totals']['spend']:,.0f} procurement spend.")
    if text := _rule_explain_question(net, question, items, sites):
        return text
    if items:
        sku = items[0]
        rows = [(sid, net.metrics(sku, sid)) for sid in (sites or net.sites) if (sku, sid) in net.positions]
        parts = [f"{net.sites[sid]['name']}: {m['fefo_stock']:,} usable, {m['in_transit']:,} in transit, "
                 + (f"short {-m['balance']:,}" if m["balance"] < 0 else f"surplus {m['balance']:,}") for sid, m in rows]
        text = f"{net.items[sku]['name']}: " + "; ".join(parts) + "."
        for sid, m in rows:
            if m["balance"] < 0 and (p := planner.plan(net, sku, sid)):
                text += f" Plan for {p['site_name']}: {planner.resolution(p)}."
        return text
    if re.search(r"calendar|season|ramadan|hajj|promotion|holiday|flu|event", low):
        cal = planner.calendar_view(net)
        return "Demand calendar: " + "; ".join(
            f"{e['name']} ({e['kind']}) {e['start']} to {e['end']}, ×{e['factor']:g}, "
            + ("under way" if e["status"] == "active" else f"starts in {e['starts_in']} d" if e["status"] == "upcoming"
               else "past") for e in cal) + "." if cal else "The demand calendar is empty."
    if re.search(r"alert|deficit|short|risk|critical|urgent|priorit|what should|to.do|overview|summar|status|brief", low):
        return _rule_briefing(net, planner.briefing_facts(net))
    return None


def _safety_text(net, sku, site):
    m, d = net.metrics(sku, site), net.metrics(sku, site)["safety_detail"]
    name = net.sites[site]["name"]
    if d["mode"] != "smart":
        return f"{name}: fixed buffer of {m['safety']:,}"
    return (f"{name}: {m['safety']:,} units for {d['service_level']:g}% service (demand varies ±{d['demand_sd']:g}/day, "
            f"{d['lead_days']:g} d to replenish" + (f" from {d['source']}" if d["source"] else "")
            + f", lead-time spread {d['lead_sd']:g} d; the fixed buffer was {d['fixed']:,})")


def _rule_explain_question(net, question, items, sites):
    """Safety stock, forecast and 'why this plan' questions about the live network."""
    low = question.lower()
    sku = items[0] if items else None
    held = [s for s in (sites or net.sites) if sku and (sku, s) in net.positions and net.metrics(sku, s)["forecast"]]
    if re.search(r"safety|buffer|service level", low):
        if not sku:
            example = next(((a["sku"], a["site"]) for a in planner.alerts(net)), None)
            return ("Safety stock is z × √(lead time × demand variance + demand² × lead-time variance): z comes from the "
                    "service level (95%, or 99% for critical items), demand variance from the last 28 days with calendar "
                    "effects removed, and lead time and its spread from the fastest compliant supplier's deliveries "
                    "[policy_planning.md]." + (f" For example, {net.items[example[0]]['name']} at "
                                               + _safety_text(net, *example) + "." if example else ""))
        return f"Safety stock for {net.items[sku]['name']}: " + "; ".join(_safety_text(net, sku, s) for s in held) + "."
    if sku and re.search(r"forecast|demand|expect|sell", low):
        parts = []
        for s in held:
            m = net.metrics(sku, s)
            lift = ", ".join(f"{u['name']} ×{u['factor']:g} from day {u['from_day']}" for u in m["uplifts"])
            parts.append(f"{net.sites[s]['name']}: {m['forecast']:,} over the next 14 days (baseline {m['base_forecast']:,}"
                         + (f"; {lift}" if lift else "") + ")")
        return f"Forecast for {net.items[sku]['name']}: " + "; ".join(parts) + "."
    if sku and re.search(r"\bwhy\b|how come|reason|decid|choos|instead", low):
        site = next((s for s in (sites or net.sites) if (sku, s) in net.positions and net.metrics(sku, s)["balance"] < 0),
                    None)
        if not site:
            return f"{net.items[sku]['name']} has no deficit at {net.sites[sites[0]]['name'] if sites else 'any site'}."
        return f"{net.items[sku]['name']} at {net.sites[site]['name']}: " + _rule_explain(net, planner.plan(net, sku, site))
    return None


def _situation(net):
    al = planner.alerts(net)
    return {"kpis": planner.kpis(net, al), "scenarios": [s["label"] for s in net.scenarios],
            "alerts": [f"{a['severity']}: {a['sku_name']} at {a['site_name']}, short {a['need']:,}; plan: "
                       f"{a['resolution']}" + (f"; sign-off: {', '.join(a['signoff'])}" if a["signoff"] else "")
                       for a in al[:12]]}


def answer(conn, question):
    net = Network(conn)
    passages = rag.search(question, k=3, domain=net.domain.name)
    if not llm_enabled():
        if text := _rule_answer(net, question):
            text += " (Answered by the rules engine from live data; no AI model is connected.)"
        elif hits := rag.sentences(question, passages, 2, net.domain.name):
            text = (" ".join(f"{h['text']} [{h['source']}]" for h in hits)
                    + " (Quoted from the policy documents; no AI model is connected.)")
        else:
            text = ("Nothing in the live network or the policies matched that question. Try asking about an item, a "
                    "site, suppliers, late shipments, safety stock, the calendar or what to do first.")
        return {"text": text, "sources": passages, "llm": False, "tools": []}
    messages = [{"role": "user", "content": f"QUESTION: {question}\n\nSITUATION NOW:\n{json.dumps(_situation(net))}"
                                            f"\n\nPOLICY EXCERPTS:\n{_context(passages)}"}]
    text, used, found, err = _loop(net, messages)
    if err:
        text = (_rule_answer(net, question) or "See the policy excerpts below.") + f" (Model call failed: {err}.)"
    return {"text": text, "sources": _unique(passages + found)[:6], "llm": not err, "tools": used}


def _rule_briefing(net, f):
    k, cur = f["kpis"], f["currency"]
    lines = [f"{k['open_deficits']} open deficits, {k['stockout_risks']} at risk of stockout, {k['need_signoff']} "
             f"waiting for sign-off."]
    lines += [f"• {a['severity']}: {a['sku_name']} at {a['site_name']} short {a['need']:,} → {a['resolution']}"
              + (f" (sign-off: {', '.join(a['signoff'])})" if a["signoff"] else "") for a in f["top_alerts"][:4]]
    lines += [f"• {s['title']}: {s['detail']}" for s in f["signals"][:4]]
    lines += [f"• Rebalance {r['qty']:,} × {r['sku_name']} {r['from_name']} → {r['to_name']} to save {cur} "
              f"{r['value_saved']:,.0f}" for r in f["rebalance"][:2]]
    if k["held_units"]:
        lines.append(f"• {k['held_units']:,} units held in quarantine awaiting QA disposition.")
    return "\n".join(lines)


def briefing(conn):
    net = Network(conn)
    facts = planner.briefing_facts(net)
    if llm_enabled():
        text, _, _, err = _loop(net, [{"role": "user", "content":
                                       "Write a shift handover briefing from these facts: the top risks now, the "
                                       "actions to take and which role must approve them, and what to watch. Under 180 "
                                       "words, short bullets.\n\nFACTS:\n" + json.dumps(facts, default=str)}],
                                tools=False)
        if not err:
            return {"text": text, "llm": True}
    return {"text": _rule_briefing(net, facts), "llm": False}

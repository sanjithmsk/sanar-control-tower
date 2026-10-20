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
]


# ---- resolving names in tool input and in free text -----------------------------------------------
_STOP = {"the", "and", "for", "from", "with", "into", "branch", "site", "facility", "warehouse", "mg", "ml",
         "export", "import"}


def _words(text):
    return [w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2 and w not in _STOP]


def _mentions(text, names, by_position=False):
    """Keys whose id (3+ characters) or name words appear in `text`: best match first, or in order
    of first mention when `by_position`."""
    low, found = text.lower(), []
    for key, name in names.items():
        words = set(_words(name)) | ({key.lower()} if len(key) > 2 else set())
        pos = [m.start() for w in words for m in re.finditer(rf"\b{re.escape(w)}", low)]
        if pos:
            hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}", low))
            found.append((min(pos), -hits / len(words), key) if by_position else (-hits / len(words), min(pos), key))
    return [k for *_, k in sorted(found)]


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
    if re.search(r"expir|waste|rebalanc", low):
        props = planner.rebalance(net)
        watch = planner.expiry_watch(net)
        lines = [f"{len(watch)} {lab['lot'].lower()}s are on the expiry watch."]
        lines += [f"Move {r['qty']:,} × {r['sku_name']} ({r['lot_no']}) {r['from_name']} → {r['to_name']} to save "
                  f"{net.domain.currency} {r['value_saved']:,.0f}." for r in props[:3]]
        return " ".join(lines)
    if re.search(r"recall|quarantin", low):
        held = planner.quarantine(net)
        return (f"{len(held)} {lab['lot'].lower()}s are held: " + "; ".join(
            f"{q['lot_no']} {q['sku_name']} at {q['site_name']} ({q['qty']:,} units, {q['status']})" for q in held[:6])
            + "." if held else "Nothing is in quarantine.")
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
    if re.search(r"alert|deficit|short|risk|critical|urgent|priorit", low):
        al = planner.alerts(net)
        return (f"{len(al)} open deficits. " + " ".join(
            f"{a['severity']}: {a['sku_name']} at {a['site_name']}, short {a['need']:,} → {a['resolution']}."
            for a in al[:4])) if al else "No open deficits."
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
        text = _rule_answer(net, question)
        if text:
            text += " (No API key set: answered by the rules engine.)"
        else:
            text = ("No API key is set, so here are the most relevant policy excerpts instead of a written answer."
                    if passages else "No API key is set and nothing in the knowledge base matched the question.")
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

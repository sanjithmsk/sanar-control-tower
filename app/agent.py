"""LLM orchestration: Claude explains plans and answers questions, grounded in retrieved SOPs
and live figures from the semantic layer. Without an API key it falls back to retrieved text."""
import json
import os

from . import rag

SYSTEM = (
    "You are the supply chain control tower agent for Sanar Pharmacy. "
    "Use only the inventory figures and policy excerpts supplied in the message. "
    "Never invent stock levels, batch numbers, suppliers or rules. If the context does not answer "
    "the question, say so. Be brief and write for a pharmacist or logistics manager. "
    "Cite policy sources by file name in brackets."
)


def _ask(prompt, max_tokens=500):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5"),
            max_tokens=max_tokens, system=SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if b.type == "text")
    except Exception as e:  # the tower keeps working if the model call fails
        return f"(Model call failed: {e})"


def _context(passages):
    return "\n\n".join(f"[{p['source']}] {p['text']}" for p in passages)


def explain_plan(plan):
    query = " ".join(filter(None, [
        "transfer FEFO surplus shelf life", "cold chain reefer" if plan["cold"] else "",
        "controlled substance sign-off" if plan["controlled"] else "",
        "purchase order supplier lead time" if plan["po"] else "", "approval limit value"]))
    passages = rag.search(query, k=4)
    text = _ask(
        "Explain in 3-5 sentences why this resolution plan is correct and what the approver should check.\n\n"
        f"PLAN (computed by the planning engine):\n{json.dumps(plan, indent=1)}\n\n"
        f"POLICY EXCERPTS:\n{_context(passages)}")
    if text is None:
        parts = []
        if plan["transfers"]:
            parts.append("Internal surplus covers " + ", ".join(
                f"{t['qty']:,} units from {t['from_name']} in {t['hours']} h" for t in plan["transfers"]) + ".")
        if plan["po"]:
            parts.append(f"The remaining shortfall goes to {plan['po']['supplier']} "
                         f"({plan['po']['lead_days']} days lead time).")
        if plan["signoff"]:
            parts.append("Human sign-off is required: " + ", ".join(plan["signoff"]) + ".")
        text = " ".join(parts) + " (No API key set, so this summary is rule-based.)"
    return {"text": text, "sources": passages, "llm": bool(os.environ.get("ANTHROPIC_API_KEY"))}


def answer(question, snapshot):
    passages = rag.search(question, k=4)
    text = _ask(
        f"QUESTION: {question}\n\nLIVE INVENTORY SNAPSHOT:\n{json.dumps(snapshot)}\n\n"
        f"POLICY EXCERPTS:\n{_context(passages)}", max_tokens=700)
    if text is None:
        text = ("No API key is set, so here are the most relevant policy excerpts instead of a written answer."
                if passages else "No API key is set and nothing in the knowledge base matched the question.")
    return {"text": text, "sources": passages, "llm": bool(os.environ.get("ANTHROPIC_API_KEY"))}

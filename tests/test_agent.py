import os
from types import SimpleNamespace as NS

from app import agent, planner
from app.semantic import Network


def test_rule_engine_answers_transfer_questions(c):
    r = agent.answer(c, "Can insulin move from North to East?")
    assert not r["llm"] and r["text"].startswith("Not now") and "Cold chain 2–8 °C: lane not equipped" in r["text"]
    r = agent.answer(c, "Can we send insulin from North Branch to Central Retail?")
    spare = planner.check_transfer(Network(c), "INS", "B", "A")["max_qty"]
    assert r["text"].startswith(f"Yes. North Branch can send up to {spare} units")


def test_rule_engine_answers_stock_questions(c):
    r = agent.answer(c, "How is tramadol looking?")
    assert "Tramadol 50 mg" in r["text"] and "Plan for North Branch" in r["text"]
    assert agent.answer(c, "When is sign-off required?")["sources"]


def test_tools_resolve_names_and_report_errors(c):
    net = Network(c)
    out, bad = agent.run_tool(net, "check_transfer", {"item": "insulin", "from_site": "north", "to_site": "central"})
    assert not bad and out["allowed"] and out["max_qty"] > 0 and out["transport"]["trip_cost"] == 180
    out, bad = agent.run_tool(net, "item_inventory", {"item": "unobtainium"})
    assert bad and "Unknown item" in out["error"]
    out, bad = agent.run_tool(net, "resolution_plan", {"item": "PCM", "site": "Central Retail"})
    assert not bad and out["no_deficit"]


def test_rule_based_explanation_and_briefing(c):
    net = Network(c)
    assert "rule-based" in agent.explain_plan(net, planner.plan(net, "GLM", "C"))["text"]
    text = agent.briefing(c)["text"]
    assert text.startswith("7 open deficits") and "Rebalance 122 × Oseltamivir" in text and "Late: PO-" in text


def test_rule_engine_covers_suppliers_late_savings_and_calendar(c):
    assert "Supplier B (secondary) 87.5% on time in full" in agent.answer(c, "How reliable are our suppliers?")["text"]
    assert "days past ETA (not counted as cover, re-planned)" in agent.answer(c, "Which shipments are late?")["text"]
    assert agent.answer(c, "What have we saved so far?")["text"].startswith("Since the demo started: 0 stockouts")
    assert "Flu season (season)" in agent.answer(c, "What is on the demand calendar?")["text"]
    for tool in ("shipments", "supplier_scorecards", "performance_report", "demand_calendar"):
        out, bad = agent.run_tool(Network(c), tool, {})
        assert not bad and out


class FakeClient:
    """Stands in for anthropic.Anthropic: a tool call first, then a final answer."""

    def __init__(self):
        self.calls = []
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        if len(self.calls) == 1:
            return NS(stop_reason="tool_use", content=[
                NS(type="thinking", thinking="", signature="sig"),
                NS(type="tool_use", id="t1", name="check_transfer",
                   input={"item": "insulin", "from_site": "North Branch", "to_site": "East Branch"})])
        return NS(stop_reason="end_turn", content=[NS(type="text", text="No: no reefer on that lane [sop_cold_chain.md].")])


def test_tool_loop_with_model(c, monkeypatch):
    fake = FakeClient()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(agent, "_client", lambda: fake)
    r = agent.answer(c, "Can insulin move from North to East?")
    assert r["llm"] and r["text"].startswith("No: no reefer") and r["tools"][0]["tool"] == "check_transfer"
    first, second = fake.calls
    assert first["model"] == "claude-opus-5-5" and first["fallbacks"] == "default" and first["tools"] == agent.TOOLS
    assert first["output_config"] == {"effort": "medium"} and first["cache_control"] == {"type": "ephemeral"}
    user, assistant, results = second["messages"]  # history is append-only; thinking blocks are passed back
    assert [b.type for b in assistant["content"]] == ["thinking", "tool_use"]
    (result,) = results["content"]
    assert result["tool_use_id"] == "t1" and '"allowed": false' in result["content"]


def test_fallback_marker_drops_the_declined_turn():
    blocks = [NS(type="thinking"), NS(type="tool_use"), NS(type="text"), NS(type="fallback"), NS(type="text"),
              NS(type="tool_use")]
    assert [b.type for b in agent._echo(blocks)] == ["text", "text", "tool_use"]


def test_answers_without_a_model_are_written_not_excerpts(c):
    ask = lambda q: agent.answer(c, q)["text"]  # noqa: E731
    assert ask("Why does the tower transfer stock instead of ordering?").startswith("The tower first looks for surplus")
    assert ask("Can expired stock be released?").startswith("Recalled and expired lots can never be released. "
                                                            "[sop_recall_quarantine.md]")
    assert ask("What is the safety stock for insulin?").startswith("Safety stock for Insulin glargine 100 IU/mL")
    assert "Flu season ×1.3" in ask("What is the paracetamol forecast?")
    assert ask("hello").startswith("Nothing in the live network or the policies matched")


def test_item_ids_match_whole_words_only(c):
    items = {k: v["name"] for k, v in Network(c).items.items()}
    assert "INS" not in agent._mentions("transfer stock instead of ordering", items)  # "INS" is not "instead"
    assert agent._mentions("send INS today", items)[0] == "INS"


def test_env_file_fills_gaps_but_never_overrides(tmp_path, monkeypatch):
    from app import load_env
    env = tmp_path / ".env"
    env.write_text("# comment\nSANAR_T1='from file'\nSANAR_T2=from file\nSANAR_T3=\n", encoding="utf-8")
    monkeypatch.setenv("SANAR_T2", "real")
    monkeypatch.delenv("SANAR_T1", raising=False)
    monkeypatch.delenv("SANAR_DOTENV")
    load_env(env)
    assert (os.environ["SANAR_T1"], os.environ["SANAR_T2"]) == ("from file", "real")
    assert "SANAR_T3" not in os.environ
    monkeypatch.delenv("SANAR_T1")

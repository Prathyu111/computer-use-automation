"""Compiler turns a discovery trace into a parameterized capability."""

from cua.compiler import CompileError, compile_capability


def _trace():
    return [
        {"type": "goto", "url": "http://127.0.0.1:8765/", "intent": "open lookup"},
        {"type": "click", "name": "Accounts", "role": "button", "intent": "open accounts"},
        {"type": "click", "name": "Wrong menu", "role": "button", "intent": "explore"},
        {"type": "dismiss", "name": "OK", "role": "button", "intent": "dismiss notice"},
        {"type": "type", "name": "Member ID", "role": "textbox", "intent": "enter member", "value": "12345"},
        {"type": "click", "name": "Search", "role": "button", "intent": "Search"},
        {"type": "extract", "extract_to": "savingsBalance", "intent": "read savings"},
        {"type": "extract", "extract_to": "memberName", "intent": "read name"},
    ]


def test_parameterizes_member_id_and_keeps_search_handlers():
    cap = compile_capability(
        run_id="t1",
        model_id="test",
        entry="http://127.0.0.1:8765/",
        goal="Look up member 12345 and read savings",
        recorded_steps=_trace(),
        last_heading="Account summary",
        last_text="Savings 1840.22",
    )
    type_steps = [s for s in cap.steps if s.action == "clear_and_type"]
    assert type_steps[0].value_from == "$input.memberId"
    search = next(s for s in cap.steps if s.action == "click" and "Search" in s.target.intent)
    assert any(h.then.code == "member_not_found" for h in search.handlers)
    assert any(h.then.action == "recover" and h.then.recover == "dismiss" for h in search.handlers)
    assert any(
        h.then.action == "escalate" and h.when.kind == "dialog" and h.when.text == "Supervisor approval required"
        for h in search.handlers
    )
    assert any(h.then.code == "session_expired" for h in search.handlers)
    assert any(s.extract_to == "savingsBalance" for s in cap.steps)
    assert not any(s.target and s.target.intent == "explore" for s in cap.steps if s.target)
    assert any(s.target and "accounts" in s.target.intent.lower() for s in cap.steps if s.target)


def test_refuses_empty_trace():
    try:
        compile_capability(
            run_id="t2",
            model_id=None,
            entry="http://127.0.0.1:8765/",
            goal="x",
            recorded_steps=[],
        )
    except CompileError:
        return
    raise AssertionError("expected CompileError")


def _base_lookup_steps(*extracts: dict) -> list[dict]:
    return [
        {"type": "goto", "url": "http://127.0.0.1:8765/", "intent": "open lookup"},
        {"type": "dismiss", "name": "OK", "role": "button", "intent": "dismiss notice"},
        {"type": "type", "name": "Member ID", "role": "textbox", "intent": "enter member", "value": "12345"},
        {"type": "click", "name": "Search", "role": "button", "intent": "Search"},
        *extracts,
    ]


def test_collapses_consecutive_duplicate_extracts():
    cap = compile_capability(
        run_id="dup",
        model_id="test",
        entry="http://127.0.0.1:8765/",
        goal="Return the posted invoice total",
        recorded_steps=_base_lookup_steps(
            {
                "type": "extract",
                "extract_to": "invoiceTotal",
                "name": "Posted total",
                "intent": "read posted total",
            },
            {
                "type": "extract",
                "extract_to": "invoiceTotal",
                "name": "Posted total",
                "intent": "return posted total",
            },
        ),
        last_heading="Account summary",
        last_text="Posted total 10.00",
    )
    extracts = [s for s in cap.steps if s.action == "extract"]
    assert len(extracts) == 1
    assert extracts[0].extract_to == "invoiceTotal"


def test_collapses_consecutive_extracts_with_same_locator_identity():
    cap = compile_capability(
        run_id="loc",
        model_id="test",
        entry="http://127.0.0.1:8765/",
        goal="Read the posted total",
        recorded_steps=_base_lookup_steps(
            {
                "type": "extract",
                "extract_to": "posted_total",
                "name": "Posted total",
                "intent": "first read",
            },
            {
                "type": "extract",
                "extract_to": "current_posted_total",
                "name": "Posted total",
                "intent": "second read",
            },
        ),
        last_heading="Account summary",
    )
    extracts = [s for s in cap.steps if s.action == "extract"]
    assert len(extracts) == 1


def test_declared_outputs_match_extract_to_and_drop_undiscovered():
    cap = compile_capability(
        run_id="alias",
        model_id="test",
        entry="http://127.0.0.1:8765/",
        goal="Look up member 12345 and return the current savings balance",
        recorded_steps=_base_lookup_steps(
            {
                "type": "extract",
                "extract_to": "current_savings_balance",
                "name": "Primary",
                "intent": "read requested balance",
            },
            {
                "type": "extract",
                "extract_to": "current_savings_balance",
                "name": "Primary",
                "intent": "return requested balance",
            },
        ),
        last_heading="Account summary",
        last_text="Primary 1840.22",
    )
    extracts = [s for s in cap.steps if s.action == "extract"]
    assert len(extracts) == 1
    assert extracts[0].extract_to == "savingsBalance"
    output_names = [o.name for o in cap.outputs]
    assert output_names == ["savingsBalance"]
    produced = {s.extract_to for s in cap.steps if s.extract_to}
    assert {o.name for o in cap.outputs} <= produced
    assert "memberName" not in output_names
    for spec in cap.outputs:
        if spec.required:
            assert spec.name in produced


def test_description_omits_typed_discovery_input():
    cap = compile_capability(
        run_id="desc",
        model_id="test",
        entry="http://127.0.0.1:8765/",
        goal="Look up member 12345 and return the current savings balance",
        recorded_steps=_base_lookup_steps(
            {
                "type": "extract",
                "extract_to": "savingsBalance",
                "name": "Primary",
                "intent": "read balance",
            },
        ),
        last_heading="Account summary",
    )
    assert "12345" not in cap.description
    assert cap.description


def test_recompiles_recorded_trace_without_llm():
    cap = compile_capability(
        run_id="discover-f83a7518",
        model_id="gpt-4o-mini",
        entry="http://127.0.0.1:8765/",
        goal="Look up member 12345 and return the current savings balance",
        recorded_steps=[
            {
                "type": "dismiss",
                "name": "OK",
                "role": "button",
                "intent": "Dismiss the system notification dialog",
            },
            {
                "type": "type",
                "name": "Member ID",
                "role": "textbox",
                "intent": "Enter the member ID to look up",
                "value": "12345",
            },
            {
                "type": "click",
                "name": "Search",
                "role": "button",
                "intent": "Click the Search button to look up the member",
            },
            {
                "type": "extract",
                "extract_to": "current_savings_balance",
                "name": "Savings",
                "role": "textbox",
                "intent": "Extract the current savings balance",
            },
            {
                "type": "extract",
                "extract_to": "current_savings_balance",
                "name": "Savings",
                "role": "textbox",
                "intent": "Return the current savings balance",
            },
        ],
        last_heading="Account summary",
        last_text="Savings 1840.22",
    )
    extracts = [s for s in cap.steps if s.action == "extract"]
    assert len(extracts) == 1
    assert extracts[0].extract_to == "savingsBalance"
    assert [o.name for o in cap.outputs] == ["savingsBalance"]
    assert "12345" not in cap.description
    assert cap.provenance.model_id == "gpt-4o-mini"
    assert cap.provenance.discovery_run_id == "discover-f83a7518"
    assert cap.provenance.notes == "error_handlers: compiler_specialization"
    search = next(s for s in cap.steps if s.action == "click")
    assert any(h.then.action == "recover" for h in search.handlers)
    assert any(h.then.action == "escalate" and h.when.text == "Supervisor approval required" for h in search.handlers)
    assert any(s.id == "s2_dismiss" and s.action == "dismiss" for s in cap.steps)

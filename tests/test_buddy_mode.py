"""Buddy Mode regressions: fresh context, final review, and UI/request wiring."""

import asyncio
import json
from pathlib import Path

import src.agent_loop as agent_loop
import src.llm_core as llm_core


ROOT = Path(__file__).resolve().parents[1]


def _collect(gen):
    async def run():
        return [chunk async for chunk in gen]

    return asyncio.run(run())


def _events(chunks):
    events = []
    for chunk in chunks:
        if not chunk.startswith("data: ") or chunk.startswith("data: [DONE]"):
            continue
        try:
            events.append(json.loads(chunk[6:]))
        except json.JSONDecodeError:
            pass
    return events


def test_buddy_subagent_uses_clean_tool_free_context(monkeypatch):
    captured = {}

    async def fake_call(**kwargs):
        captured.update(kwargs)
        return "<think>private</think>Lead with the failing test, then fix the parser."

    monkeypatch.setattr(llm_core, "llm_call_async", fake_call)

    result = asyncio.run(
        agent_loop._run_buddy_subagent(
            "Fix the parser",
            "[bash] pytest\n-> one failure",
            "I think this is done.",
            endpoint_url="http://model.test/v1",
            model="test-model",
            headers={"Authorization": "secret"},
            guide_prompt="Be a skeptical staff engineer.",
            role="worker",
            available_tools="read_file, edit_file, bash",
        )
    )

    assert result == "Lead with the failing test, then fix the parser."
    assert captured["model"] == "test-model"
    assert captured["temperature"] == 0.1
    assert captured["max_tokens"] == 1200
    assert captured["chat_template_kwargs"] == {"enable_thinking": False}
    assert len(captured["messages"]) == 2
    assert [message["role"] for message in captured["messages"]] == [
        "system",
        "user",
    ]
    assert "skeptical staff engineer" in captured["messages"][0]["content"]
    assert "bounded implementation worker" in captured["messages"][0]["content"]
    snapshot = captured["messages"][1]["content"]
    assert "Fix the parser" in snapshot
    assert "one failure" in snapshot
    assert "I think this is done." in snapshot
    assert "read_file, edit_file, bash" in snapshot
    assert "tools" not in captured


def test_buddy_reviews_final_path_and_returns_control_to_primary(monkeypatch):
    monkeypatch.setattr(
        agent_loop, "get_setting", lambda key, default=None: default, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "get_mcp_manager", lambda: None, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "estimate_tokens", lambda *_args, **_kwargs: 10, raising=False
    )
    monkeypatch.setattr(agent_loop.random, "randint", lambda *_args: 10)

    primary_calls = []

    async def fake_stream(_candidates, messages, **kwargs):
        primary_calls.append(list(messages))
        text = "Candidate answer." if len(primary_calls) == 1 else "Improved final answer."
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"

    buddy_calls = []

    async def fake_buddy(*args, **kwargs):
        buddy_calls.append((args, kwargs))
        return "Check the missing edge case, then give a clean final answer."

    monkeypatch.setattr(
        agent_loop, "stream_llm_with_fallback", fake_stream, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "_run_buddy_subagent", fake_buddy, raising=False
    )

    chunks = _collect(
        agent_loop.stream_agent_loop(
            "http://model.test/v1",
            "test-model",
            [{"role": "user", "content": "Solve the edge case"}],
            max_rounds=2,
            relevant_tools={"bash"},
            buddy_enabled=True,
            buddy_prompt="Challenge assumptions.",
            buddy_endpoint_url="http://buddy.test/v1",
            buddy_model="ornith-9b",
            buddy_headers={"Authorization": "buddy-secret"},
            buddy_role="reviewer",
        )
    )
    events = _events(chunks)

    assert len(buddy_calls) == 1
    assert buddy_calls[0][1]["endpoint_url"] == "http://buddy.test/v1"
    assert buddy_calls[0][1]["model"] == "ornith-9b"
    assert buddy_calls[0][1]["headers"] == {"Authorization": "buddy-secret"}
    assert buddy_calls[0][1]["role"] == "reviewer"
    assert len(primary_calls) == 2
    assert any(
        event.get("type") == "buddy_start" and event.get("reason") == "final"
        for event in events
    )
    assert any(
        event.get("type") == "buddy_result" and event.get("success") is True
        for event in events
    )
    assert any(
        event.get("type") == "agent_step"
        and event.get("reason") == "buddy_guidance"
        for event in events
    )
    assert any(event.get("delta") == "Improved final answer." for event in events)
    assert chunks[-1] == "data: [DONE]\n\n"


def test_buddy_accepts_complete_final_without_forcing_another_round(monkeypatch):
    monkeypatch.setattr(
        agent_loop, "get_setting", lambda key, default=None: default, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "get_mcp_manager", lambda: None, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "estimate_tokens", lambda *_args, **_kwargs: 10, raising=False
    )

    primary_calls = []

    async def fake_stream(_candidates, messages, **kwargs):
        primary_calls.append(list(messages))
        yield f'data: {json.dumps({"delta": "Verified final answer."})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_buddy(*args, **kwargs):
        return "VERDICT: ACCEPT\nNEXT: Accept the verified candidate."

    monkeypatch.setattr(
        agent_loop, "stream_llm_with_fallback", fake_stream, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "_run_buddy_subagent", fake_buddy, raising=False
    )

    events = _events(
        _collect(
            agent_loop.stream_agent_loop(
                "http://model.test/v1",
                "test-model",
                [{"role": "user", "content": "Explain the verified result"}],
                max_rounds=3,
                relevant_tools={"read_file"},
                buddy_enabled=True,
                buddy_endpoint_url="http://buddy.test/v1",
                buddy_model="ornith-9b",
                buddy_final_review=True,
            )
        )
    )

    assert len(primary_calls) == 1
    assert any(
        event.get("type") == "buddy_result"
        and event.get("verdict") == "ACCEPT"
        for event in events
    )
    assert not any(event.get("type") == "agent_step" for event in events)
    assert any(
        event.get("type") == "final_response"
        and event.get("content") == "Verified final answer."
        for event in events
    )


def test_buddy_verdict_defaults_legacy_notes_to_revision():
    assert agent_loop._buddy_verdict("VERDICT: ACCEPT\nNEXT: Finish.") == "ACCEPT"
    assert agent_loop._buddy_verdict("VERDICT: BLOCKED\nNEXT: Enable bash.") == "BLOCKED"
    assert agent_loop._buddy_verdict("Check one more edge case.") == "REVISE"


def test_buddy_ui_and_request_are_wired():
    html = (ROOT / "static" / "index.html").read_text()
    app_js = (ROOT / "static" / "app.js").read_text()
    chat_js = (ROOT / "static" / "js" / "chat.js").read_text()
    route = (ROOT / "routes" / "chat_routes.py").read_text()

    assert 'id="buddy-toggle-btn"' in html
    assert 'id="buddy-guide-prompt"' in html
    assert 'id="buddy-endpoint"' in html
    assert 'id="buddy-model"' in html
    assert 'id="buddy-role"' in html
    assert 'id="buddy-cadence"' in html
    assert 'id="buddy-final-review"' in html
    assert 'id="buddy-modal"' in html
    assert "stateKey: 'buddy'" in app_js
    assert "Buddy is deliberately opt-in" in app_js
    assert "fd.append('buddy_enabled', 'true')" in chat_js
    assert "fd.append('buddy_prompt'" in chat_js
    assert "fd.append('buddy_endpoint_id'" in chat_js
    assert "fd.append('buddy_model'" in chat_js
    assert "fd.append('buddy_role'" in chat_js
    assert "buddy_enabled=buddy_enabled" in route
    assert "buddy_prompt=buddy_prompt" in route
    assert "resolve_endpoint_by_id" in route
    assert "buddy_endpoint_url=_buddy_endpoint_url" in route
    assert 'json.type === \'final_response\'' in chat_js
    assert 'data.get("type") == "final_response"' in route

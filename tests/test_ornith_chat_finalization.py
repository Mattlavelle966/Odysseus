"""Focused regression coverage for Ornith interactive-chat finalization."""

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as database
import core.models as models
import core.session_manager as session_manager_module
import src.agent_loop as agent_loop
from core.models import ChatMessage
from routes.chat_helpers import save_assistant_response
from types import SimpleNamespace


@pytest.mark.asyncio
async def test_ornith_normal_text_stream_completes_saves_and_reloads(
    monkeypatch,
):
    visible_text = (
        "This is a normal Ornith answer.\n\n"
        "It contains prose only and does not request a tool."
    )

    monkeypatch.setattr(
        agent_loop,
        "get_setting",
        lambda key, default=None: default,
        raising=False,
    )
    monkeypatch.setattr(
        agent_loop, "get_mcp_manager", lambda: None, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "estimate_tokens", lambda *args, **kwargs: 10, raising=False
    )

    executed_tools = []

    async def fake_execute(block, *args, **kwargs):
        executed_tools.append(block)
        return ("unexpected", {"error": "must not execute"})

    async def fake_stream(_candidates, messages, **kwargs):
        # Match llama.cpp's OpenAI-compatible terminal shape: reasoning may be
        # separate, visible content streams normally, and tool_calls is absent.
        yield (
            "data: "
            + json.dumps({"delta": "private reasoning", "thinking": True})
            + "\n\n"
        )
        yield "data: " + json.dumps({"delta": visible_text}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(
        agent_loop, "execute_tool_block", fake_execute, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "stream_llm_with_fallback", fake_stream, raising=False
    )

    chunks = [
        chunk
        async for chunk in agent_loop.stream_agent_loop(
            "http://llama.cpp.test/v1/chat/completions",
            "Ornith-1.0-35B",
            [{"role": "user", "content": "Give me a normal text answer."}],
            max_rounds=2,
            relevant_tools={"ask_user"},
        )
    ]

    visible_deltas = []
    for chunk in chunks:
        if not chunk.startswith("data: ") or chunk == "data: [DONE]\n\n":
            continue
        event = json.loads(chunk[6:])
        if "delta" in event and not event.get("thinking"):
            visible_deltas.append(event["delta"])

    assert "".join(visible_deltas) == visible_text
    assert chunks[-1] == "data: [DONE]\n\n"
    assert executed_tools == []

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    test_session_local = sessionmaker(
        bind=engine, autoflush=False, autocommit=False
    )
    database.Base.metadata.create_all(engine)
    monkeypatch.setattr(database, "SessionLocal", test_session_local)
    monkeypatch.setattr(
        session_manager_module, "SessionLocal", test_session_local
    )

    previous_manager = models.get_session_manager_instance()
    try:
        manager = session_manager_module.SessionManager()
        models.set_session_manager_instance(manager)
        session = manager.create_session(
            "ornith-chat",
            "Ornith chat",
            "http://llama.cpp.test/v1/chat/completions",
            "Ornith-1.0-35B",
            owner="alice",
        )
        manager.add_message(
            session.id, ChatMessage("user", "Give me a normal text answer.")
        )
        save_assistant_response(
            session,
            manager,
            session.id,
            "".join(visible_deltas),
            {
                "model": "Ornith-1.0-35B",
                "requested_model": "Ornith-1.0-35B",
                "thinking": "private reasoning",
            },
        )

        reloaded_manager = session_manager_module.SessionManager()
        reloaded = reloaded_manager.get_session(session.id)
        assistant = reloaded.history[-1]

        assert assistant.role == "assistant"
        assert assistant.content == visible_text
        assert assistant.metadata["thinking"] == "private reasoning"
        assert "private reasoning" not in assistant.content
    finally:
        models.set_session_manager_instance(previous_manager)


def test_ornith_missing_tool_calls_is_a_normal_text_terminal_response():
    text = (
        "Ordinary text with an illustrative block:\n\n"
        "```python\nprint('display this; do not run it')\n```"
    )

    blocks, used_native, converted = agent_loop._resolve_tool_blocks(
        text,
        [],
        round_num=1,
        is_api_model=True,
        structured_only=True,
    )

    assert blocks == []
    assert used_native is False
    assert converted == []


def test_active_html_site_edit_targets_the_open_document():
    document = SimpleNamespace(
        id="doc-site",
        title="Company site",
        language="html",
        current_content="<html></html>",
        version_count=2,
    )
    prompt = (
        "Please complete the site so all menu buttons work and make the logo "
        "a centered background image."
    )
    intent = agent_loop._classify_agent_request([], prompt)

    assert agent_loop._turn_targets_active_document(
        intent, prompt, document
    ) is True


def test_new_website_request_targets_editor_preview_not_an_implicit_port():
    prompt = "Please build me a sleek full blown website for my company."

    assert agent_loop._turn_requests_new_preview_document(prompt) is True
    assert agent_loop._turn_requests_new_preview_document(
        "Build this React website in the current repo."
    ) is False
    assert agent_loop._turn_requests_new_preview_document(
        "Build a website and serve it on port 8080."
    ) is False
    assert agent_loop._turn_requests_new_preview_document(
        prompt, workspace="/workspace/project"
    ) is False


@pytest.mark.asyncio
async def test_ornith_new_website_is_saved_as_previewable_html_document(
    monkeypatch,
):
    calls = 0
    request_shapes = []

    monkeypatch.setattr(
        agent_loop,
        "get_setting",
        lambda key, default=None: default,
        raising=False,
    )
    monkeypatch.setattr(
        agent_loop, "get_mcp_manager", lambda: None, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "estimate_tokens", lambda *args, **kwargs: 10, raising=False
    )

    async def fake_stream(_candidates, messages, **kwargs):
        nonlocal calls
        calls += 1
        request_shapes.append({
            "tool_names": [
                schema["function"]["name"]
                for schema in (kwargs.get("tools") or [])
            ],
            "tool_choice": kwargs.get("tool_choice"),
            "messages": messages,
        })
        yield "data: " + json.dumps({
            "type": "tool_calls",
            "calls": [{
                "id": "call-create-site",
                "name": "create_document",
                "arguments": json.dumps({
                    "title": "Company website",
                    "language": "html",
                    "content": (
                        "<!doctype html><html><head><style>"
                        "body{color:#fff}</style></head>"
                        "<body><h1>Company</h1><script>"
                        "document.body.dataset.ready='1'</script></body></html>"
                    ),
                }),
            }],
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        assert block.tool_type == "create_document"
        return "create_document", {
            "action": "create",
            "result": "success",
            "doc_id": "doc-preview",
            "title": "Company website",
            "language": "html",
            "content": "<!doctype html><html><body>Company</body></html>",
            "version": 1,
        }

    monkeypatch.setattr(
        agent_loop, "stream_llm_with_fallback", fake_stream, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "execute_tool_block", fake_execute, raising=False
    )

    chunks = [
        chunk
        async for chunk in agent_loop.stream_agent_loop(
            "http://llama.cpp.test/v1/chat/completions",
            "Ornith-1.0-35B",
            [{
                "role": "user",
                "content": "Build me a sleek website for my company.",
            }],
            session_id="session-site",
            owner="alice",
            max_rounds=3,
            relevant_tools={"bash", "web_fetch", "create_document"},
        )
    ]

    visible = []
    for chunk in chunks:
        if not chunk.startswith("data: ") or chunk == "data: [DONE]\n\n":
            continue
        event = json.loads(chunk[6:])
        if "delta" in event and not event.get("thinking"):
            visible.append(event["delta"])

    assert calls == 1
    assert request_shapes[0]["tool_names"] == ["create_document"]
    assert request_shapes[0]["tool_choice"] == {
        "type": "function",
        "function": {"name": "create_document"},
    }
    system_text = "\n".join(
        message.get("content", "")
        for message in request_shapes[0]["messages"]
        if message.get("role") == "system"
    )
    assert "all CSS and JavaScript inline" in system_text
    assert "start a server" in system_text
    assert "".join(visible).endswith(
        "Created **[Company website](#document-doc-preview)** as version 1."
    )


@pytest.mark.asyncio
async def test_ornith_document_write_ends_from_stored_result(monkeypatch):
    calls = 0
    active_document = SimpleNamespace(
        id="doc-site",
        title="Company site",
        language="html",
        current_content="<html>v1</html>",
        version_count=1,
    )

    monkeypatch.setattr(
        agent_loop,
        "get_setting",
        lambda key, default=None: default,
        raising=False,
    )
    monkeypatch.setattr(
        agent_loop, "get_mcp_manager", lambda: None, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "estimate_tokens", lambda *args, **kwargs: 10, raising=False
    )

    async def fake_stream(_candidates, messages, **kwargs):
        nonlocal calls
        calls += 1
        yield "data: " + json.dumps({"delta": "I will update it now."}) + "\n\n"
        yield "data: " + json.dumps({
            "type": "tool_calls",
            "calls": [{
                "id": "call-update",
                "name": "update_document",
                "arguments": json.dumps({
                    "document_id": "doc-site",
                    "expected_version": 1,
                    "content": "<html>v2</html>",
                }),
            }],
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        assert block.tool_type == "update_document"
        return "update_document", {
            "action": "update",
            "result": "success",
            "doc_id": "doc-site",
            "title": "Company site",
            "language": "html",
            "content": "<html>v2</html>",
            "version": 2,
        }

    monkeypatch.setattr(
        agent_loop, "stream_llm_with_fallback", fake_stream, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "execute_tool_block", fake_execute, raising=False
    )

    chunks = [
        chunk
        async for chunk in agent_loop.stream_agent_loop(
            "http://llama.cpp.test/v1/chat/completions",
            "Ornith-1.0-35B",
            [{
                "role": "user",
                "content": "Implement those changes as v2 of this document.",
            }],
            active_document=active_document,
            session_id="session-site",
            owner="alice",
            max_rounds=3,
            relevant_tools={
                "create_document", "update_document", "edit_document"
            },
        )
    ]

    visible = []
    metrics = {}
    for chunk in chunks:
        if not chunk.startswith("data: ") or chunk == "data: [DONE]\n\n":
            continue
        event = json.loads(chunk[6:])
        if "delta" in event and not event.get("thinking"):
            visible.append(event["delta"])
        if event.get("type") == "metrics":
            metrics = event["data"]

    final_text = "".join(visible)
    assert calls == 1
    assert "I will update it now" not in final_text
    assert "as version 2 in the same document history" in final_text
    assert "#document-doc-site" in final_text
    assert metrics["tool_events"][0]["doc_id"] == "doc-site"
    assert metrics["tool_events"][0]["doc_version"] == 2
    assert metrics["tool_events"][0]["success"] is True


@pytest.mark.asyncio
async def test_ornith_document_prose_cannot_claim_unsaved_success(monkeypatch):
    calls = 0
    active_document = SimpleNamespace(
        id="doc-site",
        title="Company site",
        language="html",
        current_content="<html>v1</html>",
        version_count=1,
    )

    monkeypatch.setattr(
        agent_loop,
        "get_setting",
        lambda key, default=None: default,
        raising=False,
    )
    monkeypatch.setattr(
        agent_loop, "get_mcp_manager", lambda: None, raising=False
    )
    monkeypatch.setattr(
        agent_loop, "estimate_tokens", lambda *args, **kwargs: 10, raising=False
    )

    async def fake_stream(_candidates, messages, **kwargs):
        nonlocal calls
        calls += 1
        yield "data: " + json.dumps({
            "delta": "Done! The site was updated successfully."
        }) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(
        agent_loop, "stream_llm_with_fallback", fake_stream, raising=False
    )

    chunks = [
        chunk
        async for chunk in agent_loop.stream_agent_loop(
            "http://llama.cpp.test/v1/chat/completions",
            "Ornith-1.0-35B",
            [{
                "role": "user",
                "content": "Complete the site and fix its menu buttons.",
            }],
            active_document=active_document,
            session_id="session-site",
            owner="alice",
            max_rounds=3,
            relevant_tools={"edit_document"},
        )
    ]

    visible = []
    metrics = {}
    for chunk in chunks:
        if not chunk.startswith("data: ") or chunk == "data: [DONE]\n\n":
            continue
        event = json.loads(chunk[6:])
        if "delta" in event and not event.get("thinking"):
            visible.append(event["delta"])
        if event.get("type") == "metrics":
            metrics = event["data"]

    final_text = "".join(visible)
    assert calls == 2
    assert "updated successfully" not in final_text
    assert final_text == (
        "I could not save the requested document change. "
        "The active document is unchanged."
    )
    assert metrics.get("tool_events") in (None, [])

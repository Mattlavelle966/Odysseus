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

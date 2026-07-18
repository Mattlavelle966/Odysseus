import asyncio
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import Base, Document, Session as ChatSession
from src.agent_tools import document_tools
from src.model_profiles import (
    DEFAULT_PROFILE,
    ORNITH_PROFILE,
    diagnose_ornith_endpoint,
    get_model_profile,
)
from src.tool_schemas import function_call_to_tool_block


SCHEMA = {
    "type": "function",
    "function": {
        "name": "harmless_lookup",
        "description": "Look up one harmless value.",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
        },
    },
}


def test_ornith_detection_and_non_ornith_identity_path():
    assert get_model_profile("Ornith-1.0-35B") is ORNITH_PROFILE
    assert get_model_profile("org/Ornith-1.0-35B") is ORNITH_PROFILE
    assert get_model_profile("gpt-4.1") is DEFAULT_PROFILE
    schemas = [SCHEMA]
    assert DEFAULT_PROFILE.adapt_tool_schemas(schemas) is schemas
    assert DEFAULT_PROFILE.normal_content(
        {"content": "", "reasoning_content": "legacy fallback"}
    ) == "legacy fallback"


def test_real_structured_call_executes_but_textual_pseudo_call_never_does():
    proper = ORNITH_PROFILE.validate_tool_calls(
        [{
            "id": "call-1",
            "name": "harmless_lookup",
            "arguments": '{"query":"weather"}',
        }],
        [SCHEMA],
    )
    assert proper.ok
    assert proper.calls[0]["name"] == "harmless_lookup"

    textual = ORNITH_PROFILE.validate_tool_calls(
        [],
        [SCHEMA],
        response_text='```json\n{"name":"harmless_lookup","query":"secrets"}\n```',
    )
    assert not textual.calls
    assert textual.pseudo_tool_call


def test_malformed_json_gets_only_a_schema_correction_and_no_call():
    malformed = ORNITH_PROFILE.validate_tool_calls(
        [{
            "id": "bad",
            "name": "harmless_lookup",
            "arguments": '{"query":',
        }],
        [SCHEMA],
    )
    assert not malformed.ok
    assert malformed.calls == []
    correction = ORNITH_PROFILE.correction_message([SCHEMA], malformed.errors)
    assert "Retry once" in correction
    assert "harmless_lookup" in correction
    assert "shell" not in correction


def test_reasoning_content_is_never_promoted_to_normal_content():
    message = {
        "content": "Visible answer",
        "reasoning_content": "private chain of thought and simulated tool call",
    }
    assert ORNITH_PROFILE.normal_content(message) == "Visible answer"
    assert ORNITH_PROFILE.reasoning_content(message).startswith("private")
    assert ORNITH_PROFILE.normal_content(
        {"content": None, "reasoning_content": "secret"}
    ) == ""


def test_endpoint_diagnostic_requires_a_real_harmless_tool_call(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": "",
                        "reasoning_content": "private",
                        "tool_calls": [{
                            "id": "probe",
                            "type": "function",
                            "function": {
                                "name": "report_probe",
                                "arguments": '{"value":"ornith-tool-probe"}',
                            },
                        }],
                    },
                }],
            }

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["payload"] = kwargs["json"]
            return Response()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", Client)
    result = asyncio.run(diagnose_ornith_endpoint(
        endpoint_url="http://llama.cpp/v1/chat/completions",
        model="Ornith-1.0-35B",
    ))
    assert result["ok"]
    assert result["structured_tool_calls"]
    assert result["reasoning_content_present"]
    assert captured["payload"]["tools"][0]["function"]["name"] == "report_probe"
    assert captured["payload"]["tool_choice"]["function"]["name"] == "report_probe"


def test_ornith_document_schema_pins_id_version_and_validates_ranges(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    import src.database
    monkeypatch.setattr(src.database, "SessionLocal", SessionLocal)

    db = SessionLocal()
    db.add(
        ChatSession(
            id="session-1",
            name="Test",
            owner="alice",
            endpoint_url="http://model/v1/chat/completions",
            model="test",
        )
    )
    db.add(
        Document(
            id="doc-1",
            session_id="session-1",
            owner="alice",
            title="Draft",
            language="markdown",
            current_content="Hello world",
            version_count=1,
        )
    )
    db.commit()
    db.close()

    base = {
        "type": "function",
        "function": {
            "name": "edit_document",
            "description": "legacy",
            "parameters": {"type": "object"},
        },
    }
    adapted = ORNITH_PROFILE.adapt_tool_schemas(
        [base],
        allowed_names={"edit_document"},
        active_document={"id": "doc-1", "version_count": 1},
    )[0]
    call = {
        "id": "doc-call",
        "name": "edit_document",
        "arguments": json.dumps({
            "document_id": "doc-1",
            "expected_version": 1,
            "operation": "replace_range",
            "start": 6,
            "end": 11,
            "text": "Odysseus",
        }),
    }
    validated = ORNITH_PROFILE.validate_tool_calls([call], [adapted])
    assert validated.ok
    block = function_call_to_tool_block(
        "edit_document", validated.calls[0]["arguments"]
    )
    result = asyncio.run(
        document_tools.EditDocumentTool().execute(
            block.content,
            {"doc_id": "doc-1", "owner": "alice", "model": "Ornith-1.0-35B"},
        )
    )
    assert result["result"] == "success"
    assert result["version"] == 2
    db = SessionLocal()
    assert db.get(Document, "doc-1").current_content == "Hello Odysseus"
    db.close()

    stale = function_call_to_tool_block(
        "edit_document",
        json.dumps({
            "document_id": "doc-1",
            "expected_version": 1,
            "operation": "append",
            "text": "\nunsafe overwrite",
        }),
    )
    rejected = asyncio.run(
        document_tools.EditDocumentTool().execute(
            stale.content,
            {"doc_id": "doc-1", "owner": "alice"},
        )
    )
    assert "version conflict" in rejected["error"].lower()
    db = SessionLocal()
    assert db.get(Document, "doc-1").current_content == "Hello Odysseus"
    db.close()

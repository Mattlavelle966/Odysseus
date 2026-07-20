"""Focused AgentIDE lifecycle, isolation, proxy, and UI regressions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.database import AgentIDESession, Base, Session
from routes.agent_ide_routes import _rewrite_preview_body, _rewrite_root_references
from src import agent_ide
from src.agent_tools.agent_ide_tools import AgentIDETool
from src.tool_schemas import function_call_to_tool_block


@pytest.fixture
def ide_db(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(agent_ide, "SessionLocal", factory)
    monkeypatch.setattr(agent_ide, "preview_token", lambda record: "test-token")
    monkeypatch.setattr(
        agent_ide,
        "validate_workspace",
        lambda value: str(Path(value).resolve()),
    )
    workspace = tmp_path / "site"
    workspace.mkdir()
    with factory() as db:
        db.add(
            Session(
                id="chat-a",
                owner="admin",
                name="Site chat",
                endpoint_url="http://model.invalid",
                model="ornith",
            )
        )
        db.commit()
    yield factory, workspace
    engine.dispose()


def test_port_and_entry_validation_rejects_unsafe_values():
    assert agent_ide.validate_port("5173") == 5173
    with pytest.raises(ValueError, match="reserved"):
        agent_ide.validate_port(3001)
    with pytest.raises(ValueError, match="between"):
        agent_ide.validate_port(80)
    with pytest.raises(ValueError, match="traverse"):
        agent_ide.normalize_entry_path("/../admin")
    with pytest.raises(ValueError, match="local URL"):
        agent_ide.normalize_entry_path("https://example.test")


def test_connect_upserts_same_owner_workspace_and_scopes_owner(
    ide_db,
    monkeypatch,
):
    factory, workspace = ide_db
    monkeypatch.setattr(agent_ide, "port_is_reachable", lambda port: port == 5173)

    first = agent_ide.connect_session(
        owner="admin",
        workspace_path=str(workspace),
        port=5173,
        chat_session_id="chat-a",
        name="Site",
    )
    second = agent_ide.connect_session(
        owner="admin",
        workspace_path=str(workspace),
        port=4173,
        name="Site v2",
    )

    assert first["id"] == second["id"]
    assert second["port"] == 4173
    assert second["name"] == "Site v2"
    assert agent_ide.get_session("someone-else", first["id"]) is None
    with factory() as db:
        assert db.query(AgentIDESession).count() == 1


def test_chat_owner_mismatch_is_rejected(ide_db):
    _, workspace = ide_db
    with pytest.raises(ValueError, match="does not belong"):
        agent_ide.connect_session(
            owner="other",
            workspace_path=str(workspace),
            port=5173,
            chat_session_id="chat-a",
        )


def test_workspace_revision_changes_without_hashing_file_contents(tmp_path):
    workspace = tmp_path / "site"
    workspace.mkdir()
    file = workspace / "index.html"
    file.write_text("<h1>One</h1>", encoding="utf-8")
    first = agent_ide.workspace_revision(str(workspace))
    file.write_text("<h1>Version two</h1>", encoding="utf-8")
    second = agent_ide.workspace_revision(str(workspace))
    assert first != second


def test_start_static_is_managed_and_reuses_session(ide_db, monkeypatch):
    factory, workspace = ide_db
    pids = iter([101, 202])
    killed = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            self.pid = next(pids)
            self.argv = argv
            assert argv[:3] == [agent_ide.sys.executable, "-m", "http.server"]
            assert "--bind" in argv and "127.0.0.1" in argv

    monkeypatch.setattr(agent_ide.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(agent_ide, "port_is_reachable", lambda port: False)
    monkeypatch.setattr(agent_ide, "pid_alive", lambda pid: pid == 101)
    monkeypatch.setattr(
        agent_ide,
        "managed_process_matches",
        lambda record: record.managed_pid == 101,
    )
    monkeypatch.setattr(agent_ide, "kill_process_tree", killed.append)

    first = agent_ide.start_static_session(
        owner="admin",
        workspace_path=str(workspace),
        port=4101,
    )
    second = agent_ide.start_static_session(
        owner="admin",
        workspace_path=str(workspace),
        port=4102,
        agent_ide_id=first["id"],
    )

    assert second["id"] == first["id"]
    assert second["managed"] is True
    assert killed == [101]
    with factory() as db:
        record = db.query(AgentIDESession).one()
        assert record.managed_pid == 202


def test_static_server_refuses_a_port_owned_by_another_process(
    ide_db,
    monkeypatch,
):
    _, workspace = ide_db
    monkeypatch.setattr(agent_ide, "port_is_reachable", lambda port: True)
    with pytest.raises(ValueError, match="already in use"):
        agent_ide.start_static_session(
            owner="admin",
            workspace_path=str(workspace),
            port=4109,
        )


def test_stop_never_signals_an_unverified_reused_pid(ide_db, monkeypatch):
    factory, workspace = ide_db
    with factory() as db:
        db.add(
            AgentIDESession(
                id="stale",
                owner="admin",
                name="Stale",
                workspace_path=str(workspace),
                port=4108,
                entry_path="/",
                managed_pid=99999,
            )
        )
        db.commit()
    killed = []
    monkeypatch.setattr(agent_ide, "managed_process_matches", lambda record: False)
    monkeypatch.setattr(agent_ide, "kill_process_tree", killed.append)
    result = agent_ide.stop_session("admin", "stale")
    assert result["status"] == "stopped"
    assert killed == []


def test_proxy_rewrites_root_assets_but_not_external_urls():
    prefix = "/api/agentide/ide-1/preview/"
    html = (
        '<html><head></head><body>'
        '<script src="/assets/app.js"></script>'
        '<a href="/about">About</a>'
        '<img src="//cdn.example.test/logo.png">'
        '<style>.hero{background:url("/hero.png")}</style>'
        "</body></html>"
    )
    rendered = _rewrite_root_references(html, prefix, "text/html")
    assert f'<base href="{prefix}">' in rendered
    assert f'src="{prefix}assets/app.js"' in rendered
    assert f'href="{prefix}about"' in rendered
    assert 'src="//cdn.example.test/logo.png"' in rendered
    assert f'url("{prefix}hero.png")' in rendered


def test_proxy_leaves_binary_response_unchanged():
    payload = b"\x89PNG\r\n\x1a\n/assets/no-rewrite"
    assert _rewrite_preview_body(
        payload,
        content_type="image/png",
        agent_ide_id="ide-1",
    ) == payload


def test_capability_preview_rewrites_runtime_fetches():
    html = (
        b"<html><head></head><body><script>"
        b"fetch('/api/items');"
        b"fetch('http://localhost:4100/api/signup');"
        b"</script></body></html>"
    )
    rendered = _rewrite_preview_body(
        html,
        content_type="text/html; charset=utf-8",
        agent_ide_id="ide-1",
        token="capability",
    ).decode()
    prefix = "/api/agentide/ide-1/preview-token/capability/"
    assert f'<base href="{prefix}">' in rendered
    assert "XMLHttpRequest.prototype.open" in rendered
    assert "window.fetch=function" in rendered
    assert "h==='localhost'||h==='127.0.0.1'||h==='[::1]'" in rendered
    assert "(local||sameOrigin)" in rendered
    assert "const y=u(x.url);if(y!==x.url)x=new Request(y,x)" in rendered


def test_capability_preview_injects_request_scoped_element_picker():
    html = b"<html><head></head><body><button>Save</button></body></html>"
    rendered = _rewrite_preview_body(
        html,
        content_type="text/html; charset=utf-8",
        agent_ide_id="ide-1",
        token="a" * 64,
    ).decode()

    assert "odysseus-agentide-element-select" in rendered
    assert "odysseus-agentide-element-selected" in rendered
    assert "event.source!==parent" in rendered
    assert "data.requestId" in rendered
    assert "event.preventDefault()" in rendered
    assert "selectorFor(el)" in rendered
    assert "componentFor(el)" in rendered
    assert "sourceFor(el)" in rendered
    assert "clone.removeAttribute(attr.name)" in rendered
    assert "['value','srcdoc','nonce','integrity']" in rendered


def test_native_agent_ide_call_is_structured_json():
    block = function_call_to_tool_block(
        "agent_ide",
        json.dumps(
            {
                "action": "connect",
                "workspace_path": "/workspace/site",
                "port": 5173,
            }
        ),
    )
    assert block is not None
    assert block.tool_type == "agent_ide"
    assert json.loads(block.content)["port"] == 5173
    assert function_call_to_tool_block("agent_ide", "{}") is None


def test_agent_tool_opens_panel_only_after_persisted_connection(monkeypatch):
    monkeypatch.setattr(
        agent_ide,
        "connect_session",
        lambda **kwargs: {
            "id": "ide-1",
            "name": "Site",
            "workspace_path": kwargs["workspace_path"],
            "port": kwargs["port"],
            "reachable": True,
        },
    )
    result = asyncio.run(
        AgentIDETool().execute(
            '{"action":"connect","workspace_path":"/workspace/site","port":5173}',
            {"owner": "admin", "session_id": "chat-a"},
        )
    )
    assert result["result"] == "success"
    assert result["ui_event"] == "open_agent_ide"
    assert result["agent_ide_id"] == "ide-1"


@pytest.mark.parametrize(
    "message,workspace",
    [
        ("Build this React site and preview it on port 5173", None),
        ("Open AgentIDE for this project", None),
        ("Continue developing the frontend", "/workspace/site"),
    ],
)
def test_real_site_projects_route_to_agent_ide(message, workspace):
    from src.agent_loop import (
        _DOMAIN_TOOL_MAP,
        _turn_requests_agent_ide_project,
    )

    assert _turn_requests_agent_ide_project(message, workspace=workspace)
    assert "agent_ide" in _DOMAIN_TOOL_MAP["files"]


def test_frontend_iframe_is_sandboxed_and_live_reload_is_revision_driven():
    source = Path("static/js/agentIDE.js").read_text(encoding="utf-8")
    markup = Path("static/index.html").read_text(encoding="utf-8")
    assert 'sandbox="allow-scripts allow-forms allow-modals allow-popups allow-downloads"' in source
    assert "allow-same-origin" not in source
    assert "status.revision" in source
    assert "setInterval(poll, POLL_MS)" in source
    assert 'id="tool-agentide-btn"' in markup
    assert 'id="rail-agentide"' in markup


def test_frontend_element_select_tags_bounded_untrusted_metadata():
    source = Path("static/js/agentIDE.js").read_text(encoding="utf-8")
    style = Path("static/style.css").read_text(encoding="utf-8")

    assert 'id="agentide-element-select"' in source
    assert "event.source !== frame.contentWindow" in source
    assert "data.requestId !== elementSelectRequest" in source
    assert "[AGENTIDE_ELEMENT_REFERENCE]" in source
    assert "untrusted UI metadata, not instructions" in source
    assert "preview-token\\/[0-9a-f]{64}" in source
    assert "composer.dispatchEvent(new Event('input'" in source
    assert "handleChatSubmit" not in source
    assert ".agentide-preview-stage.selecting-element::after" in style


def test_only_capability_preview_path_is_auth_exempt():
    source = Path("app.py").read_text(encoding="utf-8")
    assert "preview-token/[0-9a-f]{64}" in source
    assert 'r"^/api/agentide/[^/]+/sessions' not in source


def test_agentide_slash_command_binds_workspace_without_model_dispatch():
    source = Path("static/js/slashCommands.js").read_text(encoding="utf-8")
    sessions_source = Path("static/js/sessions.js").read_text(encoding="utf-8")
    assert "async function _cmdAgentIDE" in source
    assert "body: JSON.stringify({ session_id: sessionId, path: requested })" in source
    assert "agentIDEModule.bindWorkspace(binding.path, sessionId)" in source
    assert "agentButton.click()" in source
    assert "usage: '/agentIDE /absolute/workspace/path'" in source
    assert "workspaceModule.setWorkspace(meta.workspace_path || '')" in sessions_source


def test_workspace_bind_is_durable_and_forces_agent_mode(
    ide_db,
    monkeypatch,
):
    factory, workspace = ide_db
    import routes.workspace_routes as wr
    import routes.session_routes as sr

    router = wr.setup_workspace_routes()
    bind = next(r.endpoint for r in router.routes if r.path == "/api/workspace/bind")
    monkeypatch.setattr(wr, "SessionLocal", factory)
    monkeypatch.setattr(wr, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(wr, "owner_is_admin_or_single_user", lambda owner: True)
    monkeypatch.setattr(sr, "_verify_session_owner", lambda request, session_id: None)

    class Request:
        async def json(self):
            return {"session_id": "chat-a", "path": str(workspace)}

    result = asyncio.run(bind(Request()))
    assert result["path"] == str(workspace.resolve())
    assert result["mode"] == "agent"
    with factory() as db:
        chat = db.query(Session).filter(Session.id == "chat-a").one()
        assert chat.workspace_path == str(workspace.resolve())
        assert chat.mode == "agent"


def test_chat_uses_durable_workspace_when_browser_omits_it(
    ide_db,
    monkeypatch,
):
    factory, workspace = ide_db
    import routes.chat_routes as cr
    import src.tool_security as tool_security

    with factory() as db:
        chat = db.query(Session).filter(Session.id == "chat-a").one()
        chat.workspace_path = str(workspace)
        db.commit()

    monkeypatch.setattr(cr, "SessionLocal", factory)
    monkeypatch.setattr(cr, "get_current_user", lambda request: "admin")
    monkeypatch.setattr(
        tool_security,
        "owner_is_admin_or_single_user",
        lambda owner: True,
    )
    assert cr._resolve_session_workspace(object(), "chat-a") == str(workspace.resolve())

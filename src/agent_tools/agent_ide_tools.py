"""Agent-facing control surface for persistent AgentIDE previews."""

from __future__ import annotations

import json
from urllib.parse import urlencode

import httpx


_TEST_COOKIE_JARS: dict[tuple[str, int], httpx.Cookies] = {}


class AgentIDETool:
    async def execute(self, content: str, ctx: dict) -> dict:
        from src import agent_ide
        from src.tool_execution import agent_cwd

        try:
            args = json.loads(content or "{}")
        except (json.JSONDecodeError, TypeError) as exc:
            return {
                "error": f"agent_ide arguments must be valid JSON: {exc}",
                "result": "rejected",
                "exit_code": 1,
            }
        if not isinstance(args, dict):
            return {
                "error": "agent_ide arguments must be a JSON object",
                "result": "rejected",
                "exit_code": 1,
            }

        action = str(args.get("action") or "status").strip().lower()
        owner = ctx.get("owner")
        chat_session_id = ctx.get("session_id")
        agent_ide_id = str(
            args.get("agent_ide_id") or args.get("id") or ""
        ).strip()

        try:
            if action == "list":
                sessions = agent_ide.list_sessions(owner)
                return {
                    "result": "success",
                    "action": "list",
                    "sessions": sessions,
                    "output": (
                        "\n".join(
                            f"{item['id']} — {item['name']} — "
                            f"{item['workspace_path']} — port {item['port']} "
                            f"({item['status']})"
                            for item in sessions
                        )
                        or "No AgentIDE projects are configured."
                    ),
                    "exit_code": 0,
                }

            if action in {"status", "open", "refresh"}:
                if not agent_ide_id:
                    sessions = agent_ide.list_sessions(owner)
                    if not sessions:
                        raise ValueError("No AgentIDE project exists to open")
                    agent_ide_id = sessions[0]["id"]
                payload = agent_ide.session_status(owner, agent_ide_id)
                result = {
                    **payload,
                    "result": "success" if payload["reachable"] else "partial",
                    "action": action,
                    "output": (
                        f"AgentIDE project {payload['name']} is "
                        f"{'reachable' if payload['reachable'] else 'not reachable'} "
                        f"on port {payload['port']}."
                    ),
                    "exit_code": 0,
                }
                if action in {"open", "refresh"}:
                    result["ui_event"] = "open_agent_ide"
                    result["agent_ide_id"] = payload["id"]
                return result

            if action in {
                "connect", "serve_static", "start_static",
                "start", "restart",
            }:
                workspace = str(
                    args.get("workspace_path")
                    or args.get("workspace")
                    or agent_cwd()
                    or ""
                ).strip()
                common = {
                    "owner": owner,
                    "workspace_path": workspace,
                    "entry_path": args.get("entry_path") or "/",
                    "chat_session_id": chat_session_id,
                    "name": args.get("name"),
                    "agent_ide_id": agent_ide_id or None,
                    "service_ports": args.get("service_ports") or [],
                }
                if action == "connect":
                    if args.get("port") is None:
                        raise ValueError("connect requires a port")
                    payload = agent_ide.connect_session(
                        **common,
                        port=args.get("port"),
                    )
                elif action in {"start", "restart"}:
                    previous = (
                        agent_ide.get_session(owner, agent_ide_id)
                        if agent_ide_id
                        else None
                    )
                    command = str(
                        args.get("command")
                        or (previous.managed_command if previous else "")
                        or ""
                    ).strip()
                    port = args.get("port") or (
                        previous.port if previous is not None else None
                    )
                    if port is None:
                        raise ValueError(f"{action} requires a port")
                    if not command or command == "__static__":
                        raise ValueError(
                            f"{action} requires a development server command"
                        )
                    payload = agent_ide.start_managed_session(
                        **common,
                        command=command,
                        port=port,
                    )
                else:
                    payload = agent_ide.start_static_session(
                        **common,
                        port=args.get("port"),
                    )
                return {
                    **payload,
                    "result": (
                        "success"
                        if payload.get("reachable") or action != "connect"
                        else "partial"
                    ),
                    "action": action,
                    "ui_event": "open_agent_ide",
                    "agent_ide_id": payload["id"],
                    "output": (
                        f"AgentIDE connected {payload['workspace_path']} to "
                        f"port {payload['port']}. The live preview panel is open."
                    ),
                    "exit_code": 0,
                }

            if action in {"request", "http_request", "test"}:
                if not agent_ide_id:
                    sessions = agent_ide.list_sessions(owner)
                    if not sessions:
                        raise ValueError("No AgentIDE project exists to test")
                    agent_ide_id = sessions[0]["id"]
                record = agent_ide.get_session(owner, agent_ide_id)
                if record is None:
                    raise ValueError("AgentIDE project was not found")
                port = agent_ide.validate_port(args.get("port") or record.port)
                if not agent_ide.is_service_port_allowed(record, port):
                    raise ValueError(
                        f"Port {port} is not registered for this AgentIDE project"
                    )
                method = str(args.get("method") or "GET").strip().upper()
                if method not in {
                    "GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS",
                }:
                    raise ValueError("Unsupported HTTP method")
                path = agent_ide.normalize_entry_path(args.get("path") or "/")
                query = args.get("query")
                if isinstance(query, dict) and query:
                    path += ("&" if "?" in path else "?") + urlencode(
                        {str(k): str(v) for k, v in query.items()}
                    )
                headers = args.get("headers") if isinstance(args.get("headers"), dict) else {}
                safe_headers = {
                    str(key): str(value)
                    for key, value in headers.items()
                    if str(key).lower() not in {
                        "host", "connection", "content-length",
                        "proxy-authorization",
                    }
                }
                jar_key = (record.id, port)
                cookies = _TEST_COOKIE_JARS.setdefault(jar_key, httpx.Cookies())
                request_kwargs = {
                    "headers": safe_headers,
                    "cookies": cookies,
                }
                if "json" in args:
                    request_kwargs["json"] = args.get("json")
                elif "body" in args:
                    request_kwargs["content"] = str(args.get("body") or "")
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(15.0),
                        follow_redirects=bool(args.get("follow_redirects", False)),
                    ) as client:
                        response = await client.request(
                            method,
                            f"http://127.0.0.1:{port}{path}",
                            **request_kwargs,
                        )
                        cookies.update(response.cookies)
                except httpx.HTTPError as exc:
                    return {
                        "error": f"AgentIDE HTTP request failed: {exc}",
                        "result": "failed",
                        "exit_code": 1,
                    }
                expected = args.get("expected_status")
                if expected is None:
                    expected_statuses = set(range(200, 400))
                elif isinstance(expected, list):
                    expected_statuses = {
                        int(item) for item in expected
                        if isinstance(item, (int, str)) and str(item).isdigit()
                    }
                else:
                    expected_statuses = {int(expected)}
                body = response.text
                if len(body) > 12000:
                    body = (
                        body[:6000]
                        + f"\n...[truncated, {len(response.text)} chars total]...\n"
                        + body[-6000:]
                    )
                success = response.status_code in expected_statuses
                payload = {
                    "result": "success" if success else "failed",
                    "action": "request",
                    "method": method,
                    "path": path,
                    "port": port,
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "response_body": body,
                    "output": (
                        f"{method} {path} -> HTTP {response.status_code}\n{body}"
                    ).rstrip(),
                    "exit_code": 0 if success else 1,
                }
                if not success:
                    payload["error"] = (
                        f"Expected HTTP {sorted(expected_statuses)}, "
                        f"received {response.status_code}"
                    )
                return payload

            if action in {"stop", "disconnect", "delete"}:
                if not agent_ide_id:
                    raise ValueError(f"{action} requires agent_ide_id")
                payload = agent_ide.stop_session(
                    owner,
                    agent_ide_id,
                    delete=action == "delete",
                )
                for key in [
                    key for key in _TEST_COOKIE_JARS
                    if key[0] == agent_ide_id
                ]:
                    _TEST_COOKIE_JARS.pop(key, None)
                return {
                    **payload,
                    "result": "success",
                    "action": action,
                    "output": f"AgentIDE project {agent_ide_id} {payload['status']}.",
                    "exit_code": 0,
                }

            raise ValueError(
                "Unknown action. Use list, connect, start, restart, request, "
                "serve_static, open, status, refresh, stop, disconnect, or delete."
            )
        except ValueError as exc:
            return {
                "error": str(exc),
                "result": "rejected",
                "exit_code": 1,
            }
        except Exception as exc:
            return {
                "error": f"AgentIDE failed: {type(exc).__name__}: {exc}",
                "result": "failed",
                "exit_code": 1,
            }

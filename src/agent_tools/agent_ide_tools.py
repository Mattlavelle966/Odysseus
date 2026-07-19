"""Agent-facing control surface for persistent AgentIDE previews."""

from __future__ import annotations

import json


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

            if action in {"connect", "serve_static", "start_static"}:
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
                }
                if action == "connect":
                    if args.get("port") is None:
                        raise ValueError("connect requires a port")
                    payload = agent_ide.connect_session(
                        **common,
                        port=args.get("port"),
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

            if action in {"stop", "disconnect", "delete"}:
                if not agent_ide_id:
                    raise ValueError(f"{action} requires agent_ide_id")
                payload = agent_ide.stop_session(
                    owner,
                    agent_ide_id,
                    delete=action == "delete",
                )
                return {
                    **payload,
                    "result": "success",
                    "action": action,
                    "output": f"AgentIDE project {agent_ide_id} {payload['status']}.",
                    "exit_code": 0,
                }

            raise ValueError(
                "Unknown action. Use list, connect, serve_static, open, status, "
                "refresh, stop, disconnect, or delete."
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

"""Persistent AgentIDE workspace previews and managed static servers."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.platform_compat import (
    detached_popen_kwargs,
    kill_process_tree,
    pid_alive,
    safe_chmod,
)
from src.constants import DATA_DIR
from src.database import AgentIDESession, Session as ChatSession, SessionLocal

logger = logging.getLogger(__name__)

MIN_PREVIEW_PORT = 1024
MAX_PREVIEW_PORT = 65535
RESERVED_PORTS = {3001}
MAX_SERVICE_PORTS = 16
_REVISION_FILE_LIMIT = 5000
_REVISION_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    ".next",
    ".venv",
    "venv",
}
_PREVIEW_KEY_PATH = Path(DATA_DIR) / "agent_ide_preview.key"


def _preview_key() -> bytes:
    configured = os.getenv("AGENTIDE_PREVIEW_SECRET", "").strip()
    if configured:
        return configured.encode("utf-8")
    try:
        key = _PREVIEW_KEY_PATH.read_bytes()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    _PREVIEW_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    try:
        fd = os.open(
            _PREVIEW_KEY_PATH,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
    except FileExistsError:
        key = _PREVIEW_KEY_PATH.read_bytes()
    safe_chmod(str(_PREVIEW_KEY_PATH), 0o600)
    return key


def preview_token(record: AgentIDESession) -> str:
    payload = f"{record.id}\0{normalize_owner(record.owner) or ''}".encode("utf-8")
    return hmac.new(_preview_key(), payload, hashlib.sha256).hexdigest()


def valid_preview_token(record: AgentIDESession, token: object) -> bool:
    supplied = str(token or "")
    return bool(supplied) and hmac.compare_digest(preview_token(record), supplied)


def normalize_owner(owner: Optional[str]) -> Optional[str]:
    value = str(owner or "").strip()
    return value or None


def validate_port(value: object) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Port must be an integer") from exc
    if port < MIN_PREVIEW_PORT or port > MAX_PREVIEW_PORT:
        raise ValueError(
            f"Port must be between {MIN_PREVIEW_PORT} and {MAX_PREVIEW_PORT}"
        )
    if port in RESERVED_PORTS:
        raise ValueError(f"Port {port} is reserved by Odysseus")
    return port


def normalize_service_ports(
    values: object,
    *,
    primary_port: Optional[int] = None,
) -> list[int]:
    """Validate and deduplicate loopback service ports for one project."""
    if values is None or values == "":
        return []
    if isinstance(values, str):
        candidates = [item.strip() for item in values.split(",") if item.strip()]
    elif isinstance(values, (list, tuple, set)):
        candidates = list(values)
    else:
        raise ValueError("Service ports must be a list or comma-separated string")
    primary = validate_port(primary_port) if primary_port is not None else None
    result: list[int] = []
    for candidate in candidates:
        port = validate_port(candidate)
        if port != primary and port not in result:
            result.append(port)
        if len(result) > MAX_SERVICE_PORTS:
            raise ValueError(f"At most {MAX_SERVICE_PORTS} service ports may be registered")
    return result


def service_ports_for(record: AgentIDESession) -> list[int]:
    """Return all allowed loopback ports, with the primary service first."""
    primary = validate_port(record.port)
    try:
        stored = json.loads(record.service_ports or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        stored = []
    return [primary, *normalize_service_ports(stored, primary_port=primary)]


def is_service_port_allowed(record: AgentIDESession, port: object) -> bool:
    try:
        return validate_port(port) in service_ports_for(record)
    except ValueError:
        return False


def normalize_entry_path(value: object) -> str:
    raw = str(value or "/").strip() or "/"
    if "://" in raw or "\x00" in raw:
        raise ValueError("Entry path must be a local URL path")
    path = "/" + raw.lstrip("/")
    path_only = path.split("?", 1)[0].split("#", 1)[0]
    if any(part == ".." for part in path_only.split("/")):
        raise ValueError("Entry path cannot traverse parent directories")
    return path


def validate_workspace(path: object) -> str:
    # Imported lazily because tool_execution loads the agent tool registry,
    # which includes AgentIDE. Keeping the dependency here avoids an import
    # cycle during application startup.
    from src.tool_execution import vet_workspace

    resolved = vet_workspace(str(path or "").strip())
    if not resolved:
        raise ValueError("Workspace must be an existing, usable directory")
    return resolved


def _owner_query(db, owner: Optional[str]):
    normalized = normalize_owner(owner)
    query = db.query(AgentIDESession)
    if normalized is None:
        return query.filter(AgentIDESession.owner.is_(None))
    return query.filter(AgentIDESession.owner == normalized)


def _serialize(record: AgentIDESession, *, reachable: Optional[bool] = None) -> dict:
    created_at = record.created_at or datetime.now(timezone.utc).replace(tzinfo=None)
    updated_at = record.updated_at or created_at
    return {
        "id": record.id,
        "owner": record.owner,
        "chat_session_id": record.chat_session_id,
        "name": record.name,
        "workspace_path": record.workspace_path,
        "port": record.port,
        "service_ports": service_ports_for(record)[1:],
        "entry_path": record.entry_path,
        "status": record.status,
        "last_error": record.last_error,
        "last_connected_at": (
            record.last_connected_at.isoformat() + "Z"
            if record.last_connected_at
            else None
        ),
        "managed": bool(record.managed_pid),
        "reachable": reachable,
        "created_at": created_at.isoformat() + "Z",
        "updated_at": updated_at.isoformat() + "Z",
        "preview_url": (
            f"/api/agentide/{record.id}/preview-token/{preview_token(record)}"
            f"{normalize_entry_path(record.entry_path)}"
        ),
    }


def port_is_reachable(port: int, timeout: float = 0.35) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def allocate_preview_port(start: int = 4100, end: int = 4999) -> int:
    for port in range(start, end + 1):
        if port in RESERVED_PORTS:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("No free AgentIDE preview port is available")


def managed_process_matches(record: AgentIDESession) -> bool:
    """Verify a persisted PID is still the static server we launched.

    PIDs can be reused after a machine restart. Never signal a process based
    only on an old integer from the database.
    """
    pid = int(record.managed_pid or 0)
    if not pid or not pid_alive(pid):
        return False
    command = ""
    try:
        proc_cmdline = Path(f"/proc/{pid}/cmdline")
        if proc_cmdline.exists():
            command = proc_cmdline.read_bytes().replace(b"\0", b" ").decode(
                "utf-8",
                errors="replace",
            )
        else:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            command = result.stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        "http.server" in command
        and str(record.port) in command
        and record.workspace_path in command
    )


def workspace_revision(workspace_path: str) -> str:
    """Return a bounded revision token used by the panel's live reload."""
    root = Path(workspace_path)
    digest = hashlib.sha256()
    seen = 0
    try:
        for current, dirs, files in os.walk(root):
            dirs[:] = sorted(
                name for name in dirs
                if name not in _REVISION_SKIP_DIRS and not name.startswith(".cache")
            )
            for name in sorted(files):
                path = Path(current) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                rel = path.relative_to(root).as_posix()
                digest.update(rel.encode("utf-8", errors="replace"))
                digest.update(str(stat.st_mtime_ns).encode("ascii"))
                digest.update(str(stat.st_size).encode("ascii"))
                seen += 1
                if seen >= _REVISION_FILE_LIMIT:
                    digest.update(b"file-limit")
                    return digest.hexdigest()[:20]
    except OSError:
        digest.update(b"unreadable")
    return digest.hexdigest()[:20]


def get_session(owner: Optional[str], agent_ide_id: str) -> Optional[AgentIDESession]:
    with SessionLocal() as db:
        record = _owner_query(db, owner).filter(
            AgentIDESession.id == str(agent_ide_id)
        ).first()
        if record is not None:
            db.expunge(record)
        return record


def get_session_unscoped(agent_ide_id: str) -> Optional[AgentIDESession]:
    """Internal token-validation lookup; callers must validate the capability."""
    with SessionLocal() as db:
        record = db.query(AgentIDESession).filter(
            AgentIDESession.id == str(agent_ide_id)
        ).first()
        if record is not None:
            db.expunge(record)
        return record


def list_sessions(owner: Optional[str]) -> list[dict]:
    with SessionLocal() as db:
        records = (
            _owner_query(db, owner)
            .order_by(AgentIDESession.updated_at.desc())
            .limit(50)
            .all()
        )
        return [_serialize(record) for record in records]


def connect_session(
    *,
    owner: Optional[str],
    workspace_path: str,
    port: int,
    service_ports: object = None,
    entry_path: str = "/",
    chat_session_id: Optional[str] = None,
    name: Optional[str] = None,
    agent_ide_id: Optional[str] = None,
) -> dict:
    workspace = validate_workspace(workspace_path)
    port = validate_port(port)
    services = normalize_service_ports(service_ports, primary_port=port)
    entry = normalize_entry_path(entry_path)
    normalized_owner = normalize_owner(owner)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    with SessionLocal() as db:
        if chat_session_id:
            chat = db.query(ChatSession).filter(
                ChatSession.id == str(chat_session_id)
            ).first()
            if not chat or normalize_owner(chat.owner) != normalized_owner:
                raise ValueError("Chat does not belong to the AgentIDE owner")

        record = None
        if agent_ide_id:
            record = _owner_query(db, owner).filter(
                AgentIDESession.id == str(agent_ide_id)
            ).first()
            if record is None:
                raise ValueError("AgentIDE session was not found")
        if record is None:
            record = _owner_query(db, owner).filter(
                AgentIDESession.workspace_path == workspace
            ).order_by(AgentIDESession.updated_at.desc()).first()
        if record is None:
            record = AgentIDESession(
                id=str(uuid.uuid4()),
                owner=normalized_owner,
                workspace_path=workspace,
                port=port,
                service_ports=json.dumps(services),
                entry_path=entry,
                name=(name or Path(workspace).name or "AgentIDE")[:160],
                chat_session_id=chat_session_id,
            )
            db.add(record)
        else:
            record.workspace_path = workspace
            record.port = port
            record.service_ports = json.dumps(services)
            record.entry_path = entry
            record.chat_session_id = chat_session_id or record.chat_session_id
            if name:
                record.name = str(name)[:160]

        reachable = port_is_reachable(port)
        record.status = "running" if reachable else "unreachable"
        record.last_error = None if reachable else (
            f"No service is listening on 127.0.0.1:{port}"
        )
        if reachable:
            record.last_connected_at = now
        db.commit()
        db.refresh(record)
        return _serialize(record, reachable=reachable)


def start_static_session(
    *,
    owner: Optional[str],
    workspace_path: str,
    port: Optional[int] = None,
    entry_path: str = "/",
    chat_session_id: Optional[str] = None,
    name: Optional[str] = None,
    agent_ide_id: Optional[str] = None,
    service_ports: object = None,
) -> dict:
    workspace = validate_workspace(workspace_path)
    selected_port = validate_port(port) if port else allocate_preview_port()
    previous = None
    if agent_ide_id:
        previous = get_session(owner, agent_ide_id)
    else:
        previous = next(
            (
                item for item in list_sessions(owner)
                if item["workspace_path"] == workspace
            ),
            None,
        )
        if previous:
            previous = get_session(owner, previous["id"])
    if previous and managed_process_matches(previous):
        kill_process_tree(previous.managed_pid)
    if port_is_reachable(selected_port):
        raise ValueError(
            f"Port {selected_port} is already in use; connect to it instead "
            "or choose another static preview port"
        )

    result = connect_session(
        owner=owner,
        workspace_path=workspace,
        port=selected_port,
        entry_path=entry_path,
        chat_session_id=chat_session_id,
        name=name,
        agent_ide_id=agent_ide_id,
        service_ports=service_ports,
    )

    log_dir = Path(DATA_DIR) / "agent_ide"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{result['id']}.log"
    log_handle = log_path.open("ab")
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "http.server",
                str(selected_port),
                "--bind",
                "127.0.0.1",
                "--directory",
                workspace,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            cwd=workspace,
            **detached_popen_kwargs(),
        )
    finally:
        log_handle.close()

    with SessionLocal() as db:
        record = _owner_query(db, owner).filter(
            AgentIDESession.id == result["id"]
        ).first()
        if record is None:
            kill_process_tree(proc.pid)
            raise RuntimeError("AgentIDE session disappeared during startup")
        record.managed_pid = proc.pid
        record.managed_log_path = str(log_path)
        record.status = "starting"
        record.last_error = None
        db.commit()
        db.refresh(record)
        return _serialize(record, reachable=False)


def session_status(owner: Optional[str], agent_ide_id: str) -> dict:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with SessionLocal() as db:
        record = _owner_query(db, owner).filter(
            AgentIDESession.id == str(agent_ide_id)
        ).first()
        if record is None:
            raise ValueError("AgentIDE session was not found")
        reachable = port_is_reachable(record.port)
        managed_dead = bool(record.managed_pid) and not managed_process_matches(record)
        if reachable:
            record.status = "running"
            record.last_error = None
            record.last_connected_at = now
        elif managed_dead:
            record.status = "failed"
            record.last_error = "The managed preview process stopped"
        else:
            record.status = "unreachable"
            record.last_error = (
                f"No service is listening on 127.0.0.1:{record.port}"
            )
        db.commit()
        db.refresh(record)
        payload = _serialize(record, reachable=reachable)
        payload["revision"] = workspace_revision(record.workspace_path)
        return payload


def stop_session(
    owner: Optional[str],
    agent_ide_id: str,
    *,
    delete: bool = False,
) -> dict:
    with SessionLocal() as db:
        record = _owner_query(db, owner).filter(
            AgentIDESession.id == str(agent_ide_id)
        ).first()
        if record is None:
            raise ValueError("AgentIDE session was not found")
        if record.managed_pid and managed_process_matches(record):
            kill_process_tree(record.managed_pid)
        payload = _serialize(record, reachable=False)
        if delete:
            db.delete(record)
        else:
            record.status = "stopped"
            record.managed_pid = None
            record.last_error = None
        db.commit()
        payload["status"] = "deleted" if delete else "stopped"
        return payload


def read_managed_log(
    owner: Optional[str],
    agent_ide_id: str,
    *,
    limit: int = 12000,
) -> str:
    record = get_session(owner, agent_ide_id)
    if record is None or not record.managed_log_path:
        return ""
    try:
        text = Path(record.managed_log_path).read_text(
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return ""
    return text[-max(1000, min(int(limit), 50000)):]

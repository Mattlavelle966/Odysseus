"""Central model capability/profile adapters.

Only the Ornith profile changes behavior. The default profile is intentionally
an identity adapter so existing model prompts, tools, parsing, and generation
settings retain their prior path.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import jsonschema


_ORNITH_ID_RE = re.compile(
    r"(?:^|[/_.:-])ornith(?:[-_.]?1(?:\.0)?)?(?:[-_.:]|$)", re.IGNORECASE
)
_PSEUDO_TOOL_RE = re.compile(
    r"(?is)(?:tool[_ ]?calls?|function[_ ]?call|<tool_call>|"
    r"```(?:json|tool|function)[^\n]*\n)"
)
_DOC_TOOLS = {"create_document", "edit_document", "update_document"}


def _document_value(document: Any, field: str, default: Any = None) -> Any:
    """Read document context from either API dictionaries or ORM objects."""
    if document is None:
        return default
    if isinstance(document, Mapping):
        return document.get(field, default)
    return getattr(document, field, default)


@dataclass
class ToolValidation:
    calls: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pseudo_tool_call: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.calls) and not self.errors


class ModelProfile:
    name = "default"
    structured_tools_only = False
    preserve_reasoning_separately = False

    def adapt_tool_schemas(
        self,
        schemas: list[dict],
        *,
        allowed_names: Optional[Iterable[str]] = None,
        active_document: Optional[dict] = None,
    ) -> list[dict]:
        return schemas

    def agent_instruction(
        self,
        *,
        objective: str,
        allowed_names: Iterable[str],
        terminal_state: str,
        active_document: Optional[dict] = None,
    ) -> str:
        return ""

    def validate_tool_calls(
        self,
        calls: list[dict],
        schemas: list[dict],
        *,
        response_text: str = "",
    ) -> ToolValidation:
        return ToolValidation(calls=list(calls or []))

    def correction_message(self, schemas: list[dict], errors: list[str]) -> str:
        return ""

    def document_tools_for_objective(self, objective: str) -> set[str]:
        return set(_DOC_TOOLS)

    def normal_content(self, message: dict) -> str:
        return message.get("content") or message.get("reasoning_content") or ""

    def reasoning_content(self, message: dict) -> str:
        return str(message.get("reasoning_content") or "")


class OrnithProfile(ModelProfile):
    name = "ornith"
    structured_tools_only = True
    preserve_reasoning_separately = True

    def adapt_tool_schemas(
        self,
        schemas: list[dict],
        *,
        allowed_names: Optional[Iterable[str]] = None,
        active_document: Optional[dict] = None,
    ) -> list[dict]:
        allowed = set(allowed_names or ())
        selected = []
        for schema in schemas or []:
            name = str((schema.get("function") or {}).get("name") or "")
            if not name or (allowed and name not in allowed):
                continue
            item = copy.deepcopy(schema)
            if name in _DOC_TOOLS:
                item = self._document_schema(name, item, active_document)
            selected.append(item)
        # Ornith is more reliable with a small, exact tool set. The caller's
        # intent/RAG allowlist determines membership. Put the task's execution
        # tools ahead of incidental globally-registered schemas before capping:
        # registration order previously let search/read helpers crowd out
        # edit_file, write_file, or AgentIDE on an implementation turn.
        names = {
            str((item.get("function") or {}).get("name") or "")
            for item in selected
        }
        if "agent_ide" in names:
            priority = (
                "agent_ide", "bash", "read_file", "edit_file", "write_file",
                "grep", "glob", "ls",
            )
        elif names & _DOC_TOOLS:
            priority = (
                "create_document", "update_document", "edit_document",
                "read_file", "bash", "edit_file", "write_file", "ask_user",
            )
        else:
            priority = (
                "bash", "read_file", "edit_file", "write_file", "grep",
                "glob", "ls", "python", "web_search", "web_fetch",
            )
        rank = {name: index for index, name in enumerate(priority)}
        selected.sort(
            key=lambda item: rank.get(
                str((item.get("function") or {}).get("name") or ""),
                len(rank),
            )
        )
        return selected[:8]

    @staticmethod
    def _document_schema(
        name: str, schema: dict, active_document: Any
    ) -> dict:
        fn = schema["function"]
        if name == "create_document":
            fn["description"] = (
                "Create one editor document. Return this structured call; do not "
                "describe or simulate it. Example: "
                '{"title":"Notes","language":"markdown","content":"# Notes"}'
            )
            return schema

        doc_id = str(_document_value(active_document, "id", "") or "")
        version = int(
            _document_value(active_document, "version_count", 0) or 0
        )
        if name == "update_document":
            fn["description"] = (
                f"Replace the complete active document {doc_id!r}. "
                f"Its current version is {version}. Use the exact id/version."
            )
            fn["parameters"] = {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "document_id": {"type": "string", "const": doc_id},
                    "expected_version": {"type": "integer", "const": version},
                    "content": {"type": "string"},
                },
                "required": ["document_id", "expected_version", "content"],
            }
            return schema

        fn["description"] = (
            f"Apply one validated structured operation to active document {doc_id!r}, "
            f"currently version {version}. Read/version-check before editing. "
            "Do not print Markdown tool syntax."
        )
        fn["parameters"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "document_id": {"type": "string", "const": doc_id},
                "expected_version": {"type": "integer", "const": version},
                "operation": {
                    "type": "string",
                    "enum": [
                        "read", "insert", "replace_range", "exact_patch",
                        "append", "replace_all",
                    ],
                },
                "start": {"type": "integer", "minimum": 0},
                "end": {"type": "integer", "minimum": 0},
                "text": {"type": "string"},
                "find": {"type": "string", "minLength": 1},
                "replace": {"type": "string"},
            },
            "required": ["document_id", "expected_version", "operation"],
            "allOf": [
                {
                    "if": {"properties": {"operation": {"const": "insert"}}},
                    "then": {"required": ["start", "text"]},
                },
                {
                    "if": {"properties": {"operation": {"const": "replace_range"}}},
                    "then": {"required": ["start", "end", "text"]},
                },
                {
                    "if": {"properties": {"operation": {"const": "exact_patch"}}},
                    "then": {"required": ["find", "replace"]},
                },
                {
                    "if": {
                        "properties": {
                            "operation": {"enum": ["append", "replace_all"]}
                        }
                    },
                    "then": {"required": ["text"]},
                },
            ],
        }
        return schema

    def document_tools_for_objective(self, objective: str) -> set[str]:
        request = str(objective or "").lower()
        explicitly_separate_document = bool(
            re.search(
                r"\b(create|make|save|duplicate|copy)\b.{0,60}"
                r"\b(separate|another|new)\s+(?:doc|document|file|copy)\b",
                request,
            )
            or re.search(
                r"\b(separate|another)\s+(?:copy|doc|document|file)\b",
                request,
            )
        )
        if explicitly_separate_document:
            return {"create_document"}
        # A "v2", "new version", or "next revision" belongs to the immutable
        # version history of the active document. update_document replaces the
        # current contents under the same document id and stores the next
        # DocumentVersion row atomically.
        if re.search(
            r"\b(v\d+(?:\.\d+)?|new\s+version|next\s+(?:version|revision)|"
            r"version\s+\d+|revision\s+\d+)\b",
            request,
        ):
            return {"update_document"}
        if re.search(
            r"\b(create|make|start)\b.{0,30}\b(new\s+)?document\b",
            request,
        ):
            return {"create_document"}
        if re.search(
            r"\b(replace|rewrite|overwrite)\b.{0,40}"
            r"\b(entire|complete|whole|all)\b",
            request,
        ):
            return {"update_document"}
        return {"edit_document"}

    def agent_instruction(
        self,
        *,
        objective: str,
        allowed_names: Iterable[str],
        terminal_state: str,
        active_document: Optional[dict] = None,
    ) -> str:
        tools = ", ".join(str(name) for name in allowed_names) or "(none)"
        doc = ""
        if active_document:
            doc_id = _document_value(active_document, "id", "")
            version = _document_value(active_document, "version_count", 0)
            doc = (
                f"\nActive document id: {doc_id}. "
                f"Version: {version}."
            )
            allowed = set(allowed_names)
            if "create_document" in allowed and not (
                {"edit_document", "update_document"} & allowed
            ):
                doc += (
                    "\nCreate a separate document from the active document "
                    "context. Preserve the active document unchanged."
                )
            elif "update_document" in allowed:
                doc += (
                    "\nReplace the active document under this exact same id. "
                    "A successful update creates the next immutable version in "
                    "its version history; do not create a second document."
                )
            elif "edit_document" in allowed:
                doc += (
                    "\nEdit the active document under this exact same id. A "
                    "successful edit creates the next immutable version in its "
                    "version history."
                )
        return (
            "ORNITH OPERATION MODE\n"
            f"Objective: {objective.strip()[:1200]}\n"
            f"Allowed tools only: {tools}.\n"
            f"Required terminal state: {terminal_state}."
            f"{doc}\n"
            "Use real structured tool_calls. Do not explain tool syntax, print "
            "simulated calls, or put calls in Markdown. Continue until the "
            "required state is confirmed by a successful tool result or return "
            "one concrete error."
        )

    def validate_tool_calls(
        self,
        calls: list[dict],
        schemas: list[dict],
        *,
        response_text: str = "",
    ) -> ToolValidation:
        offered = {
            str((schema.get("function") or {}).get("name") or ""): schema
            for schema in schemas or []
            if (schema.get("function") or {}).get("name")
        }
        result = ToolValidation(
            pseudo_tool_call=bool(response_text and _PSEUDO_TOOL_RE.search(response_text))
        )
        for index, call in enumerate(calls or []):
            if not isinstance(call, dict):
                result.errors.append(f"call {index}: not an object")
                continue
            name = str(call.get("name") or "").strip()
            if name not in offered:
                result.errors.append(f"call {index}: tool {name!r} was not offered")
                continue
            raw_args = call.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (TypeError, json.JSONDecodeError):
                result.errors.append(f"call {index}: arguments are not valid JSON")
                continue
            if not isinstance(args, dict):
                result.errors.append(f"call {index}: arguments must be an object")
                continue
            parameters = (offered[name].get("function") or {}).get("parameters") or {}
            try:
                jsonschema.validate(args, parameters)
            except jsonschema.ValidationError as exc:
                result.errors.append(f"call {index}: {exc.message}")
                continue
            result.calls.append(
                {
                    "id": call.get("id") or f"call_{index}",
                    "name": name,
                    "arguments": json.dumps(args, separators=(",", ":")),
                }
            )
        return result

    def correction_message(self, schemas: list[dict], errors: list[str]) -> str:
        compact = [
            {
                "name": (schema.get("function") or {}).get("name"),
                "parameters": (schema.get("function") or {}).get("parameters"),
            }
            for schema in schemas or []
        ]
        return (
            "Your structured tool call was invalid. Retry once using exactly one "
            "offered function and valid JSON arguments. Do not output prose, "
            "Markdown, or simulated calls.\nAllowed schemas:\n"
            + json.dumps(compact, separators=(",", ":"))
        )

    def normal_content(self, message: dict) -> str:
        # Private reasoning is never promoted to the assistant response.
        return str(message.get("content") or "")


DEFAULT_PROFILE = ModelProfile()
ORNITH_PROFILE = OrnithProfile()


def _endpoint_override(endpoint_url: str) -> str:
    if not endpoint_url:
        return ""
    try:
        from core.database import ModelEndpoint, SessionLocal
        from src.endpoint_resolver import build_chat_url

        target = endpoint_url.rstrip("/")
        db = SessionLocal()
        try:
            for row in db.query(ModelEndpoint).all():
                if build_chat_url(row.base_url).rstrip("/") == target:
                    return str(getattr(row, "model_profile", "") or "")
        finally:
            db.close()
    except Exception:
        return ""
    return ""


def get_model_profile(
    model: str,
    *,
    endpoint_url: str = "",
    override: Optional[str] = None,
) -> ModelProfile:
    selected = str(override if override is not None else _endpoint_override(endpoint_url)).lower()
    if selected in {"default", "none", "off"}:
        return DEFAULT_PROFILE
    if selected == "ornith":
        return ORNITH_PROFILE
    if _ORNITH_ID_RE.search(str(model or "")):
        return ORNITH_PROFILE
    return DEFAULT_PROFILE


async def diagnose_ornith_endpoint(
    *,
    endpoint_url: str,
    model: str,
    headers: Optional[dict] = None,
    timeout: int = 30,
) -> dict:
    """Ask for one harmless function and verify a real OpenAI tool_calls object."""
    import httpx

    schema = {
        "type": "function",
        "function": {
            "name": "report_probe",
            "description": "Return the fixed diagnostic value.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "value": {"type": "string", "const": "ornith-tool-probe"}
                },
                "required": ["value"],
            },
        },
    }
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Call report_probe exactly once with value ornith-tool-probe. "
                    "Do not print or explain the call."
                ),
            }
        ],
        "tools": [schema],
        "tool_choice": {"type": "function", "function": {"name": "report_probe"}},
        "temperature": 0,
        "max_tokens": 128,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            endpoint_url,
            json=payload,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
    response.raise_for_status()
    data = response.json()
    message = ((data.get("choices") or [{}])[0].get("message") or {})
    raw_calls = message.get("tool_calls")
    calls = []
    for item in raw_calls or []:
        fn = item.get("function") or {}
        calls.append(
            {
                "id": item.get("id"),
                "name": fn.get("name"),
                "arguments": fn.get("arguments"),
            }
        )
    validation = ORNITH_PROFILE.validate_tool_calls(
        calls, [schema], response_text=str(message.get("content") or "")
    )
    real = validation.ok and validation.calls[0]["name"] == "report_probe"
    warning = ""
    if not real:
        warning = (
            "The endpoint did not emit a valid OpenAI tool_calls object. "
            "Textual pseudo-tool calls are never executed. For llama.cpp, use "
            "a tool-capable chat template with Jinja/tool-call support enabled."
        )
    return {
        "ok": real,
        "profile": "ornith",
        "structured_tool_calls": real,
        "reasoning_content_present": bool(message.get("reasoning_content")),
        "pseudo_tool_call": validation.pseudo_tool_call,
        "errors": validation.errors,
        "warning": warning,
        "llama_cpp_hint": (
            "Tool-capable llama.cpp serving requires a compatible chat template "
            "and Jinja/tool-call support."
        ),
    }

"""Administration API for deterministic email research automation."""

from __future__ import annotations

import email
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from core.database import EmailAccount, EmailResearchJob, ModelEndpoint, SessionLocal
from src.auth_helpers import require_user
from src.email_research import (
    AUTOMATION_HEADER,
    AUTOMATION_VALUE,
    EmailResearchConfig,
    EmailResearchWorker,
    build_acknowledgement,
    build_research_prompt,
    parse_inbound,
    rejection_reason,
)
from src.settings import load_settings, save_settings
from src.tool_security import owner_is_admin_or_single_user


def setup_email_research_routes(worker: EmailResearchWorker) -> APIRouter:
    router = APIRouter(prefix="/api/email-research", tags=["email-research"])

    def require_admin(request: Request) -> str:
        owner = require_user(request)
        if not owner_is_admin_or_single_user(owner):
            raise HTTPException(403, "Admin only")
        return owner

    def account_for_owner(account_id: str, owner: str) -> EmailAccount:
        db = SessionLocal()
        try:
            row = db.query(EmailAccount).filter(
                EmailAccount.id == account_id,
                EmailAccount.enabled == True,  # noqa: E712
            ).first()
            if not row or (owner and row.owner not in {None, "", owner}):
                raise HTTPException(404, "Email account not found")
            db.expunge(row)
            return row
        finally:
            db.close()

    @router.get("/settings")
    async def get_automation_settings(request: Request):
        owner = require_admin(request)
        settings = load_settings().get("email_research_automation", {}) or {}
        db = SessionLocal()
        try:
            accounts = db.query(EmailAccount).filter(
                EmailAccount.enabled == True  # noqa: E712
            )
            endpoints = db.query(ModelEndpoint).filter(
                ModelEndpoint.is_enabled == True,  # noqa: E712
                ModelEndpoint.model_type != "image",
            )
            if owner:
                accounts = accounts.filter(
                    (EmailAccount.owner == owner)
                    | (EmailAccount.owner.is_(None))
                    | (EmailAccount.owner == "")
                )
                endpoints = endpoints.filter(
                    (ModelEndpoint.owner == owner) | (ModelEndpoint.owner.is_(None))
                )
            return {
                "settings": settings,
                "accounts": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "from_address": row.from_address,
                        "has_inbox": bool(row.imap_host and row.imap_user),
                        "has_outbox": bool(row.smtp_host and row.from_address),
                    }
                    for row in accounts.order_by(
                        EmailAccount.is_default.desc(), EmailAccount.created_at.asc()
                    ).all()
                ],
                "endpoints": [
                    {
                        "id": row.id,
                        "name": row.name,
                        "cached_models": json.loads(row.cached_models or "[]"),
                    }
                    for row in endpoints.order_by(ModelEndpoint.name.asc()).all()
                ],
            }
        finally:
            db.close()

    @router.put("/settings")
    async def update_automation_settings(request: Request):
        owner = require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, "Settings must be an object")
        account_id = str(body.get("email_account_id") or "").strip()
        if body.get("enabled") and not account_id:
            raise HTTPException(400, "Select an email account before enabling automation")
        if account_id:
            account = account_for_owner(account_id, owner)
            if not (account.imap_host and account.imap_user):
                raise HTTPException(400, "Selected account has no inbox configuration")
            if not (account.smtp_host and account.from_address):
                raise HTTPException(400, "Selected account has no outbound configuration")

        allowed = set(EmailResearchConfig.__dataclass_fields__)
        unknown = sorted(set(body) - allowed)
        if unknown:
            raise HTTPException(400, f"Unknown setting(s): {', '.join(unknown)}")
        for key in (
            "allowed_sender_addresses",
            "allowed_sender_domains",
            "blocked_sender_patterns",
            "strip_subject_prefixes",
        ):
            if key in body and not isinstance(body[key], (list, str)):
                raise HTTPException(400, f"{key} must be a list or comma-separated string")
            if isinstance(body.get(key), str):
                body[key] = [
                    item.strip()
                    for item in body[key].replace("\n", ",").split(",")
                    if item.strip()
                ]
        current = load_settings()
        current["email_research_automation"] = body
        save_settings(current)
        normalized = EmailResearchConfig.load().__dict__
        normalized = {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in normalized.items()
        }
        current = load_settings()
        current["email_research_automation"] = normalized
        save_settings(current)
        return {"settings": normalized}

    @router.get("/status")
    async def automation_status(request: Request, limit: int = 100):
        owner = require_admin(request)
        return worker.dashboard(owner, limit=max(1, min(500, int(limit))))

    @router.post("/run-now")
    async def run_now(request: Request):
        require_admin(request)
        return await worker.run_now()

    @router.post("/jobs/{job_id}/retry")
    async def retry_job(job_id: str, request: Request):
        owner = require_admin(request)
        if not worker.retry_job(job_id, owner):
            raise HTTPException(409, "Job is not retryable")
        return {"result": "retrying", "job_id": job_id}

    @router.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request):
        owner = require_admin(request)
        if not worker.cancel_job(job_id, owner):
            raise HTTPException(409, "Only queued/retrying jobs can be cancelled")
        return {"result": "cancelled", "job_id": job_id}

    @router.post("/test-acknowledgement")
    async def test_acknowledgement(request: Request):
        owner = require_admin(request)
        config = EmailResearchConfig.load()
        if not config.email_account_id:
            raise HTTPException(400, "Select an email account first")
        account = account_for_owner(config.email_account_id, owner)
        message_id = f"<test-{uuid.uuid4().hex}@odysseus.local>"
        job = EmailResearchJob(
            id=f"test-{uuid.uuid4().hex[:12]}",
            owner=owner,
            email_account_id=account.id,
            provider_message_identifier="test",
            rfc_message_id=message_id,
            sender_address=account.from_address,
            reply_to_address=account.from_address,
            original_recipient=account.from_address,
            original_subject="Email Research Automation test",
            sanitized_request_text="Harmless test request",
            request_hash="0" * 64,
            received_at=datetime.now(timezone.utc).replace(tzinfo=None),
            acknowledgement_idempotency_key=f"test-ack-{uuid.uuid4().hex}",
            final_delivery_idempotency_key=f"test-final-{uuid.uuid4().hex}",
        )
        msg = build_acknowledgement(job, config, account.from_address)
        provider_id = await __import__("asyncio").to_thread(
            worker.mail.deliver,
            account.id,
            owner,
            account.from_address,
            msg,
        )
        return {"result": "success", "provider_message_id": provider_id}

    @router.post("/test-lifecycle")
    async def test_lifecycle(request: Request):
        """Dry-run the parser, policy, prompt boundary, and renderer with mock mail."""
        owner = require_admin(request)
        config = EmailResearchConfig.load()
        account = account_for_owner(config.email_account_id, owner) if config.email_account_id else None
        raw = (
            "From: approved@example.test\r\n"
            f"To: {(account.from_address if account else 'research@example.test')}\r\n"
            "Subject: Research: lifecycle test\r\n"
            f"Message-ID: <mock-{uuid.uuid4().hex}@example.test>\r\n"
            "Auto-Submitted: no\r\n"
            "\r\n"
            "Research a harmless mocked lifecycle topic.\r\n"
        ).encode()
        inbound = parse_inbound("mock-1", raw, config)
        reason = rejection_reason(
            inbound, config, account.from_address if account else ""
        )
        prompt = build_research_prompt(inbound, config)
        return {
            "result": "success" if reason is None else "rejected",
            "dry_run": True,
            "steps": [
                "received",
                "filtered",
                "acknowledged",
                "research_queued",
                "researching",
                "research_complete",
                "delivering",
                "delivered",
            ],
            "policy_result": reason[0] if reason else "accepted",
            "prompt_is_delimited": (
                "BEGIN UNTRUSTED EMAIL REQUEST" in prompt
                and "END UNTRUSTED EMAIL REQUEST" in prompt
            ),
            "external_services_called": False,
        }

    return router

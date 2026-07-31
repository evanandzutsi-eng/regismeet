"""
alert_system.py  (Vercel-adapted)

The original design used a raw FastAPI WebSocket ConnectionManager to push
live processing-state toasts to the dashboard. Vercel serverless functions
cannot hold an open socket connection between requests, so this module now
publishes status changes onto a Supabase Realtime broadcast channel instead —
you already run Supabase for the database, so this adds no new vendor.
`RealtimeBanner.jsx` subscribes to that channel directly with `supabase-js`
from the browser; this module is only ever the *publisher*.

Resend (transactional email) and Slack webhook delivery were already
stateless HTTP calls via httpx, so they need no changes for serverless and are
kept as-is here.
"""
from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from supabase import create_client, Client

from app_config import get_settings
from audio_webhook import TenantContext, get_tenant_context

router = APIRouter()
settings = get_settings()
logger = logging.getLogger("meeting_intel.alert_system")

_supabase: Client = create_client(
    str(settings.SUPABASE_URL), settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
)


def _realtime_channel_name(company_id: str) -> str:
    return f"intel_core:webinars:{company_id}"


async def broadcast_processing_update(company_id: str, webinar_id: str, state: str, title: str = "") -> None:
    """
    Publishes a processing-state transition onto the tenant's Realtime
    broadcast channel. Called from api/process_job.py as the pipeline
    progresses through QUEUED -> TRANSCRIBING -> SUMMARIZING -> COMPLETED.
    """
    channel = _supabase.realtime.channel(_realtime_channel_name(company_id))
    try:
        await channel.send_broadcast(
            "processing_update",
            {"webinar_id": webinar_id, "state": state, "title": title},
        )
    except Exception as exc:  # noqa: BLE001
        # A missed realtime push must never fail the underlying pipeline —
        # the dashboard falls back to polling /api/v1/webinars/{id}/status.
        logger.error(f"realtime_broadcast_failed company_id={company_id} error={str(exc)[:200]}")


class EmailAlertRequest(BaseModel):
    recipient_email: EmailStr
    webinar_id: str = Field(..., min_length=1)
    meeting_title: str = Field(..., min_length=1, max_length=300)
    executive_summary_html: str = Field(..., min_length=1)


@router.post("/api/v1/alerts/email", status_code=status.HTTP_202_ACCEPTED)
async def send_summary_email(
    body: EmailAlertRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Emails the rendered HTML transcript summary to a stakeholder via Resend.
    The Resend API key is only ever used server-side (Pillar 8).
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {settings.RESEND_API_KEY.get_secret_value()}"},
            json={
                "from": settings.RESEND_FROM_ADDRESS,
                "to": [body.recipient_email],
                "subject": f"Meeting Summary: {body.meeting_title}",
                "html": body.executive_summary_html,
            },
        )

    if response.status_code >= 300:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Unable to send the summary email right now.")

    return {"status": "queued"}


class SlackAlertRequest(BaseModel):
    webinar_id: str = Field(..., min_length=1)
    meeting_title: str = Field(..., min_length=1, max_length=300)
    executive_summary: str = Field(..., min_length=1, max_length=2000)


@router.post("/api/v1/alerts/slack", status_code=status.HTTP_202_ACCEPTED)
async def send_slack_alert(
    body: SlackAlertRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    result = (
        _supabase.schema("intel_core")
        .table("companies")
        .select("slack_webhook_url")
        .eq("id", tenant.company_id)
        .single()
        .execute()
    )
    webhook_url = (result.data or {}).get("slack_webhook_url")
    if not webhook_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="No Slack webhook configured for this organization.")

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(
            webhook_url,
            json={
                "text": f"*{body.meeting_title}* — new summary ready.\n{body.executive_summary[:500]}",
            },
        )

    if response.status_code >= 300:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Slack rejected the notification.")

    return {"status": "sent"}

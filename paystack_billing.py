"""
paystack_billing.py
Server-side Paystack integration. The browser never sees the secret key
(Pillar 8) — it only ever receives a Paystack-hosted authorization_url to
redirect to, and this module is the sole verifier of inbound webhook events.
"""
from __future__ import annotations

import hashlib
import hmac

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field
from supabase import create_client, Client

from app_config import get_settings
from audio_webhook import TenantContext, get_tenant_context

router = APIRouter()
settings = get_settings()

_supabase: Client = create_client(
    str(settings.SUPABASE_URL), settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
)

_PAYSTACK_BASE_URL = "https://api.paystack.co"


class CheckoutRequest(BaseModel):
    plan_code: str = Field(..., min_length=1, max_length=100)
    billing_email: EmailStr


class CheckoutResponseDTO(BaseModel):
    authorization_url: str
    reference: str


@router.post("/checkout", response_model=CheckoutResponseDTO, status_code=status.HTTP_200_OK)
async def initiate_checkout(
    body: CheckoutRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """
    Proxies a checkout-initialization call to Paystack using the server-held
    secret key. The client only ever receives the resulting hosted checkout
    URL, never the key itself.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            f"{_PAYSTACK_BASE_URL}/transaction/initialize",
            headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET_KEY.get_secret_value()}"},
            json={
                "email": body.billing_email,
                "plan": body.plan_code,
                "metadata": {"company_id": tenant.company_id},
            },
        )

    if response.status_code != 200:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Unable to initialize checkout at this time.")

    payload = response.json()
    data = payload.get("data", {})
    if not data.get("authorization_url") or not data.get("reference"):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Checkout provider returned an unexpected response.")

    return CheckoutResponseDTO(authorization_url=data["authorization_url"], reference=data["reference"])


def _verify_paystack_signature(secret: str, raw_body: bytes, signature_header: str) -> bool:
    computed = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(computed, signature_header or "")


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def paystack_webhook(request: Request, x_paystack_signature: str = Header(default="")):
    raw_body = await request.body()

    if not _verify_paystack_signature(
        settings.PAYSTACK_SECRET_KEY.get_secret_value(), raw_body, x_paystack_signature
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature.")

    import json
    event = json.loads(raw_body)
    event_type = event.get("event")
    data = event.get("data", {})

    if event_type == "subscription.create":
        company_id = (data.get("metadata") or {}).get("company_id")
        customer_code = (data.get("customer") or {}).get("customer_code")
        subscription_code = data.get("subscription_code")
        plan_name = (data.get("plan") or {}).get("name", "starter")

        if company_id:
            _supabase.schema("intel_core").table("companies").update(
                {
                    "paystack_customer_code": customer_code,
                    "paystack_subscription_code": subscription_code,
                    "subscription_status": "active",
                    "plan_tier": plan_name,
                }
            ).eq("id", company_id).execute()

    elif event_type == "subscription.disable":
        subscription_code = data.get("subscription_code")
        if subscription_code:
            _supabase.schema("intel_core").table("companies").update(
                {"subscription_status": "disabled"}
            ).eq("paystack_subscription_code", subscription_code).execute()

    # Unhandled event types are acknowledged (200) but ignored — Paystack
    # retries on non-2xx, and we only act on the two events this platform maps.
    return {"received": True}

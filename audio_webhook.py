"""
audio_webhook.py
Ingestion router for both interactive dashboard uploads and inbound signed
webhook streams. Every code path validates the true binary content
(file_processor), enforces per-org quotas, rate-limits against the AI budget,
and enqueues work onto the Redis pipeline — never processes inline.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
import uuid
from typing import Optional

import httpx
import jwt
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field
from supabase import create_client, Client

from app_config import get_settings
from file_processor import FileValidationError, validate_audio_stream
from redis_queue_engine import AudioJob, ProcessingState, RedisQueueEngine
from ai_summarizer import embed_text

router = APIRouter()
settings = get_settings()

_supabase: Client = create_client(
    str(settings.SUPABASE_URL), settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
)


# =========================================================================================
# AUTH DEPENDENCY — verifies the Supabase-issued JWT and extracts the tenant claim.
# This is the single source of truth for `company_id` throughout this router;
# it is NEVER accepted as a client-supplied form/body field (Pillar 5: anti-BOLA).
# =========================================================================================
class TenantContext(BaseModel):
    company_id: str
    user_id: str


async def get_tenant_context(authorization: str = Header(...)) -> TenantContext:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token.")
    token = authorization.removeprefix("Bearer ").strip()
    try:
        claims = jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET.get_secret_value(),
            algorithms=[settings.JWT_ALGORITHM],
            audience="authenticated",
        )
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session.")

    company_id = claims.get("organization_id")
    user_id = claims.get("sub")
    if not company_id or not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Token missing tenant claim.")
    return TenantContext(company_id=company_id, user_id=user_id)


# =========================================================================================
# RESPONSE DTOs — internal table shapes never cross the API boundary directly (Pillar 7)
# =========================================================================================
class WebinarIngestResponseDTO(BaseModel):
    webinar_id: str
    title: str
    processing_state: str
    source_channel: str


async def _enforce_quota(company_id: str, estimated_minutes: float) -> None:
    result = (
        _supabase.schema("intel_core")
        .table("companies")
        .select("monthly_audio_minutes_limit, processing_usage_this_month")
        .eq("id", company_id)
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Organization not found.")

    limit = result.data["monthly_audio_minutes_limit"]
    used = float(result.data["processing_usage_this_month"])
    if used + estimated_minutes > limit:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            detail="Monthly audio processing quota exceeded for this organization.",
        )


async def _enforce_ai_rate_limit(request: Request, company_id: str) -> None:
    queue_engine: RedisQueueEngine = request.app.state.queue_engine
    allowed = await queue_engine.consume_ai_rate_limit_token(
        bucket_key=f"ai:{company_id}",
        capacity=settings.RATE_LIMIT_AI_CAPACITY,
        refill_per_minute=settings.RATE_LIMIT_AI_REFILL_PER_MINUTE,
    )
    if not allowed:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            detail="AI processing rate limit exceeded. Please retry shortly.",
        )


def _rough_duration_minutes_estimate(size_bytes: int) -> float:
    # Conservative heuristic (128kbps ~ 16KB/s) used only to gate obviously-over-quota
    # uploads pre-transcription; the authoritative usage figure is recorded post-processing.
    return round((size_bytes / (16 * 1024)) / 60.0, 2)


# =========================================================================================
# ENDPOINT — Interactive dashboard upload (Vercel-adapted: presigned direct-to-storage)
#
# Vercel serverless functions cap request bodies well under the platform's 25MB
# audio ceiling, so the browser no longer streams the file through this API at
# all. Flow: (1) client asks this endpoint for a short-lived signed upload URL,
# (2) client PUTs the file straight to Supabase Storage, (3) client calls
# /upload/confirm with the returned object path, which is where the bytes are
# actually fetched back server-side and validated before anything is trusted.
# =========================================================================================
class UploadUrlRequest(BaseModel):
    filename: str = Field(..., min_length=1, max_length=255)
    declared_size_bytes: int = Field(..., gt=0, le=settings.MAX_REQUEST_BODY_BYTES)


class UploadUrlResponseDTO(BaseModel):
    object_path: str
    signed_upload_url: str
    expires_in_seconds: int


@router.post("/upload-url", response_model=UploadUrlResponseDTO, status_code=status.HTTP_200_OK)
async def request_upload_url(
    request: Request,
    body: UploadUrlRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    await _enforce_ai_rate_limit(request, tenant.company_id)
    await _enforce_quota(tenant.company_id, _rough_duration_minutes_estimate(body.declared_size_bytes))

    ext = os.path.splitext(body.filename)[1].lower()
    if ext not in settings.ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Unsupported file extension.")

    object_path = f"{tenant.company_id}/{uuid.uuid4().hex}{ext}"

    signed = _supabase.storage.from_(settings.SUPABASE_AUDIO_BUCKET).create_signed_upload_url(
        object_path
    )

    return UploadUrlResponseDTO(
        object_path=object_path,
        signed_upload_url=signed["signed_url"],
        expires_in_seconds=settings.SIGNED_UPLOAD_URL_TTL_SECONDS,
    )


class ConfirmUploadRequest(BaseModel):
    object_path: str = Field(..., min_length=1, max_length=400)
    title: str = Field(default="", max_length=300)


@router.post("/upload/confirm", response_model=WebinarIngestResponseDTO, status_code=status.HTTP_202_ACCEPTED)
async def confirm_webinar_upload(
    request: Request,
    body: ConfirmUploadRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    # BOLA guard: a tenant can only confirm objects living under their own
    # company_id prefix, regardless of what object_path string they submit.
    if not body.object_path.startswith(f"{tenant.company_id}/"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Object does not belong to this organization.")

    try:
        raw_bytes: bytes = _supabase.storage.from_(settings.SUPABASE_AUDIO_BUCKET).download(
            body.object_path
        )
    except Exception:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Uploaded object not found.")

    try:
        validated = validate_audio_stream(raw_bytes, os.path.basename(body.object_path))
    except FileValidationError as exc:
        # Reject and remove — never let an invalid object linger in the bucket.
        _supabase.storage.from_(settings.SUPABASE_AUDIO_BUCKET).remove([body.object_path])
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))

    await _enforce_quota(tenant.company_id, _rough_duration_minutes_estimate(validated.size_bytes))

    display_title = (body.title or os.path.basename(body.object_path) or "Untitled Meeting").strip()[:300]

    insert_result = (
        _supabase.schema("intel_core")
        .table("webinars")
        .insert(
            {
                "company_id": tenant.company_id,
                "title": display_title,
                "storage_path": body.object_path,
                "source_channel": "dashboard_upload",
                "mime_type": validated.detected_mime_type,
                "file_size_bytes": validated.size_bytes,
                "processing_state": ProcessingState.QUEUED.value,
                "uploaded_by": tenant.user_id,
            }
        )
        .execute()
    )
    webinar_row = insert_result.data[0]

    queue_engine: RedisQueueEngine = request.app.state.queue_engine
    await queue_engine.enqueue_job(
        AudioJob(
            webinar_id=webinar_row["id"],
            company_id=tenant.company_id,
            storage_path=body.object_path,
            mime_type=validated.detected_mime_type,
            enqueued_at=time.time(),
        )
    )

    return WebinarIngestResponseDTO(
        webinar_id=webinar_row["id"],
        title=display_title,
        processing_state=ProcessingState.QUEUED.value,
        source_channel="dashboard_upload",
    )


# =========================================================================================
# ENDPOINT — Signed inbound webhook stream (e.g. from a conferencing platform relay)
# =========================================================================================
def _verify_webhook_signature(secret: str, body: bytes, signature_header: Optional[str]) -> bool:
    if not signature_header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@router.post("/webhook-stream", response_model=WebinarIngestResponseDTO, status_code=status.HTTP_202_ACCEPTED)
async def ingest_webhook_stream(
    request: Request,
    x_webhook_signature: Optional[str] = Header(default=None),
    x_organization_id: str = Header(...),
    x_meeting_title: str = Header(default="Webhook Meeting"),
):
    body = await request.body()

    org_result = (
        _supabase.schema("intel_core")
        .table("companies")
        .select("id")
        .eq("id", x_organization_id)
        .single()
        .execute()
    )
    if not org_result.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Unknown organization.")

    # Per-tenant webhook shared secret is derived rather than a single global secret,
    # binding signature validity to the specific organization_id claimed in the header.
    tenant_secret = hashlib.sha256(
        f"{settings.SUPABASE_JWT_SECRET.get_secret_value()}:{x_organization_id}".encode()
    ).hexdigest()

    if not _verify_webhook_signature(tenant_secret, body, x_webhook_signature):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature.")

    await _enforce_ai_rate_limit(request, x_organization_id)

    try:
        validated = validate_audio_stream(body, "webhook_stream.wav")
    except FileValidationError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=str(exc))

    await _enforce_quota(x_organization_id, _rough_duration_minutes_estimate(validated.size_bytes))

    # Land the bytes in Supabase Storage (not local /tmp): the processing
    # invocation in api/process_job.py runs as a separate serverless function
    # with its own isolated filesystem, so only a durable object path travels
    # through the job payload.
    object_path = f"{x_organization_id}/{validated.sanitized_filename}"
    _supabase.storage.from_(settings.SUPABASE_AUDIO_BUCKET).upload(
        object_path, body, {"content-type": validated.detected_mime_type}
    )

    insert_result = (
        _supabase.schema("intel_core")
        .table("webinars")
        .insert(
            {
                "company_id": x_organization_id,
                "title": x_meeting_title[:300],
                "storage_path": object_path,
                "source_channel": "webhook_stream",
                "mime_type": validated.detected_mime_type,
                "file_size_bytes": validated.size_bytes,
                "processing_state": ProcessingState.QUEUED.value,
            }
        )
        .execute()
    )
    webinar_row = insert_result.data[0]

    queue_engine: RedisQueueEngine = request.app.state.queue_engine
    await queue_engine.enqueue_job(
        AudioJob(
            webinar_id=webinar_row["id"],
            company_id=x_organization_id,
            storage_path=object_path,
            mime_type=validated.detected_mime_type,
            enqueued_at=time.time(),
        )
    )

    return WebinarIngestResponseDTO(
        webinar_id=webinar_row["id"],
        title=webinar_row["title"],
        processing_state=ProcessingState.QUEUED.value,
        source_channel="webhook_stream",
    )


@router.get("/{webinar_id}/status", response_model=dict)
async def get_processing_status(
    webinar_id: str,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    result = (
        _supabase.schema("intel_core")
        .table("webinars")
        .select("id, processing_state, title")
        .eq("id", webinar_id)
        .eq("company_id", tenant.company_id)  # BOLA guard: explicit tenant scoping on every lookup
        .single()
        .execute()
    )
    if not result.data:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Webinar not found.")

    queue_engine: RedisQueueEngine = request.app.state.queue_engine
    live_state = await queue_engine.get_state(webinar_id)

    return {
        "webinar_id": result.data["id"],
        "title": result.data["title"],
        "processing_state": result.data["processing_state"],
        "live_state": live_state,
    }


# =========================================================================================
# ENDPOINT — Semantic search (backs SemanticSearchCard.jsx)
#
# Embeds the caller's natural-language query with the same model/dimensions
# used for stored summaries, then calls the tenant-scoped `match_summaries`
# Postgres RPC (db_migration.sql) so cosine-similarity ranking and RLS-style
# isolation both happen inside a single, auditable database call rather than
# filtering in application code (Pillar 5 — anti-BOLA).
# =========================================================================================
class SemanticSearchRequest(BaseModel):
    query: str = Field(..., min_length=2, max_length=500)
    match_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    match_count: int = Field(default=10, ge=1, le=50)


class SemanticSearchResultDTO(BaseModel):
    summary_id: str
    webinar_id: str
    meeting_title: str
    executive_summary: str
    similarity: float


@router.post("/search/semantic", response_model=list[SemanticSearchResultDTO], status_code=status.HTTP_200_OK)
async def semantic_search(
    request: Request,
    body: SemanticSearchRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    # Query embedding is a paid OpenAI call, so it draws from the same AI
    # token bucket as transcription/summarization (Pillar 9).
    await _enforce_ai_rate_limit(request, tenant.company_id)

    try:
        query_embedding = await embed_text(body.query.strip())
    except Exception:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, detail="Unable to process the search query right now."
        )

    rpc_result = (
        _supabase.schema("intel_core")
        .rpc(
            "match_summaries",
            {
                "p_company_id": tenant.company_id,  # tenant scoping enforced inside the RPC body itself
                "p_query_embedding": query_embedding,
                "p_match_threshold": body.match_threshold,
                "p_match_count": body.match_count,
            },
        )
        .execute()
    )
    matches = rpc_result.data or []

    # Fire-and-forget telemetry: atomic UPSERT counter, never blocks the response
    # and never fails the search if the metrics write has a transient hiccup.
    try:
        _supabase.schema("intel_core").rpc(
            "increment_semantic_metric",
            {
                "p_company_id": tenant.company_id,
                "p_query_text": body.query.strip()[:500],
                "p_match_count": len(matches),
                "p_webinar_id": matches[0]["webinar_id"] if matches else None,
            },
        ).execute()
    except Exception:
        pass

    return [
        SemanticSearchResultDTO(
            summary_id=row["summary_id"],
            webinar_id=row["webinar_id"],
            meeting_title=row["meeting_title"],
            executive_summary=row["executive_summary"],
            similarity=row["similarity"],
        )
        for row in matches
    ]

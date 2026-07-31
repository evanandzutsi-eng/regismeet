"""
api/index.py  (Vercel entry point — formerly main.py)
FastAPI API Gateway, deployed as a single Vercel Python serverless function.

Wires together: security headers, restrictive CORS, a hard request-size ceiling,
a sanitized global exception handler, and the feature routers except the audio
processing pipeline itself, which now lives in api/process_job.py so it can run
under a much longer maxDuration than the interactive gateway needs.

Vercel's Python builder bundles this file alongside the project's root-level
modules (see vercel.json's `includeFiles`), but does not add the project root
to sys.path automatically in every runtime version — the shim below makes the
`import app_config` style imports reliable regardless.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import time
import traceback
import uuid
from contextlib import asynccontextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from app_config import get_settings
from redis_queue_engine import RedisQueueEngine
import audio_webhook
import paystack_billing
import alert_system

settings = get_settings()

# =========================================================================================
# SECURE LOGGING (Pillar 11: stack traces stay server-side only, never in the HTTP response)
# =========================================================================================
os.makedirs(settings.LOG_DIR, exist_ok=True)
logger = logging.getLogger("meeting_intel")
logger.setLevel(logging.INFO)
_file_handler = logging.handlers.RotatingFileHandler(
    filename=os.path.join(settings.LOG_DIR, "app.log"),
    maxBytes=25 * 1024 * 1024,
    backupCount=10,
)
_file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
logger.addHandler(_file_handler)


def mask_pii(message: str) -> str:
    """Best-effort PII scrub for anything destined for the log file (Pillar 7)."""
    import re
    message = re.sub(r"[\w.+-]+@[\w-]+\.[\w.-]+", "[REDACTED_EMAIL]", message)
    message = re.sub(r"\b\d{10,16}\b", "[REDACTED_NUMBER]", message)
    return message


# =========================================================================================
# LIFESPAN — async resource management for the Redis-backed task queue
# =========================================================================================
queue_engine = RedisQueueEngine()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await queue_engine.connect()
    logger.info("Startup complete: redis connected, gateway online.")
    yield
    await queue_engine.disconnect()
    logger.info("Shutdown complete: redis connection closed.")


app = FastAPI(
    title="Meeting Intelligence & Event Summarizer Platform",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.ENVIRONMENT != "production" else None,
    redoc_url=None,
)

# Share the queue engine and logger with routers via app.state (no globals leaking secrets)
app.state.queue_engine = queue_engine
app.state.logger = logger
app.state.mask_pii = mask_pii


# =========================================================================================
# PILLAR 4 — SECURITY HEADERS MIDDLEWARE
# =========================================================================================
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self' wss://app.meetingintel.example.com; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Strict-Transport-Security"] = (
            "max-age=63072000; includeSubDomains; preload"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
        return response


# =========================================================================================
# PILLAR 6 / hard ceiling — REQUEST BODY SIZE LIMIT (25 MB), enforced before any parsing
# =========================================================================================
class MaxBodySizeMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None and int(content_length) > settings.MAX_REQUEST_BODY_BYTES:
            return JSONResponse(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                content={"error": "payload_too_large", "message": "Request exceeds the 25MB ingest ceiling."},
            )
        return await call_next(request)


# =========================================================================================
# REQUEST CORRELATION ID (for tracing without leaking internals to the client)
# =========================================================================================
class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = str(uuid.uuid4())
        request.state.request_id = request_id
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Request-Id"] = request_id
        logger.info(f"request_id={request_id} path={request.url.path} status={response.status_code} ms={duration_ms:.1f}")
        return response


app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MaxBodySizeMiddleware)
app.add_middleware(RequestIdMiddleware)

# Pillar 10 — CORS restricted exclusively to the configured system domains
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Authorization", "Content-Type", "X-Turnstile-Token"],
    max_age=600,
)


# =========================================================================================
# PILLAR 11 — GLOBAL SANITIZED EXCEPTION HANDLER
# =========================================================================================
@app.exception_handler(Exception)
async def sanitized_exception_handler(request: Request, exc: Exception):
    request_id = getattr(request.state, "request_id", "unknown")
    logger.error(
        f"request_id={request_id} path={request.url.path} "
        f"unhandled_exception={mask_pii(str(exc))}\n{traceback.format_exc()}"
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "internal_error",
            "message": "Something went wrong while processing your request.",
            "request_id": request_id,
        },
    )


@app.get("/healthz", tags=["system"])
async def health_check():
    return {"status": "ok", "service": settings.SERVICE_NAME}


# =========================================================================================
# ROUTERS
# =========================================================================================
app.include_router(audio_webhook.router, prefix="/api/v1/webinars", tags=["webinars"])
app.include_router(paystack_billing.router, prefix="/api/v1/billing", tags=["billing"])
app.include_router(alert_system.router, tags=["alerts"])

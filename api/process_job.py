"""
api/process_job.py
The actual heavy-compute worker, reborn as a Vercel serverless function instead
of a long-running Redis consumer loop.

Upstash QStash calls this endpoint over HTTPS once per enqueued job (see
redis_queue_engine.enqueue_job), with automatic retries/backoff if it doesn't
get a 2xx back. Every request is authenticated via QStash's signed-request
verification (Upstash's public keys, rotated and checked against both the
current and next signing key) rather than a shared static secret, so this
route is safe to expose publicly.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Request, HTTPException, status
from qstash import Receiver
from supabase import create_client, Client

from app_config import get_settings
from file_processor import FileValidationError, validate_audio_stream
from redis_queue_engine import RedisQueueEngine, ProcessingState, AudioJob
from alert_system import broadcast_processing_update
import ai_summarizer

settings = get_settings()
app = FastAPI(docs_url=None, redoc_url=None)

_supabase: Client = create_client(
    str(settings.SUPABASE_URL), settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
)


def _download_object_to_tmp(storage_object_path: str) -> str:
    """
    Pulls the audio object down from Supabase Storage into this invocation's
    own /tmp. Every invocation gets a fresh, isolated /tmp, so the object path
    (not a local path) is what travels through the job payload.
    """
    raw_bytes: bytes = _supabase.storage.from_(settings.SUPABASE_AUDIO_BUCKET).download(
        storage_object_path
    )
    try:
        validated = validate_audio_stream(raw_bytes, os.path.basename(storage_object_path))
    except FileValidationError as exc:
        raise RuntimeError(f"Stored object failed re-validation: {exc}") from exc

    os.makedirs(settings.TEMP_AUDIO_DIR, exist_ok=True)
    local_path = os.path.join(settings.TEMP_AUDIO_DIR, validated.sanitized_filename)
    with open(local_path, "wb") as fh:
        fh.write(raw_bytes)
    return local_path

_receiver = Receiver(
    current_signing_key=settings.QSTASH_CURRENT_SIGNING_KEY.get_secret_value(),
    next_signing_key=settings.QSTASH_NEXT_SIGNING_KEY.get_secret_value(),
)

_queue_engine = RedisQueueEngine()


@app.post("/api/process-job", status_code=status.HTTP_200_OK)
async def process_job(request: Request):
    body_bytes = await request.body()
    signature = request.headers.get("Upstash-Signature", "")

    try:
        _receiver.verify(
            body=body_bytes.decode("utf-8"),
            signature=signature,
            url=f"{settings.PUBLIC_APP_URL}api/process-job",
        )
    except Exception:
        # Sanitized rejection — never echo verification internals back to the caller.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid job signature.")

    import json
    payload = json.loads(body_bytes)
    job = AudioJob.from_dict(payload)

    async def on_state_change(state_name: str) -> None:
        await _queue_engine.set_state(job.webinar_id, ProcessingState[state_name])
        await broadcast_processing_update(job.company_id, job.webinar_id, state_name)

    try:
        local_path = _download_object_to_tmp(job.storage_path)
    except RuntimeError as exc:
        await _queue_engine.set_state(job.webinar_id, ProcessingState.FAILED, detail=str(exc)[:200])
        return {"status": "failed", "webinar_id": job.webinar_id, "reason": "storage_fetch_failed"}

    try:
        await ai_summarizer.run_full_pipeline(
            company_id=job.company_id,
            webinar_id=job.webinar_id,
            local_file_path=local_path,
            on_state_change=on_state_change,
        )
        # Pillar 1: once the transcript/summary is durably persisted, the raw
        # audio object itself has no further purpose — remove it from Storage too.
        _supabase.storage.from_(settings.SUPABASE_AUDIO_BUCKET).remove([job.storage_path])
    except (ai_summarizer.TranscriptionError, ai_summarizer.SummarizationError) as exc:
        # Returning 200 tells QStash the job is "handled" (we've already recorded
        # FAILED state + logged internally) rather than triggering endless retries
        # against a job that will deterministically fail again (e.g. corrupt audio).
        return {"status": "failed", "webinar_id": job.webinar_id, "reason": str(exc)[:200]}

    return {"status": "completed", "webinar_id": job.webinar_id}

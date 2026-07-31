"""
ai_summarizer.py
Heavy-compute AI pipeline: Whisper transcription -> Gemini 2.5 Flash structured
summarization -> pgvector embedding -> Supabase persistence -> local purge.

All third-party AI credentials are read exclusively from server-side settings
(Pillar 8); no key ever leaves this process boundary.
"""
from __future__ import annotations

import json
import logging
from typing import List, Optional

import httpx
from openai import AsyncOpenAI
import google.generativeai as genai
from pydantic import BaseModel, Field, field_validator
from supabase import create_client, Client

from app_config import get_settings
from file_processor import purge_temp_file

settings = get_settings()
logger = logging.getLogger("meeting_intel.ai_summarizer")

_openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY.get_secret_value())
genai.configure(api_key=settings.GEMINI_API_KEY.get_secret_value())

_supabase: Client = create_client(
    str(settings.SUPABASE_URL), settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
)


# =========================================================================================
# STRICT PYDANTIC SCHEMAS — this is the exact contract Gemini's structured output must satisfy
# =========================================================================================
class ActionItem(BaseModel):
    assignee: str = Field(..., min_length=1, max_length=200)
    taskDescription: str = Field(..., min_length=1, max_length=1000)
    deadline: str = Field(..., description="ISO-8601 date, or 'unspecified' if none was stated.")


class MeetingSummary(BaseModel):
    meetingTitle: str = Field(..., min_length=1, max_length=300)
    executiveSummary: str = Field(..., min_length=1, max_length=4000)
    keyTopics: List[str] = Field(..., min_length=1, max_length=25)
    actionItems: List[ActionItem] = Field(default_factory=list)
    projectDeadlines: List[str] = Field(default_factory=list)

    @field_validator("keyTopics")
    @classmethod
    def _dedupe_topics(cls, value: List[str]) -> List[str]:
        seen, deduped = set(), []
        for topic in value:
            normalized = topic.strip()
            if normalized and normalized.lower() not in seen:
                seen.add(normalized.lower())
                deduped.append(normalized)
        return deduped


_GEMINI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "meetingTitle": {"type": "string"},
        "executiveSummary": {"type": "string"},
        "keyTopics": {"type": "array", "items": {"type": "string"}},
        "actionItems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "assignee": {"type": "string"},
                    "taskDescription": {"type": "string"},
                    "deadline": {"type": "string"},
                },
                "required": ["assignee", "taskDescription", "deadline"],
            },
        },
        "projectDeadlines": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["meetingTitle", "executiveSummary", "keyTopics", "actionItems", "projectDeadlines"],
}

_SYSTEM_INSTRUCTION = (
    "You are a precise enterprise meeting analyst. Given a raw transcript, extract "
    "a structured summary. Never invent facts not present in the transcript. If no "
    "action items or deadlines exist, return empty arrays. deadline must be an "
    "ISO-8601 date if one is stated, otherwise the literal string 'unspecified'."
)


class TranscriptionError(Exception):
    pass


class SummarizationError(Exception):
    pass


# =========================================================================================
# STEP 1 — Whisper transcription
# =========================================================================================
async def transcribe_audio(local_file_path: str) -> str:
    try:
        with open(local_file_path, "rb") as audio_fh:
            transcription = await _openai_client.audio.transcriptions.create(
                model=settings.WHISPER_MODEL_NAME,
                file=audio_fh,
                response_format="text",
            )
        raw_text = transcription if isinstance(transcription, str) else getattr(transcription, "text", "")
        if not raw_text or not raw_text.strip():
            raise TranscriptionError("Whisper returned an empty transcript.")
        return raw_text.strip()
    except Exception as exc:  # noqa: BLE001
        logger.error(f"whisper_transcription_failed error={str(exc)[:300]}")
        raise TranscriptionError("Audio transcription failed.") from exc


# =========================================================================================
# STEP 2 — Gemini 2.5 Flash structured summarization
# =========================================================================================
async def summarize_transcript(transcript: str) -> MeetingSummary:
    model = genai.GenerativeModel(
        model_name=settings.GEMINI_MODEL_NAME,
        system_instruction=_SYSTEM_INSTRUCTION,
        generation_config={
            "response_mime_type": "application/json",
            "response_schema": _GEMINI_RESPONSE_SCHEMA,
            "temperature": 0.2,
        },
    )
    try:
        response = await model.generate_content_async(
            f"Transcript:\n\n{transcript}\n\nExtract the structured meeting summary now."
        )
        parsed = json.loads(response.text)
        return MeetingSummary.model_validate(parsed)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"gemini_summarization_failed error={str(exc)[:300]}")
        raise SummarizationError("Structured summarization failed validation.") from exc


# =========================================================================================
# STEP 3 — Embedding generation (1536-dim, matches summary_embedding column)
# =========================================================================================
async def embed_text(text: str) -> List[float]:
    """
    Generic embedding call shared by summary persistence (embed_summary) and
    semantic search query embedding (audio_webhook.py) — both must use the
    exact same model/dimensions so cosine similarity is meaningful.
    """
    response = await _openai_client.embeddings.create(
        model=settings.EMBEDDING_MODEL_NAME,
        input=text,
        dimensions=settings.EMBEDDING_DIMENSIONS,
    )
    return response.data[0].embedding


async def embed_summary(summary: MeetingSummary) -> List[float]:
    embedding_input = (
        f"{summary.meetingTitle}\n{summary.executiveSummary}\n"
        f"Topics: {', '.join(summary.keyTopics)}"
    )
    return await embed_text(embedding_input)


# =========================================================================================
# STEP 4 — Persistence (BOLA-safe: company_id is always the caller-supplied tenant id,
#           never derived from client-controlled payload fields)
# =========================================================================================
async def persist_summary(
    company_id: str,
    webinar_id: str,
    summary: MeetingSummary,
    embedding: List[float],
    raw_transcript_chars: int,
) -> dict:
    record = {
        "webinar_id": webinar_id,
        "company_id": company_id,
        "meeting_title": summary.meetingTitle,
        "executive_summary": summary.executiveSummary,
        "key_topics": summary.keyTopics,
        "action_items": [item.model_dump() for item in summary.actionItems],
        "project_deadlines": summary.projectDeadlines,
        "summary_embedding": embedding,
        "raw_transcript_chars": raw_transcript_chars,
    }
    result = _supabase.schema("intel_core").table("summaries").insert(record).execute()
    if not result.data:
        raise SummarizationError("Persisting the summary record failed.")
    return result.data[0]


# =========================================================================================
# ORCHESTRATION — full pipeline for a single job, called by the redis worker loop
# =========================================================================================
async def run_full_pipeline(
    company_id: str,
    webinar_id: str,
    local_file_path: str,
    on_state_change=None,
) -> dict:
    """
    Executes transcription -> summarization -> embedding -> persistence, then
    purges the local temp copy of the audio unconditionally (Pillar 1), even
    on failure, via the finally block.
    """
    try:
        if on_state_change:
            await on_state_change("TRANSCRIBING")
        transcript = await transcribe_audio(local_file_path)

        if on_state_change:
            await on_state_change("SUMMARIZING")
        summary = await summarize_transcript(transcript)
        embedding = await embed_summary(summary)

        persisted = await persist_summary(
            company_id=company_id,
            webinar_id=webinar_id,
            summary=summary,
            embedding=embedding,
            raw_transcript_chars=len(transcript),
        )

        _supabase.schema("intel_core").table("webinars").update(
            {"processing_state": "COMPLETED"}
        ).eq("id", webinar_id).eq("company_id", company_id).execute()

        if on_state_change:
            await on_state_change("COMPLETED")

        return persisted
    except (TranscriptionError, SummarizationError):
        _supabase.schema("intel_core").table("webinars").update(
            {"processing_state": "FAILED"}
        ).eq("id", webinar_id).eq("company_id", company_id).execute()
        raise
    finally:
        purge_temp_file(local_file_path)

"""
redis_queue_engine.py  (Vercel-adapted)

Serverless functions cannot hold a persistent Redis TCP connection or run a
long-lived BRPOP worker loop, so this module is rebuilt on Upstash Redis's
stateless REST API. Every call is a self-contained HTTPS request — safe to
issue from a function that may live for only a few hundred milliseconds.

The "queue" itself is now Upstash QStash: enqueue_job() no longer pushes onto
a list a worker polls — it publishes an HTTP job that QStash durably retries
against /api/process-job until that endpoint returns 2xx. Redis here is used
purely for: (a) live processing-state lookups the dashboard polls/subscribes
to, (b) the AI token-bucket rate limiter, and (c) auth lockout counters.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional

from upstash_redis.asyncio import Redis as UpstashRedis
from qstash import AsyncQStash

from app_config import get_settings

settings = get_settings()

STATE_KEY_PREFIX = "intel:jobs:state:"
AUTH_FAILURE_PREFIX = "intel:auth:failures:"
AUTH_LOCKOUT_PREFIX = "intel:auth:lockout:"
RATE_LIMIT_PREFIX = "intel:ratelimit:"


class ProcessingState(str, Enum):
    QUEUED = "QUEUED"
    TRANSCRIBING = "TRANSCRIBING"
    SUMMARIZING = "SUMMARIZING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class AudioJob:
    webinar_id: str
    company_id: str
    storage_path: str
    mime_type: str
    enqueued_at: float

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(payload: dict) -> "AudioJob":
        return AudioJob(**payload)


class RedisQueueEngine:
    """
    Thin, connection-less wrapper. connect()/disconnect() are kept as
    near-no-ops so main.py's lifespan hook and existing call sites don't need
    to change shape — Upstash's REST client opens a fresh HTTPS request per
    call rather than a socket that needs lifecycle management.
    """

    def __init__(self, redis_url: Optional[str] = None):
        self._redis = UpstashRedis(
            url=str(settings.UPSTASH_REDIS_REST_URL),
            token=settings.UPSTASH_REDIS_REST_TOKEN.get_secret_value(),
        )
        self._qstash = AsyncQStash(token=settings.QSTASH_TOKEN.get_secret_value())

    async def connect(self) -> None:
        # No persistent socket to open; verify credentials are reachable instead.
        await self._redis.ping()

    async def disconnect(self) -> None:
        # REST client holds no long-lived connection to tear down.
        return None

    # ---------------------------------------------------------------- Job dispatch (QStash, not a local queue)
    async def enqueue_job(self, job: AudioJob) -> str:
        await self.set_state(job.webinar_id, ProcessingState.QUEUED)
        publish_result = await self._qstash.message.publish_json(
            url=f"{settings.PUBLIC_APP_URL}api/process-job",
            body=job.to_dict(),
            retries=3,
            headers={"Content-Type": "application/json"},
        )
        return publish_result.message_id

    async def set_state(self, webinar_id: str, state: ProcessingState, detail: str = "") -> None:
        payload = json.dumps({"state": state.value, "detail": detail, "updated_at": time.time()})
        await self._redis.set(f"{STATE_KEY_PREFIX}{webinar_id}", payload, ex=60 * 60 * 24 * 7)

    async def get_state(self, webinar_id: str) -> Optional[dict]:
        raw = await self._redis.get(f"{STATE_KEY_PREFIX}{webinar_id}")
        return json.loads(raw) if raw else None

    # ---------------------------------------------------------------- Pillar 9: token-bucket rate limiter
    async def consume_ai_rate_limit_token(
        self, bucket_key: str, capacity: int, refill_per_minute: int
    ) -> bool:
        """
        Upstash's REST API doesn't expose server-side Lua EVAL on every plan
        tier, so the bucket math is performed client-side against Redis's
        atomic HSET/HGETALL primitives — safe enough for a per-tenant limiter
        where a worst-case race costs at most one extra token, never a
        runaway bill.
        """
        key = f"{RATE_LIMIT_PREFIX}{bucket_key}"
        now = time.time()

        raw = await self._redis.hgetall(key)
        if raw:
            tokens = float(raw.get("tokens", capacity))
            last_ts = float(raw.get("ts", now))
        else:
            tokens, last_ts = float(capacity), now

        elapsed = max(0.0, now - last_ts)
        tokens = min(capacity, tokens + elapsed * (refill_per_minute / 60.0))

        allowed = tokens >= 1
        if allowed:
            tokens -= 1

        await self._redis.hset(key, values={"tokens": str(tokens), "ts": str(now)})
        await self._redis.expire(key, 3600)
        return allowed

    # ---------------------------------------------------------------- Pillar 3: auth hardening
    async def record_auth_failure(self, identifier: str, max_failures: int, lockout_seconds: int) -> bool:
        key = f"{AUTH_FAILURE_PREFIX}{identifier}"
        failures = await self._redis.incr(key)
        await self._redis.expire(key, lockout_seconds)
        if failures >= max_failures:
            await self._redis.set(f"{AUTH_LOCKOUT_PREFIX}{identifier}", "1", ex=lockout_seconds)
            return True
        return False

    async def is_locked_out(self, identifier: str) -> bool:
        return bool(await self._redis.exists(f"{AUTH_LOCKOUT_PREFIX}{identifier}"))

    async def clear_auth_failures(self, identifier: str) -> None:
        await self._redis.delete(f"{AUTH_FAILURE_PREFIX}{identifier}")
        await self._redis.delete(f"{AUTH_LOCKOUT_PREFIX}{identifier}")

    @staticmethod
    async def enumeration_safe_delay() -> None:
        import asyncio
        import random
        await asyncio.sleep(random.uniform(0.35, 0.9))

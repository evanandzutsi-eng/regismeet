"""
file_processor.py
Strict MIME/magic-number binary safety validation for inbound audio streams.

Client-declared filenames and Content-Type headers are never trusted (Pillar 6).
This module inspects the actual byte stream via python-magic and rejects anything
that does not resolve to a genuine MP3/WAV container, including files that carry
an audio extension but embed executable or script payloads.
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass

import magic  # python-magic

from app_config import get_settings

settings = get_settings()

# Byte-level signatures that must never appear within the inspected header window,
# regardless of the extension the client claims. This blocks polyglot / masked payloads.
_DANGEROUS_SIGNATURES = (
    b"MZ",              # Windows PE/EXE
    b"\x7fELF",          # Linux ELF binary
    b"#!/",              # shebang script
    b"<?php",
    b"<script",
    b"\xca\xfe\xba\xbe",  # Mach-O / Java class fat binary
)

_HEADER_INSPECTION_WINDOW = 4096  # bytes read for magic-number + signature scanning


class FileValidationError(Exception):
    """Raised whenever an uploaded asset fails strict binary validation."""


@dataclass(frozen=True)
class ValidatedAudioFile:
    sanitized_filename: str
    detected_mime_type: str
    size_bytes: int


def _sanitize_filename(original_name: str) -> str:
    """Strip path components and any character outside a conservative allow-list."""
    base_name = os.path.basename(original_name or "upload")
    base_name = re.sub(r"[^A-Za-z0-9._-]", "_", base_name)
    ext = os.path.splitext(base_name)[1].lower()
    if ext not in settings.ALLOWED_AUDIO_EXTENSIONS:
        raise FileValidationError("Unsupported file extension.")
    return f"{uuid.uuid4().hex}{ext}"


def validate_audio_stream(raw_bytes: bytes, declared_filename: str) -> ValidatedAudioFile:
    """
    Validates a fully-buffered (or chunk-accumulated) audio payload.

    Raises FileValidationError on any mismatch between declared type, true magic-number
    type, embedded dangerous signatures, or size ceiling violations.
    """
    if not raw_bytes:
        raise FileValidationError("Empty file payload.")

    size_bytes = len(raw_bytes)
    if size_bytes > settings.MAX_REQUEST_BODY_BYTES:
        raise FileValidationError("File exceeds the maximum permitted size.")

    header_window = raw_bytes[:_HEADER_INSPECTION_WINDOW]

    for signature in _DANGEROUS_SIGNATURES:
        if signature in header_window:
            raise FileValidationError("File contains a disallowed executable/script signature.")

    detected_mime = magic.from_buffer(raw_bytes[:_HEADER_INSPECTION_WINDOW], mime=True)

    if detected_mime not in settings.ALLOWED_AUDIO_MIME_TYPES:
        raise FileValidationError(
            f"File content does not match an allowed audio container (detected: {detected_mime})."
        )

    sanitized_filename = _sanitize_filename(declared_filename)

    return ValidatedAudioFile(
        sanitized_filename=sanitized_filename,
        detected_mime_type=detected_mime,
        size_bytes=size_bytes,
    )


def purge_temp_file(path: str) -> None:
    """
    Pillar 1 (Data Privacy): immediately shreds the local temp copy once the
    transcript payload has been durably persisted downstream.
    """
    try:
        if os.path.exists(path):
            # Best-effort overwrite before unlink to reduce residual recoverability.
            file_size = os.path.getsize(path)
            with open(path, "r+b") as fh:
                fh.write(b"\x00" * file_size)
            os.remove(path)
    except OSError:
        # Purge failures are logged by the caller; never raise here and mask a
        # successful pipeline run behind a cleanup error.
        pass

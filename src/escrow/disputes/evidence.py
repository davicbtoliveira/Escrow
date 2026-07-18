"""Pure validation and metadata preparation for private dispute evidence."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from uuid import UUID

MAX_EVIDENCE_BYTES = 10 * 1024 * 1024


class EvidenceValidationError(ValueError):
    """An upload cannot become private dispute evidence."""


@dataclass(frozen=True, slots=True)
class PreparedEvidenceUpload:
    """Verified metadata passed to the object-storage and persistence boundaries."""

    evidence_id: UUID
    dispute_id: UUID
    object_key: str
    extension: str
    media_type: str
    size_bytes: int
    sha256: str


def prepare_evidence_upload(
    *,
    dispute_id: UUID,
    filename: str,
    content: bytes,
) -> PreparedEvidenceUpload:
    """Derive storage-safe metadata without retaining customer file bytes."""
    if (
        not isinstance(filename, str)
        or not filename
        or len(filename) > 255
        or filename != filename.strip()
        or "/" in filename
        or "\\" in filename
        or any(character.isspace() and character not in {" ", "\t"} for character in filename)
    ):
        raise EvidenceValidationError("unsafe evidence filename")
    if not content or len(content) > MAX_EVIDENCE_BYTES:
        raise EvidenceValidationError("evidence size is outside the allowed limit")
    extension = filename.rsplit(".", 1)[-1].lower()
    if content.startswith(b"%PDF-"):
        media_type, expected_extensions = "application/pdf", {"pdf"}
    elif content.startswith(b"\x89PNG\r\n\x1a\n"):
        media_type, expected_extensions = "image/png", {"png"}
    elif content.startswith(b"\xff\xd8\xff"):
        media_type, expected_extensions = "image/jpeg", {"jpg", "jpeg"}
    else:
        raise EvidenceValidationError("unsupported evidence content")
    if extension not in expected_extensions:
        raise EvidenceValidationError("file extension does not match evidence content")
    evidence_id = uuid.uuid4()
    return PreparedEvidenceUpload(
        evidence_id=evidence_id,
        dispute_id=dispute_id,
        object_key=f"private/disputes/{dispute_id}/{evidence_id}.{extension}",
        extension=extension,
        media_type=media_type,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )

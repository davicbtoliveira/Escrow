from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from escrow.disputes.evidence import (
    MAX_EVIDENCE_BYTES,
    EvidenceValidationError,
    prepare_evidence_upload,
)


def test_prepare_evidence_upload_derives_private_metadata_without_retaining_content() -> None:
    dispute_id = UUID("caa2b0f9-ef30-43b9-afdb-1e5d2f5ab811")
    content = b"%PDF-1.7\nfictional purchase receipt\n"

    prepared = prepare_evidence_upload(
        dispute_id=dispute_id,
        filename="receipt.PDF",
        content=content,
    )

    assert prepared.dispute_id == dispute_id
    assert prepared.media_type == "application/pdf"
    assert prepared.extension == "pdf"
    assert prepared.size_bytes == len(content)
    assert prepared.sha256 == hashlib.sha256(content).hexdigest()
    assert prepared.object_key.startswith(f"private/disputes/{dispute_id}/")
    assert prepared.object_key.endswith(".pdf")
    assert not hasattr(prepared, "content")


def test_prepare_evidence_upload_rejects_a_filename_that_disagrees_with_magic_bytes() -> None:
    with pytest.raises(EvidenceValidationError, match="extension"):
        prepare_evidence_upload(
            dispute_id=UUID("caa2b0f9-ef30-43b9-afdb-1e5d2f5ab811"),
            filename="receipt.png",
            content=b"%PDF-1.7\nfictional purchase receipt\n",
        )


def test_prepare_evidence_upload_accepts_png_when_the_extension_matches_its_magic_bytes() -> None:
    prepared = prepare_evidence_upload(
        dispute_id=UUID("caa2b0f9-ef30-43b9-afdb-1e5d2f5ab811"),
        filename="damaged-package.png",
        content=b"\x89PNG\r\n\x1a\nfictional image bytes",
    )

    assert prepared.media_type == "image/png"
    assert prepared.extension == "png"
    assert prepared.object_key.endswith(".png")


def test_prepare_evidence_upload_rejects_a_path_like_customer_filename() -> None:
    with pytest.raises(EvidenceValidationError, match="filename"):
        prepare_evidence_upload(
            dispute_id=UUID("caa2b0f9-ef30-43b9-afdb-1e5d2f5ab811"),
            filename="../receipt.pdf",
            content=b"%PDF-1.7\nfictional purchase receipt\n",
        )


def test_prepare_evidence_upload_rejects_files_larger_than_the_private_evidence_limit() -> None:
    with pytest.raises(EvidenceValidationError, match="size"):
        prepare_evidence_upload(
            dispute_id=UUID("caa2b0f9-ef30-43b9-afdb-1e5d2f5ab811"),
            filename="large-receipt.pdf",
            content=b"%PDF-" + (b"x" * MAX_EVIDENCE_BYTES),
        )


@pytest.mark.parametrize("filename", ["package.jpg", "package.jpeg"])
def test_prepare_evidence_upload_accepts_jpeg_magic_bytes_for_supported_extensions(
    filename: str,
) -> None:
    prepared = prepare_evidence_upload(
        dispute_id=UUID("caa2b0f9-ef30-43b9-afdb-1e5d2f5ab811"),
        filename=filename,
        content=b"\xff\xd8\xff\xe0fictional jpeg bytes",
    )

    assert prepared.media_type == "image/jpeg"
    assert prepared.object_key.endswith(f".{filename.rsplit('.', 1)[1]}")

"""S3-compatible object boundary for private dispute evidence in Ceph RGW."""

from __future__ import annotations

from typing import Any

import boto3
from botocore.config import Config
from django.conf import settings


def evidence_s3_client() -> Any:
    """Build a path-style S3 client pointed at the configured Ceph RGW endpoint."""
    return boto3.client(
        "s3",
        endpoint_url=settings.EVIDENCE_S3_ENDPOINT_URL,
        region_name=settings.EVIDENCE_S3_REGION,
        aws_access_key_id=settings.EVIDENCE_S3_ACCESS_KEY_ID,
        aws_secret_access_key=settings.EVIDENCE_S3_SECRET_ACCESS_KEY,
        config=Config(s3={"addressing_style": "path"}),
    )


def store_evidence_object(
    client: Any,
    *,
    object_key: str,
    content: bytes,
    media_type: str,
) -> None:
    """Write one private evidence object under the generated key."""
    client.put_object(
        Bucket=settings.EVIDENCE_S3_BUCKET,
        Key=object_key,
        Body=content,
        ContentType=media_type,
    )


def presign_evidence_download(client: Any, *, object_key: str, ttl_seconds: int) -> str:
    """Issue a short-lived download URL for one already-authorized access."""
    url: str = client.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.EVIDENCE_S3_BUCKET, "Key": object_key},
        ExpiresIn=ttl_seconds,
    )
    return url

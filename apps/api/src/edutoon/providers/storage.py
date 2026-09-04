"""S3-compatible object storage - the only module allowed to import
``boto3``/``botocore`` (rule 3). MinIO locally, any S3-compatible bucket in
production, addressed through one explicit endpoint URL.

``boto3`` is synchronous; every call here runs in a worker thread via
``asyncio.to_thread`` so it never blocks the event loop, while still
presenting the same async-first shape as ``providers/cache.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

_NOT_FOUND_ERROR_CODES = {"404", "NoSuchKey"}


class Storage:
    """Thin async wrapper over a ``boto3`` S3 client bound to one endpoint."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def put_object(
        self, *, bucket: str, key: str, body: bytes, content_type: str
    ) -> None:
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
        )

    async def object_exists(self, *, bucket: str, key: str) -> bool:
        try:
            await asyncio.to_thread(self._client.head_object, Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_FOUND_ERROR_CODES:
                return False
            raise
        return True

    async def delete_object(self, *, bucket: str, key: str) -> None:
        await asyncio.to_thread(self._client.delete_object, Bucket=bucket, Key=key)

    async def ping(self, *, bucket: str) -> None:
        """Raise if ``bucket`` isn't reachable. Used by the ``/health`` route."""
        await asyncio.to_thread(self._client.head_bucket, Bucket=bucket)


def get_storage_client(
    *, endpoint_url: str, access_key_id: str, secret_access_key: str
) -> Storage:
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        # MinIO needs path-style addressing (``endpoint/bucket/key``) rather
        # than AWS's default virtual-hosted style (``bucket.endpoint/key``).
        config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    return Storage(client)

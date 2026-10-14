"""S3-compatible object storage - the only module allowed to import
``boto3``/``botocore`` (rule 3). MinIO locally, any S3-compatible bucket in
production, addressed through one explicit endpoint URL.

``boto3`` is synchronous; every call here runs in a worker thread via
``asyncio.to_thread`` so it never blocks the event loop, while still
presenting the same async-first shape as ``providers/cache.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import ClientError

_NOT_FOUND_ERROR_CODES = {"404", "NoSuchKey"}


class ObjectNotFoundError(Exception):
    """Raised by :meth:`Storage.get_object` for a bucket/key that doesn't
    exist - never a bare ``botocore`` traceback."""


@dataclass(frozen=True, slots=True)
class ObjectData:
    """One ``get_object`` read: either the whole object (``status_code=200``)
    or, when ``range_header`` was given and the server honoured it, exactly
    the requested byte range (``status_code=206``, ``content_range`` set).
    """

    body: bytes
    content_length: int
    content_type: str
    content_range: str | None
    status_code: int


class InvalidRangeError(Exception):
    """Raised by :meth:`Storage.open_object` when the requested byte range cannot be satisfied
    (it starts beyond the end of the object). ``size`` is the object's real length, which the
    caller needs for the ``Content-Range: bytes */size`` header of a 416 response."""

    def __init__(self, size: int) -> None:
        super().__init__(f"Range not satisfiable for an object of {size} bytes.")
        self.size = size


@dataclass(slots=True)
class ObjectStream:
    """One streamed read of an object (or of one byte range of it). The body is read in bounded
    chunks by :meth:`iter_chunks`, never all at once - this is how a large video is served
    without holding it in memory. Always ``close()`` it (``iter_chunks`` does, when exhausted
    or abandoned)."""

    body: Any
    content_length: int
    content_type: str
    content_range: str | None
    status_code: int

    async def iter_chunks(self, chunk_size: int = 1 << 20) -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await asyncio.to_thread(self.body.read, chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            await asyncio.to_thread(self.body.close)

    async def close(self) -> None:
        await asyncio.to_thread(self.body.close)


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

    async def get_object(
        self, *, bucket: str, key: str, range_header: str | None = None
    ) -> ObjectData:
        """Read an object, or (with ``range_header``, an HTTP ``Range``
        header value such as ``bytes=0-1023``) one byte range of it - passed
        straight through to the S3-compatible server, which does the actual
        range arithmetic; this method only reports back what it returned.

        Raises :class:`ObjectNotFoundError` for a missing bucket/key.
        """
        kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if range_header:
            kwargs["Range"] = range_header
        try:
            response = await asyncio.to_thread(self._client.get_object, **kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _NOT_FOUND_ERROR_CODES or code == "InvalidRange":
                raise ObjectNotFoundError(f"{bucket}/{key} not found.") from exc
            raise
        body = await asyncio.to_thread(response["Body"].read)
        content_range = response.get("ContentRange")
        return ObjectData(
            body=body,
            content_length=response["ContentLength"],
            content_type=response.get("ContentType", "application/octet-stream"),
            content_range=content_range,
            status_code=206 if content_range else 200,
        )

    async def upload_file(self, *, bucket: str, key: str, path: Path, content_type: str) -> None:
        """Upload a local file, streaming from disk (multipart for large files) - the file is
        never read into memory whole. The object appears atomically: readers see the old
        object or the new one, never a partial upload."""
        await asyncio.to_thread(
            self._client.upload_file,
            str(path),
            bucket,
            key,
            ExtraArgs={"ContentType": content_type},
        )

    async def download_file(self, *, bucket: str, key: str, dest: Path) -> None:
        """Download an object to a local file, streaming to disk.

        Raises :class:`ObjectNotFoundError` for a missing bucket/key.
        """
        try:
            await asyncio.to_thread(self._client.download_file, bucket, key, str(dest))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_FOUND_ERROR_CODES:
                raise ObjectNotFoundError(f"{bucket}/{key} not found.") from exc
            raise

    async def object_size(self, *, bucket: str, key: str) -> int:
        """The object's length in bytes. Raises :class:`ObjectNotFoundError` if missing."""
        try:
            head = await asyncio.to_thread(self._client.head_object, Bucket=bucket, Key=key)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_FOUND_ERROR_CODES:
                raise ObjectNotFoundError(f"{bucket}/{key} not found.") from exc
            raise
        return int(head["ContentLength"])

    async def open_object(
        self, *, bucket: str, key: str, range_header: str | None = None
    ) -> ObjectStream:
        """Open an object (or, with ``range_header`` such as ``bytes=0-1023``, one byte range
        of it) for streaming. The range arithmetic is done by the S3-compatible server.

        Raises :class:`ObjectNotFoundError` for a missing bucket/key and
        :class:`InvalidRangeError` for a range that starts beyond the end of the object.
        """
        kwargs: dict[str, Any] = {"Bucket": bucket, "Key": key}
        if range_header:
            kwargs["Range"] = range_header
        try:
            response = await asyncio.to_thread(self._client.get_object, **kwargs)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in _NOT_FOUND_ERROR_CODES:
                raise ObjectNotFoundError(f"{bucket}/{key} not found.") from exc
            if code == "InvalidRange":
                raise InvalidRangeError(await self.object_size(bucket=bucket, key=key)) from exc
            raise
        content_range = response.get("ContentRange")
        return ObjectStream(
            body=response["Body"],
            content_length=response["ContentLength"],
            content_type=response.get("ContentType", "application/octet-stream"),
            content_range=content_range,
            status_code=206 if content_range else 200,
        )

    async def copy_object(
        self,
        *,
        source_bucket: str,
        source_key: str,
        dest_bucket: str,
        dest_key: str,
        content_type: str,
    ) -> None:
        """Copy an object inside the storage server - the bytes never pass through this process.

        Uses boto3's managed copy, which is one request for a small object and a multipart copy
        for a large one, so it is not limited to 5 GB. The destination is written under its own
        key; an existing object at ``dest_key`` would be replaced, so callers pass a key nothing
        else uses. The destination's content type is set explicitly rather than inherited.

        Raises :class:`ObjectNotFoundError` if the source does not exist.
        """
        try:
            await asyncio.to_thread(
                self._client.copy,
                {"Bucket": source_bucket, "Key": source_key},
                dest_bucket,
                dest_key,
                ExtraArgs={"ContentType": content_type, "MetadataDirective": "REPLACE"},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in _NOT_FOUND_ERROR_CODES:
                raise ObjectNotFoundError(f"{source_bucket}/{source_key} not found.") from exc
            raise

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

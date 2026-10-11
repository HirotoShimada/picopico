"""Object storage abstraction: local filesystem or S3-compatible.

Keys are server-generated, ``/``-separated relative paths. Scheme:
``projects/{project_id}/{kind}/{asset_id}{ext}`` (see :func:`make_key`).
They never come from user input; :class:`LocalStorage` still rejects any key
that could escape its root.

URLs returned by :meth:`Storage.url` are what API responses expose: signed
``/media/...`` URLs for local storage, presigned GET URLs for S3.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import unicodedata
import uuid
from abc import ABC, abstractmethod
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.config import get_settings

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024
_KEY_RE = re.compile(r"^[A-Za-z0-9._\-]+(?:/[A-Za-z0-9._\-]+)*$")


class StorageKeyError(ValueError):
    """Raised for keys that are malformed or would escape the storage root."""


class StorageNotFound(FileNotFoundError):
    pass


def validate_key(key: str) -> str:
    if not key or len(key) > 512 or not _KEY_RE.match(key):
        raise StorageKeyError(f"invalid storage key: {key!r}")
    if any(part in (".", "..") for part in key.split("/")):
        raise StorageKeyError(f"invalid storage key: {key!r}")
    return key


def make_key(project_id: str, kind: str, asset_id: str, ext: str = "") -> str:
    """``projects/{project_id}/{kind}/{asset_id}{ext}``."""
    ext = ext.lower()
    if ext and not re.fullmatch(r"\.[a-z0-9]{1,8}", ext):
        raise StorageKeyError(f"invalid extension: {ext!r}")
    return validate_key(f"projects/{project_id}/{kind}/{asset_id}{ext}")


def project_prefix(project_id: str) -> str:
    return validate_key(f"projects/{project_id}") + "/"


def ascii_filename_fallback(filename: str, default: str = "download") -> str:
    """ASCII-only variant of ``filename`` for the legacy ``filename=`` parameter."""
    stem, ext = os.path.splitext(filename)
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    ascii_stem = re.sub(r"[^A-Za-z0-9._\- ]+", "_", ascii_stem).strip(" ._")
    ascii_ext = ext if re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext) else ""
    if not ascii_stem or set(ascii_stem) <= {"_"}:
        ascii_stem = default
    return f"{ascii_stem}{ascii_ext}"


def build_content_disposition(filename: str, disposition: str = "attachment") -> str:
    """``attachment; filename="ascii"; filename*=UTF-8''percent-encoded`` (RFC 6266/5987)."""
    clean = "".join(ch for ch in filename if not unicodedata.category(ch).startswith("C")) or "download"
    fallback = ascii_filename_fallback(clean)
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(clean, safe='')}"


class Storage(ABC):
    backend: str

    @abstractmethod
    def put_file(
        self, key: str, src_path: str | os.PathLike[str], *, content_type: str | None = None, move: bool = False
    ) -> int:
        """Store a local file under ``key``; returns its size. ``move`` consumes ``src_path``."""

    @abstractmethod
    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> int: ...

    @abstractmethod
    def fetch_to(self, key: str, dest_path: str | os.PathLike[str]) -> Path:
        """Copy the object to a local path (parents created); returns ``dest_path``."""

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def delete(self, key: str) -> None:
        """Delete one object; missing objects are ignored."""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix`` (should end with ``/``); returns count."""

    @abstractmethod
    def size(self, key: str) -> int: ...

    @abstractmethod
    def url(self, key: str, *, download: bool = False, filename: str | None = None, ttl: int | None = None) -> str:
        """Time-limited URL a browser can GET."""

    @abstractmethod
    def iter_bytes(self, key: str, chunk_size: int = _CHUNK) -> Iterator[bytes]: ...

    def local_path(self, key: str) -> Path | None:
        """Direct filesystem path when the backend is local (avoids copies), else ``None``."""
        return None


class LocalStorage(Storage):
    backend = "local"

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        validate_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise StorageKeyError(f"storage key escapes root: {key!r}")
        return path

    def local_path(self, key: str) -> Path | None:
        return self.path(key)

    def _tmp_for(self, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")

    def put_file(
        self, key: str, src_path: str | os.PathLike[str], *, content_type: str | None = None, move: bool = False
    ) -> int:
        dest = self.path(key)
        tmp = self._tmp_for(dest)
        try:
            if move:
                shutil.move(os.fspath(src_path), tmp)
            else:
                shutil.copyfile(src_path, tmp)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return dest.stat().st_size

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> int:
        dest = self.path(key)
        tmp = self._tmp_for(dest)
        try:
            tmp.write_bytes(data)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)
        return len(data)

    def fetch_to(self, key: str, dest_path: str | os.PathLike[str]) -> Path:
        src = self.path(key)
        if not src.is_file():
            raise StorageNotFound(key)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)

    def delete_prefix(self, prefix: str) -> int:
        base = self.path(prefix.rstrip("/"))
        if base.is_file():
            base.unlink()
            return 1
        if not base.is_dir():
            return 0
        count = sum(1 for p in base.rglob("*") if p.is_file())
        shutil.rmtree(base)
        return count

    def size(self, key: str) -> int:
        path = self.path(key)
        if not path.is_file():
            raise StorageNotFound(key)
        return path.stat().st_size

    def url(self, key: str, *, download: bool = False, filename: str | None = None, ttl: int | None = None) -> str:
        from app.security import sign_media_url

        validate_key(key)
        return sign_media_url(key, download=download, filename=filename, ttl=ttl)

    def iter_bytes(self, key: str, chunk_size: int = _CHUNK) -> Iterator[bytes]:
        path = self.path(key)
        if not path.is_file():
            raise StorageNotFound(key)
        with path.open("rb") as fh:
            while chunk := fh.read(chunk_size):
                yield chunk


class S3Storage(Storage):
    """S3 or S3-compatible (MinIO, R2, ...) storage via boto3."""

    backend = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        region: str | None = None,
        client: Any = None,
    ):
        import boto3
        from botocore.config import Config

        self.bucket = bucket
        if client is None:
            s3_options: dict[str, Any] = {"addressing_style": "path"} if endpoint_url else {}
            client = boto3.client(
                "s3",
                endpoint_url=endpoint_url,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                region_name=region or "us-east-1",
                config=Config(signature_version="s3v4", s3=s3_options, retries={"max_attempts": 3, "mode": "standard"}),
            )
        self.client = client

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        from botocore.exceptions import ClientError

        if not isinstance(exc, ClientError):
            return False
        code = str(exc.response.get("Error", {}).get("Code", ""))
        return code in ("404", "NoSuchKey", "NotFound")

    def put_file(
        self, key: str, src_path: str | os.PathLike[str], *, content_type: str | None = None, move: bool = False
    ) -> int:
        validate_key(key)
        extra = {"ContentType": content_type} if content_type else None
        self.client.upload_file(os.fspath(src_path), self.bucket, key, ExtraArgs=extra)
        size = os.path.getsize(src_path)
        if move:
            os.unlink(src_path)
        return size

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None) -> int:
        validate_key(key)
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        self.client.put_object(**kwargs)
        return len(data)

    def fetch_to(self, key: str, dest_path: str | os.PathLike[str]) -> Path:
        validate_key(key)
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.client.download_file(self.bucket, key, os.fspath(dest))
        except Exception as exc:
            if self._is_not_found(exc):
                raise StorageNotFound(key) from exc
            raise
        return dest

    def exists(self, key: str) -> bool:
        validate_key(key)
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._is_not_found(exc):
                return False
            raise
        return True

    def delete(self, key: str) -> None:
        validate_key(key)
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def delete_prefix(self, prefix: str) -> int:
        validate_key(prefix.rstrip("/"))
        count = 0
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            for start in range(0, len(objects), 1000):
                batch = objects[start : start + 1000]
                self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True})
                count += len(batch)
        return count

    def size(self, key: str) -> int:
        validate_key(key)
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as exc:
            if self._is_not_found(exc):
                raise StorageNotFound(key) from exc
            raise
        return int(head["ContentLength"])

    def url(self, key: str, *, download: bool = False, filename: str | None = None, ttl: int | None = None) -> str:
        validate_key(key)
        params: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if download:
            params["ResponseContentDisposition"] = build_content_disposition(filename or key.rsplit("/", 1)[-1])
        expires = int(ttl if ttl is not None else get_settings().MEDIA_URL_TTL)
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires)

    def iter_bytes(self, key: str, chunk_size: int = _CHUNK) -> Iterator[bytes]:
        validate_key(key)
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        except Exception as exc:
            if self._is_not_found(exc):
                raise StorageNotFound(key) from exc
            raise
        try:
            yield from body.iter_chunks(chunk_size)
        finally:
            body.close()


@lru_cache(maxsize=1)
def get_storage() -> Storage:
    """Process-wide storage backend chosen by ``STORAGE_BACKEND``."""
    settings = get_settings()
    if settings.STORAGE_BACKEND == "s3":
        if not settings.S3_BUCKET:
            raise RuntimeError("STORAGE_BACKEND=s3 requires S3_BUCKET")
        return S3Storage(
            settings.S3_BUCKET,
            endpoint_url=settings.S3_ENDPOINT_URL,
            access_key_id=settings.S3_ACCESS_KEY_ID,
            secret_access_key=settings.S3_SECRET_ACCESS_KEY,
            region=settings.S3_REGION,
        )
    return LocalStorage(settings.STORAGE_DIR)

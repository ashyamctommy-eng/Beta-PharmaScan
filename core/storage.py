"""
core/storage.py — where an uploaded document actually lives.
-----------------------------------------------------------
One interface, two backends, chosen by `STORAGE_BACKEND`:

  * **disk** (default) — `uploaded_notes/` beside the app. Right for cPanel, a VPS,
    or PythonAnywhere: a real filesystem, and the web server can stream files itself.
  * **database** — the bytes go into the `uploaded_files` table. Right for hosts with
    an **ephemeral filesystem** (Render, Koyeb, rollout.host free tiers), where a
    restart or a sleep wipes local files. It also makes the app stateless: database +
    code is the whole deployment.

Both backends expose the same four operations, so neither the upload route nor the
summary pipeline knows which one is in use. `local_path()` returning `None` is the only
visible difference, and it is what lets the caller use the cheaper path-based extraction
when a real file exists.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from pathlib import Path
from typing import Optional, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from models.upload import UploadedFile

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when a document cannot be stored or read."""


def secure_name(filename: str) -> str:
    """A safe, readable file name: no directories, no traversal, real extension kept.

    Works on the *basename* first, so "…/etc/passwd" becomes "passwd" rather than a
    mangled string, and only treats the tail as an extension when it looks like one
    (so "archive.tar.gz" keeps ".gz" but "no-extension" keeps its name).
    """
    raw = (filename or "").replace("\\", "/").split("/")[-1]
    raw = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    raw = raw.replace("\x00", "").strip()

    stem, dot, suffix = raw.rpartition(".")
    if not (dot and 1 <= len(suffix) <= 6 and suffix.isalnum()):
        stem, suffix = raw, ""
    stem = re.sub(r"[^\w\-]", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_") or "file"
    return f"{stem}.{suffix.lower()}" if suffix else stem


async def unique_file_name(db: AsyncSession, candidate: str) -> str:
    """`Resource.file_name` is unique, so two 'notes.pdf' get 'notes.pdf', 'notes_1.pdf'.

    Uniqueness is checked in the database rather than on disk, so it behaves the same
    for both backends.
    """
    from models.resource import Resource

    taken = {row[0] for row in (await db.execute(select(Resource.file_name))).all()}
    if candidate not in taken:
        return candidate
    stem, dot, suffix = candidate.rpartition(".")
    stem = stem or candidate
    counter = 1
    while True:
        trial = f"{stem}_{counter}{dot}{suffix}" if dot else f"{stem}_{counter}"
        if trial not in taken:
            return trial
        counter += 1


class Storage(Protocol):
    """Every backend works on a resource, which carries `id` and `file_name`.

    Taking the object (rather than an id and re-querying) keeps this usable from the
    upload path, where the row exists but is not yet committed.
    """

    name: str

    async def save(self, db: AsyncSession, resource, data: bytes, content_type: str) -> str: ...
    async def local_path(self, db: AsyncSession, resource) -> Optional[Path]: ...
    async def load_bytes(self, db: AsyncSession, resource) -> bytes: ...
    async def fingerprint(self, db: AsyncSession, resource) -> str: ...
    async def delete(self, db: AsyncSession, resource) -> None: ...


def document_url(resource_id: int) -> str:
    """Where a document is downloaded from — one URL shape for both backends."""
    return f"/api/notes/{resource_id}/file"


class DiskStorage:
    name = "disk"

    async def save(self, db: AsyncSession, resource, data: bytes, content_type: str) -> str:
        directory = Path(settings.UPLOAD_DIR)
        directory.mkdir(parents=True, exist_ok=True)
        try:
            (directory / resource.file_name).write_bytes(data)
        except OSError as exc:
            raise StorageError(f"Could not save the file on disk: {exc}") from exc
        return document_url(resource.id)

    async def local_path(self, db: AsyncSession, resource) -> Optional[Path]:
        candidate = Path(settings.UPLOAD_DIR) / resource.file_name
        return candidate if candidate.exists() else None

    async def load_bytes(self, db: AsyncSession, resource) -> bytes:
        path = await self.local_path(db, resource)
        if path is None:
            raise StorageError("The uploaded file is no longer on disk.")
        try:
            return path.read_bytes()
        except OSError as exc:
            raise StorageError(f"Could not read the file: {exc}") from exc

    async def fingerprint(self, db: AsyncSession, resource) -> str:
        """Cheap change detector: mtime + size, so a 50 MB file is never re-read."""
        path = await self.local_path(db, resource)
        if path is None:
            raise StorageError("The uploaded file is no longer on disk.")
        stat = path.stat()
        return f"{stat.st_mtime_ns}:{stat.st_size}"

    async def delete(self, db: AsyncSession, resource) -> None:
        path = await self.local_path(db, resource)
        if path is not None:
            try:
                path.unlink()
            except OSError as exc:  # pragma: no cover - unusual, but not fatal
                logger.warning("Could not delete %s: %s", path, exc)


class DatabaseStorage:
    name = "database"

    async def save(self, db: AsyncSession, resource, data: bytes, content_type: str) -> str:
        digest = hashlib.sha256(data).hexdigest()
        existing = (await db.execute(
            select(UploadedFile).where(UploadedFile.resource_id == resource.id))).scalars().first()
        if existing is None:
            db.add(UploadedFile(resource_id=resource.id, file_name=resource.file_name,
                                content_type=content_type or "application/octet-stream",
                                size=len(data), sha256=digest, data=data))
        else:                                   # re-upload replaces the bytes
            existing.file_name = resource.file_name
            existing.content_type = content_type or "application/octet-stream"
            existing.size = len(data)
            existing.sha256 = digest
            existing.data = data
        await db.flush()
        return document_url(resource.id)

    async def local_path(self, db: AsyncSession, resource) -> Optional[Path]:
        return None                             # by design: nothing on disk

    async def load_bytes(self, db: AsyncSession, resource) -> bytes:
        row = (await db.execute(
            select(UploadedFile).where(UploadedFile.resource_id == resource.id))).scalars().first()
        if row is None:
            raise StorageError("The uploaded file is missing from the database.")
        return bytes(row.data)

    async def fingerprint(self, db: AsyncSession, resource) -> str:
        row = (await db.execute(
            select(UploadedFile.sha256).where(UploadedFile.resource_id == resource.id))).first()
        if row is None:
            raise StorageError("The uploaded file is missing from the database.")
        return str(row[0])

    async def delete(self, db: AsyncSession, resource) -> None:
        row = (await db.execute(
            select(UploadedFile).where(UploadedFile.resource_id == resource.id))).scalars().first()
        if row is not None:
            await db.delete(row)
            await db.flush()


def get_storage(backend: Optional[str] = None) -> Storage:
    """The configured backend. `database` is the stateless-host choice."""
    choice = (backend or settings.STORAGE_BACKEND or "disk").strip().lower()
    if choice in ("db", "database", "postgres", "sqlite"):
        return DatabaseStorage()
    if choice not in ("disk", "filesystem", "file"):
        logger.warning("Unknown STORAGE_BACKEND %r — falling back to disk", choice)
    return DiskStorage()

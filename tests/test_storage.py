"""
tests/test_storage.py — the storage backends, and the stateless promise.

The point of the `database` backend is that *nothing* may depend on the local
filesystem: uploads, summaries and the token ledger all live in the database. These
tests check that directly, including that the summary pipeline can read a document
that exists nowhere on disk.

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from core import storage as storage_module  # noqa: E402
from core import summarise as S  # noqa: E402
from core.config import settings  # noqa: E402
from core.database import Base  # noqa: E402
from core.storage import DatabaseStorage, DiskStorage, StorageError, get_storage  # noqa: E402
from models import resource as _resource  # noqa: F401,E402
from models import summary as _summary  # noqa: F401,E402
from models import upload as _upload  # noqa: F401,E402
from models.resource import Resource  # noqa: E402
from models.upload import UploadedFile  # noqa: E402

try:
    import fpdf  # noqa: F401
    HAVE_FPDF = True
except ImportError:
    HAVE_FPDF = False


class StorageCase(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name)
        self._saved_upload_dir = settings.UPLOAD_DIR
        self._saved_backend = settings.STORAGE_BACKEND
        settings.UPLOAD_DIR = self.root / "uploads"
        S.reset_extraction_cache()

        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.root / 'test.db'}")
        self.Session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def create() -> None:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(create())

    def tearDown(self) -> None:
        asyncio.run(self.engine.dispose())
        settings.UPLOAD_DIR = self._saved_upload_dir
        settings.STORAGE_BACKEND = self._saved_backend
        S.reset_extraction_cache()
        self._dir.cleanup()

    async def _resource(self, db: AsyncSession, name: str = "notes.pdf") -> Resource:
        row = Resource(title="t", subject="s", semester="Y1S1", file_name=name, file_path="")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


class TestSelection(StorageCase):
    def test_default_is_disk(self) -> None:
        settings.STORAGE_BACKEND = "disk"
        self.assertIsInstance(get_storage(), DiskStorage)

    def test_database_aliases(self) -> None:
        for value in ("database", "db", "postgres", "sqlite"):
            settings.STORAGE_BACKEND = value
            self.assertIsInstance(get_storage(), DatabaseStorage, value)

    def test_unknown_value_falls_back_to_disk(self) -> None:
        settings.STORAGE_BACKEND = "banana"
        self.assertIsInstance(get_storage(), DiskStorage)


class TestDiskBackend(StorageCase):
    def test_round_trip_and_delete(self) -> None:
        payload = b"%PDF-1.4 hello disk"

        async def scenario() -> tuple[str, bytes, bool, bool]:
            async with self.Session() as db:
                resource = await self._resource(db)
                url = await DiskStorage().save(db, resource, payload, "application/pdf")
                on_disk = (settings.UPLOAD_DIR / resource.file_name).exists()
                read_back = await DiskStorage().load_bytes(db, resource)
                await DiskStorage().delete(db, resource)
                gone = not (settings.UPLOAD_DIR / resource.file_name).exists()
                return url, read_back, on_disk, gone

        url, read_back, on_disk, gone = asyncio.run(scenario())
        self.assertEqual(read_back, payload)
        self.assertTrue(on_disk, "the disk backend must write a real file")
        self.assertTrue(gone, "delete must remove the file")
        self.assertEqual(url, "/api/notes/1/file", "one URL shape for both backends")

    def test_fingerprint_tracks_the_file(self) -> None:
        async def scenario() -> tuple[str, str]:
            async with self.Session() as db:
                resource = await self._resource(db)
                storage = DiskStorage()
                await storage.save(db, resource, b"one", "application/pdf")
                first = await storage.fingerprint(db, resource)
                (settings.UPLOAD_DIR / resource.file_name).write_bytes(b"a longer payload")
                second = await storage.fingerprint(db, resource)
                return first, second

        first, second = asyncio.run(scenario())
        self.assertNotEqual(first, second, "a changed file must invalidate the cache")


class TestDatabaseBackend(StorageCase):
    def test_round_trip_and_delete(self) -> None:
        payload = b"%PDF-1.4 hello database"

        async def scenario() -> tuple[str, bytes, bool, int, int]:
            async with self.Session() as db:
                resource = await self._resource(db)
                storage = DatabaseStorage()
                url = await storage.save(db, resource, payload, "application/pdf")
                await db.commit()
                on_disk = (settings.UPLOAD_DIR / resource.file_name).exists()
                local = await storage.local_path(db, resource)
                read_back = await storage.load_bytes(db, resource)
                rows = len((await db.execute(select(UploadedFile))).scalars().all())
                await storage.delete(db, resource)
                await db.commit()
                after = len((await db.execute(select(UploadedFile))).scalars().all())
                self.assertIsNone(local)
                return url, read_back, on_disk, rows, after

        url, read_back, on_disk, rows, after = asyncio.run(scenario())
        self.assertEqual(read_back, payload)
        self.assertFalse(on_disk, "the database backend must not touch the filesystem")
        self.assertEqual(rows, 1)
        self.assertEqual(after, 0, "delete must remove the row")
        self.assertEqual(url, "/api/notes/1/file")

    def test_fingerprint_is_the_content_hash(self) -> None:
        payload = b"%PDF-1.4 fingerprinted"

        async def scenario() -> str:
            async with self.Session() as db:
                resource = await self._resource(db)
                storage = DatabaseStorage()
                await storage.save(db, resource, payload, "application/pdf")
                await db.commit()
                return await storage.fingerprint(db, resource)

        self.assertEqual(asyncio.run(scenario()), hashlib.sha256(payload).hexdigest())

    def test_reupload_replaces_rather_than_duplicates(self) -> None:
        async def scenario() -> tuple[int, bytes]:
            async with self.Session() as db:
                resource = await self._resource(db)
                storage = DatabaseStorage()
                await storage.save(db, resource, b"first version", "application/pdf")
                await db.commit()
                await storage.save(db, resource, b"second version", "application/pdf")
                await db.commit()
                rows = (await db.execute(select(UploadedFile))).scalars().all()
                return len(rows), rows[0].data if rows else b""

        count, data = asyncio.run(scenario())
        self.assertEqual(count, 1)
        self.assertEqual(data, b"second version")


class TestNames(StorageCase):
    def test_secure_name_neutralises_paths(self) -> None:
        self.assertEqual(storage_module.secure_name("../../etc/passwd"), "passwd")
        self.assertEqual(storage_module.secure_name("..\\..\\windows\\system32\\x.pdf"), "x.pdf")
        self.assertEqual(storage_module.secure_name("notes of ch 3.pdf"), "notes_of_ch_3.pdf")
        self.assertEqual(storage_module.secure_name("wéird näme.pdf"), "weird_name.pdf")
        # real extensions survive, invented ones do not
        self.assertEqual(storage_module.secure_name("Study.Notes.pdf"), "Study_Notes.pdf")
        self.assertEqual(storage_module.secure_name("archive.tar.gz"), "archive_tar.gz")
        self.assertEqual(storage_module.secure_name("no-extension"), "no-extension")
        self.assertEqual(storage_module.secure_name(".env"), "file.env")
        self.assertEqual(storage_module.secure_name(""), "file")

    def test_unique_file_name_avoids_collisions(self) -> None:
        async def scenario() -> list[str]:
            async with self.Session() as db:
                names = []
                for _ in range(3):
                    name = await storage_module.unique_file_name(db, "notes.pdf")
                    names.append(name)
                    row = Resource(title="t", subject="s", semester="Y1S1",
                                   file_name=name, file_path="")
                    db.add(row)
                    await db.commit()
                return names

        self.assertEqual(asyncio.run(scenario()),
                         ["notes.pdf", "notes_1.pdf", "notes_2.pdf"])


@unittest.skipUnless(HAVE_FPDF, "fpdf2 not installed (requirements-dev.txt)")
class TestStatelessPipeline(StorageCase):
    """The promise: a document that exists ONLY in the database can still be summarised."""

    def test_extraction_from_the_database(self) -> None:
        from tests import fixtures

        pdf_path = fixtures.build_pdf(self.root / "source.pdf", pages=6)
        payload = pdf_path.read_bytes()
        settings.STORAGE_BACKEND = "database"

        async def scenario() -> tuple[int, bool, str, str]:
            async with self.Session() as db:
                resource = await self._resource(db)
                storage = DatabaseStorage()
                await storage.save(db, resource, payload, "application/pdf")
                await db.commit()
                # Nothing on disk for this resource:
                self.assertFalse((settings.UPLOAD_DIR / resource.file_name).exists())
                extraction, digest = await S.source_identity(db, resource)
                return (len(extraction.sections), extraction.scanned, digest,
                        extraction.sections[0].heading if extraction.sections else "")

        sections, scanned, digest, heading = asyncio.run(scenario())
        self.assertGreater(sections, 3, "the PDF must be parsed out of the database")
        self.assertFalse(scanned)
        self.assertEqual(digest, hashlib.sha256(payload).hexdigest())
        self.assertIn("Pharmacokinetics", heading)

    def test_cache_is_per_backend_and_self_invalidating(self) -> None:
        from tests import fixtures

        pdf_path = fixtures.build_pdf(self.root / "source.pdf", pages=4)
        payload = pdf_path.read_bytes()

        async def scenario() -> tuple[bool, bool]:
            async with self.Session() as db:
                resource = await self._resource(db)
                storage = DatabaseStorage()
                await storage.save(db, resource, payload, "application/pdf")
                await db.commit()
                settings.STORAGE_BACKEND = "database"
                first, _ = await S.source_identity(db, resource)
                second, _ = await S.source_identity(db, resource)
                cached = first is second
                # Replacing the bytes must produce a fresh parse, not the cached one.
                await storage.save(db, resource, payload + b"% trailing", "application/pdf")
                await db.commit()
                third, _ = await S.source_identity(db, resource)
                return cached, third is not first

        cached, invalidated = asyncio.run(scenario())
        self.assertTrue(cached, "the second read must come from the cache")
        self.assertTrue(invalidated, "changed bytes must invalidate the cache")

    def test_missing_bytes_are_reported_clearly(self) -> None:
        settings.STORAGE_BACKEND = "database"

        async def scenario() -> str:
            async with self.Session() as db:
                resource = await self._resource(db)
                try:
                    await S.source_identity(db, resource)
                except S.ExtractionError as exc:
                    return str(exc)
                return "no error raised"

        message = asyncio.run(scenario())
        self.assertIn("missing from the database", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)

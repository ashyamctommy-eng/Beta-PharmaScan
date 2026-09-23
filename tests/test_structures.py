"""
tests/test_structures.py — the structure-image cache and its endpoint.

What has to hold: a compound is fetched from PubChem once and then served from disk; a
bad reference never reaches the network; a dead PubChem degrades to a clear error instead
of a broken image; and the cache cannot grow without bound or outlive its TTL.

No network: `fetch_structure` is replaced with a stub, which is also why the fetch itself
is a separate function.

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import structures as S  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes"


class StructureCase(unittest.TestCase):
    """Gives every test its own cache directory and a counting stub for PubChem."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self._real_data_dir = S.settings.DATA_DIR
        self._real_fetch = S.fetch_structure
        self._real_locks = dict(S._locks)
        S.settings.DATA_DIR = Path(self._dir.name)
        S._locks.clear()
        self.calls: list[tuple[str, str]] = []

        async def stub(kind: str, value: str) -> bytes:
            self.calls.append((kind, value))
            if value.lower() == "doesnotexist":
                raise S.StructureNotFound(value)
            if value.lower() == "unreachable":
                raise S.StructureUnavailable(value)
            return PNG

        S.fetch_structure = stub

    def tearDown(self) -> None:
        S.settings.DATA_DIR = self._real_data_dir
        S.fetch_structure = self._real_fetch
        S._locks.clear()
        S._locks.update(self._real_locks)
        self._dir.cleanup()

    def get(self, term: str) -> bytes:
        return asyncio.run(S.get_structure(term))


class TestTermValidation(StructureCase):
    def test_accepts_names_and_cids(self) -> None:
        self.assertEqual(S.parse_term("name/amoxicillin"), ("name", "amoxicillin"))
        self.assertEqual(S.parse_term("cid/2244"), ("cid", "2244"))
        self.assertEqual(S.parse_term("name/ethinyl%20estradiol"), ("name", "ethinyl%20estradiol"))

    def test_rejects_anything_that_could_steer_the_request(self) -> None:
        for term in ("", "amoxicillin", "name/../../etc/passwd", "name/a/b", "http://evil.test/x",
                     "name/", "name/  ", "other/amoxicillin", "name/" + "a" * 200):
            with self.subTest(term=term):
                self.assertRaises(S.StructureNotFound, S.parse_term, term)

    def test_a_rejected_term_never_reaches_the_network(self) -> None:
        self.assertRaises(S.StructureNotFound, self.get, "name/../../secrets")
        self.assertEqual(self.calls, [])


class TestCaching(StructureCase):
    def test_second_request_is_served_from_disk(self) -> None:
        self.assertEqual(self.get("name/amoxicillin"), PNG)
        self.assertEqual(self.get("name/amoxicillin"), PNG)
        self.assertEqual(len(self.calls), 1, "the compound was fetched more than once")

    def test_case_and_formatting_share_one_cache_entry(self) -> None:
        self.get("name/Amoxicillin")
        self.get("name/amoxicillin")
        self.assertEqual(len(self.calls), 1)

    def test_different_compounds_are_cached_separately(self) -> None:
        self.get("name/amoxicillin")
        self.get("name/ibuprofen")
        self.get("cid/2244")
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(list(S.cache_dir().glob("*.png"))), 3)

    def test_a_stale_entry_is_refetched(self) -> None:
        self.get("name/amoxicillin")
        path = S.cache_path("name", "amoxicillin")
        old = time.time() - S.CACHE_TTL_SECONDS - 60
        import os
        os.utime(path, (old, old))
        self.get("name/amoxicillin")
        self.assertEqual(len(self.calls), 2, "a month-old image should be fetched again")

    def test_a_corrupt_cache_entry_is_not_served(self) -> None:
        self.get("name/amoxicillin")
        S.cache_path("name", "amoxicillin").write_bytes(b"<html>an error page</html>")
        self.assertEqual(self.get("name/amoxicillin"), PNG)
        self.assertEqual(len(self.calls), 2)

    def test_the_cache_stays_bounded(self) -> None:
        original = S.MAX_CACHED_FILES
        S.MAX_CACHED_FILES = 5
        try:
            for i in range(12):
                self.get(f"cid/{i}")
        finally:
            S.MAX_CACHED_FILES = original
        self.assertLessEqual(len(list(S.cache_dir().glob("*.png"))), 5)

    def test_an_unwritable_cache_directory_still_serves_the_image(self) -> None:
        """A misconfigured DATA_DIR (a file, or a read-only volume) must not break the
        feature — the image is still served, it just is not cached."""
        blocked = Path(self._dir.name) / "not-a-directory"
        blocked.write_text("a file where a directory is expected", encoding="utf-8")
        S.settings.DATA_DIR = blocked
        self.assertEqual(self.get("name/amoxicillin"), PNG)

    def test_concurrent_requests_fetch_once(self) -> None:
        async def both() -> None:
            await asyncio.gather(S.get_structure("name/amoxicillin"),
                                 S.get_structure("name/amoxicillin"))

        asyncio.run(both())
        self.assertEqual(len(self.calls), 1)


class TestFailuresAreDistinguishable(StructureCase):
    def test_unknown_compound_is_not_found(self) -> None:
        self.assertRaises(S.StructureNotFound, self.get, "name/doesnotexist")

    def test_dead_upstream_is_unavailable(self) -> None:
        self.assertRaises(S.StructureUnavailable, self.get, "name/unreachable")

    def test_first_failure_is_not_cached_as_success(self) -> None:
        self.assertRaises(S.StructureUnavailable, self.get, "name/unreachable")
        self.assertEqual(list(S.cache_dir().glob("*.png")) if S.cache_dir().exists() else [], [])


class TestEndpoint(StructureCase):
    """The route's contract: an image, a 404, or a 503 — never a stack trace."""

    def setUp(self) -> None:
        super().setUp()
        from api.routes import router
        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def test_serves_a_png_and_lets_the_browser_keep_it(self) -> None:
        response = self.client.get("/api/structure/name/amoxicillin")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertIn("max-age", response.headers.get("cache-control", ""))
        self.assertEqual(response.content, PNG)

    def test_second_request_hits_the_cache_not_pubchem(self) -> None:
        self.client.get("/api/structure/name/ibuprofen")
        self.client.get("/api/structure/name/ibuprofen")
        self.assertEqual(len(self.calls), 1)

    def test_cid_reference_works(self) -> None:
        response = self.client.get("/api/structure/cid/2244")
        self.assertEqual(response.status_code, 200)

    def test_unknown_compound_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/structure/name/doesnotexist").status_code, 404)

    def test_upstream_trouble_is_503(self) -> None:
        self.assertEqual(self.client.get("/api/structure/name/unreachable").status_code, 503)

    def test_traversal_attempt_is_404_and_never_leaves_the_host(self) -> None:
        response = self.client.get("/api/structure/name/..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

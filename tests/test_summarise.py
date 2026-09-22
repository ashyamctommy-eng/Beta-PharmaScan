"""
tests/test_summarise.py — pipeline tests for core/summarise.py.

Runs against a private SQLite file and a stub model caller, so no API key is
needed. Verifies the properties the design depends on: outline-first planning,
bounded ticks, resumability without re-paying, quota enforcement, rate-limit
parking, citation clamping and deterministic merging.

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from core import summarise as S  # noqa: E402
from core.database import Base  # noqa: E402
from core.extract import Extraction, Section, estimate_tokens  # noqa: E402
from models import resource as _resource  # noqa: F401,E402  (registers tables)
from models import summary as _summary  # noqa: F401,E402
from models.summary import Summary, SummarySection, UsageEvent  # noqa: E402

try:
    import fpdf  # noqa: F401
    HAVE_FPDF = True
except ImportError:
    HAVE_FPDF = False


# ── Stub model ────────────────────────────────────────────────────────────────
class StubCaller:
    """Deterministic stand-in for GroqCaller, recording every call it receives."""

    def __init__(self, fail_status: int | None = None, prose_once: bool = False) -> None:
        self.calls: list[dict] = []
        self.fail_status = fail_status
        self.prose_once = prose_once

    async def call(self, *, kind, model, system, user, max_tokens, temperature, json_mode=True):
        self.calls.append({"kind": kind, "user": user, "model": model})
        if self.fail_status:
            exc = RuntimeError("rate limited")
            exc.status_code = self.fail_status            # type: ignore[attr-defined]
            exc.response = type("R", (), {"headers": {"retry-after": "7"}})()
            raise exc
        if self.prose_once and len(self.calls) == 1:
            return S.CallOutcome("Sure! Here is the JSON:\nnot json at all",
                                 model, 10, 10, 20, payload=None)
        if kind == "outline":
            payload = {"title": "Pharmacology Unit Notes", "sections": []}
            for match in re.finditer(r"^\[(s\d+)\]\s*(.+?)\s*\(p\.(\d+)\)", user, re.MULTILINE):
                section_id, heading, page = match.group(1), match.group(2), int(match.group(3))
                payload["sections"].append({
                    "id": section_id, "heading": heading, "page": page,
                    "gist": "intro material", "importance": 5 if page <= 4 else 2,
                })
            return S.CallOutcome(json.dumps(payload), model, estimate_tokens(user), 90,
                                 estimate_tokens(user) + 90, payload)
        if kind == "section":
            heading = re.search(r"Section: (.+)", user)
            page_match = re.search(r"Pages: p\.(\d+)", user)
            page = int(page_match.group(1)) if page_match else 0   # no-page formats omit the line
            payload = {
                "usable": True,
                "bullets": [
                    {"text": f"A key fact from {heading.group(1) if heading else 'the section'}.", "page": page},
                    {"text": "Same fact, restated differently.", "page": page},
                ],
                "key_terms": ["CYP3A4", "bioavailability"],
                "drug_table": [{"name": "Midazolam", "class": "Benzodiazepine",
                                "mechanism": "CYP3A4 substrate", "note": "sedation"}],
                "mnemonic": "Phase One Prepares", "flags": [],
            }
            return S.CallOutcome(json.dumps(payload), model, estimate_tokens(user), 120,
                                 estimate_tokens(user) + 120, payload)
        payload = {"title": "Pharmacology Unit Notes",
                   "remember_5": ["F is the fraction reaching systemic circulation"],
                   "exam_traps": ["Confusing clearance with half-life"]}
        return S.CallOutcome(json.dumps(payload), model, estimate_tokens(user), 60,
                             estimate_tokens(user) + 60, payload)


class SlowStubCaller(StubCaller):
    """Same stub, but every call takes `delay` seconds — so parallelism is measurable."""

    def __init__(self, delay: float = 0.15, **kwargs) -> None:
        super().__init__(**kwargs)
        self.delay = delay

    async def call(self, **kwargs):
        await asyncio.sleep(self.delay)
        return await super().call(**kwargs)


# ── Helpers ───────────────────────────────────────────────────────────────────
def fake_extraction(sections: int = 4, tokens_each: int = 300) -> Extraction:
    parts = [
        Section(id=f"s{i + 1}", heading=f"{i + 1}. Topic {i + 1}", level=1, page=i + 1,
                end_page=i + 1, text=f"Body text for topic {i + 1}. " * (tokens_each // 6),
                tokens=tokens_each)
        for i in range(sections)
    ]
    text = "\n".join(p.text for p in parts)
    return Extraction(kind="pdf", pages=sections, sections=parts, text=text,
                      tokens=estimate_tokens(text), chars_per_page=1500)


class PipelineCase(unittest.TestCase):
    """Base class giving each test a private DB + summary row."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self._dir.name) / "test.db"
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{self.db_path}")
        self.Session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        # Stage 2 opens one session PER PARALLEL TASK from core.database.AsyncSessionLocal.
        # Point that at this private engine, or the parallel path would write to the
        # developer's real database.
        self._real_session_local = S.AsyncSessionLocal
        S.AsyncSessionLocal = self.Session
        self._seq = 0
        self._original = {
            "SUMMARISE_CALLS_PER_REQUEST": S.settings.SUMMARISE_CALLS_PER_REQUEST,
            "SUMMARISE_CONCURRENCY": S.settings.SUMMARISE_CONCURRENCY,
            "SUMMARISE_DAILY_TOKEN_BUDGET": S.settings.SUMMARISE_DAILY_TOKEN_BUDGET,
            "SUMMARISE_PER_IP_DAILY_TOKENS": S.settings.SUMMARISE_PER_IP_DAILY_TOKENS,
            "SUMMARISE_MAX_SECTIONS": S.settings.SUMMARISE_MAX_SECTIONS,
            "SUMMARISE_MAX_INPUT_TOKENS": S.settings.SUMMARISE_MAX_INPUT_TOKENS,
            "GROQ_MODEL": S.settings.GROQ_MODEL,
        }
        asyncio.run(self._create_schema())

    def tearDown(self) -> None:
        S.AsyncSessionLocal = self._real_session_local
        for key, value in self._original.items():
            setattr(S.settings, key, value)
        asyncio.run(self.engine.dispose())
        self._dir.cleanup()

    async def _create_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _new_summary(self, depth: str = "standard") -> Summary:
        self._seq += 1
        async with self.Session() as db:
            summary = Summary(resource_id=1, file_hash=f"hash-{depth}-{id(self)}-{self._seq}",
                              file_name="notes.pdf", depth=depth, status="pending")
            db.add(summary)
            await db.commit()
            await db.refresh(summary)
            return summary

    async def _tick(self, summary: Summary, extraction: Extraction, caller: StubCaller,
                    max_calls: int | None = None) -> S.TickResult:
        async with self.Session() as db:
            fresh = await db.get(Summary, summary.id)
            return await S.run_tick(db, fresh, extraction, client_id="test-client",
                                    caller=caller, max_calls=max_calls)


# ── Tests ─────────────────────────────────────────────────────────────────────
class TestHappyPath(PipelineCase):
    def test_full_run_produces_notes(self) -> None:
        extraction = fake_extraction(sections=3)
        summary = asyncio.run(self._new_summary())
        caller = StubCaller()

        result = asyncio.run(self._tick(summary, extraction, caller, max_calls=10))

        self.assertEqual(result.status, "done", result.message)
        notes = result.notes
        self.assertIsNotNone(notes)
        self.assertEqual(notes["title"], "Pharmacology Unit Notes")
        self.assertEqual(len(notes["topics"]), 3)
        self.assertTrue(notes["remember_5"])

        kinds = [c["kind"] for c in caller.calls]
        self.assertEqual(kinds.count("outline"), 1)
        self.assertEqual(kinds.count("reduce"), 1)
        self.assertEqual(kinds.count("section"), 3)
        for topic in notes["topics"]:
            self.assertTrue(topic["bullets"])
            for bullet in topic["bullets"]:
                self.assertGreaterEqual(bullet["page"], topic["page"])
                self.assertLessEqual(bullet["page"], topic["end_page"])

    def test_brief_depth_skips_section_calls(self) -> None:
        extraction = fake_extraction(sections=5)
        summary = asyncio.run(self._new_summary(depth="brief"))
        caller = StubCaller()

        result = asyncio.run(self._tick(summary, extraction, caller, max_calls=10))

        self.assertEqual(result.status, "done")
        kinds = [c["kind"] for c in caller.calls]
        self.assertEqual(kinds.count("section"), 0, "brief depth must not expand sections")
        self.assertEqual(result.sections_total, 0)

    def test_standard_depth_expands_only_important_sections(self) -> None:
        extraction = fake_extraction(sections=6)
        summary = asyncio.run(self._new_summary(depth="standard"))
        caller = StubCaller()

        result = asyncio.run(self._tick(summary, extraction, caller, max_calls=10))

        self.assertEqual(result.status, "done")
        # The stub marks pages <= 4 as importance 5, the rest as 2.
        self.assertEqual(result.sections_total, 4)

    def test_cache_hit_does_not_repay(self) -> None:
        extraction = fake_extraction(sections=2)
        summary = asyncio.run(self._new_summary())
        caller = StubCaller()
        asyncio.run(self._tick(summary, extraction, caller, max_calls=10))
        first_round = len(caller.calls)

        result = asyncio.run(self._tick(summary, extraction, caller, max_calls=10))

        self.assertEqual(result.status, "done")
        self.assertEqual(len(caller.calls), first_round, "a finished summary must not call the model again")
        self.assertTrue(result.notes)


class TestBoundedWork(PipelineCase):
    def test_ticks_are_bounded_and_resume_without_duplicate_calls(self) -> None:
        extraction = fake_extraction(sections=5)
        summary = asyncio.run(self._new_summary(depth="full"))
        caller = StubCaller()

        statuses, guard = [], 0
        while guard < 12:
            guard += 1
            result = asyncio.run(self._tick(summary, extraction, caller, max_calls=2))
            statuses.append(result.status)
            if result.status == "done":
                break

        self.assertEqual(statuses[-1], "done")
        self.assertGreater(len(statuses), 1, "work must be spread over several bounded ticks")
        kinds = [c["kind"] for c in caller.calls]
        self.assertEqual(kinds.count("outline"), 1, "outline must not be recomputed on resume")
        self.assertEqual(kinds.count("section"), 5, "each section must be expanded exactly once")
        self.assertEqual(kinds.count("reduce"), 1)

    def test_section_rows_are_committed_as_they_finish(self) -> None:
        extraction = fake_extraction(sections=4)
        summary = asyncio.run(self._new_summary(depth="full"))
        caller = StubCaller()
        asyncio.run(self._tick(summary, extraction, caller, max_calls=2))

        async def count_done() -> int:
            async with self.Session() as db:
                from sqlalchemy import func, select
                return int((await db.execute(
                    select(func.count()).select_from(SummarySection)
                    .where(SummarySection.summary_id == summary.id, SummarySection.status == "done")
                )).scalar() or 0)

        self.assertGreaterEqual(asyncio.run(count_done()), 1,
                                "finished sections must be persisted for resume")


class TestQuotaAndFailures(PipelineCase):
    def test_daily_budget_stops_the_run_and_keeps_progress(self) -> None:
        extraction = fake_extraction(sections=4)
        summary = asyncio.run(self._new_summary(depth="full"))
        caller = StubCaller()
        S.settings.SUMMARISE_DAILY_TOKEN_BUDGET = 1

        result = asyncio.run(self._tick(summary, extraction, caller, max_calls=5))
        self.assertEqual(result.status, "budget_exhausted")
        self.assertIn("budget", result.message.lower())

        # Raise the budget: the run continues instead of restarting.
        S.settings.SUMMARISE_DAILY_TOKEN_BUDGET = 200_000
        resumed = asyncio.run(self._tick(summary, extraction, caller, max_calls=10))
        self.assertEqual(resumed.status, "done")
        self.assertGreater(len(resumed.notes["topics"]), 0)

    def test_per_client_budget_is_enforced(self) -> None:
        extraction = fake_extraction(sections=3)
        summary = asyncio.run(self._new_summary())

        async def seed_usage() -> None:
            async with self.Session() as db:
                db.add(UsageEvent(kind="section", model="m", total_tokens=50_000, client="noisy-client"))
                await db.commit()

        asyncio.run(seed_usage())
        S.settings.SUMMARISE_PER_IP_DAILY_TOKENS = 100

        async def run() -> S.TickResult:
            async with self.Session() as db:
                fresh = await db.get(Summary, summary.id)
                return await S.run_tick(db, fresh, extraction, client_id="noisy-client",
                                        caller=StubCaller(), max_calls=5)

        result = asyncio.run(run())
        self.assertEqual(result.status, "budget_exhausted")
        self.assertIn("daily share", result.message)

    def test_rate_limit_parks_the_job_with_a_clear_message(self) -> None:
        extraction = fake_extraction(sections=3)
        summary = asyncio.run(self._new_summary())

        result = asyncio.run(self._tick(summary, extraction, StubCaller(fail_status=429), max_calls=5))

        self.assertEqual(result.status, "rate_limited")
        self.assertIn("rate-limit", result.message.lower())
        self.assertIn("7s", result.message, "retry-after from the vendor should be surfaced")

    def test_malformed_json_is_retried_then_recovers(self) -> None:
        extraction = fake_extraction(sections=1)
        summary = asyncio.run(self._new_summary())
        caller = StubCaller(prose_once=True)

        result = asyncio.run(self._tick(summary, extraction, caller, max_calls=6))

        self.assertEqual(result.status, "done")
        self.assertGreaterEqual(len(caller.calls), 3, "prose reply should trigger one retry")


class TestReviewRegressions(PipelineCase):
    """Each test here locks in a bug found by an independent review of the diff."""

    def test_non_numeric_importance_does_not_crash_the_run(self) -> None:
        class BadImportance(StubCaller):
            async def call(self, **kwargs):
                outcome = await super().call(**kwargs)
                if kwargs["kind"] == "outline" and outcome.payload:
                    for section in outcome.payload["sections"]:
                        section["importance"] = "high"          # not an int
                return outcome

        extraction = fake_extraction(sections=3)
        summary = asyncio.run(self._new_summary())
        result = asyncio.run(self._tick(summary, extraction, BadImportance(), max_calls=10))
        self.assertEqual(result.status, "done", result.message)

    def test_string_citations_are_coerced(self) -> None:
        class StringPages(StubCaller):
            async def call(self, **kwargs):
                outcome = await super().call(**kwargs)
                if kwargs["kind"] == "section" and outcome.payload:
                    outcome.payload["bullets"] = [{"text": "A fact.", "page": "p.7"},   # prefixed
                                                  {"text": "Another.", "page": "9-10"}]  # a range
                    outcome.payload["drug_table"] = []
                return outcome

        extraction = fake_extraction(sections=2)
        summary = asyncio.run(self._new_summary(depth="full"))
        result = asyncio.run(self._tick(summary, extraction, StringPages(), max_calls=10))
        self.assertEqual(result.status, "done")
        pages = [b["page"] for t in result.notes["topics"] for b in t["bullets"]]
        self.assertTrue(all(isinstance(p, int) and p > 0 for p in pages), pages)
        self.assertLessEqual(max(pages), 2, "citations must be clamped into the section range")

    def test_formats_without_pages_carry_no_citations(self) -> None:
        extraction = fake_extraction(sections=2)
        for section in extraction.sections:
            section.page = 0
            section.end_page = 0
        summary = asyncio.run(self._new_summary(depth="full"))

        class PaperNoPages(StubCaller):
            async def call(self, **kwargs):
                outcome = await super().call(**kwargs)
                if kwargs["kind"] == "section" and outcome.payload:
                    for bullet in outcome.payload["bullets"]:
                        bullet["page"] = 4                      # model invents one anyway
                return outcome

        result = asyncio.run(self._tick(summary, extraction, PaperNoPages(), max_calls=10))
        self.assertEqual(result.status, "done")
        self.assertEqual(result.sections_failed, 0, "the run must not lose sections in this format")
        topics = result.notes["topics"]
        self.assertTrue(topics, "notes must still be produced for a page-less format")
        pages = [b["page"] for t in topics for b in t["bullets"]]
        self.assertTrue(pages, "there should be bullets to check")
        self.assertTrue(all(p == 0 for p in pages), f"no pages exist, so none may be cited: {pages}")

    def test_a_second_concurrent_tick_is_refused(self) -> None:
        summary = asyncio.run(self._new_summary())

        async def claim_twice() -> tuple[bool, bool]:
            async with self.Session() as db:
                first = await S.claim_lease(db, summary.id)
                second = await S.claim_lease(db, summary.id)
            return first, second

        first, second = asyncio.run(claim_twice())
        self.assertTrue(first)
        self.assertFalse(second, "a second tick must not run concurrently on the same document")

        # The lease holder then tries to run: it should report 'busy', not duplicate work.
        extraction = fake_extraction(sections=3)
        caller = StubCaller()
        result = asyncio.run(self._tick(summary, extraction, caller, max_calls=5))
        self.assertEqual(result.status, "busy")
        self.assertEqual(len(caller.calls), 0, "no model calls may happen while another tick holds the lease")

    def test_expired_lease_can_be_reclaimed(self) -> None:
        from datetime import datetime, timedelta, timezone
        summary = asyncio.run(self._new_summary())

        async def claim() -> tuple[bool, bool]:
            async with self.Session() as db:
                await S.claim_lease(db, summary.id)
                fresh = await db.get(Summary, summary.id)
                fresh.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
                await db.commit()
                return await S.claim_lease(db, summary.id)

        self.assertTrue(asyncio.run(claim()), "an expired lease must be reclaimable (crash recovery)")

    def test_chunks_completed_before_a_rate_limit_are_not_repaid(self) -> None:
        """A long section is several calls; a 429 mid-section must not throw them away."""
        long_text = "\n".join(f"Sentence {i} about pharmacokinetics and drug clearance." for i in range(900))
        extraction = fake_extraction(sections=1)
        extraction.sections[0].text = long_text
        extraction.sections[0].tokens = estimate_tokens(long_text)
        summary = asyncio.run(self._new_summary(depth="full"))

        class FailsOnSecondChunk(StubCaller):
            async def call(self, **kwargs):
                if kwargs["kind"] == "section" and sum(1 for c in self.calls if c["kind"] == "section") >= 1:
                    exc = RuntimeError("rate limited")
                    exc.status_code = 429                                    # type: ignore[attr-defined]
                    exc.response = type("R", (), {"headers": {}})()
                    raise exc
                return await super().call(**kwargs)

        first_caller = FailsOnSecondChunk()
        first = asyncio.run(self._tick(summary, extraction, first_caller, max_calls=6))
        self.assertEqual(first.status, "rate_limited")

        async def partial_state() -> tuple[str, int]:
            async with self.Session() as db:
                from sqlalchemy import select
                row = (await db.execute(select(SummarySection)
                                        .where(SummarySection.summary_id == summary.id))).scalars().first()
                return row.payload_json, row.status

        payload, status = asyncio.run(partial_state())
        self.assertEqual(status, "pending")
        self.assertIn("_chunks_done", payload, "completed chunks must be persisted for the resume")

        resumed_caller = StubCaller()
        result = asyncio.run(self._tick(summary, extraction, resumed_caller, max_calls=10))
        self.assertEqual(result.status, "done", result.message)
        section_calls = sum(1 for c in resumed_caller.calls if c["kind"] == "section")
        total_chunks = len(S._split_text(long_text, S.settings.SUMMARISE_MAX_INPUT_TOKENS * 4 - 1200))
        self.assertGreater(total_chunks, 1, "this fixture must need several chunks")
        self.assertLess(section_calls, total_chunks,
                        "the resumed run must not repeat the chunks already completed")

    def test_chunking_uses_the_token_budget_not_characters(self) -> None:
        """Regression: the chunk size was compared against a *token* budget in
        characters, so a section produced ~4x more calls than the quoted estimate."""
        text = "word " * 2400                                    # ~12,000 chars ≈ 3,000 tokens
        section = Section(id="s1", heading="T", level=1, page=1, end_page=1,
                          text=text, tokens=estimate_tokens(text))
        chunks = S._split_text(section.text, S.settings.SUMMARISE_MAX_INPUT_TOKENS * 4 - 1200)
        self.assertEqual(len(chunks), 1, "a 3,000-token section must fit in one 5,000-token call")
        quoted = S.estimate_cost_tokens(Extraction(
            kind="pdf", pages=1, sections=[section], text=text, tokens=section.tokens), "full")
        self.assertLess(quoted, 12_000, "the quote must be of the same order as the real cost")

    def test_spend_is_recorded_exactly_once(self) -> None:
        extraction = fake_extraction(sections=3)
        summary = asyncio.run(self._new_summary(depth="full"))
        caller = StubCaller()
        asyncio.run(self._tick(summary, extraction, caller, max_calls=10))

        async def totals() -> tuple[int, int]:
            async with self.Session() as db:
                from sqlalchemy import func, select
                events = int((await db.execute(
                    select(func.coalesce(func.sum(UsageEvent.total_tokens), 0)))).scalar() or 0)
                fresh = await db.get(Summary, summary.id)
                return events, fresh.tokens_spent

        events, reported = asyncio.run(totals())
        self.assertEqual(reported, events, "summary.tokens_spent must equal the usage ledger")

    def test_budget_room_is_the_smaller_of_the_two_ceilings(self) -> None:
        async def seed() -> None:
            async with self.Session() as db:
                db.add(UsageEvent(kind="section", model="m", total_tokens=149_000, client="someone"))
                await db.commit()

        asyncio.run(seed())
        S.settings.SUMMARISE_DAILY_TOKEN_BUDGET = 150_000
        S.settings.SUMMARISE_PER_IP_DAILY_TOKENS = 30_000

        async def room() -> tuple[int, str]:
            async with self.Session() as db:
                return await S.budget_room(db, "fresh-client")

        remaining, blocked = asyncio.run(room())
        self.assertFalse(blocked)
        self.assertEqual(remaining, 1_000, "the app-wide room must bound the per-client room")

    def test_same_content_uploaded_twice_resolves_to_the_same_notes(self) -> None:
        """Regression: notes are cached by file hash but were looked up by resource
        id, so the second copy of an identical PDF reported 'no summary'."""
        from types import SimpleNamespace

        from core.config import settings as app_settings

        original = app_settings.UPLOAD_DIR
        upload_dir = Path(self._dir.name) / "uploads"
        upload_dir.mkdir(exist_ok=True)
        (upload_dir / "notes.md").write_text("# Topic\n\nIdentical content.\n", encoding="utf-8")
        app_settings.UPLOAD_DIR = upload_dir
        try:
            first = SimpleNamespace(id=1, file_name="notes.md", title="First")
            second = SimpleNamespace(id=2, file_name="notes.md", title="Second")

            async def scenario() -> tuple[int, int, int, bool]:
                async with self.Session() as db:
                    created = await S.get_or_create_summary(db, first, depth="brief")
                    found_for_second = await S.get_summary(db, second)
                    repointed = await S.get_or_create_summary(db, second, depth="brief")
                    return (created.id, found_for_second.id if found_for_second else -1,
                            repointed.id, repointed.resource_id == 2)

            created_id, found_id, repointed_id, repointed = asyncio.run(scenario())
            self.assertEqual(found_id, created_id, "the second copy must find the existing notes")
            self.assertEqual(repointed_id, created_id, "it must not create a duplicate summary")
            self.assertTrue(repointed, "the notes should re-point at the newest vault item")
        finally:
            app_settings.UPLOAD_DIR = original

    def test_changing_depth_rebuilds_the_plan_instead_of_returning_empty_notes(self) -> None:
        """Regression: a depth change cleared the notes and the section rows but kept
        the outline, so the next run skipped planning and produced 0 topics."""
        from types import SimpleNamespace

        from core.config import settings as app_settings

        original = app_settings.UPLOAD_DIR
        upload_dir = Path(self._dir.name) / "uploads"
        upload_dir.mkdir(exist_ok=True)
        (upload_dir / "notes.md").write_text("# Topic one\n\nClearance is volume per time.\n",
                                            encoding="utf-8")
        app_settings.UPLOAD_DIR = upload_dir
        try:
            resource = SimpleNamespace(id=7, file_name="notes.md", title="Doc")
            extraction = fake_extraction(sections=3)
            caller = StubCaller()

            async def scenario() -> tuple[list[str], int]:
                async with self.Session() as db:
                    summary = await S.get_or_create_summary(db, resource, depth="brief")
                    await S.run_tick(db, summary, extraction, client_id="c", caller=caller, max_calls=8)
                    # Now ask for a deeper rebuild of the same document.
                    summary = await S.get_or_create_summary(db, resource, depth="standard")
                    result = await S.run_tick(db, summary, extraction, client_id="c",
                                              caller=caller, max_calls=10)
                    guard = 0
                    while result.status in ("running", "pending") and guard < 6:
                        guard += 1
                        result = await S.run_tick(db, summary, extraction, client_id="c",
                                                  caller=caller, max_calls=10)
                    return [t["heading"] for t in (result.notes or {}).get("topics", [])], result.sections_total

            headings, planned = asyncio.run(scenario())
            self.assertTrue(headings, "a depth change must rebuild the plan, not return empty notes")
            self.assertGreater(planned, 0)
        finally:
            app_settings.UPLOAD_DIR = original

    def test_reupload_at_a_new_depth_rebuilds_instead_of_serving_stale_notes(self) -> None:
        """Regression: re-pointing an identical re-upload and rebuilding for a new
        depth were chained as elif, so the combination returned the old depth's notes
        (and 'brief' notes have no topics — the UI showed an empty summary)."""
        from types import SimpleNamespace

        from core.config import settings as app_settings

        original = app_settings.UPLOAD_DIR
        upload_dir = Path(self._dir.name) / "uploads2"
        upload_dir.mkdir(exist_ok=True)
        (upload_dir / "notes.md").write_text("# Topic\n\nClearance is volume per time.\n",
                                             encoding="utf-8")
        app_settings.UPLOAD_DIR = upload_dir
        try:
            first = SimpleNamespace(id=1, file_name="notes.md", title="First")
            second = SimpleNamespace(id=2, file_name="notes.md", title="Re-upload")
            extraction = fake_extraction(sections=3)
            caller = StubCaller()

            async def scenario() -> dict:
                async with self.Session() as db:
                    summary = await S.get_or_create_summary(db, first, depth="brief")
                    await S.run_tick(db, summary, extraction, client_id="c", caller=caller, max_calls=8)
                    brief_notes = S._load_notes(await db.get(Summary, summary.id))
                    # Same bytes, new resource id, *and* a deeper depth in one request.
                    again = await S.get_or_create_summary(db, second, depth="standard")
                    result = await S.run_tick(db, again, extraction, client_id="c",
                                              caller=caller, max_calls=10)
                    guard = 0
                    while result.status in ("running", "pending") and guard < 6:
                        guard += 1
                        result = await S.run_tick(db, again, extraction, client_id="c",
                                                  caller=caller, max_calls=10)
                    return {"brief_topics": len(brief_notes.get("topics", [])),
                            "depth": result.notes.get("depth") if result.notes else None,
                            "topics": len(result.notes.get("topics", [])) if result.notes else -1}

            outcome = asyncio.run(scenario())
            self.assertEqual(outcome["brief_topics"], 0, "brief notes legitimately have no topics")
            self.assertEqual(outcome["depth"], "standard")
            self.assertGreater(outcome["topics"], 0,
                               "the re-upload at a new depth must come back with notes, not the stale cache")
        finally:
            app_settings.UPLOAD_DIR = original

    def test_failed_sections_are_counted_and_reported(self) -> None:
        extraction = fake_extraction(sections=2)
        summary = asyncio.run(self._new_summary(depth="full"))

        class SectionFails(StubCaller):
            async def call(self, **kwargs):
                if kwargs["kind"] == "section":
                    exc = RuntimeError("model exploded")
                    exc.status_code = 500                                   # type: ignore[attr-defined]
                    raise exc
                return await super().call(**kwargs)

        result = asyncio.run(self._tick(summary, extraction, SectionFails(), max_calls=10))
        self.assertEqual(result.status, "done")
        self.assertEqual(result.sections_failed, 2)
        self.assertTrue(any("could not be summaris" in w for w in result.warnings + (result.notes or {}).get("warnings", [])))


class TestMergingAndHelpers(unittest.TestCase):
    def test_merge_deduplicates_bullets_and_drugs(self) -> None:
        section = Section(id="s1", heading="Topic", level=1, page=3, end_page=4, text="x", tokens=10)
        payloads = [
            {"usable": True, "bullets": [{"text": "Same fact.", "page": 3}],
             "key_terms": ["A"], "drug_table": [{"name": "Midazolam", "class": "BZD"}], "mnemonic": "M1"},
            {"usable": True, "bullets": [{"text": "same fact", "page": 4}, {"text": "New fact.", "page": 4}],
             "key_terms": ["A", "B"], "drug_table": [{"name": "midazolam", "class": "bzd"}], "mnemonic": "M2"},
        ]
        merged = S._merge_payloads(payloads, section)
        self.assertEqual(len(merged["bullets"]), 2, "duplicate bullets must collapse")
        self.assertEqual(merged["key_terms"], ["A", "B"])
        self.assertEqual(len(merged["drug_table"]), 1, "duplicate drug rows must collapse")
        self.assertEqual(merged["mnemonic"], "M1")

    def test_unusable_sections_are_reported_not_invented(self) -> None:
        section = Section(id="s1", heading="Scan", level=1, page=1, end_page=1, text="", tokens=1)
        merged = S._merge_payloads([{"usable": False, "reason": "garbled"}], section)
        self.assertFalse(merged["usable"])
        self.assertEqual(merged["bullets"], [])

    def test_pages_are_clamped_to_the_section(self) -> None:
        section = Section(id="s1", heading="T", level=1, page=5, end_page=7, text="x", tokens=5)
        merged = S._merge_payloads([{"usable": True, "bullets": [{"text": "f", "page": 99}]}], section)
        self.assertEqual(merged["bullets"][0]["page"], 7)

    def test_json_lenient_handles_fences_and_prose(self) -> None:
        self.assertEqual(S.parse_json_lenient('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(S.parse_json_lenient('Here you go: {"a": 2} hope that helps'), {"a": 2})
        self.assertIsNone(S.parse_json_lenient("no json here"))
        self.assertIsNone(S.parse_json_lenient(""))

    def test_cost_estimate_increases_with_depth(self) -> None:
        extraction = fake_extraction(sections=6)
        brief = S.estimate_cost_tokens(extraction, "brief")
        standard = S.estimate_cost_tokens(extraction, "standard")
        full = S.estimate_cost_tokens(extraction, "full")
        self.assertLess(brief, standard)
        self.assertLessEqual(standard, full)

    def test_digest_stays_within_the_call_budget(self) -> None:
        topics = [{"heading": f"T{i}", "page": i, "importance": 5,
                   "bullets": [{"text": "x" * 300, "page": i} for _ in range(8)],
                   "drug_table": [{"name": f"D{i}"}]} for i in range(60)]
        digest = S._digest_for_reduce(topics)
        self.assertLess(len(digest), S.settings.SUMMARISE_MAX_INPUT_TOKENS * 4)




class TestParallelSectionExpansion(PipelineCase):
    """Stage 2 with SUMMARISE_CONCURRENCY > 1: identical results, less wall-clock,
    one session per task, and the per-tick call budget still bounded."""

    def _section_states(self, summary: Summary) -> dict[str, tuple[str, str]]:
        from sqlalchemy import select

        async def read() -> dict[str, tuple[str, str]]:
            async with self.Session() as db:
                rows = (await db.execute(
                    select(SummarySection).where(SummarySection.summary_id == summary.id)
                )).scalars().all()
                return {row.section_id: (row.status, row.payload_json) for row in rows}

        return asyncio.run(read())

    def test_parallel_batch_is_faster_and_writes_the_same_state(self) -> None:
        delay, sections = 0.15, 4
        runs: dict[int, tuple[float, S.TickResult, StubCaller, Summary, Extraction]] = {}
        for concurrency in (1, 4):
            extraction = fake_extraction(sections=sections)
            summary = asyncio.run(self._new_summary(depth="full"))
            caller = SlowStubCaller(delay=delay)
            S.settings.SUMMARISE_CONCURRENCY = concurrency
            # The first tick spends its single call on the outline, so the timed tick is
            # pure stage-2 work: 4 sections x delay when sequential, one batch otherwise.
            outline = asyncio.run(self._tick(summary, extraction, caller, max_calls=1))
            self.assertEqual(outline.status, "running", outline.message)

            started = time.perf_counter()
            result = asyncio.run(self._tick(summary, extraction, caller, max_calls=sections))
            elapsed = time.perf_counter() - started
            runs[concurrency] = (elapsed, result, caller, summary, extraction)

            self.assertEqual(result.status, "running")
            self.assertEqual(result.sections_done, sections)
            self.assertEqual(sum(1 for c in caller.calls if c["kind"] == "section"), sections)

        sequential, parallel = runs[1][0], runs[4][0]
        self.assertGreaterEqual(sequential, 1.8 * parallel,
                                f"4 parallel sections must beat the sequential path by "
                                f"1.8x (took {sequential:.2f}s vs {parallel:.2f}s)")

        # Same rows, in the same state, and the same notes once the run is finished.
        self.assertEqual(self._section_states(runs[1][3]), self._section_states(runs[4][3]))
        notes = {}
        for concurrency, (_, _, caller, summary, extraction) in runs.items():
            final = asyncio.run(self._tick(summary, extraction, caller, max_calls=10))
            self.assertEqual(final.status, "done", final.message)
            self.assertEqual(final.sections_failed, 0)
            notes[concurrency] = final.notes
        # Only the per-document identity/timestamp may differ (they are different summaries).
        def strip(notes: dict) -> dict:
            return {k: v for k, v in notes.items() if k not in ("file_hash", "generated_at")}

        self.assertEqual(strip(notes[1]), strip(notes[4]),
                         "parallelism must not change the notes")

    def test_call_budget_is_honoured_with_concurrency(self) -> None:
        extraction = fake_extraction(sections=6)
        summary = asyncio.run(self._new_summary(depth="full"))
        caller = StubCaller()
        S.settings.SUMMARISE_CONCURRENCY = 4

        result = asyncio.run(self._tick(summary, extraction, caller, max_calls=3))

        self.assertEqual(len(caller.calls), 3, "a tick must never exceed its call budget")
        kinds = [c["kind"] for c in caller.calls]
        self.assertEqual(kinds.count("outline"), 1)
        self.assertEqual(kinds.count("section"), 2,
                         "the batch must be clamped to the remaining budget (3 - outline)")
        self.assertEqual(result.status, "running")
        self.assertEqual(result.sections_done, 2)

    def test_a_failing_section_does_not_lose_its_batch(self) -> None:
        extraction = fake_extraction(sections=4)
        summary = asyncio.run(self._new_summary(depth="full"))

        class OneBadSection(StubCaller):
            async def call(self, **kwargs):
                if kwargs["kind"] == "section" and "3. Topic 3" in kwargs["user"]:
                    raise RuntimeError("provider exploded")
                await asyncio.sleep(0.02)
                return await super().call(**kwargs)

        caller = OneBadSection()
        S.settings.SUMMARISE_CONCURRENCY = 4
        result = asyncio.run(self._tick(summary, extraction, caller, max_calls=10))

        self.assertEqual(result.status, "done", result.message)
        self.assertEqual(result.sections_done, 4, "an errored section still counts as done")
        self.assertEqual(result.sections_failed, 1)
        self.assertTrue(any("3. Topic 3" in w for w in result.warnings), result.warnings)

        states = self._section_states(summary)
        self.assertEqual(states["s3"][0], "error")
        self.assertIn("provider exploded", states["s3"][1])
        for section_id in ("s1", "s2", "s4"):
            self.assertEqual(states[section_id][0], "done",
                             f"{section_id} must still complete when a sibling fails")
        self.assertTrue(result.notes and result.notes["topics"],
                        "the surviving sections must still produce notes")

    def test_rate_limit_inside_a_batch_parks_the_run_and_resumes(self) -> None:
        extraction = fake_extraction(sections=4)
        summary = asyncio.run(self._new_summary(depth="full"))

        class RateLimitsOneSection(StubCaller):
            async def call(self, **kwargs):
                if kwargs["kind"] == "section" and "3. Topic 3" in kwargs["user"]:
                    exc = RuntimeError("rate limited")
                    exc.status_code = 429                       # type: ignore[attr-defined]
                    exc.response = type("R", (), {"headers": {}})()
                    raise exc
                await asyncio.sleep(0.15)
                return await super().call(**kwargs)

        S.settings.SUMMARISE_CONCURRENCY = 4
        result = asyncio.run(self._tick(summary, extraction, RateLimitsOneSection(), max_calls=10))
        self.assertEqual(result.status, "rate_limited")
        self.assertIn("rate-limit", result.message.lower())
        self.assertEqual(sum(1 for s in self._section_states(summary).values() if s[0] == "error"), 0,
                         "a rate limit must not mark a section failed")

        resumed = asyncio.run(self._tick(summary, extraction, StubCaller(), max_calls=10))
        self.assertEqual(resumed.status, "done", resumed.message)
        self.assertEqual(resumed.sections_done, 4)

    def test_concurrency_one_runs_the_sequential_path(self) -> None:
        extraction = fake_extraction(sections=3)
        summary = asyncio.run(self._new_summary(depth="full"))
        caller = StubCaller()
        S.settings.SUMMARISE_CONCURRENCY = 1

        result = asyncio.run(self._tick(summary, extraction, caller, max_calls=10))

        self.assertEqual(result.status, "done")
        self.assertEqual(sum(1 for c in caller.calls if c["kind"] == "section"), 3)
        self.assertEqual(result.sections_done, 3)
        self.assertEqual(result.sections_failed, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

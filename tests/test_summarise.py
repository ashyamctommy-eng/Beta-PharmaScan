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

    async def call(self, *, kind, model, system, user, max_tokens, temperature):
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
            page = int(re.search(r"Pages: p\.(\d+)", user).group(1))
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
        self._original = {
            "SUMMARISE_CALLS_PER_REQUEST": S.settings.SUMMARISE_CALLS_PER_REQUEST,
            "SUMMARISE_DAILY_TOKEN_BUDGET": S.settings.SUMMARISE_DAILY_TOKEN_BUDGET,
            "SUMMARISE_PER_IP_DAILY_TOKENS": S.settings.SUMMARISE_PER_IP_DAILY_TOKENS,
            "SUMMARISE_MAX_SECTIONS": S.settings.SUMMARISE_MAX_SECTIONS,
            "SUMMARISE_MAX_INPUT_TOKENS": S.settings.SUMMARISE_MAX_INPUT_TOKENS,
            "GROQ_MODEL": S.settings.GROQ_MODEL,
        }
        asyncio.run(self._create_schema())

    def tearDown(self) -> None:
        for key, value in self._original.items():
            setattr(S.settings, key, value)
        asyncio.run(self.engine.dispose())
        self._dir.cleanup()

    async def _create_schema(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _new_summary(self, depth: str = "standard") -> Summary:
        async with self.Session() as db:
            summary = Summary(resource_id=1, file_hash="hash-" + depth + str(id(self)),
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

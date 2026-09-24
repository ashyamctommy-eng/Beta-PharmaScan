"""
core/summarise.py — the document -> short-notes pipeline.
----------------------------------------------------------
Design in one paragraph: **map the document cheaply, then deepen only what is
asked for.** Stage 1 sends the extracted *skeleton* (headings + first sentence,
~12% of the tokens on a dense 12-page fixture) and gets back a study map with an
importance score per section. Stage 2 expands the sections that matter into note
bullets with page citations, respecting a per-call token budget. Stage 3 merges
everything into the notes JSON the UI renders. Bullets are assembled
deterministically; the model is only asked for the parts that genuinely need
judgement, which keeps cost and hallucination surface down.

Everything is resumable: each section's result is committed as it completes, so a
run that stops (daily token budget, rate limit, crash) continues where it left off
instead of paying again. Results are cached by file hash forever.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.access import ADMIN_ALLOWANCE_NOTE
from core.storage import StorageError, get_storage
from core.ai import (
    CallOutcome,
    Caller,
    ProviderError,
    GroqCaller,               # re-exported: existing imports and tests use this name
    OpenAICompatibleCaller,
    make_caller,
    parse_json_lenient,
)
from core.config import settings
from core.database import AsyncSessionLocal
from core.extract import (
    CHARS_PER_TOKEN,
    Extraction,
    ExtractionError,
    estimate_tokens,
    extract_document,
)
from models.resource import Resource
from models.summary import Summary, SummarySection, UsageEvent

logger = logging.getLogger(__name__)

DEPTHS = ("brief", "standard", "full")
LEASE_SECONDS = 180      # a tick holds the document for this long, then the lease expires


async def claim_lease(db: AsyncSession, summary_id: int) -> bool:
    """Take the per-document lease for one tick (atomic, expires on its own).

    Two tabs, a double submit, or two students opening the same cached file would
    otherwise run the outline twice, create duplicate section rows and produce
    duplicated notes.
    """
    from sqlalchemy import or_, update

    now = datetime.now(timezone.utc)
    result = await db.execute(
        update(Summary)
        .where(Summary.id == summary_id,
               or_(Summary.lease_until.is_(None), Summary.lease_until < now))
        .values(lease_until=now + timedelta(seconds=LEASE_SECONDS)),
        # synchronize_session=False: let SQLite do the comparison. SQLite hands
        # datetimes back naive, and evaluating the criteria against the in-session
        # object would raise "can't compare offset-naive and offset-aware datetimes".
        execution_options={"synchronize_session": False},
    )
    await db.commit()
    return bool(result.rowcount)

# ── Prompts ───────────────────────────────────────────────────────────────────
OUTLINE_SYSTEM = """You are building a study map for Kenyan D.Pharm (CDACC) students.
You receive a document outline: one line per section, formatted "[id] heading (p.N) - first sentence".
Return ONLY JSON with this shape:
{"title": "document title", "sections": [{"id": "s1", "heading": "...", "page": 1, "gist": "...", "importance": 4}]}
Rules:
- Keep every id exactly as given, in the same order. Never invent or drop sections.
- "gist": at most 12 words describing what that section actually teaches.
- "importance": 1-5, how much it matters for exams and revision notes.
- "title": a short title for the document, not a sentence."""

SECTION_SYSTEM = """You are extracting short notes from ONE section of a D.Pharm study document.
Rules, strictly:
1. Use ONLY the text provided. Never add outside knowledge or invent facts.
2. Every bullet must end with its source page in the form (p.12).
3. Prefer numbers, drug names, mechanisms and comparisons. Drop filler and repetition.
4. If the text is unusable (empty, garbled, or a scanned image with no text), return
   {"usable": false, "reason": "brief reason"} and nothing else.
5. Write every formula, unit or calculation as plain text with Unicode symbols — t½, ×, ÷,
   ≤, ≥, Δ, μ, ₁ ₂ — and never in LaTeX. A backslash inside JSON is either invalid (so the
   whole section is lost) or silently mangled (\\frac arrives as "rac"), and these notes are
   read, copied and printed as plain text.
Return ONLY JSON with this shape:
{"usable": true,
 "bullets": [{"text": "...", "page": 12}],
 "key_terms": ["..."],
 "drug_table": [{"name": "", "class": "", "mechanism": "", "note": ""}],
 "mnemonic": "..." or null,
 "flags": ["..."]}
- bullets: 3 to 6, each at most 25 words, ordered by importance.
- drug_table: only when specific drugs or agents are named, otherwise [].
- mnemonic: only if genuinely useful for recall, otherwise null."""

REDUCE_SYSTEM = """You are finishing a set of revision notes for a D.Pharm student.
You are given the topics that were already extracted from a document.
Return ONLY JSON with this shape:
{"title": "short document title", "remember_5": ["..."], "exam_traps": ["..."]}
Rules:
- "remember_5": the 5 highest-value facts across the whole document, each at most 20 words.
- "exam_traps": 1 to 3 confusions or mistakes students commonly make on this material.
- Introduce no new facts: everything must be supported by the topics given.
- Write formulas and units as plain text with Unicode symbols (t½, ×, ÷, ≤, Δ, μ), never in
  LaTeX — a backslash in JSON is either invalid or silently mangled."""


# ── Small helpers ─────────────────────────────────────────────────────────────
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def as_int(value: Any, default: int) -> int:
    """Coerce a model-provided number without ever raising.

    Model output is untrusted input: `importance` arrives as 4, "4", "4/5" or
    "high", and a citation as 12, "12" or "p.12". `int()` on those raises, which
    used to abort a whole run (and re-charge for it on retry).
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+", value)
        if match:
            return int(match.group())
    return default


def normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


def describe_error(exc: Exception) -> str:
    """A short, honest description of a vendor failure (no secrets, no traceback)."""
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    if body is None and getattr(exc, "response", None) is not None:
        body = getattr(exc.response, "text", None)
    detail = ""
    if isinstance(body, dict):
        error = body.get("error") or {}
        detail = error.get("message") or body.get("message") or json.dumps(body)[:200]
    elif isinstance(body, str):
        detail = body[:200]
    name = type(exc).__name__
    return f"{name}" + (f" (HTTP {status})" if status else "") + (f": {detail}" if detail else f": {exc}")


# ── Cost estimate + planning ──────────────────────────────────────────────────
def _planned_sections(extraction: Extraction, depth: str,
                      importance: Optional[dict[str, int]] = None) -> list:
    """Which sections a given depth will actually expand.

    Ordering is (importance desc, page asc) so that if the daily budget runs out
    mid-run, what got finished is the most exam-relevant material.
    """
    if depth == "brief":
        return []
    sections = list(extraction.sections)
    if depth == "standard" and importance:
        sections = [s for s in sections if importance.get(s.id, 3) >= 3]
    sections = [s for s in sections if s.tokens >= 20]
    sections.sort(key=lambda s: (-(importance or {}).get(s.id, 3), s.page))
    return sections[: settings.SUMMARISE_MAX_SECTIONS]


def estimate_cost_tokens(extraction: Extraction, depth: str) -> int:
    """Upper-bound estimate shown to the student *before* anything is spent."""
    output_per_call = 350
    calls = 1 + (1 if depth != "brief" else 0)          # outline (+ reduce)
    total = estimate_tokens(extraction.skeleton())
    if depth == "brief":
        planned: list = []
    else:
        median = sorted(s.tokens for s in extraction.sections)[len(extraction.sections) // 2] if extraction.sections else 0
        planned = extraction.sections if depth == "full" else [s for s in extraction.sections if s.tokens >= median]
        planned = planned[: settings.SUMMARISE_MAX_SECTIONS]
    for section in planned:
        chunks = max(1, -(-section.tokens // settings.SUMMARISE_MAX_INPUT_TOKENS))
        calls += chunks
        total += section.tokens + chunks * output_per_call
    total += output_per_call * (calls - len(planned))
    return int(total)


# ── Daily budget ──────────────────────────────────────────────────────────────
async def tokens_used_today(db: AsyncSession, client: str | None = None) -> int:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    stmt = select(func.coalesce(func.sum(UsageEvent.total_tokens), 0)).where(UsageEvent.created_at >= since)
    if client:
        stmt = stmt.where(UsageEvent.client == client)
    return int((await db.execute(stmt)).scalar() or 0)


async def record_usage(db: AsyncSession, *, kind: str, model: str, input_tokens: int = 0,
                       output_tokens: int = 0, total_tokens: int = 0,
                       resource_id: int = 0, client: str = "") -> None:
    """Append to the token ledger that the panel reports and the budget enforces."""
    db.add(UsageEvent(kind=kind, model=model, input_tokens=input_tokens,
                      output_tokens=output_tokens, total_tokens=total_tokens,
                      resource_id=resource_id, client=(client or "")[:64]))
    await db.commit()


async def budget_room(db: AsyncSession, client: str, *, admin: bool = False) -> tuple[int, str]:
    """Return (tokens still available to this caller, reason-if-exhausted).

    The answer is the smaller of the app-wide and per-client rooms, so a mid-section
    guard against overspending is meaningful in both cases. `admin=True` means the
    request carried a valid signed admin session (see core.access.guard): the
    per-device allowance is skipped for it, but the app-wide budget is never
    skipped — it is the ceiling that protects the API key.
    """
    global_used = await tokens_used_today(db)
    global_room = settings.SUMMARISE_DAILY_TOKEN_BUDGET - global_used
    if global_room <= 0:
        return 0, app_budget_message(global_used)
    if admin or not client:
        return global_room, ""
    client_used = await tokens_used_today(db, client)
    client_room = settings.SUMMARISE_PER_IP_DAILY_TOKENS - client_used
    if client_room <= 0:
        return 0, device_budget_message(client_used)
    return min(global_room, client_room), ""


def app_budget_message(used: int) -> str:
    """The app-wide refusal, told honestly: rolling 24h, real numbers, no 'tomorrow'.

    Both budgets are rolling windows over the UsageEvent ledger, so the allowance
    does not reset at midnight — it frees up as the oldest calls age out.
    """
    return (
        f"The app-wide daily budget is used up: {used:,} of "
        f"{settings.SUMMARISE_DAILY_TOKEN_BUDGET:,} tokens used in the last 24 hours "
        "(0 left). It is a rolling 24-hour window, so it frees up gradually rather "
        "than at midnight — finished sections are saved, so press Continue later."
    )


def device_budget_message(used: int) -> str:
    """The per-device refusal — names *this* budget, so it is not confused with the
    app-wide one the admin raises."""
    return (
        f"This device's daily allowance is used up: {used:,} of "
        f"{settings.SUMMARISE_PER_IP_DAILY_TOKENS:,} tokens used in the last 24 hours "
        "(0 left). It is a rolling 24-hour window, so it frees up gradually rather "
        "than at midnight — press Continue later to use what has freed up."
    )


# ── The pipeline ──────────────────────────────────────────────────────────────
@dataclass
class TickResult:
    status: str
    progress: float
    sections_done: int
    sections_total: int
    tokens_spent: int
    sections_failed: int = 0
    notes: Optional[dict] = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    calls_made: int = 0


async def run_tick(db: AsyncSession, summary: Summary, extraction: Extraction, *,
                   client_id: str = "", caller: Optional[Caller] = None,
                   max_calls: Optional[int] = None, admin: bool = False) -> TickResult:
    """Do a bounded amount of work (a few model calls) and return the new state.

    Bounded on purpose: on shared hosting a request must not hold a worker for
    minutes, so the UI calls this repeatedly and shows progress between ticks.

    `admin` must be True only for a valid signed admin session (core.access.guard);
    it skips the per-device allowance, never the app-wide budget.
    """
    caller = caller or make_caller(settings.GROQ_API_KEY, settings.GROQ_BASE_URL)
    budget_calls = max_calls or settings.SUMMARISE_CALLS_PER_REQUEST

    if not await claim_lease(db, summary.id):
        return TickResult(
            status="busy", progress=0.0,
            sections_done=summary.sections_done, sections_total=summary.sections_total,
            tokens_spent=summary.tokens_spent, sections_failed=summary.sections_failed, calls_made=0,
            message="Another run for this document is in progress — waiting for it.",
        )
    await db.refresh(summary)
    map_model = settings.GROQ_MAP_MODEL or settings.GROQ_MODEL
    reduce_model = settings.GROQ_SUMMARY_MODEL or settings.GROQ_MODEL
    warnings: list[str] = []
    # Say in the response that the per-device allowance did not apply. It goes in the
    # warnings (present for every status) and, when the tick has nothing else to say,
    # in the message the UI renders next to Continue.
    admin_note = ADMIN_ALLOWANCE_NOTE if (admin and client_id) else ""
    if admin_note:
        warnings.append(admin_note)

    async def finish(status: str, message: str = "", notes: Optional[dict] = None) -> TickResult:
        summary.status = status
        summary.updated_at = datetime.now(timezone.utc)
        summary.lease_until = None                 # release for the next tick
        await db.commit()
        total = summary.sections_total or 0
        return TickResult(
            status=status,
            progress=1.0 if status == "done" else (summary.sections_done / total if total else 0.0),
            sections_done=summary.sections_done, sections_total=total,
            tokens_spent=summary.tokens_spent, sections_failed=summary.sections_failed,
            notes=notes or (_load_notes(summary) if status == "done" else None),
            message=message or admin_note, warnings=warnings,
        )

    remaining, blocked = await budget_room(db, client_id, admin=admin)
    if blocked:
        return await finish("budget_exhausted", blocked)

    calls = 0

    # ── Stage 1: the study map ────────────────────────────────────────────────
    if not summary.outline_json:
        try:
            outcome = await _call(db, caller, summary, client_id, kind="outline",
                                  model=map_model, system=OUTLINE_SYSTEM,
                                  user=extraction.skeleton(), max_tokens=settings.GROQ_SUMMARY_MAX_TOKENS,
                                  temperature=0.2)
        except RateLimited as exc:
            # Nothing has been spent yet: park the job so the student can retry.
            return await finish("rate_limited", str(exc))
        except InvalidApiKey as exc:
            return await finish("failed", str(exc))
        calls += 1
        if outcome.warning:
            warnings.append(outcome.warning)
        payload = outcome.payload or {}
        importance = {s.get("id"): as_int(s.get("importance"), 3)
                      for s in payload.get("sections", []) if isinstance(s, dict)}
        summary.outline_json = json.dumps(payload)
        summary.model = outcome.model

        # Plan the work now that importance is known, and persist it so that a
        # resumed run continues the same plan.
        planned = _planned_sections(extraction, summary.depth or "standard", importance)
        for section in planned:
            db.add(SummarySection(
                summary_id=summary.id, section_id=section.id, heading=section.heading,
                page=section.page, end_page=section.end_page,
                importance=importance.get(section.id, 3), tokens=section.tokens,
                status="pending",
            ))
        summary.sections_total = len(planned)
        summary.sections_done = 0
        summary.pages = extraction.pages
        await db.commit()
        if not planned:
            warnings.append("Depth is 'brief': the notes are built from the study map only.")

    # ── Stage 2: expand sections ──────────────────────────────────────────────
    concurrency = max(1, int(settings.SUMMARISE_CONCURRENCY or 1))

    if concurrency <= 1:
        # SUMMARISE_CONCURRENCY=1 keeps the original strictly-sequential path: one
        # section at a time, in the shared session, in importance order. Kept verbatim
        # so that setting it to 1 is exactly the old behaviour (and the old load).
        while calls < budget_calls:
            pending = (await db.execute(
                select(SummarySection)
                .where(SummarySection.summary_id == summary.id, SummarySection.status == "pending")
                .order_by(SummarySection.importance.desc(), SummarySection.page.asc())
                .limit(1)
            )).scalars().first()
            if pending is None:
                break
            remaining, blocked = await budget_room(db, client_id, admin=admin)
            if blocked:
                return await finish("budget_exhausted", blocked)

            source = next((s for s in extraction.sections if s.id == pending.section_id), None)
            if source is None:
                pending.status = "skipped"
                await db.commit()
                continue

            try:
                payload, spent, used_calls, notes_warnings = await _expand_section(
                    db, caller, summary, client_id, source, map_model, remaining, pending
                )
                warnings.extend(notes_warnings)
                pending.payload_json = json.dumps(payload)
                pending.status = "done"
                pending.tokens_spent = spent
                summary.sections_done += 1
                calls += used_calls          # tokens are recorded by _call(), once
                await db.commit()
            except RateLimited as exc:
                logger.warning("Groq rate limit while expanding %s: %s", source.id, exc)
                return await finish("rate_limited", str(exc))
            except InvalidApiKey as exc:
                return await finish("failed", str(exc))
            except Exception as exc:  # noqa: BLE001 - one bad section must not kill the run
                message = describe_error(exc)
                logger.error("Section %s failed: %s", source.id, message)
                pending.status = "error"
                pending.payload_json = json.dumps({"error": message})
                summary.sections_done += 1
                summary.sections_failed += 1
                warnings.append(f"Section '{source.heading}' could not be summarised: {message}")
                await db.commit()
    else:
        # Parallel path: claim a small batch of pending sections, expand them
        # concurrently, then apply their results here and commit once. Every task runs
        # in its own session — see _expand_one_section for why that is not optional.
        while calls < budget_calls:
            batch_size = min(concurrency, budget_calls - calls)
            claimed = list((await db.execute(
                select(SummarySection.id)
                .where(SummarySection.summary_id == summary.id, SummarySection.status == "pending")
                .order_by(SummarySection.importance.desc(), SummarySection.page.asc())
                .limit(batch_size)
            )).scalars().all())
            if not claimed:
                break
            remaining, blocked = await budget_room(db, client_id, admin=admin)
            if blocked:
                return await finish("budget_exhausted", blocked)

            batch = await _expand_batch(caller, summary.resource_id, extraction,
                                        claimed, map_model, client_id, remaining, concurrency)
            for outcome in batch.outcomes:
                warnings.extend(outcome.warnings)
                calls += outcome.used_calls     # a failed section counts 0, as before
                summary.sections_done += outcome.sections_done
                summary.sections_failed += outcome.sections_failed
            # Tokens are added by this session only, from the per-task ledgers that
            # survived even the cancelled tasks (see _LedgerSummary): two tasks writing
            # `summary.tokens_spent` from their own copies would lose one increment.
            summary.tokens_spent += batch.tokens
            await db.commit()

            if batch.stop == "rate_limited":
                return await finish("rate_limited", batch.message)
            if batch.stop:
                return await finish("failed", batch.message)

    # ── Stage 3: synthesise the notes ─────────────────────────────────────────
    outstanding = (await db.execute(
        select(func.count()).select_from(SummarySection).where(
            SummarySection.summary_id == summary.id, SummarySection.status == "pending")
    )).scalar() or 0
    if outstanding:
        return await finish("running", f"{outstanding} section(s) left — press Continue.")

    if not summary.notes_json:
        if calls >= budget_calls:
            return await finish("running", "Ready to assemble the notes — press Continue.")
        try:
            notes = await _assemble(db, caller, summary, extraction, client_id, reduce_model, warnings)
            calls += 1
        except RateLimited as exc:
            return await finish("rate_limited", str(exc))
        except Exception as exc:  # noqa: BLE001
            message = describe_error(exc)
            logger.error("Reduce stage failed: %s", message)
            notes = _assemble_offline(summary, extraction, warnings)
            warnings.append(f"Synthesis call failed ({message}); notes were assembled without it.")
        summary.notes_json = json.dumps(notes)
        summary.tokens_spent += 0        # the reduce call was recorded by _call()
        summary.warnings_json = json.dumps(warnings)
        summary.sections_done = summary.sections_total
        return await finish("done", "", notes)

    summary.sections_done = summary.sections_total
    return await finish("done")


class RateLimited(Exception):
    """Raised when Groq asks us to back off; the caller parks the job."""


class InvalidApiKey(Exception):
    """The key itself was rejected — retrying will not help, so say so plainly."""


# ── Call plumbing ─────────────────────────────────────────────────────────────
async def _call(db: AsyncSession, caller: Caller, summary: Summary, client_id: str, *,
                kind: str, model: str, system: str, user: str,
                max_tokens: int, temperature: float,
                json_mode: bool = True) -> CallOutcome:
    """One model call, with usage recorded and 429s turned into RateLimited."""
    try:
        outcome = await caller.call(kind=kind, model=model, system=system, user=user,
                                    max_tokens=max_tokens, temperature=temperature,
                                    json_mode=json_mode)
    except Exception as exc:
        status = getattr(exc, "status_code", None)
        if status in (401, 403):
            raise InvalidApiKey(
                "Groq rejected the server's API key (HTTP "
                f"{status}). Check GROQ_API_KEY in .env: keys are revoked when "
                "regenerated, and a copy-paste with trailing whitespace also fails."
            ) from exc
        if status == 429:
            retry_after = None
            response = getattr(exc, "response", None)
            if response is not None:
                retry_after = response.headers.get("retry-after") if hasattr(response, "headers") else None
            wait = f" Retry after {retry_after}s." if retry_after else ""
            raise RateLimited(
                "Groq is rate-limiting this key (free tier allows about 8,000 tokens per "
                f"minute). Progress is saved; press Continue in a minute.{wait}"
            ) from exc
        raise
    db.add(UsageEvent(kind=kind, model=outcome.model, input_tokens=outcome.input_tokens,
                      output_tokens=outcome.output_tokens, total_tokens=outcome.total_tokens,
                      resource_id=summary.resource_id, client=client_id[:64]))
    await db.commit()
    summary.tokens_spent += outcome.total_tokens
    return outcome


async def _expand_section(db: AsyncSession, caller: Caller, summary: Summary, client_id: str,
                          section, model: str, remaining: int,
                          row: Optional[SummarySection] = None) -> tuple[dict, int, int, list[str]]:
    """Summarise one section, chunking it if it exceeds the per-call input budget.

    Each finished chunk is written to the row immediately. A section is often
    several calls long, and the per-minute token limit makes a mid-section 429
    likely; without this, the resume would re-run (and re-pay for) the chunks that
    already succeeded.
    """
    warnings: list[str] = []
    # SUMMARISE_MAX_INPUT_TOKENS is tokens; _split_text counts characters. Convert
    # (chars/4) and leave room for the prompt and the reply, or callers get ~4x more
    # calls than the estimate quoted to the student.
    budget_chars = settings.SUMMARISE_MAX_INPUT_TOKENS * CHARS_PER_TOKEN
    chunk_chars = max(600, budget_chars - 1200)
    text = section.text
    chunks = _split_text(text, chunk_chars)
    payloads: list[dict] = []
    spent = 0
    calls = 0
    done_chunks = 0
    if row is not None:
        state = parse_json_lenient(row.payload_json or "") or {}
        if state.get("_partial") and isinstance(state.get("_partials"), list):
            payloads = [p for p in state["_partials"] if isinstance(p, dict)]
            done_chunks = as_int(state.get("_chunks_done"), 0)
            spent = as_int(state.get("_spent"), 0)

    for index, chunk in enumerate(chunks, start=1):
        if index <= done_chunks:
            continue                                   # already paid for, and saved
        if remaining is not None and spent >= remaining:
            # `remaining` is the smaller of the two rooms, so which budget ran out is
            # not knowable here — name the window, not the budget, rather than guess.
            raise RateLimited(
                "The run's token budget ran out mid-section (the app-wide daily budget and "
                "this device's allowance are rolling 24-hour windows, so they free up "
                "gradually, not at midnight); progress is saved — press Continue later."
            )
        pages_line = f"Pages: p.{section.page}-{section.end_page}\n" if section.page else ""
        header = (f"Section: {section.heading}\n{pages_line}"
                  f"Part {index} of {len(chunks)}\n\n--- BEGIN SECTION TEXT ---\n")
        outcome = await _call(db, caller, summary, client_id, kind="section", model=model,
                              system=section_system(bool(section.page)),
                              user=header + chunk + "\n--- END SECTION TEXT ---",
                              max_tokens=settings.GROQ_SUMMARY_MAX_TOKENS, temperature=0.25)
        calls += 1
        spent += outcome.total_tokens
        if outcome.warning:
            warnings.append(outcome.warning)
        if outcome.payload is None:
            warnings.append(f"Section '{section.heading}': the model's reply was not JSON; skipped.")
            continue
        payloads.append(outcome.payload)
        if row is not None and len(chunks) > 1:
            row.payload_json = json.dumps({
                "_partial": True, "_chunks_done": index, "_partials": payloads, "_spent": spent,
            })
            await db.commit()

    if not payloads:
        return {"usable": False, "reason": "no usable model output"}, spent, calls, warnings
    return _merge_payloads(payloads, section), spent, calls, warnings


# ── Stage-2 parallelism ───────────────────────────────────────────────────────
class _LedgerSummary:
    """Stand-in for the `Summary` row inside one parallel section task.

    `_call()` records usage and does `summary.tokens_spent += tokens` on whatever object
    it is handed. Two tasks doing that read-modify-write on the *same* ORM row would each
    write back a total that misses the other's tokens (lost update), so each task counts
    its own spend here and the tick's session adds the ledgers once, after the batch.
    """

    def __init__(self, resource_id: int) -> None:
        self.resource_id = resource_id
        self.tokens_spent = 0


@dataclass
class _SectionOutcome:
    section_id: str                   # the extraction id, for messages
    status: str                       # done | error | skipped
    used_calls: int = 0
    sections_done: int = 0
    sections_failed: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class _BatchResult:
    outcomes: list[_SectionOutcome] = field(default_factory=list)
    tokens: int = 0                   # tokens spent by the whole batch (incl. cancelled tasks)
    stop: str = ""                    # "" | "rate_limited" | "failed"
    message: str = ""


async def _expand_one_section(caller: Caller, extraction: Extraction, row_id: int,
                              model: str, client_id: str, remaining: int,
                              ledger: _LedgerSummary) -> _SectionOutcome:
    """Expand ONE section, in its OWN session, with no lease handling.

    The session is opened here and closed in every path: a SQLAlchemy AsyncSession (and
    the connection under it) is not safe to use from two tasks at once, so the tick's
    session must never be touched from inside a batch task. `ledger` is created by the
    batch driver, so the tokens this task spends are visible even if it is cancelled.
    """
    async with AsyncSessionLocal() as task_db:
        row = await task_db.get(SummarySection, row_id)
        if row is None or row.status != "pending":
            return _SectionOutcome(section_id=str(row_id), status="skipped")
        source = next((s for s in extraction.sections if s.id == row.section_id), None)
        if source is None:
            row.status = "skipped"
            await task_db.commit()
            return _SectionOutcome(section_id=row.section_id, status="skipped")

        try:
            payload, spent, used_calls, section_warnings = await _expand_section(
                task_db, caller, ledger, client_id, source, model, remaining, row
            )
        except (RateLimited, InvalidApiKey):
            # Leave the row pending: _expand_section already committed the chunks that
            # succeeded, so the next tick resumes without re-paying for them.
            raise
        except Exception as exc:  # noqa: BLE001 - one bad section must not kill the run
            message = describe_error(exc)
            logger.error("Section %s failed: %s", source.id, message)
            row.status = "error"
            row.payload_json = json.dumps({"error": message})
            await task_db.commit()
            # Same accounting as the sequential path: an errored section counts as done
            # *and* failed, and its calls are not charged against the tick's budget.
            return _SectionOutcome(
                section_id=source.id, status="error", sections_done=1, sections_failed=1,
                warnings=[f"Section '{source.heading}' could not be summarised: {message}"],
            )

        row.payload_json = json.dumps(payload)
        row.status = "done"
        row.tokens_spent = spent
        await task_db.commit()
        return _SectionOutcome(section_id=source.id, status="done", used_calls=used_calls,
                               sections_done=1, warnings=section_warnings)


async def _expand_batch(caller: Caller, resource_id: int,
                        extraction: Extraction, row_ids: list[int], model: str,
                        client_id: str, remaining: int, concurrency: int) -> _BatchResult:
    """Expand up to `len(row_ids)` sections concurrently and collect their outcomes.

    Bounded two ways: the caller passes no more ids than the tick may still spend calls,
    and the semaphore caps how many model calls are in flight at once. A rate limit or a
    rejected key stops the batch (the rest are cancelled, committed work is kept); any
    other failure stays inside its own section.
    """
    semaphore = asyncio.Semaphore(concurrency)
    ledgers = {row_id: _LedgerSummary(resource_id) for row_id in row_ids}

    async def expand(row_id: int) -> _SectionOutcome:
        async with semaphore:
            return await _expand_one_section(caller, extraction, row_id, model, client_id,
                                             remaining, ledgers[row_id])

    tasks = {asyncio.create_task(expand(row_id)) for row_id in row_ids}
    outcomes: list[_SectionOutcome] = []
    stop = ""
    message = ""
    while tasks:
        done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            try:
                outcomes.append(task.result())
            except RateLimited as exc:
                logger.warning("Groq rate limit while expanding a batch: %s", exc)
                stop, message = "rate_limited", str(exc)
            except InvalidApiKey as exc:
                stop, message = "failed", str(exc)
            except Exception as exc:  # noqa: BLE001 - defensive: never kill the whole tick
                logger.error("Section expansion crashed: %s", describe_error(exc))
                stop, message = "failed", describe_error(exc)
        if stop:
            for task in tasks:
                task.cancel()
            if tasks:
                # Wait for the cancelled tasks to unwind their sessions before the
                # tick's session commits again.
                await asyncio.gather(*tasks, return_exceptions=True)
            tasks = set()
    logger.debug("Section batch finished: %s",
                 ", ".join(f"{outcome.section_id}:{outcome.status}" for outcome in outcomes) or "none")
    return _BatchResult(outcomes=outcomes,
                        tokens=sum(ledger.tokens_spent for ledger in ledgers.values()),
                        stop=stop, message=message)


def _split_text(text: str, chunk_chars: int) -> list[str]:
    """Split on line boundaries, never losing text, with no overlap (bullets are
    deduplicated later, so overlap would only cost tokens)."""
    chunk_chars = max(400, chunk_chars)
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) > chunk_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks or [text]


def _merge_payloads(payloads: list[dict], section) -> dict:
    """Deterministic merge of chunk results — no extra model call, no drift."""
    bullets: list[dict] = []
    seen: set[str] = set()
    terms: list[str] = []
    drugs: list[dict] = []
    drug_keys: set[tuple] = set()
    mnemonics: list[str] = []
    flags: list[str] = []
    usable = False

    for payload in payloads:
        if payload.get("usable") is False:
            flags.extend(str(f) for f in payload.get("flags", []) if f)
            continue
        usable = True
        for bullet in payload.get("bullets", []):
            if isinstance(bullet, dict):
                text = str(bullet.get("text") or "").strip()
                page = as_int(bullet.get("page"), section.page or 0)
            else:
                text, page = str(bullet).strip(), section.page or 0
            if not text:
                continue
            key = normalize_text(text)[:120]
            if key in seen:
                continue
            seen.add(key)
            bullets.append({"text": text, "page": _clamp_page(page, section)})
        for term in payload.get("key_terms", []) or []:
            term = str(term).strip()
            if term and term.lower() not in {t.lower() for t in terms}:
                terms.append(term)
        for row in payload.get("drug_table", []) or []:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            key = (name.lower(), str(row.get("class") or "").lower())
            if key in drug_keys:
                continue
            drug_keys.add(key)
            drugs.append({
                "name": name,
                "class": str(row.get("class") or "").strip(),
                "mechanism": str(row.get("mechanism") or "").strip(),
                "note": str(row.get("note") or "").strip(),
            })
        if payload.get("mnemonic"):
            mnemonics.append(str(payload["mnemonic"]).strip())

    return {
        "usable": usable,
        "bullets": bullets[:24],
        "key_terms": terms[:24],
        "drug_table": drugs[:24],
        "mnemonic": mnemonics[0] if mnemonics else None,
        "flags": flags[:8],
    }


def _clamp_page(page: int, section) -> int:
    """Keep citations inside the section's real page range (0 = no pages to cite)."""
    if not section.page:
        return 0                      # DOCX/TXT: no page numbers exist, so cite none
    low = section.page
    high = max(section.end_page or low, low)
    return min(max(page, low), high)


def section_system(has_pages: bool) -> str:
    """The section prompt, with the citation rule matching the format."""
    if has_pages:
        return SECTION_SYSTEM
    return SECTION_SYSTEM.replace(
        "2. Every bullet must end with its source page in the form (p.12).",
        "2. This format has no page numbers: do not add any citation.",
    ).replace('{"text": "...", "page": 12}', '{"text": "..."}')


# ── Final assembly ────────────────────────────────────────────────────────────
async def _assemble(db: AsyncSession, caller: Caller, summary: Summary, extraction: Extraction,
                    client_id: str, model: str, warnings: list[str]) -> dict:
    rows = (await db.execute(
        select(SummarySection).where(SummarySection.summary_id == summary.id)
        .order_by(SummarySection.importance.desc(), SummarySection.page.asc())
    )).scalars().all()

    topics: list[dict] = []
    for row in rows:
        payload = parse_json_lenient(row.payload_json or "") or {}
        if payload.get("_partial"):
            warnings.append(f"Section '{row.heading}' was only partly processed; press Continue.")
            continue
        if payload.get("error"):
            warnings.append(f"Section '{row.heading}' failed: {payload['error']}")
            continue
        if payload.get("usable") is False:
            continue
        topics.append({
            "heading": row.heading, "page": row.page, "end_page": row.end_page,
            "importance": row.importance,
            "bullets": payload.get("bullets", []),
            "key_terms": payload.get("key_terms", []),
            "drug_table": payload.get("drug_table", []),
            "mnemonic": payload.get("mnemonic"),
            "flags": payload.get("flags", []),
        })

    digest = _digest_for_reduce(topics)
    reduce_tokens = 0
    synthesis: dict = {}
    try:
        outcome = await _call(db, caller, summary, client_id, kind="reduce", model=model,
                              system=REDUCE_SYSTEM, user=digest,
                              max_tokens=settings.GROQ_SUMMARY_MAX_TOKENS, temperature=0.3)
        reduce_tokens = outcome.total_tokens
        if outcome.warning:
            warnings.append(outcome.warning)
        synthesis = outcome.payload or {}
    except RateLimited:
        raise
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"Synthesis call failed ({describe_error(exc)}); used the best bullets instead.")

    remember_5 = [str(item).strip() for item in synthesis.get("remember_5", []) if str(item).strip()][:5]
    if not remember_5:
        remember_5 = [t["bullets"][0]["text"] for t in topics if t["bullets"]][:5]
    exam_traps = [str(item).strip() for item in synthesis.get("exam_traps", []) if str(item).strip()][:3]

    outline = parse_json_lenient(summary.outline_json or "") or {}
    notes = {
        "title": str(synthesis.get("title") or outline.get("title") or summary.file_name),
        "resource_id": summary.resource_id,
        "file_name": summary.file_name,
        "file_hash": summary.file_hash,
        "depth": summary.depth,
        "model": summary.model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages": extraction.pages,
        "coverage": {
            "pages_with_text": extraction.pages if not extraction.scanned else 0,
            "chars_per_page": extraction.chars_per_page,
            "scanned": extraction.scanned,
            "sections_summarised": len(topics),
            "duplicate_lines_collapsed": extraction.duplicate_lines_collapsed,
        },
        "topics": topics,
        "remember_5": remember_5,
        "exam_traps": exam_traps,
        "warnings": warnings + extraction.warnings,
        "_reduce_tokens": reduce_tokens,
    }
    return notes


def _digest_for_reduce(topics: list[dict], max_bullets: int = 6, max_chars: int = 140) -> str:
    """Compact digest of the topics, trimmed to fit the per-call input budget."""
    budget_chars = settings.SUMMARISE_MAX_INPUT_TOKENS * 4 - 1200
    for bullets_per_topic in (max_bullets, 4, 3, 2, 1):
        lines: list[str] = []
        for topic in topics:
            lines.append(f"[{topic['heading']}] (p.{topic['page']}, importance {topic['importance']})")
            for bullet in topic["bullets"][:bullets_per_topic]:
                lines.append(f"- {str(bullet.get('text', ''))[:max_chars]} (p.{bullet.get('page')})")
            if topic.get("drug_table"):
                names = ", ".join(row.get("name", "") for row in topic["drug_table"][:8] if row.get("name"))
                if names:
                    lines.append(f"- drugs: {names}")
        digest = "\n".join(lines)
        if len(digest) <= budget_chars or bullets_per_topic == 1:
            return digest
    return digest  # pragma: no cover


def _assemble_offline(summary: Summary, extraction: Extraction, warnings: list[str]) -> dict:
    """Fallback notes when the synthesis call cannot run — never lose the work."""
    return {
        "title": summary.file_name,
        "resource_id": summary.resource_id,
        "file_name": summary.file_name,
        "file_hash": summary.file_hash,
        "depth": summary.depth,
        "model": summary.model,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages": extraction.pages,
        "coverage": {"scanned": extraction.scanned, "chars_per_page": extraction.chars_per_page,
                     "sections_summarised": summary.sections_done},
        "topics": [],
        "remember_5": [],
        "exam_traps": [],
        "warnings": warnings + extraction.warnings,
    }


def _load_notes(summary: Summary) -> Optional[dict]:
    if not summary.notes_json:
        return None
    notes = parse_json_lenient(summary.notes_json)
    if notes is None:
        try:
            notes = json.loads(summary.notes_json)
        except json.JSONDecodeError:
            return None
    notes.pop("_reduce_tokens", None)
    return notes


# ── Public helpers used by the API layer ──────────────────────────────────────
_CACHE: "OrderedDict[str, tuple[Extraction, str]]" = OrderedDict()
_CACHE_MAX = 8


def _remember(key: str, value: tuple[Extraction, str]) -> None:
    _CACHE[key] = value
    _CACHE.move_to_end(key)
    while len(_CACHE) > _CACHE_MAX:
        _CACHE.popitem(last=False)


def reset_extraction_cache() -> None:
    """Forget cached parses (tests, and anything that swaps storage backends)."""
    _CACHE.clear()


async def source_identity(db: AsyncSession, resource: Resource) -> tuple[Extraction, str]:
    """Parse *and* hash a document once per (backend, resource, fingerprint).

    Without the cache, every preview and every tick re-parses and re-hashes the whole
    upload: the preview endpoint is free and needs no key, and a run fires many ticks,
    so a 50 MB PDF becomes a cheap way to keep a shared worker busy. The fingerprint
    (mtime+size on disk, sha256 in the database) makes the cache self-invalidating.

    Extraction always happens against a **file path**: on disk that is the upload
    itself, and for the database backend the bytes are materialised to a temporary
    file, so there is exactly one extraction code path to trust.
    """
    storage = get_storage()
    try:
        fingerprint = await storage.fingerprint(db, resource)
    except StorageError as exc:
        raise ExtractionError(str(exc)) from exc

    key = f"{storage.name}:{resource.id}:{fingerprint}"
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    local = await storage.local_path(db, resource)
    if local is not None:
        extraction = extract_document(local)
        digest = file_sha256(local)
    else:
        try:
            data = await storage.load_bytes(db, resource)
        except StorageError as exc:
            raise ExtractionError(str(exc)) from exc
        suffix = Path(resource.file_name).suffix
        with tempfile.NamedTemporaryFile(prefix="pharmascan-", suffix=suffix, delete=True) as handle:
            handle.write(data)
            handle.flush()
            extraction = extract_document(Path(handle.name))
        digest = hashlib.sha256(data).hexdigest()

    _remember(key, (extraction, digest))
    return extraction, digest


async def extract_resource(db: AsyncSession, resource: Resource) -> Extraction:
    return (await source_identity(db, resource))[0]


async def get_summary(db: AsyncSession, resource: Resource) -> Optional[Summary]:
    """The summary for this document, resolved by **content** first.

    Summaries are cached by file hash, so the same PDF uploaded twice (or re-uploaded
    under a new id) must resolve to the same notes. Looking up by `resource_id` alone
    returned nothing for the second copy — the vault card then reported "no summary"
    for a document that was already summarised.
    """
    try:
        digest = (await source_identity(db, resource))[1]
    except ExtractionError:
        digest = ""
    if digest:
        row = (await db.execute(
            select(Summary).where(Summary.file_hash == digest))).scalars().first()
        if row is not None:
            return row
    return (await db.execute(
        select(Summary).where(Summary.resource_id == resource.id).order_by(Summary.id.desc())
    )).scalars().first()


async def get_or_create_summary(db: AsyncSession, resource: Resource, *, depth: str) -> Summary:
    digest = (await source_identity(db, resource))[1]
    summary = (await db.execute(select(Summary).where(Summary.file_hash == digest))).scalars().first()
    if summary is None:
        summary = Summary(resource_id=resource.id, file_hash=digest, file_name=resource.file_name,
                          depth=depth if depth in DEPTHS else "standard", status="pending")
        db.add(summary)
        try:
            await db.commit()
        except IntegrityError:
            # Another request created the same row first (same file, two clicks).
            await db.rollback()
            summary = (await db.execute(
                select(Summary).where(Summary.file_hash == digest))).scalars().first()
            if summary is None:                       # pragma: no cover - defensive
                raise
        await db.refresh(summary)
    else:
        # Same content as an existing summary — it *is* this document's summary.
        # The two adjustments below are independent: an identical file re-uploaded
        # under a new resource id can also be asked for at a different depth, and
        # chaining them as elif meant the rebuild was skipped and the previous
        # depth's notes were served from cache.
        changed = False
        if summary.resource_id != resource.id:
            summary.resource_id = resource.id       # keep per-resource lookups true
            changed = True
        if summary.depth != depth and summary.status not in ("running", "pending"):
            # The plan (which sections to expand) depends on the depth, so it has to be
            # rebuilt: clearing outline_json is what forces the planning stage to run
            # again. Without it the pipeline would find no pending sections and
            # assemble *empty* notes.
            summary.depth = depth
            summary.status = "pending"
            summary.notes_json = ""
            summary.outline_json = ""
            summary.warnings_json = "[]"
            summary.error = ""
            summary.sections_done = 0
            summary.sections_total = 0
            summary.sections_failed = 0
            summary.lease_until = None
            for row in (await db.execute(
                    select(SummarySection).where(SummarySection.summary_id == summary.id))).scalars().all():
                await db.delete(row)
            changed = True
        if changed:
            await db.commit()
    return summary


async def sections_state(db: AsyncSession, summary: Summary) -> list[dict]:
    rows = (await db.execute(
        select(SummarySection).where(SummarySection.summary_id == summary.id)
        .order_by(SummarySection.importance.desc(), SummarySection.page.asc())
    )).scalars().all()
    return [{
        "id": row.section_id, "heading": row.heading, "page": row.page,
        "importance": row.importance, "status": row.status, "tokens": row.tokens,
    } for row in rows]

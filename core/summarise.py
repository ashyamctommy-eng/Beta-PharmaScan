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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.extract import Extraction, ExtractionError, estimate_tokens, extract_document
from models.resource import Resource
from models.summary import Summary, SummarySection, UsageEvent

logger = logging.getLogger(__name__)

DEPTHS = ("brief", "standard", "full")

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
- Introduce no new facts: everything must be supported by the topics given."""

JSON_REMINDER = "\n\nReturn ONLY valid JSON. No prose, no markdown fences."


# ── Small helpers ─────────────────────────────────────────────────────────────
def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_json_lenient(raw: str) -> Optional[dict]:
    """Models sometimes wrap JSON in fences or add a sentence. Recover what we can."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start, depth = None, 0
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    value = json.loads(text[start:index + 1])
                    if isinstance(value, dict):
                        return value
                except json.JSONDecodeError:
                    start = None
    return None


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


class CallOutcome:
    """What one model call produced."""

    def __init__(self, text: str, model: str, input_tokens: int, output_tokens: int,
                 total_tokens: int, payload: Optional[dict] = None, warning: str = "") -> None:
        self.text = text
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens
        self.payload = payload
        self.warning = warning


class Caller(Protocol):
    """Interface the pipeline needs; tests inject a stub implementation."""

    async def call(self, *, kind: str, model: str, system: str, user: str,
                   max_tokens: int, temperature: float) -> CallOutcome: ...


# ── Live Groq caller ──────────────────────────────────────────────────────────
class GroqCaller:
    """Thin wrapper over groq.AsyncGroq with defensive behaviour.

    * JSON mode is attempted, and silently dropped if the model rejects it.
    * A reasoning model that returns no `content` is reported with the actual
      cause (its thinking spent the token budget), not as a mysterious failure.
    * Malformed JSON is retried once with an explicit reminder.
    """

    def __init__(self, api_key: str, timeout: float = 90.0) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self._client = None
        self._json_mode_ok = True

    @property
    def client(self):
        if self._client is None:
            from groq import AsyncGroq

            # max_retries=1: the SDK retries 429/5xx internally and honours the vendor's
            # retry-after, which on a shared host can block a worker for minutes. One
            # retry absorbs a brief hiccup; anything longer is parked by the tick model
            # ("press Continue"), which is friendlier than a hanging request.
            kwargs: dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout,
                                      "max_retries": 1}
            if settings.GROQ_BASE_URL:
                kwargs["base_url"] = settings.GROQ_BASE_URL
            self._client = AsyncGroq(**kwargs)
        return self._client

    async def call(self, *, kind: str, model: str, system: str, user: str,
                   max_tokens: int, temperature: float) -> CallOutcome:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        warning = ""
        kwargs: dict[str, Any] = {"model": model, "messages": messages,
                                  "max_tokens": max_tokens, "temperature": temperature}
        if self._json_mode_ok:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            completion = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            if self._json_mode_ok and getattr(exc, "status_code", None) == 400:
                # Model does not support response_format — retry without it.
                self._json_mode_ok = False
                warning = "model rejected JSON mode; continuing without it"
                kwargs.pop("response_format", None)
                completion = await self.client.chat.completions.create(**kwargs)
            else:
                raise

        message = completion.choices[0].message if completion.choices else None
        text = (getattr(message, "content", "") or "").strip()
        reasoning = (getattr(message, "reasoning", "") or "").strip()

        if not text and reasoning:
            raise RuntimeError(
                "The model returned reasoning but no answer: its thinking used up the whole "
                f"token budget. Raise GROQ_SUMMARY_MAX_TOKENS (currently {max_tokens})."
            )
        usage = completion.usage
        outcome = CallOutcome(
            text=text,
            model=getattr(completion, "model", model),
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
            warning=warning,
        )
        outcome.payload = parse_json_lenient(text)
        if outcome.payload is None:
            # One strict retry, then give up on this call.
            reminder = user + JSON_REMINDER
            retry = await self.call(kind=kind, model=model, system=system, user=reminder,
                                    max_tokens=max_tokens, temperature=max(0.0, temperature - 0.1))
            retry.total_tokens += outcome.total_tokens
            retry.input_tokens += outcome.input_tokens
            retry.output_tokens += outcome.output_tokens
            if retry.payload is not None:
                retry.warning = (retry.warning + "; " if retry.warning else "") + \
                                "retried once after malformed JSON"
            return retry
        return outcome


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


async def budget_room(db: AsyncSession, client: str) -> tuple[int, str]:
    """Return (remaining tokens, reason-if-exhausted)."""
    global_used = await tokens_used_today(db)
    if global_used >= settings.SUMMARISE_DAILY_TOKEN_BUDGET:
        return 0, (f"Today's summary budget is used up ({global_used:,} of "
                   f"{settings.SUMMARISE_DAILY_TOKEN_BUDGET:,} tokens). "
                   "Finished sections are saved — press Continue tomorrow, or raise "
                   "SUMMARISE_DAILY_TOKEN_BUDGET.")
    if client:
        client_used = await tokens_used_today(db, client)
        if client_used >= settings.SUMMARISE_PER_IP_DAILY_TOKENS:
            return 0, (f"This device has used its daily share ({client_used:,} of "
                       f"{settings.SUMMARISE_PER_IP_DAILY_TOKENS:,} tokens). "
                       "Try again tomorrow.")
        return max(0, min(settings.SUMMARISE_DAILY_TOKEN_BUDGET, settings.SUMMARISE_PER_IP_DAILY_TOKENS) - client_used), ""
    return settings.SUMMARISE_DAILY_TOKEN_BUDGET - global_used, ""


# ── The pipeline ──────────────────────────────────────────────────────────────
@dataclass
class TickResult:
    status: str
    progress: float
    sections_done: int
    sections_total: int
    tokens_spent: int
    notes: Optional[dict] = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    calls_made: int = 0


async def run_tick(db: AsyncSession, summary: Summary, extraction: Extraction, *,
                   client_id: str = "", caller: Optional[Caller] = None,
                   max_calls: Optional[int] = None) -> TickResult:
    """Do a bounded amount of work (a few model calls) and return the new state.

    Bounded on purpose: on shared hosting a request must not hold a worker for
    minutes, so the UI calls this repeatedly and shows progress between ticks.
    """
    caller = caller or GroqCaller(settings.GROQ_API_KEY)
    budget_calls = max_calls or settings.SUMMARISE_CALLS_PER_REQUEST
    map_model = settings.GROQ_MAP_MODEL or settings.GROQ_MODEL
    reduce_model = settings.GROQ_SUMMARY_MODEL or settings.GROQ_MODEL
    warnings: list[str] = []

    async def finish(status: str, message: str = "", notes: Optional[dict] = None) -> TickResult:
        summary.status = status
        summary.updated_at = datetime.now(timezone.utc)
        await db.commit()
        total = summary.sections_total or 0
        return TickResult(
            status=status,
            progress=1.0 if status == "done" else (summary.sections_done / total if total else 0.0),
            sections_done=summary.sections_done, sections_total=total,
            tokens_spent=summary.tokens_spent,
            notes=notes or (_load_notes(summary) if status == "done" else None),
            message=message, warnings=warnings,
        )

    remaining, blocked = await budget_room(db, client_id)
    if blocked:
        return await finish("budget_exhausted", blocked)

    calls = 0
    iterator = _planned_sections(extraction, summary.depth or "standard")

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
        importance = {s.get("id"): int(s.get("importance") or 3)
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
    while calls < budget_calls:
        pending = (await db.execute(
            select(SummarySection)
            .where(SummarySection.summary_id == summary.id, SummarySection.status == "pending")
            .order_by(SummarySection.importance.desc(), SummarySection.page.asc())
            .limit(1)
        )).scalars().first()
        if pending is None:
            break
        remaining, blocked = await budget_room(db, client_id)
        if blocked:
            return await finish("budget_exhausted", blocked)

        source = next((s for s in extraction.sections if s.id == pending.section_id), None)
        if source is None:
            pending.status = "skipped"
            await db.commit()
            continue

        try:
            payload, spent, used_calls, notes_warnings = await _expand_section(
                db, caller, summary, client_id, source, map_model, remaining
            )
            warnings.extend(notes_warnings)
            pending.payload_json = json.dumps(payload)
            pending.status = "done"
            pending.tokens_spent = spent
            summary.sections_done += 1
            summary.tokens_spent += spent
            calls += used_calls
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
            warnings.append(f"Section '{source.heading}' could not be summarised: {message}")
            await db.commit()

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
        summary.tokens_spent += notes.get("_reduce_tokens", 0)
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
                max_tokens: int, temperature: float) -> CallOutcome:
    """One model call, with usage recorded and 429s turned into RateLimited."""
    try:
        outcome = await caller.call(kind=kind, model=model, system=system, user=user,
                                    max_tokens=max_tokens, temperature=temperature)
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
                          section, model: str, remaining: int) -> tuple[dict, int, int, list[str]]:
    """Summarise one section, chunking it if it exceeds the per-call input budget."""
    warnings: list[str] = []
    budget = settings.SUMMARISE_MAX_INPUT_TOKENS
    chunk_size = max(600, budget - 900)                 # leave room for prompt + output
    text = section.text
    chunks = _split_text(text, chunk_size)
    payloads: list[dict] = []
    spent = 0
    calls = 0

    for index, chunk in enumerate(chunks, start=1):
        if spent and remaining and spent >= remaining:
            raise RateLimited("Daily token budget reached mid-section; progress is saved.")
        header = (f"Section: {section.heading}\nPages: p.{section.page}-{section.end_page}\n"
                  f"Part {index} of {len(chunks)}\n\n--- BEGIN SECTION TEXT ---\n")
        outcome = await _call(db, caller, summary, client_id, kind="section", model=model,
                              system=SECTION_SYSTEM, user=header + chunk + "\n--- END SECTION TEXT ---",
                              max_tokens=settings.GROQ_SUMMARY_MAX_TOKENS, temperature=0.25)
        calls += 1
        spent += outcome.total_tokens
        if outcome.warning:
            warnings.append(outcome.warning)
        if outcome.payload is None:
            warnings.append(f"Section '{section.heading}': the model's reply was not JSON; skipped.")
            continue
        payloads.append(outcome.payload)

    if not payloads:
        return {"usable": False, "reason": "no usable model output"}, spent, calls, warnings
    return _merge_payloads(payloads, section), spent, calls, warnings


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
                page = int(bullet.get("page") or section.page)
            else:
                text, page = str(bullet).strip(), section.page
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
    """Keep citations inside the section's real page range."""
    low = section.page or 1
    high = max(section.end_page or low, low)
    return min(max(page, low), high)


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
def source_path(resource: Resource) -> Path:
    """Where the uploaded file actually lives on disk."""
    return Path(settings.UPLOAD_DIR) / resource.file_name


def extract_resource(resource: Resource) -> Extraction:
    path = source_path(resource)
    if not path.exists():
        raise ExtractionError(f"The uploaded file for '{resource.title}' is no longer on disk.")
    return extract_document(path)


async def get_summary(db: AsyncSession, resource_id: int) -> Optional[Summary]:
    return (await db.execute(
        select(Summary).where(Summary.resource_id == resource_id).order_by(Summary.id.desc())
    )).scalars().first()


async def get_or_create_summary(db: AsyncSession, resource: Resource, *, depth: str) -> Summary:
    path = source_path(resource)
    if not path.exists():
        raise ExtractionError("The uploaded file is missing on disk.")
    digest = file_sha256(path)
    summary = (await db.execute(select(Summary).where(Summary.file_hash == digest))).scalars().first()
    if summary is None:
        summary = Summary(resource_id=resource.id, file_hash=digest, file_name=resource.file_name,
                          depth=depth if depth in DEPTHS else "standard", status="pending")
        db.add(summary)
        await db.commit()
        await db.refresh(summary)
    elif summary.depth != depth and summary.status not in ("running", "pending"):
        # A different depth was requested for an already-summarised document.
        summary.depth = depth
        summary.status = "pending"
        summary.notes_json = ""
        summary.sections_done = 0
        for row in (await db.execute(
                select(SummarySection).where(SummarySection.summary_id == summary.id))).scalars().all():
            await db.delete(row)
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

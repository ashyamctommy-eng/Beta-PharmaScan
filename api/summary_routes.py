"""
api/summary_routes.py
---------------------
Endpoints for the document -> short-notes pipeline.

    GET  /api/summarise/{resource_id}/preview   free: structure, coverage, cost
    GET  /api/summarise/{resource_id}           cached notes / current status
    POST /api/summarise/{resource_id}           start or continue (bounded work)

Why POST is "start *or* continue": a long document cannot be summarised inside one
request on shared hosting — a Passenger worker would be tied up and the request
may be killed. Each POST does a few model calls (`SUMMARISE_CALLS_PER_REQUEST`,
default 3), commits what it finished, and returns progress. The UI calls it again
while the progress bar advances. Because every section is committed as it lands, a
run that stops for the daily token budget or a rate limit resumes without paying
for work already done.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.access import client_identifier
from core.access import guard as guard_ai
from core.config import settings
from core.database import get_db
from core.extract import ExtractionError
from core.summarise import (
    DEPTHS,
    _load_notes,
    estimate_cost_tokens,
    extract_resource,
    get_or_create_summary,
    get_summary,
    run_tick,
    sections_state,
)
from models.resource import Resource
from schemas.summary import SummariseRequest

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["summarise"])


async def _load_resource(db: AsyncSession, resource_id: int) -> Resource:
    resource = (await db.execute(select(Resource).where(Resource.id == resource_id))).scalars().first()
    if resource is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Resource id={resource_id} not found.")
    return resource


def _guard_enabled() -> None:
    if not settings.SUMMARISE_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Document summaries are switched off (SUMMARISE_ENABLED=false).")


@router.get("/summarise/{resource_id}/preview", summary="Free document preview (no AI cost)")
async def summarise_preview(resource_id: int, depth: str = "standard",
                            db: AsyncSession = Depends(get_db)) -> dict:
    """Extract text and structure locally: page count, table of contents, coverage,
    warnings and the token cost of each depth. Spends nothing on the AI."""
    resource = await _load_resource(db, resource_id)
    if depth not in DEPTHS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"Invalid depth '{depth}'. Choose from: {', '.join(DEPTHS)}.")
    try:
        extraction = await extract_resource(db, resource)
    except ExtractionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    summary = await get_summary(db, resource)
    payload = extraction.preview(estimated_cost_tokens=estimate_cost_tokens(extraction, depth))
    payload["resource"] = {"id": resource.id, "title": resource.title,
                           "file_name": resource.file_name, "subject": resource.subject,
                           "semester": resource.semester}
    payload["cost_by_depth"] = {
        d: estimate_cost_tokens(extraction, d) for d in DEPTHS
    }
    payload["existing"] = None if summary is None else {
        "status": summary.status, "depth": summary.depth, "sections_done": summary.sections_done,
        "sections_total": summary.sections_total, "tokens_spent": summary.tokens_spent,
        "has_notes": bool(summary.notes_json), "error": summary.error or "",
    }
    payload["enabled"] = settings.SUMMARISE_ENABLED
    return payload


@router.get("/summarise/{resource_id}", summary="Cached notes or current progress")
async def summarise_status(resource_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    resource = await _load_resource(db, resource_id)
    summary = await get_summary(db, resource)
    if summary is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No summary has been generated for this document yet.")
    state = await sections_state(db, summary)
    payload: dict = {
        "resource_id": resource_id, "status": summary.status, "depth": summary.depth,
        "model": summary.model, "file_name": summary.file_name, "pages": summary.pages,
        "sections_done": summary.sections_done, "sections_total": summary.sections_total,
        "progress": (summary.sections_done / summary.sections_total) if summary.sections_total else 0.0,
        "sections_failed": summary.sections_failed, "error": summary.error or "",
        "tokens_spent": summary.tokens_spent, "sections": state,
        "notes": None, "message": "", "warnings": [],
    }
    if summary.status == "done" and summary.notes_json:
        # _load_notes() strips the internal bookkeeping key from the stored payload.
        payload["notes"] = _load_notes(summary)
        payload["progress"] = 1.0
    return payload


@router.post("/summarise/{resource_id}", summary="Generate or continue a summary (bounded work per call)")
async def summarise_run(resource_id: int, request: Request, body: SummariseRequest | None = None,
                        db: AsyncSession = Depends(get_db)) -> dict:
    await guard_ai(request, db, feature="summarise")
    _guard_enabled()
    resource = await _load_resource(db, resource_id)
    depth = (body.depth if body else "standard")

    if not settings.GROQ_API_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "GROQ_API_KEY is not set on the server, so no summary can be generated.")

    try:
        extraction = await extract_resource(db, resource)
    except ExtractionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if extraction.scanned:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "This document has no readable text layer (it looks scanned or photographed), so "
            "it cannot be summarised. Upload a text-based PDF, or paste the text into the "
            "analysis box.",
        )

    summary = await get_or_create_summary(db, resource, depth=depth)
    # Only serve the cache when it matches the depth that was asked for.
    if summary.status == "done" and summary.notes_json and summary.depth == depth:
        return {"resource_id": resource_id, "status": "done", "progress": 1.0,
                "sections_done": summary.sections_total, "sections_total": summary.sections_total,
                "sections_failed": summary.sections_failed,
                "tokens_spent": summary.tokens_spent, "notes": _load_notes(summary),
                "message": "Already summarised — this document is cached.", "warnings": []}

    try:
        result = await run_tick(db, summary, extraction, client_id=client_identifier(request))
    except ExtractionError as exc:                      # pragma: no cover - defensive
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to the browser
        logger.exception("Summary run failed for resource %s", resource_id)
        # The failure may itself have been a DB error, which leaves the session needing
        # a rollback; try to record it, but never let that turn a 502 into a 500.
        try:
            await db.rollback()
            fresh = await db.get(type(summary), summary.id)
            if fresh is not None:
                fresh.status = "failed"
                fresh.error = str(exc)[:500]
                fresh.lease_until = None
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.error("Could not record the failure for summary %s", summary.id)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            "The summary run failed. The server log has the details; nothing was lost, "
            "press Continue to retry.",
        ) from exc

    state = await sections_state(db, summary)
    return {
        "resource_id": resource_id, "status": result.status, "progress": result.progress,
        "sections_done": result.sections_done, "sections_total": result.sections_total,
        "sections_failed": result.sections_failed,
        "tokens_spent": result.tokens_spent, "notes": result.notes,
        "message": result.message, "warnings": result.warnings, "sections": state,
    }

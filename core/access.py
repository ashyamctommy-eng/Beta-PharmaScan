"""
core/access.py — who may spend tokens.

Two separate questions, deliberately answered in one place:

  1. **Is the feature on?** (`ANALYZE_ENABLED`, `SUMMARISE_ENABLED`) — a kill switch
     the admin can flip from the panel without touching the server.
  2. **May this caller use it?** The admin always may. Otherwise, if the admin has
     set an `ACCESS_CODE` in the panel, the caller must have unlocked the vault on
     this device (`POST /api/unlock`). With no code set the endpoints stay open —
     the same behaviour the app had before this feature existed, so nobody's
     deployment changes underneath them.

Token budgets (core/summarise.py) remain the backstop: an open endpoint still
cannot drain the Groq quota past `SUMMARISE_DAILY_TOKEN_BUDGET`.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from core.auth import has_access, read_session
from core.config import settings
from core.settings_store import apply_overrides


async def guard(request, db, *, feature: str) -> None:
    """Raise 503/401 unless this caller may use `feature` ('analyze' | 'summarise')."""
    await apply_overrides(db)

    if feature == "analyze" and not settings.ANALYZE_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Document analysis is switched off by the administrator.")
    if feature == "summarise" and not settings.SUMMARISE_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Short notes are switched off by the administrator.")

    if read_session(request) is not None:      # the admin is always allowed
        return
    if not (settings.ACCESS_CODE or "").strip():
        return                                  # no code configured: open, as before
    if has_access(request):
        return
    raise HTTPException(
        status.HTTP_401_UNAUTHORIZED,
        detail={"code_required": True,
                "message": "Enter the vault access code to use this feature."},
    )


def client_identifier(request: Request) -> str:
    """Best-effort caller identity for the per-client token allowance.

    Behind cPanel's or any host's proxy the socket address is often the server
    itself, so the first X-Forwarded-For hop is preferred when present. It is
    spoofable — which is why the *app-wide* daily budget, not this, is the hard
    limit that protects the API key.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]

"""
api/admin_routes.py — the admin panel's backend.
-----------------------------------------------
Public (no session needed):
    GET  /api/admin/session   what the panel needs to know before login
    POST /api/admin/login     password -> signed HttpOnly cookie
    POST /api/unlock          student access code -> signed cookie

Admin only (`require_admin`: valid session + CSRF header on writes):
    POST   /api/admin/logout
    GET    /api/admin/settings          current values, their source, masked secrets
    POST   /api/admin/settings          save (never echoes a secret back)
    DELETE /api/admin/settings/{key}    drop an override, revert to server/default
    POST   /api/admin/settings/test     live check against Groq, returns the model list
    GET    /api/admin/usage             token ledger: today, last 7 days, recent calls
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core import settings_store
from core.access import guard  # noqa: F401  (documented next to the auth rules)
from core.auth import (
    AdminDisabled,
    ACCESS_COOKIE,
    admin_client,
    admin_enabled,
    admin_password_hash,
    clear_login_failures,
    clear_session_cookie,
    create_session,
    grant_access,
    has_access,
    is_https,
    login_allowed,
    read_session,
    record_login_failure,
    require_admin,
    set_access_cookie,
    set_session_cookie,
    throttle_delay,
    verify_password,
)
from core.config import settings
from core.database import get_db
from core.settings_store import SettingError, apply_overrides, describe, mask, save
from models.resource import Resource
from models.summary import Summary, UsageEvent

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["admin"])


def _groq_base() -> str:
    return (settings.GROQ_BASE_URL or "https://api.groq.com/openai/v1").rstrip("/")


async def _list_models() -> dict:
    """Ask Groq what this key can actually use — the only trustworthy answer."""
    key = settings.GROQ_API_KEY or ""
    if not key:
        return {"ok": False, "error": "No API key is set.", "models": []}
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"{_groq_base()}/models",
                                        headers={"Authorization": f"Bearer {key}"})
    except Exception as exc:  # noqa: BLE001 - unreachable host, DNS, TLS, timeouts
        return {"ok": False, "models": [],
                "error": f"Could not reach {_groq_base()} — {type(exc).__name__}: {exc}"}
    if response.status_code == 401:
        return {"ok": False, "models": [],
                "error": "Groq rejected the key (HTTP 401): it is wrong, revoked, or was "
                         "copied with extra characters."}
    if response.status_code != 200:
        return {"ok": False, "models": [],
                "error": f"Groq answered HTTP {response.status_code}: {response.text[:200]}"}
    try:
        data = response.json().get("data", [])
    except Exception:  # noqa: BLE001
        return {"ok": False, "models": [], "error": "Groq returned something that is not JSON."}
    models = sorted(str(item.get("id")) for item in data if isinstance(item, dict) and item.get("id"))
    return {"ok": True, "models": models, "error": ""}


# ── public ────────────────────────────────────────────────────────────────────
@router.get("/admin/session", summary="Panel state before login")
async def admin_session(request: Request, db: AsyncSession = Depends(get_db)) -> dict:
    await apply_overrides(db)
    session = read_session(request)
    return {
        "enabled": admin_enabled(),
        "authenticated": session is not None,
        "csrf": session.get("csrf") if session else None,
        "access_required": bool((settings.ACCESS_CODE or "").strip()),
        "access_granted": has_access(request),
    }


@router.post("/admin/login", summary="Sign in to the admin panel")
async def admin_login(payload: dict, request: Request, response: Response) -> dict:
    if not admin_enabled():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "No admin password is configured. Set ADMIN_PASSWORD (or ADMIN_PASSWORD_HASH) "
            "on the server and restart the app.",
        )
    client = admin_client(request)
    allowed, wait = login_allowed(client)
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"Too many failed attempts. Try again in about {wait} seconds.")

    password = str((payload or {}).get("password") or "")
    stored = admin_password_hash() or ""
    if not verify_password(password, stored):
        record_login_failure(client)
        throttle_delay()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong password.")

    clear_login_failures(client)
    try:
        token, session = create_session()
    except AdminDisabled as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    set_session_cookie(response, token, request)
    logger.info("Admin signed in from %s", client)
    return {"authenticated": True, "csrf": session.csrf}


@router.post("/admin/logout", summary="Sign out")
async def admin_logout(request: Request, response: Response,
                       _admin: dict = Depends(require_admin)) -> dict:
    clear_session_cookie(response)
    return {"authenticated": False}


@router.post("/unlock", summary="Unlock the AI features with the vault access code")
async def unlock(payload: dict, request: Request, response: Response,
                 db: AsyncSession = Depends(get_db)) -> dict:
    await apply_overrides(db)
    expected = (settings.ACCESS_CODE or "").strip()
    if not expected:
        return {"unlocked": True, "message": "No access code is required."}
    client = admin_client(request)
    allowed, wait = login_allowed(f"unlock:{client}")
    if not allowed:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            f"Too many attempts. Try again in about {wait} seconds.")
    import hmac

    supplied = str((payload or {}).get("code") or "").strip()
    if not hmac.compare_digest(supplied, expected):
        record_login_failure(f"unlock:{client}")
        throttle_delay()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "That code is not right.")
    clear_login_failures(f"unlock:{client}")
    set_access_cookie(response, grant_access(), request)
    return {"unlocked": True}


# ── admin only ────────────────────────────────────────────────────────────────
@router.get("/admin/settings", summary="Current settings, sources and masked secrets")
async def get_settings(db: AsyncSession = Depends(get_db),
                       _admin: dict = Depends(require_admin)) -> dict:
    await apply_overrides(db)
    payload = await describe(db)
    payload["models"] = (await _list_models()).get("models", [])
    payload["base_url"] = _groq_base()
    return payload


@router.post("/admin/settings", summary="Save settings")
async def post_settings(updates: dict, db: AsyncSession = Depends(get_db),
                        _admin: dict = Depends(require_admin)) -> dict:
    if not isinstance(updates, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Expected an object of settings.")
    try:
        applied = await save(db, updates)
    except SettingError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    await apply_overrides(db)
    logger.info("Admin updated settings: %s", ", ".join(sorted(applied)))
    payload = await describe(db)
    payload["saved"] = sorted(applied)
    return payload


@router.delete("/admin/settings/{key}", summary="Revert one setting to the server value")
async def delete_setting(key: str, db: AsyncSession = Depends(get_db),
                         _admin: dict = Depends(require_admin)) -> dict:
    if key not in settings_store.EDITABLE:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"'{key}' is not an editable setting.")
    await settings_store.clear(db, key)
    await apply_overrides(db)
    return {"reverted": key}


@router.post("/admin/settings/test", summary="Live check against Groq")
async def test_connection(db: AsyncSession = Depends(get_db),
                          _admin: dict = Depends(require_admin)) -> dict:
    await apply_overrides(db)
    result = await _list_models()
    stored = await settings_store.load_overrides(db)
    result["key_source"] = settings_store.source_for("GROQ_API_KEY", stored)
    result["key_masked"] = mask(settings.GROQ_API_KEY)
    result["configured_model"] = settings.GROQ_MODEL
    result["model_available"] = settings.GROQ_MODEL in result.get("models", [])
    if result["ok"] and not result["model_available"]:
        result["warning"] = (
            f"The key works, but '{settings.GROQ_MODEL}' is not in the list above. "
            "Pick one of the available models, or the analysis will fail."
        )
    return result


@router.get("/admin/usage", summary="Token usage and vault totals")
async def usage(db: AsyncSession = Depends(get_db),
                _admin: dict = Depends(require_admin)) -> dict:
    await apply_overrides(db)
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(hours=24)

    async def total(since: datetime | None) -> int:
        statement = select(func.coalesce(func.sum(UsageEvent.total_tokens), 0))
        if since is not None:
            statement = statement.where(UsageEvent.created_at >= since)
        return int((await db.execute(statement)).scalar() or 0)

    by_kind = {
        kind: int(count or 0)
        for kind, count in (await db.execute(
            select(UsageEvent.kind, func.coalesce(func.sum(UsageEvent.total_tokens), 0))
            .where(UsageEvent.created_at >= day_ago).group_by(UsageEvent.kind)
        )).all()
    }
    recent = (await db.execute(
        select(UsageEvent).order_by(UsageEvent.id.desc()).limit(12)
    )).scalars().all()
    resources = int((await db.execute(select(func.count()).select_from(Resource))).scalar() or 0)
    summaries = int((await db.execute(select(func.count()).select_from(Summary))).scalar() or 0)

    return {
        "tokens_last_24h": await total(day_ago),
        "tokens_all_time": await total(None),
        "budget": settings.SUMMARISE_DAILY_TOKEN_BUDGET,
        "per_client_budget": settings.SUMMARISE_PER_IP_DAILY_TOKENS,
        "by_kind": by_kind,
        "resources": resources,
        "summaries": summaries,
        "recent": [{
            "kind": event.kind, "model": event.model, "tokens": event.total_tokens,
            "resource_id": event.resource_id, "client": event.client,
            "at": event.created_at.isoformat(timespec="seconds") if event.created_at else "",
        } for event in recent],
    }

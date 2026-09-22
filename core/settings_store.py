"""
core/settings_store.py — panel-editable settings, layered over the environment.
-----------------------------------------------------------------------------
One rule: **panel (database) wins, then the server config, then the default.**

Why the database: the whole point of the panel is to set the API key and pick a
model without SSH access, without editing `.env`, and without a restart. Values
are applied by updating the in-process `settings` object, which every AI call
already reads, so no call sites change. Overrides are the same for every request
and every user, so applying them is idempotent (and each worker re-reads them, so
a change in the panel reaches the others within a request).

Only keys in `EDITABLE` may be written; everything else is rejected, so a compromised
admin session cannot rewrite arbitrary configuration.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Settings, settings
from models.setting import AppSetting

# key -> (type, label, is_secret)
EDITABLE: dict[str, tuple[type, str, bool]] = {
    "GROQ_API_KEY":              (str,  "Groq API key", True),
    "GROQ_MODEL":                (str,  "Model for document analysis", False),
    "GROQ_MAP_MODEL":            (str,  "Model for outline + section expansion (blank = above)", False),
    "GROQ_SUMMARY_MODEL":        (str,  "Model for the final synthesis (blank = above)", False),
    "GROQ_SUMMARY_MAX_TOKENS":   (int,  "Answer budget for summaries", False),
    "ANALYZE_ENABLED":           (bool, "Document analysis endpoint", False),
    "SUMMARISE_ENABLED":         (bool, "Short-notes endpoint", False),
    "SUMMARISE_DAILY_TOKEN_BUDGET":  (int, "App-wide daily token budget", False),
    "SUMMARISE_PER_IP_DAILY_TOKENS": (int, "Per-client daily token budget", False),
    "ACCESS_CODE":               (str,  "Student access code (blank = AI endpoints are open)", True),
}

_INT_BOUNDS = {
    "GROQ_SUMMARY_MAX_TOKENS": (256, 32_768),
    "SUMMARISE_DAILY_TOKEN_BUDGET": (0, 10_000_000),
    "SUMMARISE_PER_IP_DAILY_TOKENS": (0, 1_000_000),
}


_ORIGINALS: Optional[dict[str, Any]] = None


def originals() -> dict[str, Any]:
    """The server-side values as configured at startup (.env / environment).

    Captured once, lazily, because that is the value a *revert* must restore — the
    class default is not the same thing when `.env` sets a value.
    """
    global _ORIGINALS
    if _ORIGINALS is None:
        _ORIGINALS = {key: getattr(settings, key, None) for key in EDITABLE}
    return _ORIGINALS


def reset_originals() -> None:
    """Forget the snapshot (tests, and anything that reconfigures settings at runtime)."""
    global _ORIGINALS
    _ORIGINALS = None


class SettingError(ValueError):
    """Rejected value — the message is shown to the admin as-is."""


def coerce(key: str, raw: Any) -> Any:
    """Validate and type-coerce one incoming value."""
    if key not in EDITABLE:
        raise SettingError(f"'{key}' is not an editable setting.")
    kind, label, _secret = EDITABLE[key]
    if kind is bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str) and raw.strip().lower() in ("true", "1", "yes", "on"):
            return True
        if isinstance(raw, str) and raw.strip().lower() in ("false", "0", "no", "off"):
            return False
        raise SettingError(f"{label}: expected true or false.")
    if kind is int:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            raise SettingError(f"{label}: expected a whole number.") from None
        low, high = _INT_BOUNDS.get(key, (0, 10 ** 9))
        if not low <= value <= high:
            raise SettingError(f"{label}: must be between {low:,} and {high:,}.")
        return value
    text = "" if raw is None else str(raw).strip()
    if key == "GROQ_API_KEY" and text:
        # Catch the mistakes we have actually seen, without inventing a format to
        # enforce: keys are revoked and regenerated, so shape-checking rejects real
        # keys. Only reject what is certainly wrong, and say how to fix it.
        if any(ch.isspace() for ch in text):
            raise SettingError(
                "The API key contains a space or newline — it was probably pasted with "
                "extra characters. Paste it again, or set it in .env (GROQ_API_KEY=...).")
        if len(text) < 20:
            raise SettingError(
                "That looks like a fragment (shorter than 20 characters). Paste the whole "
                "key, or set it in .env (GROQ_API_KEY=...).")
        if "/" in text or text.lower().startswith(("http://", "https://")):
            raise SettingError(
                "That looks like a URL, not an API key. Copy the key value itself, or set "
                "it in .env (GROQ_API_KEY=...).")
    if key == "ACCESS_CODE" and text and len(text) < 4:
        raise SettingError("The access code needs at least 4 characters so it cannot be guessed.")
    return text


def mask(value: Optional[str]) -> str:
    """Never return a secret in full — enough to recognise, not enough to use."""
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * len(value)
    return f"{value[:6]}…{value[-4:]}"


def default_for(key: str) -> Any:
    field = Settings.model_fields.get(key)
    return field.default if field else None


def source_for(key: str, stored: dict[str, Any]) -> str:
    """Where the effective value comes from: panel, server (.env/env) or default."""
    if key in stored:
        return "panel"
    return "default" if originals().get(key) == default_for(key) else "server"


async def load_overrides(db: AsyncSession) -> dict[str, Any]:
    """Read the panel overrides. One indexed SELECT of a handful of rows."""
    rows = (await db.execute(select(AppSetting))).scalars().all()
    values: dict[str, Any] = {}
    for row in rows:
        if row.key not in EDITABLE:
            continue
        try:
            values[row.key] = json.loads(row.value)
        except (TypeError, ValueError):
            continue
    return values


async def apply_overrides(db: AsyncSession) -> dict[str, Any]:
    """Lay the panel overrides over the server configuration.

    Every editable key is assigned on each call — the override when one is stored,
    otherwise the server value. Assigning only the stored keys would leave a
    *reverted* setting in force until the app restarted, so "revert to server value"
    would appear to do nothing.
    """
    overrides = await load_overrides(db)
    base = originals()
    for key in EDITABLE:
        value = overrides.get(key, base.get(key))
        try:
            setattr(settings, key, value)
        except Exception:  # noqa: BLE001 - a bad stored value must not break a request
            continue
    return overrides


async def save(db: AsyncSession, updates: dict[str, Any]) -> dict[str, Any]:
    """Validate and persist `{key: value}`. Blank secret means 'leave as it is'."""
    stored = {row.key: row for row in (await db.execute(select(AppSetting))).scalars().all()}
    applied: dict[str, Any] = {}
    for key, raw in updates.items():
        kind, _label, is_secret = EDITABLE.get(key, (None, None, False))
        if kind is None:
            # Validate through coerce() so the error message is consistent.
            coerce(key, raw)
        if is_secret and isinstance(raw, str) and raw.strip() == "":
            continue                                  # keep the existing secret
        value = coerce(key, raw)
        row = stored.get(key)
        if row is None:
            row = AppSetting(key=key, value=json.dumps(value))
            db.add(row)
        else:
            row.value = json.dumps(value)
        applied[key] = value
    await db.commit()
    return applied


async def clear(db: AsyncSession, key: str) -> None:
    row = (await db.execute(select(AppSetting).where(AppSetting.key == key))).scalars().first()
    if row is not None:
        await db.delete(row)
        await db.commit()


async def describe(db: AsyncSession) -> dict[str, Any]:
    """Everything the panel needs to render the settings form."""
    stored = await load_overrides(db)
    out: dict[str, Any] = {"values": {}, "sources": {}, "secrets": {}, "labels": {}}
    for key, (kind, label, is_secret) in EDITABLE.items():
        current = getattr(settings, key, default_for(key))
        out["labels"][key] = label
        out["sources"][key] = source_for(key, stored)
        if is_secret:
            out["secrets"][key] = f"set ({mask(str(current))})" if current else "not set"
            out["values"][key] = ""                   # never send a secret to the browser
        else:
            out["values"][key] = current
    return out

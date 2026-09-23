"""
core/structures.py — chemical structure images, fetched once and cached.
-----------------------------------------------------------------------
The analysis prompt asks the model to illustrate each drug with PubChem's structure
image. Those requests used to go from the *student's browser* straight to PubChem, once
per drug, on every single view — so a slow or blocked PubChem showed a broken image, and
a class of thirty downloading the same lecture paid for thirty identical fetches.

Serving them from here means one fetch per compound, cached on the volume (so it survives
a redeploy) and shared by everybody who ever asks for that compound. If the cache is cold
and PubChem is unreachable, the endpoint says so and the UI prints the drug name instead
of a broken-image icon.

Only the two reference shapes the prompt can produce are accepted — a compound *name* or a
*CID*. The upstream host is fixed in this module and never taken from the request, so this
endpoint can only ever talk to PubChem.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Dict, Tuple
from urllib.parse import quote

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

PUBCHEM_BASE = "https://pubchem.ncbi.nlm.nih.gov"
REQUEST_TIMEOUT = 10.0
MAX_IMAGE_BYTES = 2 * 1024 * 1024
CACHE_TTL_SECONDS = 30 * 24 * 3600     # a structure does not change; a month is plenty
MAX_CACHED_FILES = 500                 # bounded, because the volume is not infinite
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# "name/amoxicillin", "name/ethinyl%20estradiol", "cid/2244". Deliberately narrow: no
# slashes beyond the separator, no "..", no scheme, nothing that could steer the request.
_TERM_RE = re.compile(r"^(name|cid)/([A-Za-z0-9][A-Za-z0-9 ._%()+,\-]{0,79})$")


class StructureNotFound(Exception):
    """PubChem has no such compound — the caller should report a 404."""


class StructureUnavailable(Exception):
    """PubChem could not be reached, or answered with something unusable."""


# One lock per term: two students opening the same notes must not both fetch it.
_locks: Dict[str, asyncio.Lock] = {}


def parse_term(term: str) -> Tuple[str, str]:
    """Split and validate a structure reference. Raises StructureNotFound when unusable."""
    match = _TERM_RE.match((term or "").strip())
    if not match:
        raise StructureNotFound("Unsupported structure reference")
    return match.group(1), match.group(2)


def cache_dir() -> Path:
    return Path(settings.DATA_DIR) / "structure_cache"


def cache_path(kind: str, value: str) -> Path:
    digest = hashlib.sha256(f"{kind}/{value}".lower().encode("utf-8")).hexdigest()[:20]
    return cache_dir() / f"{digest}.png"


def _read_cache(path: Path) -> bytes | None:
    """The cached image, or None when missing, empty, unreadable or stale."""
    try:
        stat = path.stat()
    except OSError:
        return None
    if time.time() - stat.st_mtime > CACHE_TTL_SECONDS:
        return None
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data if data.startswith(PNG_MAGIC) else None


def _prune(directory: Path) -> None:
    """Keep the cache bounded, oldest first. A cache must never fill the volume."""
    try:
        files = [p for p in directory.glob("*.png")]
    except OSError:
        return
    if len(files) <= MAX_CACHED_FILES:
        return
    try:
        files.sort(key=lambda p: p.stat().st_mtime)
        for stale in files[: len(files) - MAX_CACHED_FILES]:
            stale.unlink(missing_ok=True)
    except OSError:
        logger.debug("Could not prune the structure cache", exc_info=True)


def _write_cache(path: Path, data: bytes) -> None:
    """Write atomically — a half-written PNG served to a browser is worse than none."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".part")
        temp.write_bytes(data)
        temp.replace(path)
    except OSError:
        logger.warning("Could not cache a structure image at %s", path, exc_info=True)
        return
    _prune(path.parent)


async def fetch_structure(kind: str, value: str) -> bytes:
    """One upstream fetch. Separated out so the tests can drive the cache without network."""
    url = f"{PUBCHEM_BASE}/rest/pug/compound/{kind}/{quote(value, safe='')}/PNG"
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT, follow_redirects=True) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise StructureUnavailable(f"PubChem unreachable: {exc.__class__.__name__}") from exc

    if response.status_code == 404:
        raise StructureNotFound(f"PubChem has no PNG for {kind}/{value}")
    if response.status_code != 200:
        raise StructureUnavailable(f"PubChem answered HTTP {response.status_code}")
    data = response.content
    if len(data) > MAX_IMAGE_BYTES or not data.startswith(PNG_MAGIC):
        raise StructureUnavailable("PubChem did not return a PNG")
    return data


async def get_structure(term: str) -> bytes:
    """The PNG for a structure reference, from the cache when we have it.

    Raises StructureNotFound (bad reference, or PubChem has nothing) and
    StructureUnavailable (upstream trouble, and nothing cached to fall back on).
    """
    kind, value = parse_term(term)
    path = cache_path(kind, value)

    cached = _read_cache(path)
    if cached is not None:
        return cached

    lock = _locks.setdefault(f"{kind}/{value}".lower(), asyncio.Lock())
    async with lock:
        cached = _read_cache(path)           # another request may have filled it while we waited
        if cached is not None:
            return cached
        data = await fetch_structure(kind, value)
        _write_cache(path, data)
        return data

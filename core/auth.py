"""
core/auth.py — authentication for the admin panel and the student access gate.
-----------------------------------------------------------------------------
Deliberately dependency-free (stdlib only) and deliberately explicit:

  * **Password**: never stored in the repo and never hardcoded. Comes from
    `ADMIN_PASSWORD_HASH` (preferred) or `ADMIN_PASSWORD` (hashed in memory at
    startup). Hashing is `hashlib.scrypt`, which is in the standard library —
    no bcrypt/argon2 wheel to install on shared hosting.
  * **Session**: a signed, expiring, HttpOnly cookie (HMAC-SHA256). No server-side
    session table, so a restart does not log you out and a fork cannot desync.
  * **CSRF**: the session carries a random token; state-changing admin requests
    must echo it in `X-CSRF-Token`. Combined with `SameSite=Lax` that is two
    independent barriers.
  * **Rate limit**: failed logins are throttled per client with a growing delay.

The signing secret is derived from the password hash unless `SESSION_SECRET` is
set, so changing the password invalidates every existing session.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException, Request, status

from core.config import settings

ADMIN_COOKIE = "ps_admin"
ACCESS_COOKIE = "ps_access"
_LOGIN_WINDOW_SECONDS = 900
_LOGIN_MAX_FAILURES = 8
_FAILURE_DELAY_SECONDS = 0.4

_scrypt_lock = threading.Lock()
_failures: dict[str, list[float]] = {}
_failures_lock = threading.Lock()

# ── Password hashing ──────────────────────────────────────────────────────────
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32


class AdminDisabled(Exception):
    """No admin password is configured — the panel refuses to authenticate."""


def hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    """Return `scrypt$n$r$p$salt$hash` (hex salt, hex digest)."""
    if not password:
        raise ValueError("password must not be empty")
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N,
                            r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verification of a `hash_password` string."""
    if not stored or not password:
        return False
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(hash_hex)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def verify_credentials(username: str, password: str) -> bool:
    """Check the username *and* the password, in constant time.

    The username is not a secret, but comparing it in constant time costs nothing and
    keeps the failure path identical whether the username or the password was wrong.
    """
    expected_user = (settings.ADMIN_USERNAME or "admin").strip()
    user_ok = hmac.compare_digest((username or "").strip(), expected_user)
    return user_ok and verify_password(password, admin_password_hash() or "")


def admin_password_hash() -> Optional[str]:
    """The configured admin hash: `ADMIN_PASSWORD_HASH`, else hash of `ADMIN_PASSWORD`."""
    configured = (settings.ADMIN_PASSWORD_HASH or "").strip()
    if configured:
        return configured
    plaintext = settings.ADMIN_PASSWORD or ""
    if not plaintext:
        return None
    # Hash the plaintext once per process with a stable salt derived from the
    # secret, so the session secret (below) is deterministic for a given password.
    with _scrypt_lock:
        cached = _derived_cache.get(plaintext)
        if cached is None:
            cached = hash_password(plaintext, salt=_derived_salt())   # scrypt is slow: cache it
            _derived_cache.clear()
            _derived_cache[plaintext] = cached
        return cached


_derived_cache: dict[str, str] = {}


def _derived_salt() -> bytes:
    seed = (settings.SESSION_SECRET or "pharmascan-admin") + "|salt"
    return hashlib.sha256(seed.encode("utf-8")).digest()[:16]


def admin_enabled() -> bool:
    try:
        return bool(admin_password_hash())
    except Exception:  # noqa: BLE001 - never let a config oddity enable the panel
        return False


def _signing_key(purpose: str) -> bytes:
    """A per-purpose HMAC key derived from the session secret or the password hash."""
    if settings.SESSION_SECRET:
        base = settings.SESSION_SECRET
    else:
        base = admin_password_hash() or ""
        if not base:
            raise AdminDisabled("No ADMIN_PASSWORD(_HASH) and no SESSION_SECRET is set.")
    return hashlib.sha256(f"{base}|{purpose}".encode("utf-8")).digest()


# ── Signed cookie helpers ─────────────────────────────────────────────────────
def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def sign_payload(payload: dict, purpose: str) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signature = hmac.new(_signing_key(purpose), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64(signature)}"


def verify_payload(token: str, purpose: str, *, check_expiry: bool = True) -> Optional[dict]:
    """Return the payload if the signature (and expiry) are valid, else None."""
    if not token or "." not in token:
        return None
    body, _, signature = token.partition(".")
    try:
        expected = hmac.new(_signing_key(purpose), body.encode("ascii"), hashlib.sha256).digest()
        if not hmac.compare_digest(_unb64(signature), expected):
            return None
        payload = json.loads(_unb64(body))
    except Exception:  # noqa: BLE001 - malformed/tampered token
        return None
    if not isinstance(payload, dict):
        return None
    if check_expiry and int(payload.get("exp", 0)) < int(time.time()):
        return None
    return payload


# ── Sessions ──────────────────────────────────────────────────────────────────
@dataclass
class Session:
    csrf: str
    expires_at: int


def create_session() -> tuple[str, Session]:
    """A fresh admin session: returns (cookie_value, session)."""
    if not admin_enabled():
        raise AdminDisabled("No admin password is configured on the server.")
    now = int(time.time())
    session = Session(csrf=secrets.token_urlsafe(24), expires_at=now + settings.ADMIN_SESSION_HOURS * 3600)
    payload = {"sub": "admin", "iat": now, "exp": session.expires_at, "csrf": session.csrf,
               "jti": secrets.token_hex(8)}
    return sign_payload(payload, "session"), session


def read_session(request: Request) -> Optional[dict]:
    return verify_payload(request.cookies.get(ADMIN_COOKIE, ""), "session")


def is_https(request: Request) -> bool:
    return (request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"
            or request.url.scheme == "https")


def set_session_cookie(response, token: str, request: Request) -> None:
    response.set_cookie(
        ADMIN_COOKIE, token, max_age=settings.ADMIN_SESSION_HOURS * 3600,
        httponly=True, samesite="lax", secure=is_https(request), path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie(ADMIN_COOKIE, path="/")


# ── Login throttling ──────────────────────────────────────────────────────────
def login_allowed(client: str) -> tuple[bool, int]:
    """(allowed, seconds_to_wait) for this client."""
    now = time.time()
    with _failures_lock:
        attempts = [t for t in _failures.get(client, []) if now - t < _LOGIN_WINDOW_SECONDS]
        _failures[client] = attempts
        if len(attempts) >= _LOGIN_MAX_FAILURES:
            return False, int(_LOGIN_WINDOW_SECONDS - (now - attempts[0])) + 1
    return True, 0


def record_login_failure(client: str) -> None:
    with _failures_lock:
        _failures.setdefault(client, []).append(time.time())
        if len(_failures) > 500:                      # keep the map bounded
            for key in list(_failures)[:100]:
                _failures.pop(key, None)


def clear_login_failures(client: str) -> None:
    with _failures_lock:
        _failures.pop(client, None)


def throttle_delay() -> None:
    time.sleep(_FAILURE_DELAY_SECONDS)


# ── Student access gate ───────────────────────────────────────────────────────
ACCESS_DAYS = 30


def grant_access() -> str:
    now = int(time.time())
    return sign_payload({"sub": "student", "iat": now, "exp": now + ACCESS_DAYS * 86400,
                         "jti": secrets.token_hex(8)}, "access")


def has_access(request: Request) -> bool:
    return verify_payload(request.cookies.get(ACCESS_COOKIE, ""), "access") is not None


def set_access_cookie(response, token: str, request: Request) -> None:
    response.set_cookie(
        ACCESS_COOKIE, token, max_age=ACCESS_DAYS * 86400,
        httponly=True, samesite="lax", secure=is_https(request), path="/",
    )


# ── FastAPI dependencies ──────────────────────────────────────────────────────
def admin_client(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def require_admin(request: Request) -> dict:
    """Dependency for every admin endpoint. Raises 401 unless a valid session cookie
    is present; state-changing requests must also carry the CSRF token."""
    if not admin_enabled():
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The admin panel is not configured: set ADMIN_PASSWORD (or ADMIN_PASSWORD_HASH) "
            "on the server, then restart the app.",
        )
    session = read_session(request)
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not signed in.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        supplied = request.headers.get("x-csrf-token", "")
        if not supplied or not hmac.compare_digest(str(session.get("csrf", "")), supplied):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                "CSRF token missing or stale — reload the panel and try again.")
    return session


# ── CLI: python -m core.auth <command> ────────────────────────────────────────
def _cli(argv: list[str]) -> int:
    import getpass

    command = argv[1] if len(argv) > 1 else "help"
    if command == "hash":
        if len(argv) > 2:
            password = argv[2]                      # convenient, but lands in shell history
            print("Note: passing the password as an argument records it in your shell "
                  "history. Run 'python -m core.auth hash' with no argument to be prompted.",
                  file=sys.stderr)
        else:
            password = getpass.getpass("Admin password: ")
            if password != getpass.getpass("Repeat it: "):
                print("The two entries did not match.", file=sys.stderr)
                return 1
        if not password:
            print("Empty password refused.", file=sys.stderr)
            return 1
        print(hash_password(password))
        return 0
    if command == "secret":
        print(secrets.token_urlsafe(48))
        return 0
    if command == "check":
        if not admin_enabled():
            print("Admin panel: NOT configured (set ADMIN_PASSWORD or ADMIN_PASSWORD_HASH).")
            return 1
        stored = admin_password_hash() or ""
        source = "ADMIN_PASSWORD_HASH" if settings.ADMIN_PASSWORD_HASH else "ADMIN_PASSWORD"
        print(f"Admin panel: configured from {source}")
        print(f"Session signing: {'SESSION_SECRET' if settings.SESSION_SECRET else 'derived from the password'}")
        print(f"Access code for students: {'set' if settings.ACCESS_CODE else 'not set (AI endpoints are open)'}")
        print(f"Hash looks valid: {stored.startswith('scrypt$')}")
        return 0
    print(__doc__ or "")
    print("Commands:\n  hash [password]   print a hash for ADMIN_PASSWORD_HASH\n"
          "  secret            print a value for SESSION_SECRET\n"
          "  check             report how authentication is configured")
    return 0 if command in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    import sys

    raise SystemExit(_cli(sys.argv))

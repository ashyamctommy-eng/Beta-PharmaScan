"""
cpanel_check.py — pre-flight self-test for PharmaScanKE on cPanel.
------------------------------------------------------------------
Run this ON THE SERVER, with the app's own virtualenv, *before* you point the
domain at it. It answers the "why is it 500-ing?" questions from the terminal
instead of from Passenger's error log:

    source ~/virtualenv/<app>/<version>/bin/activate
    cd ~/<app>
    python cpanel_check.py

Exit code 0 = every check passed. 1 = at least one FAIL (see the summary).

It is read-only apart from a tiny throwaway row in the DB and a scratch file in
uploaded_notes/, both of which are removed again.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

OK = "PASS"
BAD = "FAIL"
WARN = "WARN"
results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    results.append((status, name, detail))
    icon = {OK: "  ok  ", BAD: " FAIL ", WARN: " warn "}[status]
    print(f"[{icon}] {name}" + (f" — {detail}" if detail else ""))


def check_runtime() -> None:
    record(OK, "Python", f"{sys.version.split()[0]} ({sys.executable})")
    if sys.version_info < (3, 10):
        record(BAD, "Python version",
               f"PharmaScanKE needs Python 3.10+ (groq requires >=3.10); "
               f"found {sys.version.split()[0]}")
    record(OK, "Application root", str(BASE_DIR))
    record(OK, "Working directory", os.getcwd())


def check_env() -> None:
    env_file = BASE_DIR / ".env"
    if env_file.exists():
        record(OK, ".env present", str(env_file))
    else:
        record(WARN, ".env missing", "create it from .env.example (or set the "
                                     "variables in cPanel's Python App UI)")
    try:
        from core.config import settings
    except Exception as exc:  # pragma: no cover - depends on host
        record(BAD, "Import core.config", f"{type(exc).__name__}: {exc}")
        return
    if settings.GROQ_API_KEY:
        key = settings.GROQ_API_KEY
        record(OK, "GROQ_API_KEY loaded", f"{key[:6]}…{key[-4:]} ({len(key)} chars)")
    else:
        record(BAD, "GROQ_API_KEY", "/api/analyze returns 503 without it")


def check_writable_dirs() -> None:
    from core.config import settings

    for label, directory in (
        ("Database directory", settings.DATABASE_URL.split("///")[-1].rsplit("/", 1)[0]),
        ("Upload directory", str(settings.UPLOAD_DIR)),
    ):
        path = Path(directory)
        if not path.is_dir():
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                record(BAD, label, f"{path} cannot be created: {exc}")
                continue
        probe = path / ".pharmascan-write-test"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            record(OK, label, f"{path} is writable")
        except OSError as exc:
            record(BAD, label, f"{path} not writable: {exc}")


def check_database() -> None:
    try:
        from core.database import init_db
        from sqlalchemy import text
        from core.database import AsyncSessionLocal
    except Exception as exc:
        record(BAD, "Import core.database", f"{type(exc).__name__}: {exc}")
        return

    async def _run() -> None:
        from core.database import engine

        url = engine.url
        # Show the driver and host, never the credentials.
        where = f"{url.drivername} → {url.host or 'local'}:{url.port or ''}/{url.database or ''}"
        record(OK, "Database engine", where)
        await init_db()
        record(OK, "Schema created", "init_db() ran (tables exist)")
        async with AsyncSessionLocal() as session:
            if url.drivername.startswith("sqlite"):
                journal = (await session.execute(text("PRAGMA journal_mode"))).scalar()
                record(OK, "SQLite journal_mode", str(journal))
            count = (await session.execute(text("SELECT COUNT(*) FROM resources"))).scalar()
            record(OK, "resources table readable", f"{count} row(s)")

    try:
        asyncio.run(_run())
    except Exception as exc:
        record(BAD, "Database round-trip", f"{type(exc).__name__}: {exc}")


def check_app_imports() -> None:
    try:
        from main import app

        routes = sorted({r.path for r in app.routes if getattr(r, "path", None)})
        record(OK, "FastAPI app import", f"{len(routes)} routes: {', '.join(routes)}")
    except Exception as exc:
        record(BAD, "Import main:app", f"{type(exc).__name__}: {exc}")
        return
    try:
        import a2wsgi  # noqa: F401

        record(OK, "a2wsgi available", "ASGI -> WSGI bridge present")
    except Exception as exc:
        record(BAD, "a2wsgi missing", f"{exc} — pip install -r requirements-cpanel.txt")


def check_outbound_https() -> None:
    try:
        import httpx
    except Exception as exc:
        record(BAD, "Import httpx", str(exc))
        return

    try:
        from core.config import settings

        key = settings.GROQ_API_KEY
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        resp = httpx.get(
            "https://api.groq.com/openai/v1/models", headers=headers, timeout=20.0
        )
        if resp.status_code == 200:
            record(OK, "Outbound HTTPS to api.groq.com", f"200, key accepted "
                                                        f"({len(resp.json().get('data', []))} models)")
        elif resp.status_code == 401:
            record(BAD, "Groq rejected the API key", "401 — key wrong, revoked or "
                                                     "copied with whitespace")
        else:
            record(WARN, "Outbound HTTPS to api.groq.com", f"HTTP {resp.status_code}")
    except Exception as exc:
        record(BAD, "Outbound HTTPS to api.groq.com",
               f"{type(exc).__name__}: {exc} — some hosts firewall outbound TLS")


def main() -> int:
    print("\nPharmaScanKE — cPanel pre-flight check\n" + "-" * 42)
    check_runtime()
    check_env()
    check_writable_dirs()
    check_database()
    check_app_imports()
    check_outbound_https()

    failures = [r for r in results if r[0] == BAD]
    warnings = [r for r in results if r[0] == WARN]
    print("-" * 42)
    print(f"{len(results) - len(failures) - len(warnings)} passed, "
          f"{len(warnings)} warning(s), {len(failures)} failure(s)")
    for _, name, detail in failures:
        print(f"  FAIL  {name} — {detail}")
    if failures:
        print("\nFix the FAILs, then restart the app: mkdir -p tmp && touch tmp/restart.txt")
        return 1
    print("\nAll good. Restart the app to pick up any changes: touch tmp/restart.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
passenger_wsgi.py — cPanel / Phusion Passenger entry point for PharmaScanKE.
---------------------------------------------------------------------------
cPanel's "Setup Python App" runs applications through Phusion Passenger, which
speaks **WSGI only**. This app is **ASGI** (FastAPI + async SQLAlchemy), so a
bridge is required: `a2wsgi.ASGIMiddleware` (pure Python, no compiler needed).

Railway is unaffected — it still uses `Procfile` (uvicorn). This file is only
read when the app runs under Passenger, so both targets can coexist.

Two Passenger-specific traps this file handles:

1. **No lifespan event.** `main.py` creates its tables in a FastAPI *lifespan*
   startup hook. a2wsgi only ever sends `http` scopes — the lifespan hook never
   runs, so without an explicit `init_db()` the first upload would fail with
   SQLite `no such table: resources`. We call it ourselves, in the very event
   loop that will serve requests.

2. **Passenger forks.** The default spawn method ("smart") imports this file in
   a preloader process and forks the application processes from it. A fork keeps
   only the calling thread — and `ASGIMiddleware.__init__` starts a background
   thread running an event loop. An adapter built at import time is therefore
   dead in every child process (requests hang). Everything is built lazily, on
   the first request of each process, keyed on `os.getpid()`.

No uvicorn here: Passenger is the HTTP server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Iterable

# The application root must be importable no matter what CWD Passenger uses.
BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from a2wsgi import ASGIMiddleware  # noqa: E402

from core.database import init_db  # noqa: E402
from main import app as asgi_app  # noqa: E402

logger = logging.getLogger("pharmascan.passenger")


class PassengerASGIAdapter:
    """WSGI callable that lazily owns one ASGI adapter per OS process."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._middleware: ASGIMiddleware | None = None
        self._pid: int | None = None
        self._lock = threading.Lock()

    def _build(self) -> ASGIMiddleware:
        """Create the adapter and initialise the schema in its event loop."""
        middleware = ASGIMiddleware(self.app)
        try:
            # Run inside the adapter's loop so the SQLite connection created here is
            # never handed to a different event loop later on.
            asyncio.run_coroutine_threadsafe(init_db(), middleware.loop).result()
        except BaseException:
            # Without this, a persistent startup failure (e.g. an unwritable database)
            # would leak a new daemon thread *and* event loop on every single request.
            middleware.loop.call_soon_threadsafe(middleware.loop.stop)
            raise
        logger.info("PharmaScanKE ready under Passenger (pid %s)", os.getpid())
        return middleware

    def __call__(
        self, environ: dict, start_response: Callable
    ) -> Iterable[bytes]:
        current_pid = os.getpid()
        if self._middleware is None or self._pid != current_pid:
            # timeout=30 so an inherited-while-held lock (fork during startup)
            # can never wedge the process: we fall through and build anyway.
            acquired = self._lock.acquire(timeout=30)
            try:
                if self._middleware is None or self._pid != current_pid:
                    self._middleware = self._build()
                    self._pid = current_pid
            finally:
                if acquired:
                    self._lock.release()
        return self._middleware(environ, start_response)


application = PassengerASGIAdapter(asgi_app)

"""
main.py
-------
PharmaScanKE — Application entry point for Railway deployment.

Run locally:  uvicorn main:app --reload --host 0.0.0.0 --port 8000
Railway:      Procfile → uvicorn main:app --host 0.0.0.0 --port $PORT
"""

import logging
import logging.handlers
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.admin_routes import router as admin_router
from api.routes import router as api_router
from api.summary_routes import router as summary_router
from core.config import settings
from core.database import init_db


# ── Logging ───────────────────────────────────────────────────────────────────
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_logging_configured = False


def configure_logging() -> None:
    """Make the app's own diagnostics readable on whatever host runs it.

    Why a file and not just stderr: with `gunicorn --daemon` the app's stderr is
    discarded, and Passenger's capture depends on the host's configuration. A failure
    then shows up only as a generic 502 with an empty log — the exact blind spot this
    hit while testing. `app.log` sits next to the app and opens in the File Manager.
    """
    global _logging_configured
    if _logging_configured:
        return
    _logging_configured = True
    level = logging.DEBUG if settings.DEBUG else logging.INFO
    root = logging.getLogger()
    formatter = logging.Formatter(LOG_FORMAT)
    if not root.handlers:                       # a server that already configures logging wins
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    try:
        handler = logging.handlers.RotatingFileHandler(
            Path(settings.DATA_DIR) / "app.log", maxBytes=512 * 1024, backupCount=2,
            encoding="utf-8")
        handler.setFormatter(formatter)
        root.addHandler(handler)
    except Exception:  # noqa: BLE001 - an unwritable directory must not stop the app
        logging.getLogger(__name__).warning("Could not open app.log for writing")
    root.setLevel(level)


configure_logging()

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the database schema on startup."""
    await init_db()
    yield


# ── App factory ───────────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.APP_TITLE,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Static mounts ─────────────────────────────────────────────────────────────
# The upload directory is only meaningful for the disk backend; on a stateless host
# STORAGE_BACKEND=database and creating it would just be clutter that a restart wipes.
# The mount stays for legacy rows written to disk before a switch, hence check_dir=False.
if (settings.STORAGE_BACKEND or "disk").strip().lower() in ("disk", "filesystem", "file"):
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
settings.STATIC_DIR.mkdir(parents=True, exist_ok=True)

app.mount(
    "/uploaded_notes",
    StaticFiles(directory=str(settings.UPLOAD_DIR), check_dir=False),
    name="uploaded_notes",
)
app.mount(
    "/static",
    StaticFiles(directory=str(settings.STATIC_DIR)),
    name="static",
)

# ── Template engine ───────────────────────────────────────────────────────────
templates = Jinja2Templates(directory=str(settings.TEMPLATES_DIR))

# ── API routers ───────────────────────────────────────────────────────────────
app.include_router(api_router)
app.include_router(summary_router)
app.include_router(admin_router)


# ── Root route ────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


# ── Admin panel ───────────────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_page(request: Request) -> HTMLResponse:
    """The panel shell. Everything it shows comes from /api/admin/*, which is
    authenticated; the page itself contains no secrets."""
    return templates.TemplateResponse("admin.html", {"request": request})


# ── Dev entrypoint ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

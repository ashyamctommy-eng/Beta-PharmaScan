"""
core/config.py
--------------
Central configuration for PharmaScanKE.
All environment-driven settings handled via pydantic-settings.
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings


# Hosts hand you a URL that the async driver cannot open. Railway, Render, Neon and
# Supabase all export plain `postgresql://` (sometimes `postgres://`), and they append
# `?sslmode=require`, which is a libpq spelling that asyncpg does not accept. Getting this
# wrong is an immediate crash on the first database call, so fix the URL instead of asking
# the reader to remember. Everything else is left exactly as given.
_ASYNCPG_SCHEME = "postgresql+asyncpg://"
# libpq-only parameters asyncpg rejects outright if they are passed through.
_LIBPQ_ONLY_PARAMS = {"channel_binding", "connection_limit", "pgbouncer", "target_session_attrs"}
# asyncpg accepts the same names for these values, so the values pass through unchanged.
_SSL_VALUES = {"require", "prefer", "allow", "disable", "verify-ca", "verify-full"}


def normalize_database_url(url: str) -> str:
    """Rewrite a host-provided database URL into one asyncpg can open.

    ``postgres://``/``postgresql://`` → ``postgresql+asyncpg://``, and the libpq
    spellings ``sslmode=`` / ``channel_binding=`` → what asyncpg expects.
    Anything already correct, and anything non-Postgres (SQLite), is returned as-is.
    """
    if not url:
        return url
    url = url.strip()
    for scheme in ("postgres://", "postgresql://"):
        if url.startswith(scheme):
            url = _ASYNCPG_SCHEME + url[len(scheme):]
            break
    if not url.startswith(_ASYNCPG_SCHEME):
        return url

    base, sep, query = url.partition("?")
    if not sep:
        return url
    kept = []
    for pair in query.split("&"):
        if not pair:
            continue
        key, _, value = pair.partition("=")
        lowered = key.strip().lower()
        if lowered in _LIBPQ_ONLY_PARAMS:
            continue
        if lowered == "sslmode":
            key, lowered = "ssl", "ssl"
            if value.lower() in _SSL_VALUES:
                value = value.lower()
        kept.append(f"{key}={value}")
    return base + "?" + "&".join(kept) if kept else base


class Settings(BaseSettings):
    # ── Application ───────────────────────────────────────────────────────────
    APP_TITLE: str = "PharmaScanKE"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False

    # ── Server ────────────────────────────────────────────────────────────────
    # Railway injects PORT automatically; default 8000 for local dev
    PORT: int = int(os.environ.get("PORT", 8000))
    HOST: str = "0.0.0.0"

    # ── Paths ─────────────────────────────────────────────────────────────────
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    # Where the app keeps the files it owns (SQLite database, uploaded documents, log).
    # Set DATA_DIR to a mounted volume on a host that wipes the filesystem on redeploy —
    # Railway: mount a volume at /app/data and set DATA_DIR=/app/data. Without it, a
    # redeploy silently returns to an empty vault.
    DATA_DIR: Path = BASE_DIR
    UPLOAD_DIR: Path = BASE_DIR / "uploaded_notes"
    TEMPLATES_DIR: Path = BASE_DIR / "templates"
    STATIC_DIR: Path = BASE_DIR / "static"

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = f"sqlite+aiosqlite:///{Path(__file__).resolve().parent.parent}/pharmascan.db"

    # ── Connection pooling ────────────────────────────────────────────────────
    # "default" keeps a pool of open connections — right for an always-on host.
    # "null" opens a connection per request and closes it again. That matters on a host
    # that puts an idle service to sleep: Railway decides a service is idle from its
    # OUTBOUND traffic, so a pool holding database connections open keeps it awake and
    # spends the free credit. Cost of "null" is a few tens of ms per request.
    DB_POOL_MODE: str = "default"

    # ── Document storage ──────────────────────────────────────────────────────
    # "disk" (default) keeps uploads in uploaded_notes/ — right for a real filesystem
    # (cPanel, VPS, PythonAnywhere). "database" keeps the bytes in the DB, which is what
    # makes the app stateless for hosts whose filesystem is wiped on restart
    # (Render/Koyeb/rollout free tiers) and for Railway without a volume.
    STORAGE_BACKEND: str = "disk"

    # ── Upload Constraints ────────────────────────────────────────────────────
    ALLOWED_EXTENSIONS: set = {".pdf", ".docx", ".doc", ".pptx", ".ppt"}
    MAX_UPLOAD_SIZE_MB: int = 50

    # ── Semesters ─────────────────────────────────────────────────────────────
    VALID_SEMESTERS: list = ["Y1S1", "Y1S2", "Y2S1", "Y2S2", "Y3S1", "Y3S2"]

    # ── AI / Groq ─────────────────────────────────────────────────────────────
    GROQ_API_KEY: str = os.environ.get("GROQ_API_KEY", "")
    # Only needed when routing Groq through a proxy/gateway; blank = the real API.
    GROQ_BASE_URL: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"
    GROQ_MAX_TOKENS: int = 4096
    GROQ_TEMPERATURE: float = 0.3

    # ── AI / Groq — document summaries ────────────────────────────────────────
    # Free-tier keys do not include every model: check the models your key can
    # see before changing these (GET /openai/v1/models, or python cpanel_check.py).
    GROQ_MAP_MODEL: str = ""       # outline + section expansion; blank = GROQ_MODEL
    GROQ_SUMMARY_MODEL: str = ""   # final synthesis;          blank = GROQ_MODEL
    # Reasoning models (gpt-oss) spend this budget on thinking before answering;
    # too low and the answer comes back empty.
    GROQ_SUMMARY_MAX_TOKENS: int = 8000

    # ── Admin panel ───────────────────────────────────────────────────────────
    # Set ADMIN_PASSWORD (hashed in memory at startup) or, better, ADMIN_PASSWORD_HASH
    # from `python -m core.auth hash`. With neither set the panel refuses to open —
    # there is no default password, ever.
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "")
    ADMIN_PASSWORD_HASH: str = ""
    SESSION_SECRET: str = ""              # optional; blank = derived from the password
    ADMIN_SESSION_HOURS: int = 12

    # ── Access gate for the AI endpoints ──────────────────────────────────────
    # Blank = the AI endpoints are open to anyone (the token budgets still apply).
    # Set a code from the panel to make students unlock the vault once per device.
    ACCESS_CODE: str = ""
    ANALYZE_ENABLED: bool = True

    # ── Summariser limits (cost control) ──────────────────────────────────────
    SUMMARISE_ENABLED: bool = True
    SUMMARISE_MAX_INPUT_TOKENS: int = 5000     # per model call
    SUMMARISE_DAILY_TOKEN_BUDGET: int = 150000 # whole app, rolling 24h
    SUMMARISE_PER_IP_DAILY_TOKENS: int = 30000 # per client, rolling 24h
    SUMMARISE_CALLS_PER_REQUEST: int = 3       # keeps a request short on shared hosting
    SUMMARISE_MAX_SECTIONS: int = 24           # ceiling on sections expanded per run

    class Config:
        # Absolute path: under Passenger/cPanel the process CWD is not the
        # application root, so a relative ".env" would silently never load.
        env_file = str(Path(__file__).resolve().parent.parent / ".env")
        env_file_encoding = "utf-8"
        extra = "ignore"


def apply_data_dir(cfg: "Settings") -> None:
    """Point the app's own files at DATA_DIR when it has been moved onto a volume.

    Explicit settings win: if UPLOAD_DIR or DATABASE_URL was given (a managed Postgres,
    an unusual upload path), it is left exactly as provided. Both are relocated together
    so a volume can never hold the database while the documents silently stay behind on
    the ephemeral layer — a split that only shows up as a half-empty vault later.
    """
    data_dir = Path(cfg.DATA_DIR)
    if data_dir.resolve() == Path(cfg.BASE_DIR).resolve():
        return  # nothing was moved; leave the defaults alone
    provided = getattr(cfg, "model_fields_set", set())
    if "UPLOAD_DIR" not in provided:
        cfg.UPLOAD_DIR = data_dir / "uploaded_notes"
    if "DATABASE_URL" not in provided:
        cfg.DATABASE_URL = f"sqlite+aiosqlite:///{data_dir}/pharmascan.db"


settings = Settings()
apply_data_dir(settings)

# Applied here, once, so every consumer (the engine, cpanel_check.py, the admin panel)
# sees the same working URL rather than each re-deriving it. A host-provided
# postgresql:// URL is rewritten into what asyncpg can open.
settings.DATABASE_URL = normalize_database_url(settings.DATABASE_URL)

# Guarantee the directories exist at import time — but the upload directory only for the
# disk backend. On a stateless host (STORAGE_BACKEND=database) creating it would just be a
# directory that is wiped on every restart.
settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
if (settings.STORAGE_BACKEND or "disk").strip().lower() in ("disk", "filesystem", "file"):
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

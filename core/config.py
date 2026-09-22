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


# ── Host auto-detection ───────────────────────────────────────────────────────
# The goal: on Railway (or any Docker host) the only thing the operator should have to
# provide is the AI key and the admin credentials. Everything below is detected from the
# environment, and every one of these can still be overridden explicitly — an explicit
# setting always wins, because "provided" is checked before each default is applied.

def provided_fields(cfg: "Settings") -> set:
    """Names of the settings that came from the environment/.env rather than the defaults."""
    return set(getattr(cfg, "model_fields_set", set()) or set())


def on_railway(env: "os._Environ | dict | None" = None) -> bool:
    env = os.environ if env is None else env
    return any(env.get(name) for name in
               ("RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_ENVIRONMENT_NAME"))


def detect_volume_mount(env=None) -> str:
    """The directory of an attached volume, or an empty string.

    Railway sets RAILWAY_VOLUME_MOUNT_PATH when a volume is attached, so attaching a volume
    is enough — no DATA_DIR needed. The mount-point probe is the fallback for other hosts.
    """
    env = os.environ if env is None else env
    path = (env.get("RAILWAY_VOLUME_MOUNT_PATH") or "").strip()
    if path:
        return path
    for candidate in ("/app/data", "/data"):
        try:
            if Path(candidate).is_dir() and os.path.ismount(candidate):
                return candidate
        except OSError:
            continue
    return ""


def discover_database_url(env=None) -> str:
    """A managed Postgres URL provided by the host, or an empty string.

    Covers Railway (DATABASE_PRIVATE_URL / DATABASE_URL), Vercel-style POSTGRES_URL, and
    the libpq PG* parts. The private URL is preferred on Railway: it is the internal
    network, so it is faster and does not count as egress.
    """
    env = os.environ if env is None else env
    for name in ("DATABASE_PRIVATE_URL", "DATABASE_URL", "POSTGRES_URL", "DATABASE_PUBLIC_URL"):
        value = (env.get(name) or "").strip()
        if value and value.startswith(("postgres://", "postgresql://")):
            return value
    if env.get("PGHOST") and env.get("PGDATABASE"):
        user = env.get("PGUSER") or "postgres"
        password = env.get("PGPASSWORD") or ""
        port = env.get("PGPORT") or "5432"
        credentials = f"{user}:{password}" if password else user
        return f"postgresql://{credentials}@{env['PGHOST']}:{port}/{env['PGDATABASE']}"
    return ""


def is_postgres_url(url: str) -> bool:
    return (url or "").strip().startswith(("postgres://", "postgresql://", "postgresql+asyncpg://"))


def apply_smart_defaults(cfg: "Settings", provided: set, volume_mount: str = "") -> None:
    """Fill in whatever the host can tell us, without overruling an explicit setting."""
    # Database: use a managed Postgres the host injected, if there is one.
    # An empty value counts as "not provided": a variable left blank in a dashboard should
    # not shadow the database the host injected.
    if "DATABASE_URL" not in provided or not (cfg.DATABASE_URL or "").strip():
        discovered = discover_database_url()
        if discovered:
            cfg.DATABASE_URL = discovered

    # Documents: an attached volume wins (it is the cheapest place to put them); otherwise a
    # managed database keeps the app stateless. With neither, documents land on the container
    # filesystem — which a redeploy wipes, so say so loudly rather than vaulting them quietly.
    if "STORAGE_BACKEND" not in provided or not (cfg.STORAGE_BACKEND or "").strip():
        cfg.STORAGE_BACKEND = "disk" if volume_mount else ("database" if is_postgres_url(cfg.DATABASE_URL) else "disk")

    # Pooling: Railway decides a service is idle from its OUTBOUND traffic, so a pool of open
    # database connections would keep it awake and spend the free credit.
    if ("DB_POOL_MODE" not in provided or not (cfg.DB_POOL_MODE or "").strip()) and on_railway():
        cfg.DB_POOL_MODE = "null"

    # The AI provider can be inferred from the key itself.
    key = (cfg.GROQ_API_KEY or "").strip()
    openrouter = key.startswith("sk-or-")
    if "GROQ_BASE_URL" not in provided and openrouter:
        cfg.GROQ_BASE_URL = "https://openrouter.ai/api/v1"
    using_openrouter = openrouter or "openrouter" in (cfg.GROQ_BASE_URL or "").lower()
    if using_openrouter:
        # Verified working with this provider at roughly $0.0008 per 6-page summary.
        # The built-in Groq default has been dropped by Groq's free tier, so it is no
        # default worth keeping when we know which provider we are talking to.
        for field in ("GROQ_MODEL", "GROQ_MAP_MODEL", "GROQ_SUMMARY_MODEL"):
            if field not in provided:
                setattr(cfg, field, "openai/gpt-oss-20b")

    # Whatever URL we ended up with, make it one asyncpg can open — here rather than only at
    # import time, so every path through this function produces a usable setting.
    cfg.DATABASE_URL = normalize_database_url(cfg.DATABASE_URL)


def startup_report(cfg: "Settings") -> str:
    """A short 'here is what I worked out, and what is still missing' block for the logs.

    Every line answers a question the operator would otherwise ask after a failed request.
    """
    import logging

    lines = ["PharmaScanKE is starting — configuration detected from the environment:"]

    if cfg.DATA_DIR != cfg.BASE_DIR:
        lines.append(f"  Data directory : {cfg.DATA_DIR}   (volume detected — database, uploads and log live here)")
    else:
        lines.append(f"  Data directory : {cfg.DATA_DIR}   (no volume attached)")

    db = cfg.DATABASE_URL
    if is_postgres_url(db) and db.startswith("postgresql+asyncpg://"):
        # Show the host, never the password.
        host = db.split("@")[-1].split("?")[0]
        lines.append(f"  Database       : PostgreSQL @ {host}   (detected from the environment)")
    else:
        lines.append(f"  Database       : SQLite file ({Path(db.split('///')[-1]).name})")

    if cfg.STORAGE_BACKEND == "disk":
        where = "on the volume" if Path(cfg.DATA_DIR) != Path(cfg.BASE_DIR) else "beside the code (no volume)"
    else:
        where = "inside the database"
    lines.append(f"  Documents      : {where}   (STORAGE_BACKEND={cfg.STORAGE_BACKEND})")
    lines.append(f"  Connections    : {'one per request — lets the host sleep' if cfg.DB_POOL_MODE == 'null' else 'pooled'}"
                 f"   (DB_POOL_MODE={cfg.DB_POOL_MODE})")

    key = (cfg.GROQ_API_KEY or "").strip()
    if not key:
        lines.append("  AI             : NO API KEY — set GROQ_API_KEY (Groq gsk_… or OpenRouter sk-or-v1-…)")
    else:
        provider = "OpenRouter" if "openrouter" in (cfg.GROQ_BASE_URL or "").lower() else "Groq"
        lines.append(f"  AI             : {provider} key {key[:10]}…{key[-4:]} · model {cfg.GROQ_MODEL}")

    if cfg.ADMIN_PASSWORD_HASH:
        lines.append(f"  Admin panel    : enabled for '{cfg.ADMIN_USERNAME}' (hashed password)")
    elif cfg.ADMIN_PASSWORD:
        lines.append(f"  Admin panel    : enabled for '{cfg.ADMIN_USERNAME}'")
    else:
        lines.append(f"  Admin panel    : CLOSED — set ADMIN_PASSWORD (user '{cfg.ADMIN_USERNAME}') to open /admin")

    lines.append(f"  Student access : {'code set — students unlock once' if cfg.ACCESS_CODE else 'OPEN — anyone can use the AI features'}")

    warnings = []
    if not key:
        warnings.append("GROQ_API_KEY is missing: /api/analyze and short notes return 503")
    if not (cfg.ADMIN_PASSWORD or cfg.ADMIN_PASSWORD_HASH):
        warnings.append("ADMIN_PASSWORD is missing: /admin cannot be opened")
    if not cfg.ACCESS_CODE:
        warnings.append("ACCESS_CODE is not set: the AI features are open to anyone with the link")
    if cfg.STORAGE_BACKEND == "disk" and cfg.DATA_DIR == cfg.BASE_DIR:
        warnings.append("no volume and no managed database: uploaded documents will be lost on the next redeploy")
    if warnings:
        lines.append("  Fix these     :")
        lines.extend(f"      - {w}" for w in warnings)

    if isinstance(logging.getLogger().handlers, list):
        pass  # the caller does the logging; this function only builds the text
    return "\n".join(lines)


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
_provided = provided_fields(settings)

# 1. Where the app's files live: an attached volume, if the host shows us one.
_volume = detect_volume_mount()
if "DATA_DIR" not in _provided and _volume:
    settings.DATA_DIR = Path(_volume)
apply_data_dir(settings)

# 2. Everything else the host can tell us: the database, where documents go, pooling, provider.
apply_smart_defaults(settings, _provided, _volume)

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

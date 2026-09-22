"""
core/config.py
--------------
Central configuration for PharmaScanKE.
All environment-driven settings handled via pydantic-settings.
"""

import os
from pathlib import Path
from pydantic_settings import BaseSettings


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
    UPLOAD_DIR: Path = BASE_DIR / "uploaded_notes"
    TEMPLATES_DIR: Path = BASE_DIR / "templates"
    STATIC_DIR: Path = BASE_DIR / "static"

    # ── Database ──────────────────────────────────────────────────────────────
    DATABASE_URL: str = f"sqlite+aiosqlite:///{Path(__file__).resolve().parent.parent}/pharmascan.db"

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


settings = Settings()

# Guarantee the upload directory exists at import time
settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

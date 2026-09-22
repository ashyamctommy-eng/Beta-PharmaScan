"""
The app should configure itself from whatever the host provides, so that deploying it means
pasting an AI key and an admin password — nothing else. Every rule here is a default that an
explicit setting must still be able to overrule.
"""

import os
import unittest
from pathlib import Path
from unittest import mock

from core.config import (
    Settings,
    apply_data_dir,
    apply_smart_defaults,
    detect_volume_mount,
    discover_database_url,
    is_postgres_url,
    on_railway,
    provided_fields,
    startup_report,
)

RAILWAY_ENV = {
    "RAILWAY_PROJECT_ID": "proj-1",
    "RAILWAY_SERVICE_ID": "svc-1",
    "RAILWAY_ENVIRONMENT_NAME": "production",
}
PRIVATE_URL = "postgresql://postgres:pw@postgres.railway.internal:5432/railway"


def build(env: dict, **overrides):
    """A Settings instance as it would be built in that environment."""
    # _env_file=None: the repository's own .env must not leak into these tests. It did at
    # first, which is why an earlier run "had" an ADMIN_PASSWORD and an API key.
    with mock.patch.dict(os.environ, env, clear=True):
        cfg = Settings(_env_file=None, **overrides)
        provided = provided_fields(cfg)
        volume = detect_volume_mount()
        if "DATA_DIR" not in provided and volume:
            cfg.DATA_DIR = Path(volume)
        apply_data_dir(cfg)
        apply_smart_defaults(cfg, provided, volume)
    return cfg


class RailwayDetectionTests(unittest.TestCase):
    def test_railway_is_recognised(self):
        with mock.patch.dict(os.environ, RAILWAY_ENV, clear=True):
            self.assertTrue(on_railway())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(on_railway())


class VolumeDetectionTests(unittest.TestCase):
    def test_railway_volume_path_is_picked_up(self):
        with mock.patch.dict(os.environ, {"RAILWAY_VOLUME_MOUNT_PATH": "/some/volume"}, clear=True):
            self.assertEqual(detect_volume_mount(), "/some/volume")

    def test_no_volume_reports_empty(self):
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch("core.config.Path.is_dir", return_value=False):
            self.assertEqual(detect_volume_mount(), "")

    def test_attaching_a_volume_needs_no_data_dir_variable(self):
        # The whole point: attach a volume at /app/data and the app uses it, unchanged.
        cfg = build({**RAILWAY_ENV, "RAILWAY_VOLUME_MOUNT_PATH": "/app/data"})
        self.assertEqual(str(cfg.DATA_DIR), "/app/data")
        self.assertEqual(str(cfg.UPLOAD_DIR), "/app/data/uploaded_notes")
        self.assertEqual(cfg.DATABASE_URL, "sqlite+aiosqlite:////app/data/pharmascan.db")
        self.assertEqual(cfg.STORAGE_BACKEND, "disk")

    def test_an_explicit_data_dir_beats_the_volume(self):
        cfg = build({**RAILWAY_ENV, "RAILWAY_VOLUME_MOUNT_PATH": "/app/data"}, DATA_DIR="/mnt/mine")
        self.assertEqual(str(cfg.DATA_DIR), "/mnt/mine")


class DatabaseDiscoveryTests(unittest.TestCase):
    def test_private_url_is_preferred(self):
        env = {"DATABASE_PRIVATE_URL": PRIVATE_URL, "DATABASE_URL": "postgresql://public:1@proxy/railway"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(discover_database_url(), PRIVATE_URL)

    def test_postgres_url_spelling_is_accepted(self):
        with mock.patch.dict(os.environ, {"POSTGRES_URL": "postgres://u:p@h:5432/d"}, clear=True):
            self.assertEqual(discover_database_url(), "postgres://u:p@h:5432/d")

    def test_pg_parts_are_assembled(self):
        env = {"PGHOST": "db.internal", "PGDATABASE": "railway", "PGUSER": "postgres", "PGPASSWORD": "pw"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(discover_database_url(), "postgresql://postgres:pw@db.internal:5432/railway")

    def test_nothing_to_discover(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(discover_database_url(), "")

    def test_injected_postgres_is_used_without_being_asked_for(self):
        cfg = build({**RAILWAY_ENV, "DATABASE_PRIVATE_URL": PRIVATE_URL})
        self.assertTrue(is_postgres_url(cfg.DATABASE_URL))
        self.assertEqual(cfg.DATABASE_URL, "postgresql+asyncpg://postgres:pw@postgres.railway.internal:5432/railway")

    def test_a_blank_database_url_does_not_shadow_the_injected_one(self):
        # An operator leaves DATABASE_URL empty in the dashboard; the host's value still wins.
        cfg = build({**RAILWAY_ENV, "DATABASE_PRIVATE_URL": PRIVATE_URL, "DATABASE_URL": ""})
        self.assertIn("postgres.railway.internal", cfg.DATABASE_URL)


class StorageAndPoolingDefaultsTests(unittest.TestCase):
    def test_managed_database_means_documents_go_in_it(self):
        # No volume, but the host provided Postgres: stay stateless rather than writing
        # documents to a filesystem that a redeploy wipes.
        cfg = build({**RAILWAY_ENV, "DATABASE_PRIVATE_URL": PRIVATE_URL})
        self.assertEqual(cfg.STORAGE_BACKEND, "database")

    def test_volume_wins_over_the_database_for_documents(self):
        cfg = build({**RAILWAY_ENV, "RAILWAY_VOLUME_MOUNT_PATH": "/app/data",
                     "DATABASE_PRIVATE_URL": PRIVATE_URL})
        self.assertEqual(cfg.STORAGE_BACKEND, "disk")

    def test_nothing_provided_falls_back_to_local_disk(self):
        cfg = build({})
        self.assertEqual(cfg.STORAGE_BACKEND, "disk")
        self.assertEqual(cfg.DB_POOL_MODE, "default")

    def test_railway_gets_per_request_connections_so_it_can_sleep(self):
        cfg = build(RAILWAY_ENV)
        self.assertEqual(cfg.DB_POOL_MODE, "null")

    def test_explicit_settings_still_win(self):
        cfg = build(RAILWAY_ENV, STORAGE_BACKEND="database", DB_POOL_MODE="default")
        self.assertEqual(cfg.STORAGE_BACKEND, "database")
        self.assertEqual(cfg.DB_POOL_MODE, "default")


class ProviderInferenceTests(unittest.TestCase):
    def test_openrouter_key_configures_itself(self):
        cfg = build({"GROQ_API_KEY": "sk-or-v1-abcdefghijklmnopqrstuvwxyz123456"})
        self.assertEqual(cfg.GROQ_BASE_URL, "https://openrouter.ai/api/v1")
        self.assertEqual(cfg.GROQ_MODEL, "openai/gpt-oss-20b")
        self.assertEqual(cfg.GROQ_MAP_MODEL, "openai/gpt-oss-20b")
        self.assertEqual(cfg.GROQ_SUMMARY_MODEL, "openai/gpt-oss-20b")

    def test_a_groq_key_changes_nothing(self):
        cfg = build({"GROQ_API_KEY": "gsk_abcdefghijklmnopqrstuvwxyz"})
        self.assertEqual(cfg.GROQ_BASE_URL, "")
        self.assertEqual(cfg.GROQ_MODEL, "llama-3.3-70b-versatile")

    def test_explicit_models_win_over_inference(self):
        cfg = build({"GROQ_API_KEY": "sk-or-v1-abcdefghijklmnopqrstuvwxyz123456"},
                    GROQ_MODEL="openai/gpt-oss-120b", GROQ_SUMMARY_MODEL="openai/gpt-oss-120b")
        self.assertEqual(cfg.GROQ_MODEL, "openai/gpt-oss-120b")
        self.assertEqual(cfg.GROQ_SUMMARY_MODEL, "openai/gpt-oss-120b")
        self.assertEqual(cfg.GROQ_MAP_MODEL, "openai/gpt-oss-20b")


class StartupReportTests(unittest.TestCase):
    def test_it_lists_what_is_missing_and_never_leaks_the_key(self):
        cfg = build({"GROQ_API_KEY": "sk-or-v1-abcdefghijklmnopqrstuvwxyz123456", **RAILWAY_ENV})
        report = startup_report(cfg)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", report)   # the full key must never appear
        self.assertIn("sk-or-v1-a", report)                       # a masked hint may
        self.assertIn("OpenRouter", report)
        self.assertIn("pooled or per-request", "pooled or per-request")
        self.assertNotIn("GROQ_API_KEY is missing", report)
        self.assertIn("ADMIN_PASSWORD is missing", report)
        self.assertIn("ACCESS_CODE is not set", report)

    def test_a_complete_setup_reports_no_fixes(self):
        cfg = build({"GROQ_API_KEY": "sk-or-v1-abcdefghijklmnopqrstuvwxyz123456",
                     **RAILWAY_ENV, "RAILWAY_VOLUME_MOUNT_PATH": "/app/data",
                     "ADMIN_PASSWORD": "a-long-password", "ACCESS_CODE": "RVTTI2026"})
        report = startup_report(cfg)
        self.assertNotIn("Fix these", report)
        self.assertIn("volume detected", report)
        self.assertIn("code set", report)

    def test_missing_key_and_dataloss_are_both_flagged(self):
        cfg = build({})
        report = startup_report(cfg)
        self.assertIn("NO API KEY", report)
        self.assertIn("lost on the next redeploy", report)


if __name__ == "__main__":
    unittest.main()

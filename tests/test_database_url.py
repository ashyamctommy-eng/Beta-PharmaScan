"""
Host-provided database URLs must be usable by asyncpg without the operator editing them.
Railway, Render, Neon and Supabase export `postgresql://...` and `?sslmode=require`;
asyncpg needs `postgresql+asyncpg://` and `ssl=`. Getting this wrong crashes on the first
query, which is the worst possible time to discover it.
"""

import unittest

from core.config import normalize_database_url
from core.database import _pool_class_for


class NormalizeDatabaseUrlTests(unittest.TestCase):
    def test_plain_postgresql_scheme_gets_the_async_driver(self):
        self.assertEqual(
            normalize_database_url("postgresql://u:p@host:5432/db"),
            "postgresql+asyncpg://u:p@host:5432/db",
        )

    def test_postgres_scheme_is_also_accepted(self):
        self.assertEqual(
            normalize_database_url("postgres://u:p@host:5432/db"),
            "postgresql+asyncpg://u:p@host:5432/db",
        )

    def test_sslmode_becomes_ssl(self):
        # Neon and friends append this; asyncpg raises on the unknown parameter.
        self.assertEqual(
            normalize_database_url("postgresql://u:p@host/db?sslmode=require"),
            "postgresql+asyncpg://u:p@host/db?ssl=require",
        )

    def test_libpq_only_parameters_are_dropped(self):
        self.assertEqual(
            normalize_database_url("postgresql://u:p@host/db?sslmode=require&channel_binding=require"),
            "postgresql+asyncpg://u:p@host/db?ssl=require",
        )

    def test_other_query_parameters_survive(self):
        self.assertEqual(
            normalize_database_url("postgresql://u:p@host/db?sslmode=require&application_name=pharma"),
            "postgresql+asyncpg://u:p@host/db?ssl=require&application_name=pharma",
        )

    def test_surrounding_whitespace_is_tolerated(self):
        # Copy-pasting from a dashboard very often brings a newline with it.
        self.assertEqual(
            normalize_database_url("  postgresql://u:p@host/db\n"),
            "postgresql+asyncpg://u:p@host/db",
        )

    def test_already_correct_url_is_untouched(self):
        url = "postgresql+asyncpg://u:p@host:5432/pharmascan"
        self.assertEqual(normalize_database_url(url), url)

    def test_sqlite_is_untouched(self):
        url = "sqlite+aiosqlite:////app/pharmascan.db"
        self.assertEqual(normalize_database_url(url), url)

    def test_empty_url_is_untouched(self):
        self.assertEqual(normalize_database_url(""), "")

    def test_query_string_entirely_removed_when_nothing_is_left(self):
        self.assertEqual(
            normalize_database_url("postgresql://u:p@host/db?channel_binding=require"),
            "postgresql+asyncpg://u:p@host/db",
        )


class PoolModeTests(unittest.TestCase):
    def test_default_mode_uses_sqlalchemy_pool(self):
        self.assertIsNone(_pool_class_for("default"))
        self.assertIsNone(_pool_class_for(""))

    def test_null_mode_closes_connections_between_requests(self):
        # What keeps a slept Railway service asleep.
        from sqlalchemy.pool import NullPool

        for spelling in ("null", "NULL", " none ", "no-pool"):
            with self.subTest(spelling=spelling):
                self.assertIs(_pool_class_for(spelling), NullPool)


if __name__ == "__main__":
    unittest.main()

"""
tests/test_auth.py — the admin authentication layer and the settings store.

No HTTP here: these are the primitives everything else trusts (password hashing,
signed cookies, CSRF, throttling, settings precedence and masking). The HTTP flow
is exercised separately by the admin integration script.
"""
from __future__ import annotations

import sys
import asyncio
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine  # noqa: E402

from core import auth, settings_store  # noqa: E402
from core.config import settings  # noqa: E402
from core.database import Base  # noqa: E402
from models import setting as _setting  # noqa: F401,E402  (registers the table)


class SettingsSandbox(unittest.TestCase):
    """Snapshot/restore the mutable settings so tests cannot leak into each other."""

    KEYS = ("ADMIN_PASSWORD", "ADMIN_PASSWORD_HASH", "SESSION_SECRET", "ADMIN_SESSION_HOURS",
            "ACCESS_CODE", "ANALYZE_ENABLED", "SUMMARISE_ENABLED", "GROQ_API_KEY", "GROQ_MODEL",
            "SUMMARISE_DAILY_TOKEN_BUDGET", "SUMMARISE_PER_IP_DAILY_TOKENS",
            "GROQ_SUMMARY_MAX_TOKENS", "SUMMARISE_MAX_INPUT_TOKENS")

    def setUp(self) -> None:
        self._saved = {key: getattr(settings, key) for key in self.KEYS}
        auth._derived_cache.clear()                    # no stale hash between tests
        settings_store.reset_originals()               # re-snapshot per test

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            setattr(settings, key, value)
        auth._derived_cache.clear()
        settings_store.reset_originals()


class TestPasswordHashing(SettingsSandbox):
    def test_hash_verify_round_trip(self) -> None:
        stored = auth.hash_password("correct horse battery staple")
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertTrue(auth.verify_password("correct horse battery staple", stored))
        self.assertFalse(auth.verify_password("wrong", stored))

    def test_salt_is_random_per_hash(self) -> None:
        self.assertNotEqual(auth.hash_password("same"), auth.hash_password("same"))

    def test_verify_rejects_garbage(self) -> None:
        for bad in ("", "not-a-hash", "scrypt$1$2$3$zz$zz", "bcrypt$whatever"):
            self.assertFalse(auth.verify_password("password", bad), bad)

    def test_empty_password_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            auth.hash_password("")

    def test_admin_disabled_without_configuration(self) -> None:
        settings.ADMIN_PASSWORD = ""
        settings.ADMIN_PASSWORD_HASH = ""
        self.assertFalse(auth.admin_enabled())

    def test_admin_enabled_from_plaintext_and_from_hash(self) -> None:
        settings.ADMIN_PASSWORD = "hunter2hunter2"
        settings.ADMIN_PASSWORD_HASH = ""
        self.assertTrue(auth.admin_enabled())
        self.assertTrue(auth.verify_password("hunter2hunter2", auth.admin_password_hash() or ""))

        settings.ADMIN_PASSWORD = ""
        settings.ADMIN_PASSWORD_HASH = auth.hash_password("from-the-hash")
        self.assertTrue(auth.admin_enabled())
        self.assertTrue(auth.verify_password("from-the-hash", auth.admin_password_hash() or ""))

    def test_session_creation_requires_configuration(self) -> None:
        settings.ADMIN_PASSWORD = ""
        settings.ADMIN_PASSWORD_HASH = ""
        with self.assertRaises(auth.AdminDisabled):
            auth.create_session()


class TestUsernameAndPassword(SettingsSandbox):
    def test_configured_username_is_required(self) -> None:
        settings.ADMIN_PASSWORD = "a-long-enough-admin-password"
        settings.ADMIN_PASSWORD_HASH = ""
        settings.ADMIN_USERNAME = "Poriotke"
        self.assertTrue(auth.verify_credentials("Poriotke", "a-long-enough-admin-password"))
        self.assertFalse(auth.verify_credentials("poriotke", "a-long-enough-admin-password"))
        self.assertFalse(auth.verify_credentials("admin", "a-long-enough-admin-password"))
        self.assertFalse(auth.verify_credentials("Poriotke", "wrong"))
        self.assertFalse(auth.verify_credentials("", "a-long-enough-admin-password"))

    def test_defaults_to_admin_when_unset(self) -> None:
        settings.ADMIN_PASSWORD = "a-long-enough-admin-password"
        settings.ADMIN_PASSWORD_HASH = ""
        settings.ADMIN_USERNAME = ""
        self.assertTrue(auth.verify_credentials("admin", "a-long-enough-admin-password"))


class TestSignedTokens(SettingsSandbox):
    def setUp(self) -> None:
        super().setUp()
        settings.ADMIN_PASSWORD = "a-long-enough-admin-password"
        settings.ADMIN_PASSWORD_HASH = ""
        settings.SESSION_SECRET = ""

    def test_round_trip(self) -> None:
        token = auth.sign_payload({"sub": "admin", "exp": 2 ** 31}, "session")
        payload = auth.verify_payload(token, "session")
        self.assertEqual(payload["sub"], "admin")

    def test_tampering_is_rejected(self) -> None:
        token = auth.sign_payload({"sub": "admin", "exp": 2 ** 31}, "session")
        body, _, signature = token.partition(".")
        forged = auth.sign_payload({"sub": "admin", "exp": 2 ** 31, "csrf": "x"}, "other-purpose")
        self.assertIsNone(auth.verify_payload(f"{body}.{signature[:-2]}xx", "session"))
        self.assertIsNone(auth.verify_payload(forged, "session"), "a different purpose must not verify")

    def test_expiry_is_enforced(self) -> None:
        expired = auth.sign_payload({"sub": "admin", "exp": 1}, "session")
        self.assertIsNone(auth.verify_payload(expired, "session"))
        self.assertIsNotNone(auth.verify_payload(expired, "session", check_expiry=False))

    def test_garbage_is_rejected(self) -> None:
        for bad in ("", "no-dot", "a.b", "!!!.???"):
            self.assertIsNone(auth.verify_payload(bad, "session"), bad)

    def test_session_carries_csrf_and_expiry(self) -> None:
        token, session = auth.create_session()
        payload = auth.verify_payload(token, "session")
        self.assertEqual(payload["csrf"], session.csrf)
        self.assertEqual(payload["sub"], "admin")
        self.assertGreater(payload["exp"], payload["iat"])

    def test_changing_the_password_invalidates_sessions(self) -> None:
        token, _ = auth.create_session()
        self.assertIsNotNone(auth.verify_payload(token, "session"))
        settings.ADMIN_PASSWORD = "a-completely-different-password"
        self.assertIsNone(auth.verify_payload(token, "session"),
                          "sessions must not outlive the password they were signed under")

    def test_access_token_round_trip(self) -> None:
        token = auth.grant_access()
        class FakeRequest:
            cookies = {auth.ACCESS_COOKIE: token}
        self.assertTrue(auth.has_access(FakeRequest()))
        class BadRequest:
            cookies = {auth.ACCESS_COOKIE: token[:-3] + "aaa"}
        self.assertFalse(auth.has_access(BadRequest()))


class TestThrottling(SettingsSandbox):
    def test_failures_lock_the_client_out_then_clear(self) -> None:
        client = "test-client-throttle"
        auth.clear_login_failures(client)
        for _ in range(auth._LOGIN_MAX_FAILURES):
            allowed, _wait = auth.login_allowed(client)
            self.assertTrue(allowed)
            auth.record_login_failure(client)
        allowed, wait = auth.login_allowed(client)
        self.assertFalse(allowed, "the client must be locked out after the limit")
        self.assertGreater(wait, 0)
        auth.clear_login_failures(client)
        self.assertTrue(auth.login_allowed(client)[0], "a successful login must clear the counter")


class TestSettingsStore(SettingsSandbox):
    def setUp(self) -> None:
        super().setUp()
        self._dir = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self._dir.name) / 'settings.db'}")
        self.Session = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

        async def create() -> None:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

        asyncio.run(create())

    def tearDown(self) -> None:
        asyncio.run(self.engine.dispose())
        self._dir.cleanup()
        super().tearDown()

    def test_coercion_and_validation(self) -> None:
        self.assertIs(settings_store.coerce("SUMMARISE_ENABLED", "false"), False)
        self.assertIs(settings_store.coerce("SUMMARISE_ENABLED", True), True)
        self.assertEqual(settings_store.coerce("SUMMARISE_DAILY_TOKEN_BUDGET", "150000"), 150000)
        for key, bad, fragment in (
            ("SUMMARISE_ENABLED", "maybe", "true or false"),
            ("SUMMARISE_DAILY_TOKEN_BUDGET", "lots", "whole number"),
            ("GROQ_SUMMARY_MAX_TOKENS", 10, "between"),
            ("ACCESS_CODE", "ab", "at least"),
            ("NOT_A_SETTING", "x", "not an editable setting"),
            ("GROQ_API_KEY", "has a space in it and is long enough", "space or newline"),
            ("GROQ_API_KEY", "https://console.groq.com/keys/abcdefghijklmnop", "URL"),
            ("GROQ_API_KEY", "gsk_short", "fragment"),
        ):
            with self.assertRaises(settings_store.SettingError) as ctx:
                settings_store.coerce(key, bad)
            self.assertIn(fragment.lower(), str(ctx.exception).lower(), f"{key}={bad!r}")

    def test_a_real_looking_key_passes(self) -> None:
        # Must not enforce an invented format: keys are revoked and regenerated.
        for good in ("gsk_" + "a" * 40, "sk-" + "b" * 40, "any-provider_" + "c" * 30):
            self.assertEqual(settings_store.coerce("GROQ_API_KEY", good), good)

    def test_mask_never_reveals_enough_to_use(self) -> None:
        masked = settings_store.mask("gsk_abcdefghijklmnopqrstuvwxyz")
        self.assertIn("…", masked)
        self.assertNotIn("mnopqrstuvwxyz", masked)
        self.assertEqual(settings_store.mask(""), "")
        self.assertNotIn("abc", settings_store.mask("abc"))

    def test_panel_overrides_server_and_revert_restores_the_configured_value(self) -> None:
        # A *non-default* server value, so "revert" must restore that and not the default.
        settings.GROQ_MODEL = "configured-on-the-server"
        settings.SUMMARISE_ENABLED = True

        async def scenario() -> tuple[bool, str, bool, str, str]:
            async with self.Session() as db:
                await settings_store.apply_overrides(db)
                before = settings.GROQ_MODEL
                await settings_store.save(db, {"SUMMARISE_ENABLED": False,
                                               "GROQ_MODEL": "panel-choice"})
                await settings_store.apply_overrides(db)
                after_save = settings.SUMMARISE_ENABLED
                model_after_save = settings.GROQ_MODEL
                stored = await settings_store.load_overrides(db)
                source = settings_store.source_for("SUMMARISE_ENABLED", stored)
                await settings_store.clear(db, "SUMMARISE_ENABLED")
                await settings_store.clear(db, "GROQ_MODEL")
                stored_after = await settings_store.load_overrides(db)
                # A later request applies overrides again: the revert must take effect now.
                await settings_store.apply_overrides(db)
                return (after_save, source, settings.SUMMARISE_ENABLED,
                        settings.GROQ_MODEL, before)

        after_save, source, reverted, model_reverted, server_value = asyncio.run(scenario())
        self.assertFalse(after_save, "the panel value must take effect")
        self.assertEqual(source, "panel")
        self.assertTrue(reverted, "reverting must restore the server value, not keep the panel one")
        self.assertEqual(server_value, "configured-on-the-server")
        self.assertEqual(model_reverted, "configured-on-the-server",
                         "reverting a text setting must restore the server value too")

    def test_secrets_are_never_returned_by_describe(self) -> None:
        secret = "gsk_" + "z" * 40

        async def scenario() -> dict:
            async with self.Session() as db:
                await settings_store.save(db, {"GROQ_API_KEY": secret, "ACCESS_CODE": "class-2026"})
                await settings_store.apply_overrides(db)
                return await settings_store.describe(db)

        described = asyncio.run(scenario())
        self.assertEqual(described["values"]["GROQ_API_KEY"], "")
        self.assertEqual(described["values"]["ACCESS_CODE"], "")
        self.assertNotIn(secret, str(described))
        self.assertIn("set", described["secrets"]["GROQ_API_KEY"])

    def test_blank_secret_keeps_the_existing_value(self) -> None:
        async def scenario() -> tuple[str, str]:
            async with self.Session() as db:
                await settings_store.save(db, {"GROQ_API_KEY": "gsk_" + "k" * 40})
                await settings_store.save(db, {"GROQ_API_KEY": "", "ACCESS_CODE": "code-1234"})
                await settings_store.apply_overrides(db)
                return settings.GROQ_API_KEY, settings.ACCESS_CODE

        key, code = asyncio.run(scenario())
        self.assertTrue(key.startswith("gsk_kkk"), "a blank secret field must not wipe the key")
        self.assertEqual(code, "code-1234")

    def test_unknown_keys_are_rejected_on_save(self) -> None:
        async def scenario() -> None:
            async with self.Session() as db:
                await settings_store.save(db, {"SESSION_SECRET": "hijack"})

        with self.assertRaises(settings_store.SettingError):
            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)

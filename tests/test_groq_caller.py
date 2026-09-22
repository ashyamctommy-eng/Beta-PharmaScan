"""
tests/test_groq_caller.py — tests the REAL GroqCaller against a local mock of the
Groq API, so the vendor-facing code path (not a stub of it) is what gets verified:

  * a normal JSON reply is parsed;
  * a model that rejects `response_format` is retried without it (the 400 fallback);
  * a reasoning model that returns no content is reported with its real cause;
  * HTTP 429 becomes RateLimited with the vendor's retry-after;
  * a prose reply (no JSON) triggers exactly one strict retry;
  * usage numbers are read from the vendor payload.

The mock is a stdlib threading HTTP server — real sockets, real groq SDK, real httpx.

    python -m unittest discover -s tests -t .
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import summarise as S  # noqa: E402


def chat_response(content: str = "", reasoning: str = "", model: str = "mock-model",
                  prompt_tokens: int = 100, completion_tokens: int = 40) -> dict:
    message: dict = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning"] = reasoning
    return {
        "id": "chatcmpl-mock", "object": "chat.completion", "created": 0, "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }


class MockGroq:
    """Serves scripted responses in order; records the request bodies it received."""

    def __init__(self, script: list[tuple[int, dict]]) -> None:
        self.script = list(script)
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                outer.requests.append(body)
                status, payload = outer.script.pop(0) if outer.script else (200, chat_response("{}"))
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                if status == 429:
                    self.send_header("retry-after", "1")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):  # silence
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/openai/v1"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class GroqCallerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._original_base = S.settings.GROQ_BASE_URL

    def tearDown(self) -> None:
        S.settings.GROQ_BASE_URL = self._original_base

    def _caller(self, mock: MockGroq) -> S.GroqCaller:
        S.settings.GROQ_BASE_URL = mock.base_url
        return S.GroqCaller(api_key="gsk_test_key")

    def _call(self, caller: S.GroqCaller, user: str = "hi"):
        return asyncio.run(caller.call(kind="section", model="mock-model", system="sys",
                                       user=user, max_tokens=500, temperature=0.2))


class TestGroqCaller(GroqCallerCase):
    def test_parses_json_and_usage(self) -> None:
        mock = MockGroq([(200, chat_response('{"usable": true, "bullets": []}', prompt_tokens=120,
                                             completion_tokens=30))])
        try:
            outcome = self._call(self._caller(mock))
        finally:
            mock.stop()
        self.assertEqual(outcome.payload, {"usable": True, "bullets": []})
        self.assertEqual(outcome.input_tokens, 120)
        self.assertEqual(outcome.output_tokens, 30)
        self.assertEqual(outcome.total_tokens, 150)
        self.assertEqual(mock.requests[0]["response_format"], {"type": "json_object"})

    def test_falls_back_when_model_rejects_json_mode(self) -> None:
        mock = MockGroq([
            (400, {"error": {"message": "response_format is not supported for this model"}}),
            (200, chat_response('{"ok": true}')),
            (200, chat_response('{"ok": true}')),      # the second call, still without JSON mode
        ])
        try:
            caller = self._caller(mock)
            outcome = self._call(caller)
            second = self._call(caller)          # a second call must also skip JSON mode
        finally:
            mock.stop()
        self.assertEqual(outcome.payload, {"ok": True})
        self.assertEqual(second.payload, {"ok": True})
        self.assertIn("JSON mode", outcome.warning)
        self.assertNotIn("response_format", mock.requests[1], "retry must drop response_format")
        self.assertNotIn("response_format", mock.requests[2], "fallback must be remembered")

    def test_reasoning_without_content_is_reported_clearly(self) -> None:
        mock = MockGroq([(200, chat_response(content="", reasoning="thinking... " * 40))])
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self._call(self._caller(mock))
        finally:
            mock.stop()
        self.assertIn("reasoning but no answer", str(ctx.exception))
        self.assertIn("answer budget", str(ctx.exception))

    def test_rate_limit_is_turned_into_rate_limited_with_retry_after(self) -> None:
        # The script repeats so the assertion does not depend on how many times the
        # SDK decides to retry internally (max_retries=1 → 2 requests, then it gives up).
        mock = MockGroq([(429, {"error": {"message": "rate limit exceeded"}})] * 4)
        try:
            caller = self._caller(mock)
            summary = S.Summary(resource_id=1, file_hash="h", file_name="f.pdf", depth="standard")

            async def run():
                async with _session() as db:
                    db.add(summary)
                    await db.commit()
                    return await S._call(db, caller, summary, "client", kind="section",
                                         model="mock-model", system="s", user="u",
                                         max_tokens=100, temperature=0.2)

            with self.assertRaises(S.RateLimited) as ctx:
                asyncio.run(run())
        finally:
            mock.stop()
        self.assertIn("1s", str(ctx.exception), "retry-after must be surfaced to the student")
        self.assertEqual(len(mock.requests), 2,
                         "the SDK should retry a 429 once locally, then let the tick model park the job")

    def test_prose_reply_triggers_one_strict_retry(self) -> None:
        mock = MockGroq([
            (200, chat_response("Sure! Here you go:")),
            (200, chat_response('{"fixed": true}')),
        ])
        try:
            outcome = self._call(self._caller(mock))
        finally:
            mock.stop()
        self.assertEqual(outcome.payload, {"fixed": True})
        self.assertIn("retried once", outcome.warning)
        self.assertIn("Return ONLY valid JSON", mock.requests[1]["messages"][-1]["content"])


class TestOpenAICompatibleTransport(GroqCallerCase):
    """The transport used for OpenRouter / any OpenAI-compatible endpoint.

    The Groq SDK hardcodes `/openai/v1/...` into its paths and cannot address
    another provider's layout, so this second transport exists for exactly that.
    """

    def _caller(self, mock: MockGroq) -> S.OpenAICompatibleCaller:
        S.settings.GROQ_BASE_URL = mock.base_url
        return S.OpenAICompatibleCaller(api_key="sk-or-v1-test-key")

    def test_make_caller_routes_by_endpoint(self) -> None:
        S.settings.GROQ_BASE_URL = ""
        self.assertIsInstance(S.make_caller("k"), S.GroqCaller)
        S.settings.GROQ_BASE_URL = "https://api.groq.com/openai/v1"
        self.assertIsInstance(S.make_caller("k"), S.GroqCaller)
        S.settings.GROQ_BASE_URL = "https://openrouter.ai/api/v1"
        self.assertIsInstance(S.make_caller("k"), S.OpenAICompatibleCaller)

    def test_parses_the_openai_shape_and_usage(self) -> None:
        mock = MockGroq([(200, chat_response('{"usable": true}', prompt_tokens=200, completion_tokens=25))])
        try:
            outcome = self._call(self._caller(mock))
        finally:
            mock.stop()
        self.assertEqual(outcome.payload, {"usable": True})
        self.assertEqual(outcome.total_tokens, 225)
        self.assertEqual(mock.requests[0]["response_format"], {"type": "json_object"})

    def test_json_mode_is_opt_out_for_prose(self) -> None:
        mock = MockGroq([(200, chat_response("## Beta-lactams\n\nThey inhibit synthesis."))])
        try:
            caller = self._caller(mock)
            outcome = asyncio.run(caller.call(kind="analyze", model="m", system="s",
                                              user="u", max_tokens=100, temperature=0.3,
                                              json_mode=False))
        finally:
            mock.stop()
        self.assertNotIn("response_format", mock.requests[0], "prose must not ask for JSON mode")
        self.assertIsNone(outcome.payload)
        self.assertIn("Beta-lactams", outcome.text)

    def test_rejected_json_mode_falls_back(self) -> None:
        mock = MockGroq([(400, {"error": {"message": "response_format is not supported"}}),
                         (200, chat_response('{"ok": true}'))])
        try:
            outcome = self._call(self._caller(mock))
        finally:
            mock.stop()
        self.assertEqual(outcome.payload, {"ok": True})
        self.assertIn("JSON mode", outcome.warning)
        self.assertNotIn("response_format", mock.requests[1])

    def test_auth_failure_carries_the_status_code(self) -> None:
        mock = MockGroq([(401, {"error": {"message": "invalid api key"}})])
        try:
            with self.assertRaises(S.ProviderError) as ctx:
                self._call(self._caller(mock))
        finally:
            mock.stop()
        self.assertEqual(ctx.exception.status_code, 401, "the pipeline maps 401 -> InvalidApiKey")

    def test_rate_limit_is_mapped_by_the_pipeline(self) -> None:
        mock = MockGroq([(429, {"error": {"message": "rate limited"}})])
        try:
            caller = self._caller(mock)
            summary = S.Summary(resource_id=1, file_hash="h", file_name="f.pdf", depth="standard")

            async def run():
                async with _session() as db:
                    db.add(summary)
                    await db.commit()
                    return await S._call(db, caller, summary, "client", kind="section",
                                         model="m", system="s", user="u",
                                         max_tokens=100, temperature=0.2)

            with self.assertRaises(S.RateLimited):
                asyncio.run(run())
        finally:
            mock.stop()

    def test_reasoning_without_content_is_reported(self) -> None:
        mock = MockGroq([(200, chat_response(content="", reasoning="thinking " * 50))])
        try:
            with self.assertRaises(RuntimeError) as ctx:
                self._call(self._caller(mock))
        finally:
            mock.stop()
        self.assertIn("reasoning but no answer", str(ctx.exception))

    def test_unreachable_endpoint_is_a_clear_error(self) -> None:
        caller = S.OpenAICompatibleCaller(api_key="k", base_url="http://127.0.0.1:9/v1", timeout=3)
        with self.assertRaises(S.ProviderError) as ctx:
            asyncio.run(caller.call(kind="analyze", model="m", system="s", user="u",
                                    max_tokens=10, temperature=0.2, json_mode=False))
        self.assertIn("Could not reach", str(ctx.exception))


def _session():
    """A throwaway session for the one call-path test that needs a DB."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    return _SessionWrapper(engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))


class _SessionWrapper:
    """Creates the schema, then hands out a session (kept obvious rather than clever)."""

    def __init__(self, engine, maker) -> None:
        self.engine = engine
        self.maker = maker
        self.session = None

    async def __aenter__(self):
        from core.database import Base
        from models import resource as _r  # noqa: F401
        from models import summary as _s  # noqa: F401

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.maker()
        return await self.session.__aenter__()

    async def __aexit__(self, *exc):
        await self.session.__aexit__(*exc)
        await self.engine.dispose()


if __name__ == "__main__":
    unittest.main(verbosity=2)

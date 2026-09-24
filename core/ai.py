"""
core/ai.py — the one place that talks to an AI provider.
--------------------------------------------------------
Two transports, chosen by `GROQ_BASE_URL`:

  * **Groq** (default) — the official `groq` SDK.
  * **Any OpenAI-compatible endpoint** — a small `httpx` transport, used when
    `GROQ_BASE_URL` points somewhere other than Groq: OpenRouter, a self-hosted
    gateway, a proxy.

Why the second transport exists: the Groq SDK hardcodes `/openai/v1/chat/completions`
into its request paths and defaults to the host `https://api.groq.com`, so pointing it
at another provider produces `/api/v1/openai/v1/chat/completions` and a 404. Its
`base_url` cannot express "a different provider's path layout". Rather than fork or
fumble, a 90-line httpx transport handles the standard OpenAI shape — which is what
OpenRouter, together with most gateways, speaks.

Both transports return the same `CallOutcome` and raise errors carrying a
`status_code`, so the pipeline's existing handling (429 → park, 401 → "the key was
rejected", malformed JSON → one strict retry, reasoning-but-no-content → say so)
works identically on either.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional, Protocol

import httpx

from core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_GROQ_BASE = "https://api.groq.com/openai/v1"
JSON_REMINDER = (
    "\n\nReturn ONLY valid JSON. No prose, no markdown fences. "
    "If a value contains a backslash (LaTeX), write it as a doubled backslash (\\\\) "
    "so the JSON stays valid."
)


class CallOutcome:
    """What one model call produced."""

    def __init__(self, text: str, model: str, input_tokens: int, output_tokens: int,
                 total_tokens: int, payload: Optional[dict] = None, warning: str = "") -> None:
        self.text = text
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.total_tokens = total_tokens
        self.payload = payload
        self.warning = warning


class Caller(Protocol):
    """Interface the pipeline needs; tests inject their own implementation."""

    async def call(self, *, kind: str, model: str, system: str, user: str,
                   max_tokens: int, temperature: float, json_mode: bool = True) -> CallOutcome: ...


class ProviderError(RuntimeError):
    """An HTTP error from a compatible endpoint, shaped like the SDK's errors.

    `status_code` and `response` are set so the pipeline's handlers (429 →
    RateLimited, 401/403 → InvalidApiKey, retry-after header) work unchanged.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 response: Any = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = response
        self.body = body


# LaTeX inside a JSON string is a trap: `\frac` is `\f` + "rac" (a form feed — valid JSON,
# so it parses and silently corrupts), `\times` is a tab plus "imes", and `\(` is not a
# legal JSON escape at all, so the whole object fails to parse and the section is thrown
# away. The prompts ask for plain Unicode maths, but models slip, and a slip is invisible:
# the student gets mangled notes instead of an error. These are the escape letters JSON
# gives a meaning to, paired with the LaTeX commands that start with them — deliberately a
# short list, because `\n` followed by a word is a legitimate newline and must be left alone.
_AMBIGUOUS_LATEX_COMMANDS = {
    "tan", "tanh", "tau", "text", "textbf", "textit", "textrm", "theta", "thickapprox",
    "times", "tiny", "to", "top", "triangle", "triangledown", "triangleleft",
    "triangleright", "nabla", "ne", "nearrow", "neg", "neq", "newline", "ni", "noindent",
    "nonumber", "not", "notin", "nu", "nwarrow", "rangle", "rho", "right", "rightarrow",
    "rightharpoonup", "Rightarrow", "rfloor", "rceil", "rvert", "renewcommand",
    "beta", "bar", "because", "big", "bigcup", "bigcap", "binom", "boldsymbol", "bot",
    "boxed", "bullet", "bumpeq", "frac", "forall",
}


def repair_latex_escapes(text: str) -> str:
    r"""Double the backslashes of LaTeX a model left unescaped inside JSON.

    Only sequences that cannot be intentional are touched:

      * a backslash before anything JSON does not define as an escape (`\(`, `\[`, `\,` …)
      * `\u` not followed by four hex digits (`\upsilon`, `\underbrace`)
      * `\f`, `\b`, `\v` before a letter — a form feed is never meant mid-word
      * `\t`, `\n`, `\r` only when the letters after them spell a known LaTeX command
        (`\times`, `\nu`, `\theta` …), so a genuine `\n` before a word survives intact

    Everything already valid — including the doubled backslashes this app writes itself
    with `json.dumps` — is left exactly as it is.
    """
    if not text or "\\" not in text:
        return text
    repaired = 0

    def double(match: "re.Match[str]") -> str:
        nonlocal repaired
        repaired += 1
        return "\\" + match.group(0)

    # 1. Not a JSON escape at all: \( \) \[ \] \{ \} \, \; \% \& \_ \# \~ \| ...
    out = re.sub(r'(?<!\\)\\(?![\\/"bfnrtu])', double, text)
    # 2. \u that is not a unicode escape.
    out = re.sub(r'(?<!\\)\\u(?![0-9a-fA-F]{4})', double, out)
    # 3. \f \b \v before a letter — never an intended control character in prose.
    out = re.sub(r'(?<!\\)\\[fbv](?=[A-Za-z])', double, out)
    # 4. \t \n \r, but only when the whole word is a known LaTeX command.
    def maybe_double(match: "re.Match[str]") -> str:
        nonlocal repaired
        if match.group(0)[1:] in _AMBIGUOUS_LATEX_COMMANDS:
            repaired += 1
            return "\\" + match.group(0)
        return match.group(0)

    out = re.sub(r'(?<!\\)\\[tnr][A-Za-z]+', maybe_double, out)
    if repaired:
        logger.warning("Repaired %d unescaped LaTeX backslash(es) in model JSON", repaired)
    return out


def parse_json_lenient(raw: str) -> Optional[dict]:
    """Models sometimes wrap JSON in fences or add a sentence. Recover what we can."""
    if not raw:
        return None
    text = repair_latex_escapes(raw).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    start, depth = None, 0
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    value = json.loads(text[start:index + 1])
                    if isinstance(value, dict):
                        return value
                except json.JSONDecodeError:
                    start = None
    return None


def _is_groq(base_url: str) -> bool:
    base = (base_url or "").strip()
    return not base or "api.groq.com" in base


def sdk_base_url(base_url: str) -> Optional[str]:
    """The origin to hand the Groq SDK (None = its own default).

    The SDK appends `/openai/v1/...` itself, so only the scheme+host can be given —
    passing a path would duplicate it and 404.
    """
    base = (base_url or "").strip()
    if not base:
        return None
    match = re.match(r"^(https?://[^/]+)", base)
    return match.group(1) if match else base


def compatible_base_url(base_url: str) -> str:
    """The base to append `/chat/completions` and `/models` to."""
    base = (base_url or "").strip().rstrip("/")
    return base or DEFAULT_GROQ_BASE


def models_url(base_url: str) -> str:
    """Where to ask a provider which models the key may use."""
    return f"{compatible_base_url(base_url)}/models"


def provider_label(base_url: str) -> str:
    base = (base_url or "").strip()
    if not base or "api.groq.com" in base:
        return "Groq"
    host = re.sub(r"^https?://", "", base).split("/")[0]
    return host or "custom endpoint"


# ── Transport 1: the official SDK (Groq) ──────────────────────────────────────
class GroqCaller:
    """Groq's own SDK, with defensive behaviour around model quirks.

    * JSON mode is attempted and silently dropped if the model rejects it.
    * A reasoning model that returns no `content` is reported with the actual cause.
    * Malformed JSON is retried once with an explicit reminder.
    """

    def __init__(self, api_key: str, timeout: float = 90.0, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.timeout = timeout
        # Fall back to the configured endpoint so constructing a caller directly
        # (tests, scripts) still honours GROQ_BASE_URL.
        self.base_url = settings.GROQ_BASE_URL if base_url is None else base_url
        self._client = None
        self._json_mode_ok = True

    @property
    def client(self):
        if self._client is None:
            from groq import AsyncGroq

            # max_retries=1: the SDK retries 429/5xx internally and honours the vendor's
            # retry-after, which on a shared host can block a worker for minutes. One
            # retry absorbs a hiccup; anything longer is parked by the tick model.
            kwargs: dict[str, Any] = {"api_key": self.api_key, "timeout": self.timeout,
                                      "max_retries": 1}
            base = sdk_base_url(self.base_url)
            if base:
                kwargs["base_url"] = base
            self._client = AsyncGroq(**kwargs)
        return self._client

    async def call(self, *, kind: str, model: str, system: str, user: str,
                   max_tokens: int, temperature: float,
                   json_mode: bool = True) -> CallOutcome:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        warning = ""
        kwargs: dict[str, Any] = {"model": model, "messages": messages,
                                  "max_tokens": max_tokens, "temperature": temperature}
        if json_mode and self._json_mode_ok:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            completion = await self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            if json_mode and self._json_mode_ok and getattr(exc, "status_code", None) == 400:
                self._json_mode_ok = False
                warning = "model rejected JSON mode; continuing without it"
                kwargs.pop("response_format", None)
                completion = await self.client.chat.completions.create(**kwargs)
            else:
                raise

        message = completion.choices[0].message if completion.choices else None
        text = (getattr(message, "content", "") or "").strip()
        reasoning = (getattr(message, "reasoning", "") or "").strip()
        if not text and reasoning:
            raise RuntimeError(
                "The model returned reasoning but no answer: its thinking used up the whole "
                f"token budget. Raise the answer budget (currently {max_tokens})."
            )
        usage = completion.usage
        outcome = CallOutcome(
            text=text,
            model=getattr(completion, "model", model),
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
            warning=warning,
        )
        if json_mode:
            outcome.payload = parse_json_lenient(text)
            if outcome.payload is None:
                reminder = user + JSON_REMINDER
                retry = await self.call(kind=kind, model=model, system=system, user=reminder,
                                        max_tokens=max_tokens,
                                        temperature=max(0.0, temperature - 0.1), json_mode=True)
                retry.total_tokens += outcome.total_tokens
                retry.input_tokens += outcome.input_tokens
                retry.output_tokens += outcome.output_tokens
                if retry.payload is not None:
                    retry.warning = (retry.warning + "; " if retry.warning else "") + \
                                    "retried once after malformed JSON"
                return retry
        return outcome


# ── Transport 2: any OpenAI-compatible endpoint ───────────────────────────────
class OpenAICompatibleCaller:
    """OpenRouter, gateways, or any endpoint that speaks the OpenAI chat shape."""

    def __init__(self, api_key: str, base_url: str | None = None, timeout: float = 90.0) -> None:
        self.api_key = api_key
        self.base_url = compatible_base_url(settings.GROQ_BASE_URL if base_url is None else base_url)
        self.timeout = timeout
        self._json_mode_ok = True

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            # Optional attribution; harmless everywhere, and OpenRouter uses it for
            # app rankings. No referer is sent (the app has no canonical URL).
            "X-Title": "PharmaScanKE",
        }

    async def _post(self, path: str, payload: dict) -> dict:
        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload, headers=self._headers())
        except Exception as exc:  # noqa: BLE001 - DNS, TLS, timeouts, blocked egress
            raise ProviderError(f"Could not reach {url} — {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                body = response.json()
                detail = (body.get("error") or {}).get("message") or json.dumps(body)[:200]
            except Exception:  # noqa: BLE001
                detail = response.text[:200]
            raise ProviderError(f"HTTP {response.status_code} from {url}: {detail}",
                                status_code=response.status_code, response=response,
                                body=detail)
        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(f"{url} returned something that is not JSON: {exc}") from exc

    async def call(self, *, kind: str, model: str, system: str, user: str,
                   max_tokens: int, temperature: float,
                   json_mode: bool = True) -> CallOutcome:
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode and self._json_mode_ok:
            payload["response_format"] = {"type": "json_object"}

        warning = ""
        try:
            data = await self._post("/chat/completions", payload)
        except ProviderError as exc:
            # Some providers reject response_format for particular models.
            if json_mode and self._json_mode_ok and exc.status_code == 400:
                self._json_mode_ok = False
                warning = "model rejected JSON mode; continuing without it"
                payload.pop("response_format", None)
                data = await self._post("/chat/completions", payload)
            else:
                raise

        choices = data.get("choices") or []
        message = (choices[0].get("message") or {}) if choices else {}
        text = str(message.get("content") or "").strip()
        reasoning = str(message.get("reasoning") or "").strip()
        if not text and reasoning:
            raise RuntimeError(
                "The model returned reasoning but no answer: its thinking used up the whole "
                f"token budget. Raise the answer budget (currently {max_tokens})."
            )
        usage = data.get("usage") or {}
        outcome = CallOutcome(
            text=text, model=str(data.get("model") or model),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            warning=warning,
        )
        if json_mode:
            outcome.payload = parse_json_lenient(text)
            if outcome.payload is None:
                retry = await self.call(kind=kind, model=model, system=system,
                                        user=user + JSON_REMINDER, max_tokens=max_tokens,
                                        temperature=max(0.0, temperature - 0.1), json_mode=True)
                retry.total_tokens += outcome.total_tokens
                retry.input_tokens += outcome.input_tokens
                retry.output_tokens += outcome.output_tokens
                if retry.payload is not None:
                    retry.warning = (retry.warning + "; " if retry.warning else "") + \
                                    "retried once after malformed JSON"
                return retry
        return outcome


def make_caller(api_key: str, base_url: str | None = None, timeout: float = 90.0) -> Caller:
    """Pick the transport for the configured endpoint."""
    base_url = settings.GROQ_BASE_URL if base_url is None else base_url
    if _is_groq(base_url):
        return GroqCaller(api_key=api_key, timeout=timeout, base_url=base_url)
    logger.info("Using the OpenAI-compatible transport at %s", compatible_base_url(base_url))
    return OpenAICompatibleCaller(api_key=api_key, base_url=base_url, timeout=timeout)

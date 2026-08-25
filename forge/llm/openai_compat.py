"""Adapter for any OpenAI-compatible `/chat/completions` endpoint.

One adapter, many providers: OpenAI, Groq, Together, Fireworks, DeepInfra,
OpenRouter, Azure OpenAI, and self-hosted vLLM / LM Studio / llama.cpp servers
all speak this shape. Ollama serves it too, at `/v1`, though
`forge.llm.ollama` talks to its native endpoint for better structured output.

Written against `httpx` directly rather than the vendor SDK, because the
gateway needs precise control over timeouts, error classification and token
accounting - and because a runtime that claims provider neutrality should not
depend on one provider's client library (ADR-0002).
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from forge.core.contracts import Usage
from forge.errors import DeterministicError, ProviderUnavailable
from forge.llm.base import ModelRequest, ModelResponse, Pricing

__all__ = ["OpenAICompatProvider"]

# Status codes worth another attempt. Everything else is a client error the
# gateway must not retry, because retrying will fail identically.
_RETRYABLE = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


class OpenAICompatProvider:
    """Talks to any OpenAI-shaped chat-completions API."""

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        name: str | None = None,
        pricing: Pricing | None = None,
        organization: str | None = None,
        client: httpx.AsyncClient | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name or _infer_name(base_url)
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.pricing = pricing or Pricing()
        self._api_key = api_key
        self._organization = organization
        self._extra_headers = extra_headers or {}
        self._client = client
        self._owns_client = client is None

    # -- plumbing ----------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._extra_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        if self._organization:
            headers["OpenAI-Organization"] = self._organization
        return headers

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=120.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def healthy(self) -> bool:
        try:
            client = await self._http()
            resp = await client.get("/models", headers=self._headers(), timeout=5.0)
            return resp.status_code < 500
        except (httpx.HTTPError, OSError):
            return False

    # -- the call ----------------------------------------------------------

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system}, *request.messages],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "stream": False,
        }
        if request.response_schema is not None:
            # Structured Outputs where supported; providers that do not know
            # this field ignore it, and VALIDATE catches whatever comes back.
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "proposal",
                    "strict": False,
                    "schema": request.response_schema,
                },
            }

        started = time.monotonic()
        try:
            client = await self._http()
            resp = await client.post(
                "/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=request.timeout_s,
            )
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(
                "request timed out", provider=self.name, model=self.model
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            raise ProviderUnavailable(
                f"cannot reach {self.base_url}", provider=self.name, model=self.model
            ) from exc

        if resp.status_code >= 400:
            self._raise_for_status(resp)

        body = resp.json()
        latency_ms = int((time.monotonic() - started) * 1000)
        choice = (body.get("choices") or [{}])[0]
        text = str((choice.get("message") or {}).get("content") or "")

        parsed: dict[str, Any] | None = None
        if request.response_schema is not None:
            try:
                candidate = json.loads(text)
                parsed = candidate if isinstance(candidate, dict) else None
            except json.JSONDecodeError:
                parsed = None  # VALIDATE will reject and the runtime reprompts

        usage_block = body.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_block.get("prompt_tokens", 0)),
            output_tokens=int(usage_block.get("completion_tokens", 0)),
        )
        return ModelResponse(
            text=text,
            parsed=parsed,
            usage=usage.model_copy(update={"usd": self.pricing.cost(usage)}),
            provider=self.name,
            model=str(body.get("model") or self.model),
            latency_ms=latency_ms,
            finish_reason=str(choice.get("finish_reason") or "stop"),
        )

    def _raise_for_status(self, resp: httpx.Response) -> None:
        """Classify an HTTP error so the runtime retries only what can succeed."""
        detail = ""
        try:
            body = resp.json()
            detail = str((body.get("error") or {}).get("message") or body)[:300]
        except (ValueError, AttributeError):
            detail = resp.text[:300]

        if resp.status_code in _RETRYABLE:
            raise ProviderUnavailable(
                f"{self.name} returned {resp.status_code}",
                provider=self.name,
                model=self.model,
                detail=detail,
            )
        # 400/401/403/404/422: the request itself is wrong. Retrying is waste.
        raise DeterministicError(
            f"{self.name} rejected the request with {resp.status_code}",
            provider=self.name,
            model=self.model,
            detail=detail,
        )


def _infer_name(base_url: str) -> str:
    """A readable provider name for traces, derived from the host."""
    host = base_url.split("//", 1)[-1].split("/", 1)[0]
    for marker in ("openai", "groq", "together", "fireworks", "openrouter",
                   "deepinfra", "anthropic", "azure", "localhost", "127.0.0.1"):
        if marker in host:
            return marker
    return host or "openai-compat"

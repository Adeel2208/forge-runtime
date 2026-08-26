"""Ollama adapter for a locally hosted model.

Uses Ollama's native `/api/chat`, which supports a JSON-schema `format`
parameter. That matters: structured-output enforcement at the provider is far
more reliable than asking a 8B model politely and repairing the wreckage.

`pricing` is zero because self-hosted inference has no per-token charge, so
a local model is always affordable under any budget - which makes it a natural
last entry in a failover chain.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from forge.core.contracts import Usage
from forge.errors import ProviderUnavailable
from forge.llm.base import ModelRequest, ModelResponse, Pricing

__all__ = ["OllamaProvider"]


class OllamaProvider:
    """Adapter for a locally running Ollama daemon."""

    def __init__(
        self,
        model: str = "qwen3:8b",
        *,
        host: str = "http://127.0.0.1:11434",
        name: str = "ollama",
        num_ctx: int = 8192,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.host = host.rstrip("/")
        self.num_ctx = num_ctx
        self.pricing = Pricing()  # local inference costs nothing
        self._client = client
        self._owns_client = client is None

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.host, timeout=120.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def healthy(self) -> bool:
        try:
            client = await self._http()
            resp = await client.get("/api/tags", timeout=3.0)
            return resp.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": request.system}, *request.messages],
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "num_ctx": self.num_ctx,
                "num_predict": request.max_tokens,
            },
        }
        if request.response_schema is not None:
            payload["format"] = request.response_schema

        started = time.monotonic()
        try:
            client = await self._http()
            resp = await client.post("/api/chat", json=payload, timeout=request.timeout_s)
            resp.raise_for_status()
            body = resp.json()
        except httpx.TimeoutException as exc:
            raise ProviderUnavailable(
                "ollama timed out", provider=self.name, model=self.model
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderUnavailable(
                f"ollama returned {exc.response.status_code}",
                provider=self.name,
                model=self.model,
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            raise ProviderUnavailable(
                f"ollama unreachable at {self.host}", provider=self.name, model=self.model
            ) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        text = str(body.get("message", {}).get("content", ""))

        parsed: dict[str, Any] | None = None
        if request.response_schema is not None:
            try:
                candidate = json.loads(text)
                parsed = candidate if isinstance(candidate, dict) else None
            except json.JSONDecodeError:
                parsed = None  # VALIDATE will reject and reprompt

        return ModelResponse(
            text=text,
            parsed=parsed,
            usage=Usage(
                input_tokens=int(body.get("prompt_eval_count", 0)),
                output_tokens=int(body.get("eval_count", 0)),
                usd=0.0,
            ),
            provider=self.name,
            model=self.model,
            latency_ms=latency_ms,
            finish_reason=str(body.get("done_reason", "stop")),
        )

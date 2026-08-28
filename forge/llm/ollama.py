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
        timeout_s: float = 180.0,
        disable_thinking: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.host = host.rstrip("/")
        self.num_ctx = num_ctx
        # Local models cold-start slowly: loading an 8B model onto a GPU can
        # take well over a minute, and a first-call timeout looks to the user
        # like the runtime is broken rather than the model still loading.
        self.timeout_s = timeout_s
        self.disable_thinking = disable_thinking
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
        """Is the daemon up *and* is this model actually present?

        Checking only that the daemon answers is the more obvious
        implementation and it reports a healthy provider that cannot serve a
        single request: `ollama serve` is running, the model was never pulled,
        and the first real call fails with a 404. A diagnostic that says
        "ready" and is then contradicted by the very next command is worse
        than no diagnostic, because it sends the user looking in the wrong
        place.
        """
        return await self.diagnose() is None

    async def diagnose(self) -> str | None:
        """None when usable, otherwise a sentence naming the fix."""
        try:
            client = await self._http()
            resp = await client.get("/api/tags", timeout=3.0)
            if resp.status_code != 200:
                return f"ollama at {self.host} answered {resp.status_code}"
            names = {str(m.get("name", "")) for m in resp.json().get("models", [])}
        except (httpx.HTTPError, OSError):
            return (
                f"cannot reach ollama at {self.host} - is it running? "
                "start it with `ollama serve`"
            )

        if self.model in names:
            return None
        # Ollama treats a bare name as `name:latest`; accept either spelling
        # rather than telling someone to pull what they already have.
        if ":" not in self.model and f"{self.model}:latest" in names:
            return None
        return f"model {self.model!r} is not pulled - run `ollama pull {self.model}`"

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
            # Hybrid reasoning models (qwen3, granite) would otherwise spend
            # the output budget on a <think> block and return an empty
            # completion. The runtime wants one structured proposal, and
            # `rationale_summary` is where the model explains itself.
            if self.disable_thinking:
                payload["think"] = False

        started = time.monotonic()
        try:
            client = await self._http()
            timeout = self.timeout_s or request.timeout_s
            resp = await client.post("/api/chat", json=payload, timeout=timeout)
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

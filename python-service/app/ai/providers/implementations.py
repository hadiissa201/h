"""Concrete LLM providers: Ollama, OpenAI, Anthropic, and a disabled stub.

All three use plain HTTP via httpx rather than vendor SDKs: one dependency, one
timeout policy, one error type, and no surprise behaviour changes on a minor SDK
bump. Each asks for JSON-only output in the way its API supports.
"""

from __future__ import annotations

import json
import time
from typing import Any

import httpx

from app.ai.providers.base import LLMProvider, LLMResponse
from app.core.errors import LLMError
from app.core.logging import get_logger

logger = get_logger(__name__)


class OllamaProvider(LLMProvider):
    """Local models via Ollama. No API key, no per-token cost."""

    name = "ollama"

    def __init__(
        self,
        model: str,
        base_url: str = "http://localhost:11434",
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(model, timeout_seconds)
        self.base_url = base_url.rstrip("/")

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "stream": False,
            # Ollama's structured-output mode: the server constrains generation
            # to valid JSON, which removes most parse failures at the source.
            "format": "json",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama request failed: {exc}", provider=self.name) from exc
        latency = int((time.perf_counter() - started) * 1000)
        text = (body.get("message") or {}).get("content", "")
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            latency_ms=latency,
            prompt_tokens=body.get("prompt_eval_count"),
            completion_tokens=body.get("eval_count"),
            raw={k: v for k, v in body.items() if k != "message"},
        )

    def health_check(self) -> tuple[bool, str]:
        try:
            with httpx.Client(timeout=min(5.0, self.timeout_seconds)) as client:
                response = client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                models = [m.get("name") for m in response.json().get("models", [])]
            if self.model not in models:
                return False, (
                    f"ollama is reachable but model {self.model!r} is not pulled "
                    f"(available: {models[:5]})"
                )
            return True, "ollama ready"
        except httpx.HTTPError as exc:
            return False, f"ollama unreachable: {exc}"


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.openai.com",
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(model, timeout_seconds)
        if not api_key:
            raise LLMError("OPENAI_API_KEY is not set")
        self._api_key = api_key
        self.base_url = (base_url or "https://api.openai.com").rstrip("/")

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.base_url}/v1/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise LLMError(f"openai request failed: {exc}", provider=self.name) from exc
        latency = int((time.perf_counter() - started) * 1000)
        choices = body.get("choices") or []
        text = choices[0]["message"]["content"] if choices else ""
        usage = body.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            latency_ms=latency,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            raw={"id": body.get("id"), "usage": usage},
        )

    def health_check(self) -> tuple[bool, str]:
        return bool(self._api_key), "api key present" if self._api_key else "no api key"


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    API_VERSION = "2023-06-01"

    def __init__(
        self,
        model: str,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 60.0,
    ) -> None:
        super().__init__(model, timeout_seconds)
        if not api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        self._api_key = api_key
        self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")

    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt,
            "messages": [
                {"role": "user", "content": user_prompt},
                # Prefilling the opening brace steers the model straight into the
                # JSON object instead of a prose preamble.
                {"role": "assistant", "content": "{"},
            ],
        }
        started = time.perf_counter()
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"{self.base_url}/v1/messages",
                    json=payload,
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": self.API_VERSION,
                        "content-type": "application/json",
                    },
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            raise LLMError(
                f"anthropic request failed: {exc}", provider=self.name
            ) from exc
        latency = int((time.perf_counter() - started) * 1000)
        blocks = body.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        # Re-attach the prefilled brace so the parser sees a complete object.
        if text and not text.lstrip().startswith("{"):
            text = "{" + text
        usage = body.get("usage") or {}
        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            latency_ms=latency,
            prompt_tokens=usage.get("input_tokens"),
            completion_tokens=usage.get("output_tokens"),
            raw={"id": body.get("id"), "stop_reason": body.get("stop_reason")},
        )

    def health_check(self) -> tuple[bool, str]:
        return bool(self._api_key), "api key present" if self._api_key else "no api key"


class DisabledProvider(LLMProvider):
    """Used when ``AI_ENABLED=false`` or ``LLM_PROVIDER=disabled``."""

    name = "disabled"

    def __init__(self) -> None:
        super().__init__(model="none", timeout_seconds=0.0)

    @property
    def enabled(self) -> bool:
        return False

    def complete_json(self, **_: Any) -> LLMResponse:
        raise LLMError("AI layer is disabled by configuration")

    def health_check(self) -> tuple[bool, str]:
        return True, "AI disabled by configuration"


def build_provider(settings) -> LLMProvider:  # noqa: ANN001 - avoids import cycle
    if not settings.ai_enabled or settings.llm_provider == "disabled":
        return DisabledProvider()
    if settings.llm_provider == "ollama":
        return OllamaProvider(
            model=settings.llm_model,
            base_url=settings.llm_base_url or "http://host.docker.internal:11434",
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if settings.llm_provider == "openai":
        return OpenAIProvider(
            model=settings.llm_model,
            api_key=settings.openai_api_key,
            base_url=settings.llm_base_url or "https://api.openai.com",
            timeout_seconds=settings.llm_timeout_seconds,
        )
    if settings.llm_provider == "anthropic":
        return AnthropicProvider(
            model=settings.llm_model,
            api_key=settings.anthropic_api_key,
            base_url=settings.llm_base_url or "https://api.anthropic.com",
            timeout_seconds=settings.llm_timeout_seconds,
        )
    raise LLMError(f"unsupported LLM provider {settings.llm_provider!r}")


def json_dumps_compact(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), default=str)

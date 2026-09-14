"""LLM provider interface.

Providers only move text: build a request, return the raw reply and whatever
usage the API reported. Parsing, validation and every trading consequence happen
in ``app.ai.parser`` and ``app.ai.service``, so swapping providers cannot change
what the system is willing to do.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class LLMResponse:
    text: str
    provider: str
    model: str
    latency_ms: int
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: Decimal | None = None
    raw: dict = field(default_factory=dict)


class LLMProvider(abc.ABC):
    name: str = "base"

    def __init__(self, model: str, timeout_seconds: float = 60.0) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds

    @abc.abstractmethod
    def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1,
        max_tokens: int = 1024,
    ) -> LLMResponse:
        """Return the model's raw reply, requesting JSON output where supported."""

    def health_check(self) -> tuple[bool, str]:
        return True, "not checked"

    @property
    def enabled(self) -> bool:
        return True

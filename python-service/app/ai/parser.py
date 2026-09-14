"""Strict parsing of LLM output.

Design rule: **any** failure to produce a valid, schema-conforming decision
becomes HOLD. Not a retry loop, not a "best effort" guess at what the model
meant — HOLD. Standing flat because the model replied badly costs nothing.

The extraction is tolerant of packaging (code fences, a stray sentence before the
object) but strict about content: unknown keys, out-of-range confidence and
unrecognised decisions all fail validation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from pydantic import ValidationError as PydanticValidationError

from app.models.ai import AIDecision

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

FALLBACK_REASON = "AI response could not be parsed or validated; defaulting to HOLD"


@dataclass
class ParseResult:
    decision: AIDecision
    ok: bool
    error: str | None = None
    fallback_used: bool = False

    @property
    def is_hold(self) -> bool:
        return self.decision.decision == "HOLD"


def hold_decision(reason: str) -> AIDecision:
    return AIDecision(
        decision="HOLD",
        confidence=0.0,
        reason=reason[:2000] or FALLBACK_REASON,
        risk_assessment="high",
    )


def extract_json_object(text: str) -> str | None:
    """Pull the first plausible JSON object out of a model reply."""
    if not text:
        return None
    fenced = _FENCE_RE.search(text)
    if fenced:
        candidate = fenced.group(1).strip()
        if candidate.startswith("{"):
            return candidate
    stripped = text.strip()
    start = stripped.find("{")
    if start == -1:
        return None
    # Walk the braces so a nested object does not truncate the match, ignoring
    # braces inside strings.
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : index + 1]
    return None


def parse_ai_decision(text: str) -> ParseResult:
    payload_text = extract_json_object(text)
    if payload_text is None:
        return ParseResult(
            decision=hold_decision(FALLBACK_REASON),
            ok=False,
            error="no JSON object found in response",
            fallback_used=True,
        )
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        return ParseResult(
            decision=hold_decision(FALLBACK_REASON),
            ok=False,
            error=f"invalid JSON: {exc}",
            fallback_used=True,
        )
    if not isinstance(payload, dict):
        return ParseResult(
            decision=hold_decision(FALLBACK_REASON),
            ok=False,
            error=f"expected a JSON object, got {type(payload).__name__}",
            fallback_used=True,
        )
    try:
        decision = AIDecision.model_validate(payload)
    except PydanticValidationError as exc:
        errors = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()[:5]
        )
        return ParseResult(
            decision=hold_decision(FALLBACK_REASON),
            ok=False,
            error=f"schema validation failed: {errors}",
            fallback_used=True,
        )
    return ParseResult(decision=decision, ok=True)

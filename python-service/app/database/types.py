"""Portable column types.

Postgres is the deployment target; SQLite is used for fast tests. These
decorators paper over the two places where that difference would otherwise
cause real bugs:

* SQLite drops timezone information, so a naive datetime would come back and
  silently compare wrong against ``utcnow()`` in staleness/cooldown checks;
* JSON should be ``JSONB`` on Postgres (indexable) but plain JSON elsewhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Numeric
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import JSON, TypeDecorator


class UTCDateTime(TypeDecorator):
    """Timezone-aware datetime that always reads back as UTC."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime, got {type(value)!r}")
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: Any, dialect: Any) -> Any:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


# Money and quantities.
#
# Scale 8 is deliberate, not arbitrary: every monetary value in this system is
# quantized to 8 decimal places by ``round_money`` before it is stored (satoshi
# precision — finer than any spot lot size we trade). Storing at a *finer* scale
# than the values actually carry buys nothing and costs exactness on SQLite,
# which round-trips Decimals through floats: a clean 8-dp value read back at
# scale 12 comes out as 9995.745819870001, and accounting assertions that should
# balance to the cent start failing on dust.
#
# Postgres is exact either way; matching the scale to the contract keeps the test
# backend faithful enough to catch real accounting drift.
Money = Numeric(28, 8, asdecimal=True)

JSONColumn = JSON().with_variant(JSONB, "postgresql")

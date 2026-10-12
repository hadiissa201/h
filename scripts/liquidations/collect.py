"""Record liquidations and what price did afterwards. Read-only, paper only.

Tests the one structural candidate left in docs/FINDINGS.md, under the plan
pre-registered in docs/LIQUIDATION_PLAN.md. A liquidated position is closed by
the exchange at whatever price is available: the seller has no choice, so the
supply is involuntary and involuntary supply overshoots. If the overshoot is
real, price recovers part of it once the forced selling stops.

No API key, no private key, no wallet, no orders. This connects to a public
market-data stream and writes rows to a local SQLite file.

Three design choices that exist because of what this project has already got
wrong:

SQLITE, NOT POSTGRES. The memecoin collector could not be restarted because a
missing N8N_ENCRYPTION_KEY stopped the whole compose stack. A research
collector should have nothing between it and the data.

OUR OWN RECEIPT TIME IS RECORDED, SEPARATELY. We learn of a liquidation after
it happened. The memecoin result turned on exactly this: a signal that keys on
the move it is reacting to captured a median 1.33x of a 14x run. Entry prices
must come from quotes timestamped after the event reached THIS process, and
that is only checkable if both timestamps are stored.

EVERY LIQUIDATION IS KEPT. Including the ones nothing interesting follows.
Filtering to the exciting ones is survivorship bias, and it is what made an
asset-class trend result read +2,271% until the basket was rebuilt.

    python scripts/liquidations/collect.py
    python scripts/liquidations/collect.py --symbols BTCUSDT ETHUSDT --hours 48
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Fixed BEFORE collection starts, per the pre-registration. Tuning these after
# seeing the data is the fitting that document exists to prevent.
MIN_CASCADE_USD = 250_000.0
CASCADE_WINDOW_S = 10.0
# Horizons are all reported, never chosen after the fact.
HORIZONS_S = (30, 60, 300, 900)

STREAM_URL = "wss://fstream.binance.com/stream?streams=!forceOrder@arr"
BOOK_URL = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"

SCHEMA = """
CREATE TABLE IF NOT EXISTS liquidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    -- 'SELL' means a LONG was liquidated: forced selling.
    side TEXT NOT NULL,
    price REAL NOT NULL,
    quantity REAL NOT NULL,
    notional_usd REAL NOT NULL,
    event_ts REAL NOT NULL,      -- exchange clock
    received_ts REAL NOT NULL,   -- our clock, so latency is measurable
    UNIQUE(symbol, side, price, quantity, event_ts)
);
CREATE INDEX IF NOT EXISTS ix_liq_symbol_ts ON liquidations(symbol, event_ts);

CREATE TABLE IF NOT EXISTS cascades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    total_usd REAL NOT NULL,
    events INTEGER NOT NULL,
    first_event_ts REAL NOT NULL,
    last_event_ts REAL NOT NULL,
    detected_ts REAL NOT NULL,
    min_cascade_usd REAL NOT NULL,
    window_s REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cascade_symbol ON cascades(symbol, detected_ts);

CREATE TABLE IF NOT EXISTS quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cascade_id INTEGER,          -- NULL for the random control series
    symbol TEXT NOT NULL,
    -- seconds after the cascade was detected; 0 is the entry quote
    offset_s REAL NOT NULL,
    bid REAL,
    ask REAL,
    quoted_ts REAL NOT NULL,
    FOREIGN KEY(cascade_id) REFERENCES cascades(id)
);
CREATE INDEX IF NOT EXISTS ix_quote_cascade ON quotes(cascade_id, offset_s);
"""


@dataclass
class Burst:
    """Liquidations accumulating in one direction, not yet a cascade."""
    symbol: str
    side: str
    total_usd: float = 0.0
    events: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def parse_force_order(payload: dict) -> dict | None:
    """One liquidation from Binance's !forceOrder@arr message.

    Returns None rather than raising on anything unexpected: a malformed
    message should cost one event, not the run. The memecoin collector lost a
    60-sample run to a single unhandled field shape.
    """
    order = (payload.get("data") or payload).get("o") or {}
    try:
        price = float(order["ap"] or order["p"])
        quantity = float(order["q"])
        symbol = str(order["s"])
        side = str(order["S"])
        event_ms = float(order.get("T") or order.get("E") or 0.0)
    except (KeyError, TypeError, ValueError):
        return None
    if price <= 0 or quantity <= 0 or not symbol or event_ms <= 0:
        return None
    return {
        "symbol": symbol,
        "side": side,
        "price": price,
        "quantity": quantity,
        "notional_usd": price * quantity,
        "event_ts": event_ms / 1000.0,
        "received_ts": time.time(),
    }


def update_burst(burst: Burst | None, liq: dict, window_s: float) -> Burst:
    """Add a liquidation to the running burst, restarting it when stale.

    A burst restarts when the direction changes or the window has elapsed.
    Without the restart, a slow trickle over an hour would eventually sum past
    the threshold and be recorded as a cascade, which is not what the word
    means and would fill the sample with non-events.
    """
    if (burst is None or burst.side != liq["side"]
            or liq["event_ts"] - burst.first_ts > window_s):
        burst = Burst(symbol=liq["symbol"], side=liq["side"],
                      first_ts=liq["event_ts"], last_ts=liq["event_ts"])
    burst.total_usd += liq["notional_usd"]
    burst.events += 1
    burst.last_ts = liq["event_ts"]
    return burst


def is_cascade(burst: Burst, min_usd: float) -> bool:
    return burst.total_usd >= min_usd


def trade_direction(side: str) -> str:
    """What the candidate trade would be.

    Binance reports the side of the LIQUIDATION order. A liquidated long is
    closed by selling, so side 'SELL' is forced selling and the candidate is
    to BUY into it. Getting this backwards would invert every return, and the
    sign would look like a finding.
    """
    return "BUY" if side.upper() == "SELL" else "SELL"


async def fetch_quote(client, symbol: str) -> dict | None:  # noqa: ANN001
    try:
        resp = await client.get(BOOK_URL, params={"symbol": symbol},
                                timeout=10.0)
        body = resp.json()
        bid, ask = float(body["bidPrice"]), float(body["askPrice"])
        if bid <= 0 or ask <= 0:
            return None
        return {"bid": bid, "ask": ask, "quoted_ts": time.time()}
    except Exception:  # noqa: BLE001
        return None


async def follow_up(conn: sqlite3.Connection, client, cascade_id: int,  # noqa: ANN001
                    symbol: str, detected_ts: float) -> None:
    """Record the entry quote and each horizon quote for one cascade."""
    for offset in (0.0, *HORIZONS_S):
        wait = detected_ts + offset - time.time()
        if wait > 0:
            await asyncio.sleep(wait)
        quote = await fetch_quote(client, symbol)
        if quote is None:
            continue
        conn.execute(
            "INSERT INTO quotes (cascade_id, symbol, offset_s, bid, ask, "
            "quoted_ts) VALUES (?, ?, ?, ?, ?, ?)",
            (cascade_id, symbol, offset, quote["bid"], quote["ask"],
             quote["quoted_ts"]))
        conn.commit()


async def run(conn: sqlite3.Connection, symbols: set[str],
              hours: float, min_usd: float, window_s: float) -> int:
    import httpx
    import websockets

    deadline = time.time() + hours * 3600 if hours > 0 else float("inf")
    bursts: dict[tuple[str, str], Burst] = {}
    recorded = cascades = 0
    tasks: set[asyncio.Task] = set()

    print(f"streaming liquidations, cascade = ${min_usd:,.0f} in "
          f"{window_s:.0f}s")
    print(f"symbols: {', '.join(sorted(symbols)) if symbols else 'ALL'}")
    print(f"horizons: {', '.join(str(h) + 's' for h in HORIZONS_S)}\n")

    async with httpx.AsyncClient() as client:
        while time.time() < deadline:
            try:
                async with websockets.connect(STREAM_URL,
                                              ping_interval=20) as ws:
                    while time.time() < deadline:
                        raw = await asyncio.wait_for(ws.recv(), timeout=120)
                        try:
                            payload = json.loads(raw)
                        except ValueError:
                            continue
                        liq = parse_force_order(payload)
                        if liq is None:
                            continue
                        if symbols and liq["symbol"] not in symbols:
                            continue

                        conn.execute(
                            "INSERT OR IGNORE INTO liquidations (symbol, side,"
                            " price, quantity, notional_usd, event_ts,"
                            " received_ts) VALUES (?,?,?,?,?,?,?)",
                            (liq["symbol"], liq["side"], liq["price"],
                             liq["quantity"], liq["notional_usd"],
                             liq["event_ts"], liq["received_ts"]))
                        conn.commit()
                        recorded += 1

                        key = (liq["symbol"], liq["side"])
                        burst = update_burst(bursts.get(key), liq, window_s)
                        bursts[key] = burst
                        if not is_cascade(burst, min_usd):
                            if recorded % 50 == 0:
                                print(f"  {recorded} liquidations, "
                                      f"{cascades} cascades")
                            continue

                        detected = time.time()
                        cursor = conn.execute(
                            "INSERT INTO cascades (symbol, side, total_usd,"
                            " events, first_event_ts, last_event_ts,"
                            " detected_ts, min_cascade_usd, window_s)"
                            " VALUES (?,?,?,?,?,?,?,?,?)",
                            (burst.symbol, burst.side, burst.total_usd,
                             burst.events, burst.first_ts, burst.last_ts,
                             detected, min_usd, window_s))
                        conn.commit()
                        cascades += 1
                        latency = detected - burst.last_ts
                        print(f"  CASCADE {cascades}: {burst.symbol} "
                              f"${burst.total_usd:,.0f} forced "
                              f"{'selling' if burst.side == 'SELL' else 'buying'}"
                              f" over {burst.events} orders, "
                              f"trade {trade_direction(burst.side)}, "
                              f"latency {latency:.2f}s")
                        task = asyncio.create_task(
                            follow_up(conn, client, cursor.lastrowid,
                                      burst.symbol, detected))
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                        bursts.pop(key, None)
            except asyncio.TimeoutError:
                print("  stream quiet for 120s, reconnecting")
            except Exception as exc:  # noqa: BLE001
                print(f"  stream error, reconnecting: {type(exc).__name__}: {exc}")
                await asyncio.sleep(5)

        for task in list(tasks):
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=max(HORIZONS_S) + 30)

    print(f"\n{recorded} liquidations recorded, {cascades} cascades")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/liquidations.sqlite")
    parser.add_argument("--symbols", nargs="*",
                        default=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                        help="empty for every symbol Binance streams")
    parser.add_argument("--hours", type=float, default=0.0,
                        help="stop after this many hours; 0 runs until Ctrl+C")
    parser.add_argument("--min-cascade-usd", type=float,
                        default=MIN_CASCADE_USD)
    parser.add_argument("--window", type=float, default=CASCADE_WINDOW_S)
    args = parser.parse_args()

    conn = connect(Path(args.db))

    def quiet(loop, context):  # noqa: ANN001, ARG001
        """One line per library-internal failure, not a traceback.

        A dropped websocket raises inside the library's own callback, outside
        any try we own, and asyncio prints the full traceback. Over days of
        running that buries the cascades. Still reported, just not shouted.
        """
        exc = context.get("exception")
        print(f"  (socket: {type(exc).__name__ if exc else context.get('message')})")

    async def main_async() -> int:
        asyncio.get_running_loop().set_exception_handler(quiet)
        return await run(conn, set(args.symbols), args.hours,
                         args.min_cascade_usd, args.window)

    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nstopped. Rows are committed as they arrive, so nothing is lost.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())

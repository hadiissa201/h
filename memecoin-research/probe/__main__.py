"""Verify every external service this research depends on, from the machine
that will actually run the collector.

Answers, with evidence rather than assumption:
  - which endpoints are live right now, and at which URL
  - what authentication each one needs
  - what the real rate limits are
  - whether read-only exit simulation actually works without a wallet
  - which launchpad program ids are current
  - the ACTUAL launch rate, which decides full collection vs sampling

Read-only throughout. No private key is used, generated, or accepted, and no
transaction is ever signed or sent.

    python -m probe --ws-seconds 120
    python -m probe --helius-key YOUR_KEY --ws-seconds 300
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import httpx

from probe.checks_http import (
    check_dexscreener,
    measure_sustained_rate,
    resolve_mint_from_signature,
    check_helius,
    check_jupiter_illiquid,
    check_jupiter_quote,
    check_jupiter_swap_build,
    check_rpc,
    check_rpc_simulate,
    measure_rate_limit,
)
from probe.checks_ws import check_ws_reachable, run_launchpad_probe
from probe.constants import (
    DEXSCREENER_BASE,
    HELIUS_RPC_TEMPLATE,
    HELIUS_WS_TEMPLATE,
    PUBLIC_RPC,
    PUBLIC_WS,
    WSOL_MINT,
)
from probe.report import Outcome, Report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--helius-key", default=os.environ.get("HELIUS_API_KEY"),
                        help="Helius API key (or set HELIUS_API_KEY)")
    parser.add_argument("--rpc-url", default=None,
                        help="Solana RPC for mint resolution (default: Helius if a "
                             "key is set, else the public endpoint)")
    parser.add_argument("--ws-seconds", type=float, default=120.0,
                        help="how long to listen for launches (longer = better rate estimate)")
    parser.add_argument("--rate-burst", type=int, default=25,
                        help="requests used to probe each rate limit")
    parser.add_argument("--sustained-seconds", type=float, default=60.0,
                        help="how long to hold a steady rate (0 to skip). A burst "
                             "says nothing about a per-minute limit.")
    parser.add_argument("--sustained-rps", type=float, default=5.0,
                        help="target rate for the sustained test")
    parser.add_argument("--test-mint", default=None,
                        help="a real low-liquidity mint to quote (proves the "
                             "no-route path, which is the 'cannot sell' signal)")
    parser.add_argument("--skip-ws", action="store_true")
    parser.add_argument("--out", default="probe_report.json")
    args = parser.parse_args()

    # Helius when we have a key: the public endpoint drops messages under load,
    # which showed up as a LOWER measured launch rate rather than as an error.
    rpc_url = args.rpc_url or (
        HELIUS_RPC_TEMPLATE.format(key=args.helius_key)
        if args.helius_key else PUBLIC_RPC)

    report = Report()
    print("=" * 78)
    print("EXTERNAL SERVICE VERIFICATION -- read-only, no wallet, no transactions")
    print("=" * 78)

    with httpx.Client(follow_redirects=True,
                      headers={"User-Agent": "memecoin-research-probe/0.1"}) as client:

        print("\n[1/6] Solana RPC (public)")
        check_rpc(report, client, PUBLIC_RPC, "solana-rpc")
        check_rpc_simulate(report, client, PUBLIC_RPC, "solana-rpc")

        print("\n[2/6] Helius")
        check_helius(report, client, args.helius_key)

        print("\n[3/6] DexScreener")
        working = check_dexscreener(report, client)
        if working:
            measure_rate_limit(report, client, "dexscreener",
                               f"{DEXSCREENER_BASE}/latest/dex/tokens/{WSOL_MINT}",
                               args.rate_burst)

        print("\n[4/6] Launchpad detection + REAL launch rate")
        # Runs BEFORE Jupiter so a genuinely fresh, thin mint is available to
        # quote. Quoting SOL/USDC proves routing works; it proves nothing about
        # whether a two-minute-old memecoin can be sold, which is the question.
        fresh_mint = args.test_mint
        if args.skip_ws:
            print("  (skipped)")
        else:
            ws_url = HELIUS_WS_TEMPLATE.format(key=args.helius_key) \
                if args.helius_key else PUBLIC_WS
            if check_ws_reachable(report, ws_url, "websocket"):
                signatures = run_launchpad_probe(report, ws_url, args.ws_seconds)
                if signatures and not fresh_mint:
                    fresh_mint = resolve_mint_from_signature(
                        report, client, rpc_url, signatures)

        print("\n[5/6] Jupiter (the exit-simulation path)")
        quote_url = check_jupiter_quote(report, client)
        if quote_url:
            check_jupiter_swap_build(report, client, quote_url)
            check_jupiter_illiquid(report, client, quote_url, fresh_mint)
            measure_rate_limit(report, client, "jupiter",
                               f"{quote_url}?inputMint={WSOL_MINT}"
                               f"&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                               f"&amount=100000000&slippageBps=100",
                               args.rate_burst)

        print("\n[6/6] Sustained rate (the number that sizes the collector)")
        if args.sustained_seconds <= 0:
            print("  (skipped)")
        else:
            if working:
                measure_sustained_rate(
                    report, client, "dexscreener",
                    f"{DEXSCREENER_BASE}/latest/dex/tokens/{WSOL_MINT}",
                    args.sustained_rps, args.sustained_seconds)
            if quote_url:
                measure_sustained_rate(
                    report, client, "jupiter",
                    f"{quote_url}?inputMint={WSOL_MINT}"
                    f"&outputMint=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
                    f"&amount=100000000&slippageBps=100",
                    args.sustained_rps, args.sustained_seconds)

    # ------------------------------------------------------------------ verdict
    counts = report.counts()
    print("\n" + "=" * 78)
    print(f"RESULT  pass={counts['OK']}  fail={counts['FAILED']}  "
          f"unverified={counts['UNVERIFIED']}")
    print("=" * 78)

    if report.failures():
        print("\nFAILED -- these block the collector:")
        for check in report.failures():
            print(f"  - {check.service}/{check.name}: {check.detail}")
    if report.unverified():
        print("\nUNVERIFIED -- could not be checked (NOT the same as broken):")
        for check in report.unverified():
            print(f"  - {check.service}/{check.name}: {check.detail}")

    rate = next((c for c in report.checks if c.name == "MEASURED LAUNCH RATE"), None)
    if rate and rate.outcome is Outcome.OK:
        per_day = rate.evidence.get("projected_per_day", 0)
        print(f"\nLAUNCH RATE: ~{per_day:,}/day measured over "
              f"{rate.evidence.get('window_seconds')}s.")
        print("Short windows are noisy -- re-run at a different hour before "
              "treating this as the real rate.")

    out = Path(args.out)
    out.write_text(report.to_json())
    print(f"\nFull evidence written to {out.resolve()}")
    print("Send me that file and I will design the collector around what it says.")
    return 1 if report.failures() else 0


if __name__ == "__main__":
    sys.exit(main())

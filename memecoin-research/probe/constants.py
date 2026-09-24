"""Known addresses and candidate endpoints.

EVERYTHING in this file is a CANDIDATE until the probe confirms it against the
live network. Endpoints move (Jupiter's quote API has relocated more than once)
and launchpad programs get redeployed. The probe tries every candidate and
reports which ones actually answered, so a stale value here shows up as a
failed check rather than as silently missing data.

Nothing here is treated as fact by the collector. The collector reads whatever
the probe confirmed.
"""

from __future__ import annotations

# --------------------------------------------------------------- known mints
# These two are stable and safe to hard-code: wrapped SOL and Circle's USDC.
# They are the quote legs for every simulated sell.
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# A burner public key used only for building a transaction we never sign and
# never send. It is a PUBLIC key with no matching private key in this codebase.
# Simulation needs *a* fee payer address; it does not need to be one we control.
SIMULATION_FEE_PAYER = "11111111111111111111111111111111"

# ---------------------------------------------------------- Jupiter endpoints
# Jupiter has served quotes from several hosts. Probe all of them; the collector
# uses whichever the probe confirms, rather than a URL baked in from memory.
# quote-api.jup.ag was removed on 2026-09-24: DNS no longer resolves it
# (getaddrinfo failed), measured by the probe rather than assumed.
JUPITER_QUOTE_CANDIDATES = (
    ("lite-v1", "https://lite-api.jup.ag/swap/v1/quote"),
    ("api-v1", "https://api.jup.ag/swap/v1/quote"),
)
JUPITER_SWAP_CANDIDATES = (
    ("lite-v1", "https://lite-api.jup.ag/swap/v1/swap"),
    ("api-v1", "https://api.jup.ag/swap/v1/swap"),
)

# ------------------------------------------------------ DexScreener endpoints
DEXSCREENER_BASE = "https://api.dexscreener.com"
DEXSCREENER_CHECKS = (
    ("token-pairs", "/token-pairs/v1/solana/{mint}"),
    ("tokens-latest", "/latest/dex/tokens/{mint}"),
    ("search", "/latest/dex/search?q={mint}"),
    ("token-profiles", "/token-profiles/latest/v1"),
)

# ------------------------------------------------------------------- Solana
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
HELIUS_RPC_TEMPLATE = "https://mainnet.helius-rpc.com/?api-key={key}"
HELIUS_WS_TEMPLATE = "wss://mainnet.helius-rpc.com/?api-key={key}"
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"

# ------------------------------------------------------- launchpad programs
# CANDIDATES ONLY. The probe subscribes to each and counts real events; a
# program that produces zero events over the listen window is reported as
# unconfirmed, which is the signal to go find the current address. Do not
# assume any of these is correct because it is written down here.
LAUNCHPAD_CANDIDATES = (
    ("pumpfun-bonding-curve", "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"),
    ("pumpswap-amm", "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"),
    ("raydium-launchlab", "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj"),
    ("meteora-dbc", "dbcij3LWUppWqq96dh6gJWwBifmcGfLSB5D4DuSMaqN"),
)

# Token program IDs, used to tell a Token-2022 mint (transfer hooks, transfer
# fees) from a classic SPL mint. Both are long-standing and stable.
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

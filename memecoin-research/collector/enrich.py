"""Facts about WHO launched a token, rather than what its price is doing.

Everything the paper strategies have used so far -- liquidity, age, volume,
buy counts -- is visible on every screen in the market. A threshold on public
information cannot be an edge, which is consistent with what we measured: the
control, which applies no filter at all, beat every filtered strategy.

This module collects a different class of fact:

  - the deployer's address, and what happened to the tokens they launched before
  - whether the mint authority is still live (they can print more supply)
  - whether the freeze authority is still live (they can freeze YOUR account)

These describe the intent of the person on the other side of the trade. They
are not priced in the way liquidity is, because reading them takes work most
buyers do not do.

That is a hypothesis, not a promise. It has to beat a base rate of about -22%
before it is worth anything, and this module only makes it MEASURABLE.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collector.models import Creator, Token, TokenStatus
from probe.constants import TOKEN_2022_PROGRAM

log = logging.getLogger("collector.enrich")


@dataclass
class MintFacts:
    mint_authority: str | None = None
    freeze_authority: str | None = None
    decimals: int | None = None
    is_token2022: bool | None = None
    supply: str | None = None
    error: str | None = None

    @property
    def can_mint_more(self) -> bool:
        """A live mint authority means the supply you bought into is not fixed."""
        return self.mint_authority is not None

    @property
    def can_freeze_you(self) -> bool:
        """A live freeze authority means they can stop YOU from selling.

        This is the cleanest structural red flag on Solana: it is the rough
        equivalent of an EVM honeypot, except it is declared on-chain and
        anyone willing to make one RPC call can see it.
        """
        return self.freeze_authority is not None


def fetch_mint_facts(client: httpx.Client, rpc_url: str, mint: str,
                     timeout: float = 20.0) -> MintFacts:
    """Read the mint account. One RPC call, no wallet, nothing signed."""
    try:
        resp = client.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getAccountInfo",
            "params": [mint, {"encoding": "jsonParsed"}],
        }, timeout=timeout)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        return MintFacts(error=f"{type(exc).__name__}: {exc}")

    if "error" in body:
        return MintFacts(error=str(body["error"])[:200])
    value = ((body.get("result") or {}).get("value")) or {}
    if not value:
        return MintFacts(error="mint account not found")

    parsed = ((value.get("data") or {}).get("parsed") or {}).get("info") or {}
    owner = value.get("owner")
    return MintFacts(
        # jsonParsed gives null when an authority has been revoked, which is
        # exactly the distinction we care about.
        mint_authority=parsed.get("mintAuthority"),
        freeze_authority=parsed.get("freezeAuthority"),
        decimals=parsed.get("decimals"),
        is_token2022=(owner == TOKEN_2022_PROGRAM),
        supply=str(parsed.get("supply")) if parsed.get("supply") else None,
    )


def fetch_creator(client: httpx.Client, rpc_url: str, signature: str,
                  timeout: float = 20.0) -> str | None:
    """The fee payer of the creation transaction -- in practice, the deployer.

    Not infallible: a launchpad relayer can pay on someone else's behalf. So
    this is recorded as an attribution, and a wallet that turns out to be a
    shared relayer will show up as one with an implausible number of launches.
    """
    try:
        resp = client.post(rpc_url, json={
            "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
            "params": [signature, {"encoding": "jsonParsed",
                                   "maxSupportedTransactionVersion": 0}],
        }, timeout=timeout)
        result = (resp.json() or {}).get("result") or {}
    except Exception as exc:  # noqa: BLE001
        log.debug("creator lookup failed: %s", exc)
        return None

    keys = ((result.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    for key in keys:
        if isinstance(key, dict) and key.get("signer") and key.get("writable"):
            return key.get("pubkey")
    return keys[0].get("pubkey") if keys and isinstance(keys[0], dict) else None


def record_creator(session: Session, chain: str, address: str) -> Creator:
    """Upsert the deployer and bump their launch count."""
    creator = session.scalar(
        select(Creator).where(Creator.chain == chain, Creator.address == address))
    if creator is None:
        creator = Creator(chain=chain, address=address,
                          first_seen_ts=datetime.now(UTC), tokens_launched=0)
        session.add(creator)
        session.flush()
    creator.tokens_launched = (creator.tokens_launched or 0) + 1
    creator.last_refreshed_ts = datetime.now(UTC)
    return creator


@dataclass
class CreatorHistory:
    """What happened to this wallet's PREVIOUS tokens.

    The whole point: a deployer whose last ten tokens all died is telling you
    something about the eleventh that its price chart cannot.
    """
    address: str
    tokens_launched: int = 0
    prior_tokens: int = 0
    prior_dead: int = 0
    prior_still_tradable: int = 0

    @property
    def death_rate(self) -> float | None:
        """None when there is no history. None is NOT zero, and a new deployer
        must never be scored as if they had a clean record."""
        if self.prior_tokens == 0:
            return None
        return self.prior_dead / self.prior_tokens


def creator_history(session: Session, chain: str, address: str,
                    before_token_id: int | None = None) -> CreatorHistory:
    """History strictly BEFORE this token, so a strategy cannot see the future.

    Counting the current token's own fate would be look-ahead of the worst
    kind: the filter would "predict" rugs using the rug it is predicting.
    """
    history = CreatorHistory(address=address)
    creator = session.scalar(
        select(Creator).where(Creator.chain == chain, Creator.address == address))
    if creator is None:
        return history
    history.tokens_launched = creator.tokens_launched or 0

    query = select(Token.id).where(Token.chain == chain,
                                   Token.creator_address == address)
    if before_token_id is not None:
        query = query.where(Token.id < before_token_id)
    prior_ids = list(session.scalars(query).all())
    history.prior_tokens = len(prior_ids)
    if not prior_ids:
        return history

    history.prior_dead = session.scalar(
        select(func.count()).select_from(TokenStatus)
        .where(TokenStatus.token_id.in_(prior_ids),
               TokenStatus.retired_ts.is_not(None))) or 0
    history.prior_still_tradable = session.scalar(
        select(func.count()).select_from(TokenStatus)
        .where(TokenStatus.token_id.in_(prior_ids),
               TokenStatus.is_tradable.is_(True))) or 0
    return history


def enrich_token(session: Session, client: httpx.Client, rpc_url: str,
                 token: Token, creation_signature: str | None) -> bool:
    """Fill in the structural facts. Returns True if anything was learned."""
    learned = False

    facts = fetch_mint_facts(client, rpc_url, token.address)
    if facts.error is None:
        token.mint_authority = facts.mint_authority
        token.freeze_authority = facts.freeze_authority
        token.is_token2022 = facts.is_token2022
        if facts.decimals is not None:
            token.decimals = facts.decimals
        learned = True
    else:
        log.debug("mint facts unavailable for %s: %s", token.address, facts.error)

    if creation_signature and not token.creator_address:
        creator = fetch_creator(client, rpc_url, creation_signature)
        if creator:
            token.creator_address = creator
            record_creator(session, token.chain, creator)
            learned = True
    return learned

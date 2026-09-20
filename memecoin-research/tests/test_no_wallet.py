"""Structural proof that this package cannot spend money.

The trading system keeps live trading behind a three-switch interlock. This
research package takes the stronger position: there is no switch at all,
because there is no code path that could sign or send a transaction.

These tests fail the build if anyone ever adds one. That is the point -- a
comment saying "read-only" is a promise; a failing test is a guarantee.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_DIRS = ("poc", "probe")

# Libraries that can produce a signature or submit a transaction. None of them
# belongs in a package whose entire job is to observe.
FORBIDDEN_IMPORTS = {
    "solders", "solana", "nacl", "ed25519", "bip_utils", "mnemonic",
    "eth_account", "web3", "bitcoinlib", "hdwallet",
}

# Substrings that would indicate a spending path even without those imports.
FORBIDDEN_CALLS = (
    "sendtransaction", "send_transaction", "sendrawtransaction",
    "signtransaction", "sign_transaction", "partial_sign", "from_secret_key",
    "keypair(", "from_private_key", "sendandconfirm",
)

# Names suggesting a secret is being read in. A research collector needs
# API keys, never a key that controls funds.
FORBIDDEN_SECRET_NAMES = (
    "private_key", "privatekey", "secret_key", "secretkey",
    "seed_phrase", "seedphrase", "mnemonic", "wallet_key",
)


def source_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for directory in SOURCE_DIRS:
        files.extend(sorted((ROOT / directory).rglob("*.py")))
    return files


def test_there_are_source_files_to_check():
    """Guards against the suite passing because it scanned nothing."""
    assert len(source_files()) >= 5


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_signing_library_is_imported(path: pathlib.Path):
    tree = ast.parse(path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    offending = imported & FORBIDDEN_IMPORTS
    assert not offending, f"{path.name} imports signing library: {offending}"


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_transaction_is_signed_or_sent(path: pathlib.Path):
    """`simulateTransaction` is allowed. Anything that submits one is not."""
    lowered = path.read_text().lower()
    # Strip the one legitimate near-match so it cannot mask a real hit.
    lowered = lowered.replace("simulatetransaction", "").replace(
        "simulate_transaction", "")
    hits = [needle for needle in FORBIDDEN_CALLS if needle in lowered]
    assert not hits, f"{path.name} contains a spending path: {hits}"


@pytest.mark.parametrize("path", source_files(), ids=lambda p: p.name)
def test_no_spendable_secret_is_read(path: pathlib.Path):
    text = path.read_text().lower()
    hits = [name for name in FORBIDDEN_SECRET_NAMES if name in text]
    assert not hits, f"{path.name} references a spendable secret: {hits}"


def test_the_simulation_fee_payer_is_not_an_address_we_control():
    """Simulation needs *a* fee payer. It must not be a wallet of ours.

    The system program id is used deliberately: it is a program-owned address
    with no private key in existence, so it cannot be funded or drained.
    """
    from probe.constants import SIMULATION_FEE_PAYER

    assert SIMULATION_FEE_PAYER == "11111111111111111111111111111111"


def test_rpc_simulation_never_verifies_signatures():
    """sigVerify=False is what lets us simulate with no signature at all.

    If this ever flipped to True the code would need a real signature, which
    would mean a real key. Pinning it keeps that door shut.
    """
    text = (ROOT / "poc" / "sources.py").read_text()
    assert '"sigVerify": False' in text
    assert '"sigVerify": True' not in text

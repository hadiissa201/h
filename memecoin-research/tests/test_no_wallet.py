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


# ------------------------------------------------------------ secret hygiene
def test_an_api_key_in_a_url_is_never_recorded():
    """probe_report.json is meant to be shared, so it must carry no credentials.

    Helius puts the key in the query string, so a recorded endpoint would leak
    a live credential into a file the user is asked to paste into a chat.
    """
    from probe.report import Check, Outcome, Report, redact

    secret = "abcd1234-dead-beef-0000-feedfacecafe"
    url = f"https://mainnet.helius-rpc.com/?api-key={secret}"

    assert secret not in redact(url)
    assert "REDACTED" in redact(url)

    report = Report()
    report.add(Check("helius", "getHealth", Outcome.OK, f"called {url}", url))
    serialized = report.to_json()
    assert secret not in serialized, "the key reached the report file"
    assert "helius-rpc.com" in serialized, "redaction destroyed the useful part"


def test_redaction_keeps_a_tail_so_two_keys_can_be_told_apart():
    from probe.report import redact

    a = redact("https://x/?api-key=aaaaaaaaaaaaaaaa1111")
    b = redact("https://x/?api-key=bbbbbbbbbbbbbbbb2222")
    assert a != b
    assert "1111" in a and "2222" in b


def test_a_short_key_is_redacted_without_revealing_a_tail():
    """A short value is mostly tail, so showing any of it gives too much away."""
    from probe.report import redact

    out = redact("https://x/?api-key=short123")
    assert "short123" not in out
    assert "..." not in out


def test_other_credential_parameter_names_are_covered():
    from probe.report import redact

    for param in ("api_key", "apikey", "token", "access_token"):
        out = redact(f"https://x/?{param}=supersecretvalue99")
        assert "supersecretvalue99" not in out, param


def test_no_source_file_prints_a_url_without_redacting_it():
    """A stored-but-redacted credential still leaks if it is printed raw.

    This is the bug that leaked a live Helius key to the terminal: redaction
    covered Check.endpoint and Check.detail, but a print() of the websocket URL
    bypassed both. Any print of a variable whose name looks like a URL must go
    through redact().
    """
    offenders: list[str] = []
    for path in source_files():
        for number, line in enumerate(path.read_text().splitlines(), 1):
            stripped = line.strip()
            if not stripped.startswith(("print(", 'print(f"')) and "print(" not in stripped:
                continue
            if "_url" not in stripped and "url}" not in stripped:
                continue
            if "redact(" in stripped:
                continue
            offenders.append(f"{path.name}:{number}: {stripped[:100]}")
    assert not offenders, "URL printed without redact():\n" + "\n".join(offenders)


def test_the_websocket_url_specifically_is_redacted_where_it_is_printed():
    from probe.report import redact

    leaked = "wss://mainnet.helius-rpc.com/?api-key=b0db2d45-a39a-4783-ba05-e9af5ca5ebe3"
    assert "b0db2d45" not in redact(leaked)
    source = (ROOT / "probe" / "checks_ws.py").read_text()
    assert "redact(ws_url)" in source

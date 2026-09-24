"""The launch-rate matcher.

An earlier version matched the substring "Instruction: Create", which also
matches `CreateIdempotent` -- the associated-token-account instruction that
fires whenever a NEW BUYER first touches a token. On a bonding curve that is
every buy, so the probe reported ~1,025,280 launches/day: it was counting
buyers, not launches, and would have sized the entire collector against a
number that was 20-35x too high.

These tests pin the distinction so it cannot come back.
"""

from __future__ import annotations

from probe.checks_ws import CREATE_CANDIDATES, INSTRUCTION_RE, TRADING_NOISE


def names_in(logs: list[str]) -> set[str]:
    return {m.group(1) for line in logs if (m := INSTRUCTION_RE.search(line))}


def test_create_and_createidempotent_are_different_instructions():
    """The exact bug that produced a million launches a day."""
    assert INSTRUCTION_RE.search(
        "Program log: Instruction: CreateIdempotent").group(1) == "CreateIdempotent"
    assert INSTRUCTION_RE.search(
        "Program log: Instruction: Create").group(1) == "Create"


def test_a_buy_transaction_is_not_counted_as_a_launch():
    """A first-time buyer's transaction contains CreateIdempotent and MintTo."""
    buy_logs = [
        "Program log: Instruction: CreateIdempotent",
        "Program log: Instruction: InitializeAccount3",
        "Program log: Instruction: Buy",
        "Program log: Instruction: MintTo",
        "Program log: Instruction: TransferChecked",
    ]
    assert not (names_in(buy_logs) & CREATE_CANDIDATES)


def test_an_actual_creation_is_counted():
    create_logs = [
        "Program log: Instruction: Create",
        "Program log: Instruction: InitializeMint2",
        "Program log: Instruction: CreateIdempotent",
    ]
    assert names_in(create_logs) & CREATE_CANDIDATES


def test_the_noisy_instructions_are_classified_as_noise_not_creation():
    for noisy in ("CreateIdempotent", "MintTo", "InitializeAccount3",
                  "Buy", "Sell", "Transfer"):
        assert noisy in TRADING_NOISE, noisy
        assert noisy not in CREATE_CANDIDATES, noisy


def test_the_two_classifications_never_overlap():
    """An instruction counted as both would be counted and discounted at once."""
    assert not (CREATE_CANDIDATES & TRADING_NOISE)


def test_lines_without_an_instruction_are_ignored():
    noise = [
        "Program 6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P invoke [1]",
        "Program log: Create",                    # no 'Instruction:' prefix
        "Program consumed 12345 of 200000 compute units",
        "Program log: AnchorError caused by account: mint",
    ]
    assert names_in(noise) == set()

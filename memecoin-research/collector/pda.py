"""Deriving a program address, in pure Python, so no heavy RPC call is needed.

The bonding curve's address was being found by asking the node for a token's
largest accounts and reading the owner. That works, but getTokenLargestAccounts
is one of the most expensive methods a Solana RPC offers, and a run of forty
tokens exhausted the quota: 110 rate limits with the pacer pinned at its
ceiling, which is a spent budget rather than a pace problem.

The address is deterministic. Deriving it needs an ed25519 on-curve test, which
is why the first version avoided it -- but that test is about forty lines and
removes an entire network round trip per token, halving RPC usage as a side
effect.

No third-party dependency: this runs on a machine where installing one has
repeatedly been the hard part.
"""

from __future__ import annotations

import hashlib

_B58 = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58)}

# ed25519 field and curve constants.
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_MARKER = b"ProgramDerivedAddress"


def b58decode(value: str) -> bytes:
    number = 0
    for char in value.encode():
        if char not in _B58_INDEX:
            raise ValueError(f"not base58: {chr(char)!r}")
        number = number * 58 + _B58_INDEX[char]
    raw = number.to_bytes((number.bit_length() + 7) // 8, "big")
    # Leading '1's are leading zero bytes and survive no other way.
    pad = len(value) - len(value.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(raw: bytes) -> str:
    number = int.from_bytes(raw, "big")
    out = bytearray()
    while number:
        number, rem = divmod(number, 58)
        out.append(_B58[rem])
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return ("1" * pad) + out[::-1].decode()


def is_on_curve(point: bytes) -> bool:
    """Is this 32-byte value a valid ed25519 point?

    A program address must NOT be, which is the entire mechanism keeping PDAs
    outside the space of keys anyone could hold a private key for.
    """
    if len(point) != 32:
        return False
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    sign = point[31] >> 7

    y2 = (y * y) % _P
    numerator = (y2 - 1) % _P
    denominator = (_D * y2 + 1) % _P
    if denominator == 0:
        return False
    x2 = (numerator * pow(denominator, _P - 2, _P)) % _P
    if x2 == 0:
        return sign == 0

    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = (x * pow(2, (_P - 1) // 4, _P)) % _P
    return (x * x - x2) % _P == 0


def find_program_address(seeds: list[bytes], program_id: str) -> tuple[str, int]:
    """The canonical address for these seeds, with its bump.

    Walks the bump down from 255 and takes the first candidate that is off the
    curve, which is what every Solana client does.
    """
    program = b58decode(program_id)
    for bump in range(255, -1, -1):
        digest = hashlib.sha256()
        for seed in seeds:
            digest.update(seed)
        digest.update(bytes([bump]))
        digest.update(program)
        digest.update(_MARKER)
        candidate = digest.digest()
        if not is_on_curve(candidate):
            return b58encode(candidate), bump
    raise ValueError("no off-curve address found, which should be impossible")


def bonding_curve_address(mint: str, program_id: str) -> str:
    """pump.fun derives its curve from the literal 'bonding-curve' and the mint."""
    address, _ = find_program_address([b"bonding-curve", b58decode(mint)],
                                      program_id)
    return address

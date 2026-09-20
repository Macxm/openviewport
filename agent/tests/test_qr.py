"""The QR encoder, checked by reading its output back and by the parity's own arithmetic.

A picture that looks like a QR code is worth nothing if a phone cannot read it, so the tests
here do not compare against a stored image. They walk the matrix back to the text, and they
check the Reed-Solomon codewords the way a decoder does — by evaluating the syndromes, which
are zero only for a valid codeword. That is arithmetic the encoder does not share.
"""

from __future__ import annotations

import pytest

from viewport import qr


# ----- reading the matrix back ------------------------------------------------------

def read_back(grid: list[list[int]]) -> str:
    """A small decoder: format, mask, zigzag, de-interleave, header, bytes."""
    size = len(grid)
    version = (size - 17) // 4
    total, parity, blocks = qr._VERSIONS[version]

    # The format bits, in the copy beside the top-left finder.
    raw = ([grid[8][i] for i in range(6)] + [grid[8][7], grid[8][8], grid[7][8]]
           + [grid[i][8] for i in (5, 4, 3, 2, 1, 0)])
    value = int("".join(str(b) for b in raw), 2) ^ qr._FORMAT_MASK
    ecc, mask = (value >> 13) & 0b11, (value >> 10) & 0b111
    assert ecc == qr._ECC_M, "the format says error correction M"

    reserved = qr._function_patterns(size, version)
    rule = qr._MASKS[mask]
    plain = [[grid[y][x] ^ (1 if reserved[y][x] is None and rule(y, x) else 0)
              for x in range(size)] for y in range(size)]

    # The same walk as the encoder, written out again rather than borrowed.
    bits: list[int] = []
    column, upward = size - 1, True
    while column > 0:
        if column == 6:
            column -= 1
        for row in (range(size - 1, -1, -1) if upward else range(size)):
            for x in (column, column - 1):
                if reserved[row][x] is None:
                    bits.append(plain[row][x])
        upward = not upward
        column -= 2
    codewords = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits) - 7, 8)]

    # Un-interleave: the data codewords were taken one from each block in turn.
    per_block = (total - parity * blocks) // blocks
    groups: list[list[int]] = [[] for _ in range(blocks)]
    for i in range(per_block * blocks):
        groups[i % blocks].append(codewords[i])
    data = [byte for group in groups for byte in group]

    stream = "".join(format(byte, "08b") for byte in data)
    assert stream[:4] == "0100", "byte mode"
    length = int(stream[4:12], 2)
    payload = bytes(int(stream[12 + i * 8:20 + i * 8], 2) for i in range(length))
    return payload.decode()


def syndromes_are_zero(grid: list[list[int]]) -> bool:
    """Every block's codewords, evaluated at the parity's roots, the way a reader checks."""
    size = len(grid)
    version = (size - 17) // 4
    total, parity, blocks = qr._VERSIONS[version]
    codewords = _codewords_from(grid, version)
    per_block = (total - parity * blocks) // blocks

    for block in range(blocks):
        data = [codewords[i * blocks + block] for i in range(per_block)]
        checks = [codewords[per_block * blocks + i * blocks + block] for i in range(parity)]
        whole = data + checks
        for root in range(parity):
            value = 0
            for byte in whole:
                value = qr._mul(value, qr._EXP[root]) ^ byte
            if value != 0:
                return False
    return True


def _codewords_from(grid: list[list[int]], version: int) -> list[int]:
    size = len(grid)
    raw = ([grid[8][i] for i in range(6)] + [grid[8][7], grid[8][8], grid[7][8]]
           + [grid[i][8] for i in (5, 4, 3, 2, 1, 0)])
    mask = ((int("".join(str(b) for b in raw), 2) ^ qr._FORMAT_MASK) >> 10) & 0b111
    reserved = qr._function_patterns(size, version)
    rule = qr._MASKS[mask]
    bits: list[int] = []
    column, upward = size - 1, True
    while column > 0:
        if column == 6:
            column -= 1
        for row in (range(size - 1, -1, -1) if upward else range(size)):
            for x in (column, column - 1):
                if reserved[row][x] is None:
                    bits.append(grid[row][x] ^ (1 if rule(row, x) else 0))
        upward = not upward
        column -= 2
    return [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits) - 7, 8)]


# ----- what it must get right --------------------------------------------------------

CASES = [
    "hi",
    "WIFI:T:WPA;S:OpenViewport-a4f2;P:6Qk2-tuVn8wZ;;",
    "http://openviewport.local:8080/admin?token=0123456789abcdefghij",
    "x" * 100,
    "café — ünicode ✓",                       # bytes, not characters
]


@pytest.mark.parametrize("text", CASES)
def test_what_goes_in_can_be_read_back_out(text):
    assert read_back(qr.encode(text)) == text


@pytest.mark.parametrize("text", CASES)
def test_the_parity_satisfies_a_readers_own_check(text):
    assert syndromes_are_zero(qr.encode(text)), "a reader would find errors in this"


@pytest.mark.parametrize("text,version", [("hi", 1), ("x" * 30, 3), ("x" * 100, 6)])
def test_the_smallest_version_that_fits_is_used(text, version):
    assert len(qr.encode(text)) == 17 + version * 4


def test_more_than_it_can_hold_is_refused_rather_than_cut_short():
    with pytest.raises(qr.TooLong):
        qr.encode("x" * 200)


def test_the_patterns_a_reader_looks_for_are_where_it_looks():
    grid = qr.encode("hi")
    size = len(grid)
    for top, left in ((0, 0), (0, size - 7), (size - 7, 0)):
        assert [grid[top + i][left] for i in range(7)] == [1, 1, 1, 1, 1, 1, 1]
        assert grid[top + 1][left + 1] == 0 and grid[top + 3][left + 3] == 1
    assert [grid[6][i] for i in range(8, 12)] == [1, 0, 1, 0], "the timing line alternates"
    assert grid[size - 8][8] == 1, "the module that is always dark"


def test_the_same_text_always_gives_the_same_code():
    assert qr.encode(CASES[1]) == qr.encode(CASES[1])


# ----- the wifi string ----------------------------------------------------------------

def test_a_wifi_string_says_what_a_phone_expects():
    assert qr.wifi_join("Kitchen", "a good password") == "WIFI:T:WPA;S:Kitchen;P:a good password;;"


def test_an_open_network_has_no_password_in_it():
    assert qr.wifi_join("Guests", "") == "WIFI:T:nopass;S:Guests;;"


def test_the_awkward_characters_are_escaped_not_dropped():
    joined = qr.wifi_join("Bill's; wifi", "pass:word")
    assert r"S:Bill's\; wifi" in joined and r"P:pass\:word" in joined
    assert read_back(qr.encode(joined)) == joined

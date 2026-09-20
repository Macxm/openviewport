"""A QR code, small enough to read from the sofa and written here rather than installed.

The wall has no build step and no dependencies, and the agent's are hash-pinned, so a library
for this would be a new supply-chain edge for something that is, in the end, a few hundred
lines of arithmetic. This module produces the matrix; the page draws squares.

Scope is deliberately narrow: **byte mode, error correction M, versions 1 to 6**. That covers a
wifi join string and a URL with a token, which is all this is for. Versions 1 to 6 have one
block size per version and no version information block, so the fiddliest parts of the
specification are not needed. Anything longer is refused rather than silently cut short.

ISO/IEC 18004. The pieces are: the data bitstream, Reed-Solomon parity over GF(256), the
function patterns, a zigzag walk that fills what is left, and eight masks of which the
least ugly wins.
"""

from __future__ import annotations

# Per version (index 1-6), at error correction M: total codewords, parity codewords per block,
# and how many blocks. Data codewords are what is left, split evenly between the blocks.
_VERSIONS = {
    1: (26, 10, 1),
    2: (44, 16, 1),
    3: (70, 26, 1),
    4: (100, 18, 2),
    5: (134, 24, 2),
    6: (172, 16, 4),
}
# Where the alignment patterns sit, by version. Version 1 has none.
_ALIGNMENT = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34]}
_ECC_M = 0b00                        # the two bits that say "error correction M"
_FORMAT_POLY = 0b101_0011_0111       # BCH(15, 5) generator
_FORMAT_MASK = 0b101_0100_0001_0010  # applied so the format never reads as all zeroes


class TooLong(ValueError):
    """More than this encoder's largest version holds."""


# ----- GF(256), the field Reed-Solomon works in ---------------------------------------

_EXP = [0] * 512
_LOG = [0] * 256


def _build_tables() -> None:
    x = 1
    for i in range(255):
        _EXP[i] = x
        _LOG[x] = i
        x <<= 1
        if x & 0x100:                # the field's polynomial, x^8 + x^4 + x^3 + x^2 + 1
            x ^= 0x11D
    for i in range(255, 512):
        _EXP[i] = _EXP[i - 255]


_build_tables()


def _mul(a: int, b: int) -> int:
    return 0 if a == 0 or b == 0 else _EXP[_LOG[a] + _LOG[b]]


def _generator(degree: int) -> list[int]:
    """The polynomial whose roots are the first `degree` powers of 2."""
    poly = [1]
    for i in range(degree):
        poly.append(0)
        for j in range(len(poly) - 1, 0, -1):
            poly[j] ^= _mul(poly[j - 1], _EXP[i])
    return poly


def _parity(data: list[int], count: int) -> list[int]:
    generator = _generator(count)
    remainder = [0] * count
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        for i in range(count):
            remainder[i] ^= _mul(generator[i + 1], factor)
    return remainder


# ----- the bitstream -------------------------------------------------------------------

def _choose_version(length: int) -> int:
    for version, (total, parity, blocks) in _VERSIONS.items():
        data_codewords = total - parity * blocks
        if length + 2 <= data_codewords:          # 4 bits of mode + 8 of length + 4 terminator
            return version
    raise TooLong(f"{length} bytes is more than a version 6 code holds")


def _codewords(text: str, version: int) -> list[int]:
    total, parity, blocks = _VERSIONS[version]
    capacity = total - parity * blocks
    payload = text.encode()
    bits = "0100" + format(len(payload), "08b") + "".join(format(b, "08b") for b in payload)
    bits += "0000"[:max(0, capacity * 8 - len(bits))]          # terminator, if it fits
    bits += "0" * (-len(bits) % 8)                             # up to a whole codeword
    data = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    for pad in (0xEC, 0x11) * capacity:                        # the specified filler
        if len(data) >= capacity:
            break
        data.append(pad)

    per_block = capacity // blocks
    groups = [data[i * per_block:(i + 1) * per_block] for i in range(blocks)]
    checks = [_parity(group, parity) for group in groups]
    # Interleaved: the first codeword of every block, then the second, and so on.
    out = [group[i] for i in range(per_block) for group in groups]
    out += [check[i] for i in range(parity) for check in checks]
    return out


# ----- the matrix -----------------------------------------------------------------------

def _finder(grid: list[list[int | None]], top: int, left: int) -> None:
    for dy in range(-1, 8):
        for dx in range(-1, 8):
            y, x = top + dy, left + dx
            if not (0 <= y < len(grid) and 0 <= x < len(grid)):
                continue
            ring = max(abs(dy - 3), abs(dx - 3))
            grid[y][x] = 1 if ring in (0, 1, 3) and 0 <= dy <= 6 and 0 <= dx <= 6 else 0


def _function_patterns(size: int, version: int) -> list[list[int | None]]:
    grid: list[list[int | None]] = [[None] * size for _ in range(size)]
    _finder(grid, 0, 0)
    _finder(grid, 0, size - 7)
    _finder(grid, size - 7, 0)
    for i in range(size):                                      # timing
        if grid[6][i] is None:
            grid[6][i] = int(i % 2 == 0)
        if grid[i][6] is None:
            grid[i][6] = int(i % 2 == 0)
    centres = _ALIGNMENT[version]
    for y in centres:
        for x in centres:
            if (y, x) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue                                       # would sit on a finder
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    grid[y + dy][x + dx] = int(max(abs(dy), abs(dx)) != 1)
    for i in range(9):                                         # reserved for the format bits
        if grid[8][i] is None:
            grid[8][i] = 0
        if grid[i][8] is None:
            grid[i][8] = 0
    for i in range(8):
        grid[8][size - 1 - i] = 0
        grid[size - 1 - i][8] = 0
    grid[size - 8][8] = 1                                      # the one module always dark
    return grid


def _place(grid: list[list[int | None]], codewords: list[int]) -> list[list[int | None]]:
    """Fill what the function patterns left, two columns at a time, bottom to top and back."""
    size = len(grid)
    bits = "".join(format(byte, "08b") for byte in codewords)
    index = 0
    upward = True
    column = size - 1
    while column > 0:
        if column == 6:                                        # the timing column is skipped
            column -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for x in (column, column - 1):
                if grid[row][x] is None:
                    grid[row][x] = int(bits[index]) if index < len(bits) else 0
                    index += 1
        upward = not upward
        column -= 2
    return grid


_MASKS = [
    lambda y, x: (y + x) % 2 == 0,
    lambda y, x: y % 2 == 0,
    lambda y, x: x % 3 == 0,
    lambda y, x: (y + x) % 3 == 0,
    lambda y, x: (y // 2 + x // 3) % 2 == 0,
    lambda y, x: (y * x) % 2 + (y * x) % 3 == 0,
    lambda y, x: ((y * x) % 2 + (y * x) % 3) % 2 == 0,
    lambda y, x: ((y + x) % 2 + (y * x) % 3) % 2 == 0,
]


def _penalty(grid: list[list[int]]) -> int:
    """How unpleasant a mask leaves the picture, by the specification's four rules."""
    size = len(grid)
    score = 0
    for line in list(grid) + [list(column) for column in zip(*grid)]:
        run, previous = 1, line[0]
        for module in line[1:]:
            if module == previous:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run, previous = 1, module
        if run >= 5:
            score += run - 2
    for y in range(size - 1):
        for x in range(size - 1):
            block = grid[y][x] + grid[y][x + 1] + grid[y + 1][x] + grid[y + 1][x + 1]
            if block in (0, 4):
                score += 3
    finder = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]
    for line in list(grid) + [list(column) for column in zip(*grid)]:
        for i in range(size - 10):
            window = line[i:i + 11]
            if window == finder or window == finder[::-1]:
                score += 40
    dark = sum(sum(row) for row in grid)
    score += 10 * (abs(dark * 100 // (size * size) - 50) // 5)
    return score


def _format_bits(mask: int) -> list[int]:
    value = (_ECC_M << 3) | mask
    remainder = value << 10
    for i in range(4, -1, -1):
        if remainder & (1 << (i + 10)):
            remainder ^= _FORMAT_POLY << i
    return [int(b) for b in format(((value << 10) | remainder) ^ _FORMAT_MASK, "015b")]


def _write_format(grid: list[list[int]], mask: int) -> None:
    size = len(grid)
    bits = _format_bits(mask)
    for i in range(6):
        grid[8][i] = bits[i]
        grid[size - 1 - i][8] = bits[i]
    grid[8][7], grid[8][8], grid[7][8] = bits[6], bits[7], bits[8]
    grid[size - 7][8] = bits[6]
    for i in range(9, 15):
        grid[8][size - 15 + i] = bits[i]
        grid[14 - i][8] = bits[i]
    grid[size - 8][8] = 1


def encode(text: str) -> list[list[int]]:
    """The QR code for `text`, as rows of 0 and 1. No quiet zone: the page adds the margin."""
    payload = text.encode()
    version = _choose_version(len(payload))
    size = 17 + version * 4
    reserved = _function_patterns(size, version)
    fixed = [[cell for cell in row] for row in reserved]        # which modules are functional
    grid = _place([row[:] for row in reserved], _codewords(text, version))

    best: tuple[int, list[list[int]]] | None = None
    for mask, rule in enumerate(_MASKS):
        candidate = [[int(cell) for cell in row] for row in grid]
        for y in range(size):
            for x in range(size):
                if fixed[y][x] is None and rule(y, x):
                    candidate[y][x] ^= 1
        _write_format(candidate, mask)
        score = _penalty(candidate)
        if best is None or score < best[0]:
            best = (score, candidate)
    assert best is not None
    return best[1]


def wifi_join(ssid: str, password: str, hidden: bool = False) -> str:
    """The string a phone's camera understands as "join this network"."""
    def escape(value: str) -> str:
        for char in ("\\", ";", ",", ":", '"'):
            value = value.replace(char, "\\" + char)
        return value

    kind = "WPA" if password else "nopass"
    return (f"WIFI:T:{kind};S:{escape(ssid)};"
            + (f"P:{escape(password)};" if password else "")
            + ("H:true;" if hidden else "") + ";")

"""Grid layouts. Positions and sizes are in grid cells.

Built-in layouts cover the usual shapes; `LayoutSet` adds any the user has defined
(`layouts:` in the config, edited in the admin page) without letting them shadow a
built-in name.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_GRID = 8          # a 8x8 wall is already 64 tiles
MAX_TILES = 64


@dataclass(frozen=True)
class Tile:
    id: str
    x: int
    y: int
    w: int = 1
    h: int = 1

    @property
    def cells(self) -> set[tuple[int, int]]:
        return {(x, y) for x in range(self.x, self.x + self.w)
                for y in range(self.y, self.y + self.h)}


@dataclass(frozen=True)
class Layout:
    id: str
    cols: int
    rows: int
    tiles: tuple[Tile, ...]

    def fraction(self, tile: Tile) -> float:
        """Share of the screen a tile covers (0..1)."""
        return (tile.w * tile.h) / (self.cols * self.rows)

    def public(self) -> dict:
        return {"id": self.id, "cols": self.cols, "rows": self.rows,
                "tiles": [{"id": t.id, "x": t.x, "y": t.y, "w": t.w, "h": t.h} for t in self.tiles]}


def _grid(cols: int, rows: int | None = None, id_: str | None = None) -> Layout:
    rows = rows if rows is not None else cols
    return Layout(
        id=id_ or f"{cols}x{rows}", cols=cols, rows=rows,
        tiles=tuple(Tile(f"t{i}", i % cols, i // cols) for i in range(cols * rows)),
    )


def _feature(id_: str, cols: int, rows: int, big_w: int, big_h: int) -> Layout:
    """One big tile in the top-left, single cells filling the rest, reading order."""
    big = Tile("t0", 0, 0, big_w, big_h)
    tiles = [big]
    for y in range(rows):
        for x in range(cols):
            if (x, y) not in big.cells:
                tiles.append(Tile(f"t{len(tiles)}", x, y))
    return Layout(id=id_, cols=cols, rows=rows, tiles=tuple(tiles))


#: Name -> layout. The name says how many tiles: "1+5" is one big and five small.
LAYOUTS: dict[str, Layout] = {
    "1x1": _grid(1),
    "1x2": _grid(2, 1, "1x2"),          # two side by side, for a wide screen
    "2x2": _grid(2),
    "2x3": _grid(3, 2, "2x3"),          # six, three across
    "3x3": _grid(3),
    "4x4": _grid(4),
    "5x5": _grid(5),
    "1+2": _feature("1+2", 3, 2, 2, 2),
    "1+3": _feature("1+3", 4, 3, 3, 3),
    "1+5": _feature("1+5", 3, 3, 2, 2),
    "1+7": _feature("1+7", 4, 4, 3, 3),
    "1+12": _feature("1+12", 4, 4, 2, 2),
    "2+8": Layout(id="2+8", cols=4, rows=4, tiles=(
        Tile("t0", 0, 0, 2, 2), Tile("t1", 2, 0, 2, 2),
        *(Tile(f"t{2 + i}", i % 4, 2 + i // 4) for i in range(8)),
    )),
}

#: Square grids `auto` may choose from, smallest first.
_AUTO = ["1x1", "2x2", "3x3", "4x4", "5x5"]
#: One-big-tile layouts `auto-feature` may choose from, smallest first. Falls back to a
#: grid past the largest, because a feature layout of 25 cameras helps nobody.
_AUTO_FEATURE = ["1x1", "1+2", "1+3", "1+5", "1+7", "1+12"]
#: Layout names that mean "work it out from how many cameras there are".
AUTO_NAMES = ("auto", "auto-feature")
#: Not a layout: "whatever the view is already using".
SAME_LAYOUT = "same"


def validate_tiles(cols: int, rows: int, tiles: list[Tile]) -> None:
    """Raise ValueError unless the tiles fit the grid without overlapping."""
    if not 1 <= cols <= MAX_GRID or not 1 <= rows <= MAX_GRID:
        raise ValueError(f"cols and rows must be between 1 and {MAX_GRID}")
    if not 1 <= len(tiles) <= MAX_TILES:
        raise ValueError(f"a layout needs between 1 and {MAX_TILES} tiles")
    used: dict[tuple[int, int], str] = {}
    for tile in tiles:
        if tile.w < 1 or tile.h < 1:
            raise ValueError(f"tile {tile.id} must be at least one cell")
        if tile.x < 0 or tile.y < 0 or tile.x + tile.w > cols or tile.y + tile.h > rows:
            raise ValueError(f"tile {tile.id} falls outside the {cols}x{rows} grid")
        for cell in tile.cells:
            if cell in used:
                raise ValueError(f"tiles {used[cell]} and {tile.id} overlap at {cell}")
            used[cell] = tile.id


def build_layout(id_: str, cols: int, rows: int, tiles: list[dict]) -> Layout:
    """A layout from plain data (user-defined). Tiles are renumbered in reading order."""
    ordered = sorted(tiles, key=lambda t: (int(t.get("y", 0)), int(t.get("x", 0))))
    built = [Tile(f"t{i}", int(t.get("x", 0)), int(t.get("y", 0)),
                  int(t.get("w", 1)), int(t.get("h", 1))) for i, t in enumerate(ordered)]
    validate_tiles(cols, rows, built)
    return Layout(id=id_, cols=cols, rows=rows, tiles=tuple(built))


@dataclass(frozen=True)
class LayoutSet:
    """The layouts available to the wall: the built-ins plus the user's own."""

    extra: tuple[Layout, ...] = ()

    @property
    def all(self) -> dict[str, Layout]:
        return {**LAYOUTS, **{layout.id: layout for layout in self.extra
                              if layout.id not in LAYOUTS}}

    def names(self) -> list[str]:
        return [*AUTO_NAMES, *self.all]

    def auto(self, camera_count: int, feature: bool = False) -> Layout:
        """The smallest layout that fits the cameras: a square grid, or one big tile."""
        candidates = _AUTO_FEATURE if feature else _AUTO
        for name in candidates:
            if camera_count <= len(LAYOUTS[name].tiles):
                return LAYOUTS[name]
        return LAYOUTS[_AUTO[-1]]      # more cameras than any feature layout: use a grid

    def get(self, name: str, camera_count: int) -> Layout:
        if name in AUTO_NAMES:
            return self.auto(camera_count, feature=name == "auto-feature")
        layouts = self.all
        try:
            return layouts[name]
        except KeyError:
            raise ValueError(f"unknown layout {name!r}; choose from {', '.join(self.names())}") from None


DEFAULT_LAYOUTS = LayoutSet()


def auto_layout(camera_count: int) -> Layout:
    return DEFAULT_LAYOUTS.auto(camera_count)


def get_layout(name: str, camera_count: int) -> Layout:
    return DEFAULT_LAYOUTS.get(name, camera_count)

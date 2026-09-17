from __future__ import annotations

import pytest

from viewport.layouts import (LAYOUTS, Layout, LayoutSet, Tile, auto_layout, build_layout,
                               get_layout, validate_tiles)


@pytest.mark.parametrize("count,expected", [(0, "1x1"), (1, "1x1"), (2, "2x2"), (4, "2x2"),
                                            (5, "3x3"), (9, "3x3"), (10, "4x4"), (16, "4x4"),
                                            (17, "5x5"), (40, "5x5")])
def test_auto_layout(count, expected):
    assert auto_layout(count).id == expected


@pytest.mark.parametrize("name", list(LAYOUTS))
def test_layouts_fill_the_grid_exactly(name):
    layout = LAYOUTS[name]
    cells = set()
    for tile in layout.tiles:
        for x in range(tile.x, tile.x + tile.w):
            for y in range(tile.y, tile.y + tile.h):
                assert (x, y) not in cells, f"{name}: overlap at {(x, y)}"
                cells.add((x, y))
    assert len(cells) == layout.cols * layout.rows
    assert abs(sum(layout.fraction(t) for t in layout.tiles) - 1.0) < 1e-9
    assert len({t.id for t in layout.tiles}) == len(layout.tiles)


def test_feature_layouts():
    assert len(LAYOUTS["1+5"].tiles) == 6
    assert len(LAYOUTS["1+7"].tiles) == 8
    assert LAYOUTS["1+5"].fraction(LAYOUTS["1+5"].tiles[0]) == pytest.approx(4 / 9)


def test_unknown_layout():
    with pytest.raises(ValueError):
        get_layout("9x9", 3)


def test_the_named_count_matches_the_tile_count():
    for name, layout in LAYOUTS.items():
        if "+" in name:
            big, small = name.split("+")
            assert len(layout.tiles) == int(big) + int(small), name
        elif "x" in name:
            cols, rows = (int(n) for n in name.split("x"))
            assert len(layout.tiles) == cols * rows, name


def test_public_includes_tile_geometry_for_the_admin_preview():
    public = LAYOUTS["1+5"].public()
    assert public["cols"] == 3 and public["rows"] == 3
    assert public["tiles"][0] == {"id": "t0", "x": 0, "y": 0, "w": 2, "h": 2}


# ----- user-defined layouts ---------------------------------------------------

def test_a_custom_layout_is_built_and_tiles_numbered_in_reading_order():
    layout = build_layout("mine", 3, 2, [{"x": 1, "y": 0, "w": 2, "h": 2}, {"x": 0, "y": 0},
                                         {"x": 0, "y": 1}])
    assert [(t.id, t.x, t.y, t.w, t.h) for t in layout.tiles] == [
        ("t0", 0, 0, 1, 1), ("t1", 1, 0, 2, 2), ("t2", 0, 1, 1, 1)]


@pytest.mark.parametrize("cols,rows,tiles,message", [
    (2, 2, [{"x": 0, "y": 0, "w": 3}], "outside"),
    (2, 2, [{"x": 0, "y": 0, "w": 2, "h": 2}, {"x": 1, "y": 1}], "overlap"),
    (2, 2, [{"x": 0, "y": 0, "w": 0}], "at least one cell"),
    (0, 2, [{"x": 0, "y": 0}], "between 1 and"),
    (99, 2, [{"x": 0, "y": 0}], "between 1 and"),
    (2, 2, [], "between 1 and"),
])
def test_bad_custom_layouts_are_refused(cols, rows, tiles, message):
    with pytest.raises(ValueError, match=message):
        build_layout("bad", cols, rows, tiles)


def test_gaps_are_allowed_so_a_layout_can_be_sparse():
    layout = build_layout("sparse", 2, 2, [{"x": 0, "y": 0}])
    assert len(layout.tiles) == 1 and layout.fraction(layout.tiles[0]) == 0.25


def test_validate_tiles_accepts_a_full_grid():
    validate_tiles(2, 1, [Tile("a", 0, 0), Tile("b", 1, 0)])


# ----- the layout set ---------------------------------------------------------

def test_a_layout_set_offers_built_ins_plus_custom_ones():
    mine = build_layout("mine", 2, 1, [{"x": 0, "y": 0}, {"x": 1, "y": 0}])
    layouts = LayoutSet(extra=(mine,))
    assert layouts.get("mine", 2) is mine
    assert layouts.get("2x2", 4).id == "2x2"
    assert "mine" in layouts.names() and layouts.names()[0] == "auto"


def test_a_custom_layout_cannot_shadow_a_built_in():
    impostor = Layout(id="2x2", cols=1, rows=1, tiles=(Tile("t0", 0, 0),))
    assert len(LayoutSet(extra=(impostor,)).get("2x2", 4).tiles) == 4


def test_an_unknown_name_lists_what_is_available():
    with pytest.raises(ValueError, match="choose from auto"):
        LayoutSet().get("nope", 1)


@pytest.mark.parametrize("count,expected", [(0, "1x1"), (1, "1x1"), (2, "1+2"), (3, "1+2"),
                                            (4, "1+3"), (5, "1+5"), (6, "1+5"), (8, "1+7"),
                                            (13, "1+12"), (30, "5x5")])
def test_auto_feature_picks_the_one_big_tile_layout_that_fits(count, expected):
    """Three cameras should be 1+2, not 1+5 with three empty tiles."""
    assert LayoutSet().get("auto-feature", count).id == expected


def test_both_auto_names_are_offered():
    assert LayoutSet().names()[:2] == ["auto", "auto-feature"]

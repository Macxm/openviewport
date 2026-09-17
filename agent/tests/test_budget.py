from __future__ import annotations

from conftest import make_camera
from viewport.budget import BudgetLimits, TileRequest, assign_streams, summarize
from viewport.layouts import LAYOUTS


def requests_for(layout_id, cameras, fullscreen=None):
    layout = LAYOUTS[layout_id]
    out = []
    for i, tile in enumerate(layout.tiles):
        cam = cameras[i] if i < len(cameras) else None
        if fullscreen is None:
            out.append(TileRequest(tile.id, cam, layout.fraction(tile), True))
        elif tile.id == fullscreen:
            out.append(TileRequest(tile.id, cam, 1.0, True))
        else:
            out.append(TileRequest(tile.id, cam, layout.fraction(tile), False))
    return out


def by_tile(assignments):
    return {a.tile_id: a for a in assignments}


def test_grid_uses_sub_streams_only():
    cams = [make_camera(i) for i in range(4)]
    result = assign_streams(requests_for("2x2", cams), BudgetLimits())
    assert [a.quality for a in result] == ["sub"] * 4
    assert [a.stream for a in result] == [f"nvr_{i}_sub" for i in range(4)]


def test_single_camera_gets_main():
    result = assign_streams(requests_for("1x1", [make_camera(0)]), BudgetLimits())
    assert result[0].quality == "main"
    assert result[0].stream == "nvr_0_main"


def test_fullscreen_gets_main_and_grid_stays_warm_on_sub():
    cams = [make_camera(i) for i in range(4)]
    result = by_tile(assign_streams(requests_for("2x2", cams, fullscreen="t2"), BudgetLimits()))
    assert result["t2"].quality == "main"
    assert {result[t].quality for t in ("t0", "t1", "t3")} == {"sub"}
    assert result["t0"].reason == "sub: kept warm"


def test_fullscreen_without_warm_grid_drops_hidden_tiles():
    cams = [make_camera(i) for i in range(4)]
    limits = BudgetLimits(keep_hidden_warm=False)
    result = by_tile(assign_streams(requests_for("2x2", cams, fullscreen="t1"), limits))
    assert result["t1"].quality == "main"
    assert [result[t].quality for t in ("t0", "t2", "t3")] == [None, None, None]


def test_main_budget_limits_big_tiles():
    cams = [make_camera(i) for i in range(8)]
    # 1+7: the big tile covers 9/16 of the screen, the rest 1/16 each.
    result = assign_streams(requests_for("1+7", cams), BudgetLimits(max_main=1))
    assert result[0].quality == "main"
    assert all(a.quality == "sub" for a in result[1:])
    assert summarize(result, BudgetLimits(max_main=1))["main_in_use"] == 1


def test_zero_main_budget_falls_back_to_sub():
    result = assign_streams(requests_for("1x1", [make_camera(0)]), BudgetLimits(max_main=0))
    assert result[0].quality == "sub"
    assert "budget" in result[0].reason


def test_offline_and_empty_tiles():
    cams = [make_camera(0), make_camera(1, online=False)]
    result = by_tile(assign_streams(requests_for("2x2", cams), BudgetLimits()))
    assert result["t1"].quality is None and result["t1"].reason == "offline"
    assert result["t2"].camera_id is None and result["t2"].reason == "empty"


def test_total_cap_prefers_visible_and_earlier_tiles():
    cams = [make_camera(i) for i in range(9)]
    result = assign_streams(requests_for("3x3", cams), BudgetLimits(max_total=4))
    played = [a.tile_id for a in result if a.stream]
    assert played == ["t0", "t1", "t2", "t3"]
    assert all(a.reason.startswith("none: stream budget") for a in result[4:])


def test_same_camera_twice_counts_once():
    cam = make_camera(0)
    reqs = [TileRequest("t0", cam, 0.25), TileRequest("t1", cam, 0.25)]
    result = assign_streams(reqs, BudgetLimits(max_total=1))
    assert [a.stream for a in result] == ["nvr_0_sub", "nvr_0_sub"]
    assert summarize(result, BudgetLimits(max_total=1))["streams_in_use"] == 1


# ----- main-stream hold-down ------------------------------------------------

def test_held_main_survives_leaving_fullscreen():
    """Back in the grid, the camera that was full screen keeps main while held."""
    cams = [make_camera(i) for i in range(4)]
    result = by_tile(assign_streams(requests_for("2x2", cams), BudgetLimits(),
                                    held_main=frozenset({"nvr_0_main"})))
    assert result["t0"].quality == "main"
    assert result["t0"].reason == "main: held after full screen"
    assert {result[t].quality for t in ("t1", "t2", "t3")} == {"sub"}


def test_held_main_never_outranks_real_demand():
    """A hold must not keep a newly full-screened camera off the main budget."""
    cams = [make_camera(i) for i in range(4)]
    result = by_tile(assign_streams(requests_for("2x2", cams, fullscreen="t2"),
                                    BudgetLimits(max_main=1),
                                    held_main=frozenset({"nvr_0_main"})))
    assert result["t2"].quality == "main" and result["t2"].reason == "main: large tile"
    assert result["t0"].quality == "sub"          # the held stream yields


def test_held_main_respects_the_main_budget():
    cams = [make_camera(i) for i in range(4)]
    held = frozenset({"nvr_0_main", "nvr_1_main", "nvr_2_main"})
    result = assign_streams(requests_for("2x2", cams), BudgetLimits(max_main=1), held_main=held)
    assert sum(a.quality == "main" for a in result) == 1


def test_hold_is_not_given_to_hidden_tiles():
    """Hidden tiles are kept warm on sub; a hold must not promote them to main."""
    cams = [make_camera(i) for i in range(4)]
    result = by_tile(assign_streams(requests_for("2x2", cams, fullscreen="t1"),
                                    BudgetLimits(max_main=2),
                                    held_main=frozenset({"nvr_0_main"})))
    assert result["t0"].quality == "sub"


def test_no_hold_without_held_streams():
    cams = [make_camera(i) for i in range(4)]
    result = assign_streams(requests_for("2x2", cams), BudgetLimits())
    assert all(a.quality == "sub" for a in result)


def test_when_the_budget_cannot_keep_every_held_stream_the_newest_wins():
    """Keeping the older one would close the stream the NVR has open and reopen another."""
    cams = [make_camera(i) for i in range(4)]
    result = by_tile(assign_streams(requests_for("2x2", cams), BudgetLimits(max_main=1),
                                    held_main=("nvr_3_main", "nvr_1_main")))
    assert result["t3"].quality == "main"
    assert result["t1"].quality == "sub"


def test_a_small_tile_can_still_be_asked_to_take_the_full_resolution_stream():
    """`force_main`: a camera that has seen something, in a layout that keeps it small."""
    cams = [make_camera(i) for i in range(4)]
    reqs = requests_for("2x2", cams)
    reqs[2] = TileRequest("t2", cams[2], 0.25, True, force_main=True)
    result = by_tile(assign_streams(reqs, BudgetLimits(main_min_fraction=0.4)))
    assert result["t2"].quality == "main"


def test_a_forced_tile_outranks_a_bigger_one_for_the_main_budget():
    cams = [make_camera(i) for i in range(4)]
    reqs = requests_for("1+3", cams)
    big = max(range(len(reqs)), key=lambda i: reqs[i].fraction)
    small = min(range(len(reqs)), key=lambda i: reqs[i].fraction)
    reqs[small] = TileRequest(reqs[small].tile_id, cams[small], reqs[small].fraction,
                              True, force_main=True)
    result = by_tile(assign_streams(reqs, BudgetLimits(max_main=1)))
    assert result[reqs[small].tile_id].quality == "main"
    assert result[reqs[big].tile_id].quality == "sub"


def test_a_tile_can_be_enlarged_without_taking_a_full_resolution_stream():
    cams = [make_camera(i) for i in range(4)]
    reqs = requests_for("2x2", cams, fullscreen="t0")
    reqs[0] = TileRequest("t0", cams[0], 1.0, True, allow_main=False)
    result = by_tile(assign_streams(reqs, BudgetLimits()))
    assert result["t0"].quality == "sub"
    assert summarize(assign_streams(reqs, BudgetLimits()), BudgetLimits())["main_in_use"] == 0

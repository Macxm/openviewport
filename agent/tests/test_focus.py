"""The detection focus state machine."""

from __future__ import annotations

from viewport.focus import FocusPolicy, FocusTracker

P = FocusPolicy(triggers=("person", "vehicle", "motion"), hold_seconds=10, min_focus_seconds=5)


def tracker(**kw):
    return FocusTracker(FocusPolicy(**{**P.__dict__, **kw}), camera_order=["a", "b", "c"])


def det(**cams):
    return {cam: frozenset(types.split(",")) if types else frozenset() for cam, types in cams.items()}


def test_nothing_detected_means_no_focus():
    assert tracker().update(det(a="", b=""), now=0) is None


def test_a_detection_that_is_not_a_trigger_is_ignored():
    assert tracker(triggers=("person",)).update(det(a="motion,animal"), now=0) is None


def test_the_first_trigger_takes_focus_with_its_reason():
    f = tracker().update(det(a="motion,person"), now=0)
    assert (f.camera_id, f.reason) == ("a", "person")     # best trigger wins, not the first listed


def test_higher_priority_wins_among_simultaneous_triggers():
    assert tracker().update(det(a="motion", b="person"), now=0).camera_id == "b"


def test_equal_priority_goes_to_whoever_started_first():
    t = tracker()
    t.update(det(b="person"), now=0)
    t.clear()                                             # b's onset stays remembered
    assert t.update(det(a="person", b="person"), now=1).camera_id == "b"


def test_equal_priority_and_onset_falls_back_to_camera_order():
    assert tracker().update(det(c="person", a="person"), now=0).camera_id == "a"


def test_focus_holds_after_the_detection_ends():
    t = tracker(hold_seconds=10)
    t.update(det(a="person"), now=0)
    assert t.update(det(a=""), now=5).camera_id == "a"
    assert t.update(det(a=""), now=9.9).camera_id == "a"
    assert t.update(det(a=""), now=10) is None


def test_the_hold_counts_from_the_last_detection_not_the_first():
    t = tracker(hold_seconds=10)
    t.update(det(a="person"), now=0)
    t.update(det(a="person"), now=8)
    assert t.update(det(a=""), now=15).camera_id == "a"   # 7 s after the last sighting
    assert t.update(det(a=""), now=18) is None


def test_a_busy_camera_is_not_preempted_by_an_equal_one():
    t = tracker()
    t.update(det(a="person"), now=0)
    assert t.update(det(a="person", b="person"), now=60).camera_id == "a"


def test_higher_priority_preempts_only_after_the_minimum_focus_time():
    t = tracker(min_focus_seconds=5)
    t.update(det(a="motion"), now=0)
    assert t.update(det(a="motion", b="person"), now=3).camera_id == "a"   # too soon
    f = t.update(det(a="motion", b="person"), now=5)
    assert (f.camera_id, f.reason) == ("b", "person")


def test_when_the_focus_goes_quiet_it_follows_the_action_at_once():
    t = tracker(min_focus_seconds=5)
    t.update(det(a="person"), now=0)
    assert t.update(det(a="", b="vehicle"), now=1).camera_id == "b"


def test_the_reason_tracks_what_the_camera_sees_now():
    t = tracker()
    t.update(det(a="motion"), now=0)
    assert t.update(det(a="motion,person"), now=1).reason == "person"


def test_clear_drops_the_focus_until_something_triggers_again():
    t = tracker()
    t.update(det(a="person"), now=0)
    t.clear()
    assert t.focus is None
    assert t.update(det(a="person"), now=1).camera_id == "a"


def test_since_is_kept_while_the_same_camera_stays_focused():
    t = tracker()
    t.update(det(a="person"), now=0)
    assert t.update(det(a="person"), now=30).since == 0

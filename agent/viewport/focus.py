"""Which camera should be primary because of what it detects.

A pure state machine with an injected clock, so every timing rule is unit-testable.
The wall decides what "primary" looks like; this only decides *which* camera and *why*.

Rules, in order:

* A camera is **triggered** when any of its active detections is in `triggers`. Its
  priority is the position of its best such detection in `triggers` (earlier = higher).
* Nothing focused: focus the best triggered camera (priority, then whoever started
  detecting first, then camera order, so the choice is deterministic).
* The focused camera is still triggered: keep it. Another camera only takes over if it
  has a strictly higher priority *and* the current focus has lasted `min_focus_seconds`,
  so two busy cameras cannot ping-pong the screen.
* The focused camera stopped detecting: if another camera is triggered, follow it at once;
  otherwise hold the focus until `hold_seconds` after the last detection, then let go.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DETECTION_TYPES = ("person", "vehicle", "animal", "face", "motion")


@dataclass
class DetectionGate:
    """What counts as *new*, given sources that report a state rather than an event.

    Reolink answers `GetAiState` with a level: a car parked on the drive keeps `vehicle`
    asserted for as long as it sits there, so a tile would stay marked, and the screen held,
    for hours. Here a type counts from when it appears and for at most `max_seconds`; after
    that it is scenery, and counts again only once it has stopped and happened afresh.

    `max_seconds = 0` passes every reported state through, which is what a source with real
    events wants. Restarting re-reads whatever is asserted at that moment as new, once:
    nothing tells us how long it has been going on.
    """

    max_seconds: float = 30.0
    _since: dict[tuple[str, str], float] = field(default_factory=dict)

    def update(self, active: dict[str, frozenset[str]], now: float) -> dict[str, frozenset[str]]:
        """Filter a poll's asserted types down to the ones that are still news."""
        asserted = {(cam, kind) for cam, kinds in active.items() for kind in kinds}
        for key in list(self._since):
            if key not in asserted:
                del self._since[key]            # stopped, so the next one is new again
        fresh: dict[str, frozenset[str]] = {}
        for cam, kinds in active.items():
            new = frozenset(k for k in kinds
                            if self.max_seconds <= 0
                            or now - self._since.setdefault((cam, k), now) < self.max_seconds)
            if new:
                fresh[cam] = new
        return fresh


@dataclass(frozen=True)
class FocusPolicy:
    triggers: tuple[str, ...] = ("person", "vehicle")
    hold_seconds: float = 15.0
    min_focus_seconds: float = 5.0


@dataclass(frozen=True)
class Focus:
    camera_id: str
    reason: str          # the detection type that justifies the focus, e.g. "person"
    since: float         # when this camera became the focus
    last_seen: float     # when it last had a triggering detection


@dataclass
class FocusTracker:
    policy: FocusPolicy
    camera_order: list[str] = field(default_factory=list)
    focus: Focus | None = None
    _onset: dict[str, float] = field(default_factory=dict)

    def update(self, active: dict[str, frozenset[str]], now: float) -> Focus | None:
        """Feed the latest detections; returns the focus to show (or None)."""
        triggered = {cam: self._best(types) for cam, types in active.items() if self._best(types)}
        # Remember when each camera started detecting, for first-come tie-breaks.
        for cam in triggered:
            self._onset.setdefault(cam, now)
        for cam in list(self._onset):
            if cam not in triggered:
                del self._onset[cam]

        current = self.focus
        if current is None:
            self.focus = self._pick(triggered, now)
            return self.focus

        if current.camera_id in triggered:
            reason = triggered[current.camera_id]
            challenger = self._pick({c: r for c, r in triggered.items() if c != current.camera_id}, now)
            if (challenger is not None
                    and self._rank(challenger.reason) < self._rank(reason)
                    and now - current.since >= self.policy.min_focus_seconds):
                self.focus = challenger
            else:
                self.focus = Focus(current.camera_id, reason, current.since, now)
            return self.focus

        # The focused camera is quiet.
        others = self._pick(triggered, now)
        if others is not None:
            self.focus = others
        elif now - current.last_seen >= self.policy.hold_seconds:
            self.focus = None
        return self.focus

    def clear(self) -> None:
        """Drop the focus (a person dismissed it). Detections still running re-trigger later."""
        self.focus = None

    # ----- helpers ----------------------------------------------------------

    def _best(self, types: frozenset[str]) -> str | None:
        for trigger in self.policy.triggers:
            if trigger in types:
                return trigger
        return None

    def _rank(self, reason: str) -> int:
        return self.policy.triggers.index(reason) if reason in self.policy.triggers else len(self.policy.triggers)

    def _pick(self, triggered: dict[str, str], now: float) -> Focus | None:
        if not triggered:
            return None
        order = {cam: i for i, cam in enumerate(self.camera_order)}
        cam = min(triggered, key=lambda c: (self._rank(triggered[c]), self._onset.get(c, now),
                                            order.get(c, len(order)), c))
        return Focus(cam, triggered[cam], since=now, last_seen=now)

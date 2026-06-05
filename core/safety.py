"""
Safety layer — the absolute red line of the entire system.

Rules:
  1. ANY ship at 大破 (HP ≤ 25%) → MUST retreat. No override.
  2. Attempting to advance with 大破 ship → raises TaihaAdvanceError immediately.
  3. Anti-detection: random delays, bezier mouse curves, mandatory 4h+ rest per 24h.

Nothing in executor.py may bypass check_taiha().
"""
from __future__ import annotations

import logging
import math
import random
import time
from datetime import datetime, timedelta
from typing import Optional

from .models import Ship

log = logging.getLogger(__name__)


class TaihaAdvanceError(Exception):
    """Raised when code attempts to advance with a 大破 ship. Never catch silently."""


# ── 大破 detection ─────────────────────────────────────────────────────────────

def is_taiha(ship: Ship) -> bool:
    """True if ship HP ≤ 25% of max."""
    if ship.max_hp <= 0:
        return False
    return ship.now_hp / ship.max_hp <= 0.25


def taiha_ships(ships: list[Optional[Ship]]) -> list[Ship]:
    """Return all ships currently at 大破."""
    return [s for s in ships if s and is_taiha(s)]


def check_taiha(ships: list[Optional[Ship]], context: str = "") -> None:
    """
    Hard gate — call before EVERY advance decision.
    Raises TaihaAdvanceError if any ship is 大破.
    Never catches, never suppresses.
    """
    damaged = taiha_ships(ships)
    if damaged:
        names = ", ".join(s.name for s in damaged)
        msg = f"TAIHA ADVANCE BLOCKED [{context}]: {names} are at 大破. Retreating."
        log.critical(msg)
        raise TaihaAdvanceError(msg)


# ── Anti-detection utilities ──────────────────────────────────────────────────

def random_delay(base: float, jitter: float = 2.5) -> None:
    """Sleep for base ± jitter seconds (uniform distribution)."""
    delay = base + random.uniform(-jitter / 2, jitter / 2)
    delay = max(0.3, delay)
    log.debug("Delay %.2fs", delay)
    time.sleep(delay)


def action_delay() -> None:
    """Standard delay between UI actions (2~5s)."""
    random_delay(base=3.0, jitter=3.0)


def click_delay() -> None:
    """Short delay between individual clicks (0.3~1.2s)."""
    random_delay(base=0.7, jitter=0.9)


def bezier_points(
    start: tuple[float, float],
    end: tuple[float, float],
    n: int = 30,
) -> list[tuple[float, float]]:
    """
    Generate cubic Bézier curve waypoints between start and end.
    Control points are randomized to avoid straight-line detection.
    """
    x0, y0 = start
    x1, y1 = end
    dx, dy = x1 - x0, y1 - y0

    # Random control points offset from midpoint
    cp1 = (
        x0 + dx * 0.25 + random.uniform(-abs(dy) * 0.3, abs(dy) * 0.3),
        y0 + dy * 0.25 + random.uniform(-abs(dx) * 0.3, abs(dx) * 0.3),
    )
    cp2 = (
        x0 + dx * 0.75 + random.uniform(-abs(dy) * 0.3, abs(dy) * 0.3),
        y0 + dy * 0.75 + random.uniform(-abs(dx) * 0.3, abs(dx) * 0.3),
    )

    points = []
    for i in range(n + 1):
        t = i / n
        # Cubic Bézier
        mt = 1 - t
        x = mt**3 * x0 + 3*mt**2*t*cp1[0] + 3*mt*t**2*cp2[0] + t**3 * x1
        y = mt**3 * y0 + 3*mt**2*t*cp1[1] + 3*mt*t**2*cp2[1] + t**3 * y1
        points.append((x, y))
    return points


def jitter_point(x: float, y: float, radius: int = 5) -> tuple[int, int]:
    """Add small random offset to a target click coordinate."""
    return (
        int(x + random.uniform(-radius, radius)),
        int(y + random.uniform(-radius, radius)),
    )


# ── Rest watchdog ─────────────────────────────────────────────────────────────

class RestWatchdog:
    """
    Ensures the bot rests ≥ MIN_REST_HOURS every 24 hours.
    Call .check() before starting each sortie loop iteration.
    """
    MIN_REST_HOURS = 4
    WINDOW_HOURS = 24

    def __init__(self):
        self._rest_periods: list[tuple[datetime, datetime]] = []
        self._session_start = datetime.now()

    def record_rest_start(self) -> None:
        self._rest_start = datetime.now()

    def record_rest_end(self) -> None:
        if hasattr(self, "_rest_start"):
            self._rest_periods.append((self._rest_start, datetime.now()))

    def rest_hours_in_window(self) -> float:
        """Total rest hours in the last 24h window."""
        cutoff = datetime.now() - timedelta(hours=self.WINDOW_HOURS)
        total = timedelta()
        for start, end in self._rest_periods:
            if end > cutoff:
                total += end - max(start, cutoff)
        return total.total_seconds() / 3600

    def active_hours_in_window(self) -> float:
        """Hours the bot has been running (not resting) in the last 24h window."""
        window_hours = (datetime.now() - self._session_start).total_seconds() / 3600
        return min(window_hours, self.WINDOW_HOURS) - self.rest_hours_in_window()

    def needs_rest(self) -> bool:
        """True only after running for (WINDOW_HOURS - MIN_REST_HOURS) hours straight."""
        return self.active_hours_in_window() >= (self.WINDOW_HOURS - self.MIN_REST_HOURS)

    def enforce_rest(self, hours: float = MIN_REST_HOURS) -> None:
        """Block until enough rest has accumulated."""
        log.warning("Rest watchdog: resting for %.1f hours to stay safe", hours)
        self.record_rest_start()
        time.sleep(hours * 3600)
        self.record_rest_end()
        log.info("Rest complete. Resuming.")

    def check_and_rest_if_needed(self) -> None:
        if self.needs_rest():
            self.enforce_rest(self.MIN_REST_HOURS)

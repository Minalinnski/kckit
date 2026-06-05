"""
Repair dock manager — decides which ships to send to repair.
Priority: most damaged → highest level → most HP to restore.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)


@dataclass
class RepairAction:
    dock_id: int
    action: str        # "start_repair", "use_bucket", "wait", "idle", "collect"
    ship_id: int = 0
    ship_name: str = ""
    complete_dt: Optional[datetime] = None
    note: str = ""


class RepairManager:
    """
    Reads game state and determines repair dock actions.
    Does NOT perform UI interaction — returns action objects.
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.bucket_threshold: float = self.config.get("bucket_threshold", 0.5)  # use bucket if repair > N hours
        self.auto_bucket: bool = self.config.get("auto_bucket", False)

    def assess(self, state) -> list[RepairAction]:
        """
        Examine repair docks and ship roster; return recommended actions.
        state must have .repair_docks and .ships.
        """
        actions: list[RepairAction] = []

        # Categorize docks
        empty_docks = [d for d in state.repair_docks if d.is_empty]
        busy_docks = [d for d in state.repair_docks if not d.is_empty]

        # Report on busy docks
        for dock in busy_docks:
            if dock.complete_dt and dock.complete_dt <= datetime.now():
                actions.append(RepairAction(
                    dock_id=dock.dock_id,
                    action="collect",
                    ship_id=dock.ship_id,
                    note=f"dock {dock.dock_id} repair complete",
                ))
            else:
                actions.append(RepairAction(
                    dock_id=dock.dock_id,
                    action="wait",
                    ship_id=dock.ship_id,
                    complete_dt=dock.complete_dt,
                    note=f"dock {dock.dock_id} in use",
                ))

        # Find damaged ships needing repair
        if empty_docks:
            candidates = self._repair_candidates(state)
            for dock, ship in zip(empty_docks, candidates):
                actions.append(RepairAction(
                    dock_id=dock.dock_id,
                    action="start_repair",
                    ship_id=ship.instance_id,
                    ship_name=ship.name,
                    note=f"queue {ship.name} (HP {ship.now_hp}/{ship.max_hp}) in dock {dock.dock_id}",
                ))

        if not busy_docks and not empty_docks:
            actions.append(RepairAction(dock_id=0, action="idle", note="no docks available"))

        return actions

    def _repair_candidates(self, state) -> list:
        """Return ships that need repair, sorted by priority (most damaged first)."""
        ships = list(state.ships.values())
        damaged = [
            s for s in ships
            if s.now_hp < s.max_hp
            and not s.in_repair
        ]
        # Sort: 大破 first, then by damage ratio desc, then by level desc
        damaged.sort(key=lambda s: (-int(s.is_taiha), -(1 - s.hp_ratio), -s.level))
        return damaged

    def next_wakeup_seconds(self, state) -> float:
        """How many seconds until the next repair completes."""
        now = datetime.now()
        busy = [d for d in state.repair_docks if not d.is_empty and d.complete_dt]
        times = [(d.complete_dt - now).total_seconds() for d in busy if d.complete_dt > now]
        return min(times) if times else float("inf")

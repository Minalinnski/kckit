"""Fleet composition algorithm — selects ships from roster for a given strategy preset."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from .models import GameState, Ship
from .schema import FleetRequirement, PresetConfig, ShipClass, SlotRequirement

log = logging.getLogger(__name__)

SPEED_VALUES = {
    "slow": (5,),
    "standard": (10,),
    "fast": (10, 15, 20),
    "fast+": (15, 20),
}


@dataclass
class ProposedFleet:
    preset_name: str
    ships: list[Ship]
    warnings: list[str]

    @property
    def is_valid(self) -> bool:
        return len(self.ships) >= 1 and not any(
            "ERROR" in w for w in self.warnings
        )


class FleetComposer:
    """
    Selects ships from available roster to satisfy a PresetConfig's fleet requirement.

    Priority ordering per slot:
      1. Specific named ships (exact match)
      2. Higher remodel level (api_ship_id range heuristic — higher ID = remodeled)
      3. Higher level
      4. Higher max HP (durability)
    """

    def __init__(self, state: GameState):
        self.state = state

    def compose(self, preset: PresetConfig) -> ProposedFleet:
        available = self.state.available_ships()
        all_ships = list(self.state.ships.values())
        used_ids: set[int] = set()
        selected: list[Ship] = []
        warnings: list[str] = []

        for slot_req in preset.fleet.slots:
            ships_for_slot, missing_names = self._select_for_slot(
                slot_req, available, used_ids, all_ships
            )
            for name in missing_names:
                in_roster = any(s.name == name for s in all_ships)
                if in_roster:
                    warnings.append(
                        f"ERROR: Required ship '{name}' not available "
                        f"(in repair/expedition/taiha)"
                    )
                else:
                    warnings.append(
                        f"ERROR: Required ship '{name}' not found in roster"
                    )
            if len(ships_for_slot) < slot_req.count and not missing_names:
                classes = "/".join(c.value for c in slot_req.ship_class)
                warnings.append(
                    f"ERROR: Need {slot_req.count}×{classes}, "
                    f"only found {len(ships_for_slot)} available"
                )
            selected.extend(ships_for_slot)
            for s in ships_for_slot:
                used_ids.add(s.instance_id)

        # Validate total size
        if len(selected) > 6:
            warnings.append("ERROR: Fleet exceeds 6 ships")
            selected = selected[:6]

        # Warn about morale
        tired = [s.name for s in selected if s.morale < 30]
        if tired:
            warnings.append(f"Low morale (<30): {', '.join(tired)}")

        sparkled = [s.name for s in selected if s.is_sparkled]
        if len(sparkled) < len(selected):
            not_sparkled = [s.name for s in selected if not s.is_sparkled]
            warnings.append(f"Not sparkled: {', '.join(not_sparkled)}")

        return ProposedFleet(
            preset_name=preset.name,
            ships=selected,
            warnings=warnings,
        )

    def _select_for_slot(
        self,
        req: SlotRequirement,
        available: list[Ship],
        used_ids: set[int],
        all_ships: list[Ship] | None = None,
    ) -> tuple[list[Ship], list[str]]:
        """Return (selected_ships, missing_required_names).

        specific_ships is an ALTERNATIVES list: pick `count` ships from it in order.
        e.g. ["大和改二重", "大和改二"] with count=1 → take whichever is available first.
        If none of the alternatives are available, record missing and do not substitute.
        Any remaining count beyond pinned ships is filled with generic ship_class candidates.
        """
        missing_names: list[str] = []

        # Try alternatives in order, pick up to count
        pinned: list[Ship] = []
        if req.specific_ships:
            not_used = [s for s in available if s.instance_id not in used_ids]
            for name in req.specific_ships:
                if len(pinned) >= req.count:
                    break
                match = next((s for s in not_used if s.name == name and s not in pinned), None)
                if match is not None:
                    pinned.append(match)

            # If we couldn't fill the slot from alternatives, it's missing
            if len(pinned) < req.count:
                tried = req.specific_ships
                missing_names.append(f"({'/'.join(tried)})")
                return pinned, missing_names

        # Generic candidates (ship_class filtered) for remaining slots
        remaining_count = req.count - len(pinned)
        if remaining_count > 0:
            pinned_ids = {s.instance_id for s in pinned}
            candidates = [
                s for s in available
                if s.instance_id not in used_ids
                and s.instance_id not in pinned_ids
                and self._matches(s, req)
            ]
            candidates.sort(key=self._priority_key, reverse=True)
            result = pinned + candidates[:remaining_count]
        else:
            result = pinned[: req.count]

        return result, missing_names

    @staticmethod
    def _matches(ship: Ship, req: SlotRequirement) -> bool:
        # Ship class
        if ship.ship_class not in req.ship_class:
            return False
        # Min level
        if req.min_level and ship.level < req.min_level:
            return False
        # Min ASW
        if req.min_asw and ship.asw < req.min_asw:
            return False
        # Speed
        if req.speed:
            allowed = SPEED_VALUES.get(req.speed, ())
            if ship.speed not in allowed:
                return False
        return True

    @staticmethod
    def _priority_key(ship: Ship) -> tuple:
        """Higher is better."""
        return (
            ship.level,       # level first
            ship.max_hp,      # then durability
            ship.master_id,   # higher master ID tends to be remodeled
        )

"""
Equipment optimizer — given fleet + strategy requirements,
produce an optimal equipment plan (air power, LoS, then combat stats).

All formulas are pure functions for easy unit testing.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .models import Equipment, Ship, AIRCRAFT_TYPES, LOS_FACTORS, LOS_FACTOR_DEFAULT
from .schema import AirState, Requirements


# ── Air power formulas ────────────────────────────────────────────────────────

def slot_air_power(slot_count: int, anti_air: int, proficiency: int = 0) -> float:
    """Air power contributed by one aircraft slot."""
    base = math.floor(math.sqrt(slot_count)) * anti_air
    # Simplified proficiency bonus (accurate table varies by aircraft type)
    prof_bonus = [0, 0, 2, 5, 9, 14, 14, 22][min(proficiency, 7)]
    return base + prof_bonus


def ship_air_power(ship: Ship) -> float:
    """Total air power of a ship with its current equipment."""
    total = 0.0
    for i, equip in enumerate(ship.equipped):
        if equip and equip.is_aircraft and i < len(ship.slot_counts):
            total += slot_air_power(ship.slot_counts[i], equip.anti_air, equip.proficiency)
    return total


def fleet_air_power(ships: list[Ship]) -> float:
    return sum(ship_air_power(s) for s in ships if s)


# ── 33式索敵 formula ──────────────────────────────────────────────────────────

def los_33(ships: list[Ship], hq_level: int) -> float:
    """
    33式索敵計算 (LoS branching formula).
    LoS = Σ(equip_los * type_factor + naked_los * 0.1) + 2*fleet_size - 0.4*ceil(HQ)
    """
    total = 0.0
    fleet_size = sum(1 for s in ships if s)
    for ship in ships:
        if not ship:
            continue
        total += ship.naked_los * 0.1
        all_equipped = list(ship.equipped)
        if ship.equipped_ex:
            all_equipped.append(ship.equipped_ex)
        for equip in all_equipped:
            if equip:
                factor = LOS_FACTORS.get(equip.icon_type, LOS_FACTOR_DEFAULT)
                total += equip.los * factor

    total += 2 * fleet_size - 0.4 * math.ceil(hq_level)
    return total


# ── Equipment plan ────────────────────────────────────────────────────────────

@dataclass
class ShipEquipPlan:
    ship_id: int
    ship_name: str
    slots: list[Optional[int]]   # equip instance IDs (6 slots + ex), None = empty
    slot_ex: Optional[int]

    @property
    def all_equip_ids(self) -> list[int]:
        return [e for e in self.slots + [self.slot_ex] if e is not None]


@dataclass
class EquipPlan:
    ships: list[ShipEquipPlan]
    air_power: float
    los_value: float
    requirements_met: bool
    notes: list[str]


# ── Optimizer ─────────────────────────────────────────────────────────────────

class EquipOptimizer:
    """
    Greedy equipment optimizer.
    Phase 1: Assign aircraft to reach air_power_min
    Phase 2: Assign LoS equipment to reach los_min
    Phase 3: Fill remaining slots with combat equipment
    """

    def __init__(
        self,
        ships: list[Ship],
        available_equips: list[Equipment],
        requirements: Requirements,
        hq_level: int = 120,
        knowledge=None,
        preset=None,
    ):
        self.ships = ships
        self.pool = list(available_equips)   # mutable pool, equips get "used"
        self._equip_index = {e.instance_id: e for e in available_equips}
        self.req = requirements
        self.hq_level = hq_level
        self.knowledge = knowledge
        self.preset = preset

    def optimize(self) -> EquipPlan:
        # Start with empty slots for all ships
        plans: dict[int, list[Optional[int]]] = {
            s.instance_id: [None] * s.slot_num
            for s in self.ships
        }
        ex_plans: dict[int, Optional[int]] = {s.instance_id: None for s in self.ships}
        used: set[int] = set()
        notes: list[str] = []

        def use(equip: Equipment) -> None:
            self.pool.remove(equip)
            used.add(equip.instance_id)

        # ── Phase 0: Example-based assignment ─────────────────────────────────
        if self.preset and self.preset.example and self.knowledge:
            for ex_ship in self.preset.example:
                ship = next((s for s in self.ships if s.name == ex_ship.ship), None)
                if ship is None:
                    continue
                for slot_i, equip_name in enumerate(ex_ship.equips):
                    if slot_i >= ship.slot_num:
                        break
                    if plans[ship.instance_id][slot_i] is not None:
                        continue
                    found = self.knowledge.find_substitute(equip_name, self.pool, used)
                    if found:
                        plans[ship.instance_id][slot_i] = found.instance_id
                        use(found)

        # ── Phase 1: Air power ─────────────────────────────────────────────
        air_needed = self.req.air_power_min or 0
        if air_needed > 0:
            current_air = 0.0
            aircraft = sorted(
                [e for e in self.pool if e.is_aircraft],
                key=lambda e: e.anti_air,
                reverse=True,
            )
            for ship in self.ships:
                for slot_i in range(ship.slot_num):
                    if current_air >= air_needed:
                        break
                    if not aircraft:
                        break
                    slot_count = ship.slot_counts[slot_i] if slot_i < len(ship.slot_counts) else 0
                    if slot_count == 0:
                        continue
                    best = max(
                        aircraft,
                        key=lambda e: slot_air_power(slot_count, e.anti_air, e.proficiency),
                    )
                    plans[ship.instance_id][slot_i] = best.instance_id
                    current_air += slot_air_power(slot_count, best.anti_air, best.proficiency)
                    aircraft.remove(best)
                    use(best)

            if current_air < air_needed:
                notes.append(
                    f"Air power shortfall: achieved {current_air:.0f} / needed {air_needed}"
                )

        # ── Phase 2: LoS ───────────────────────────────────────────────────
        los_needed = self.req.los_min or 0
        if los_needed > 0:
            # Estimate current LoS from naked stats + already-assigned aircraft LoS
            def estimate_los() -> float:
                temp_ships = self._ships_with_plan(plans, ex_plans)
                return los_33(temp_ships, self.hq_level)

            los_equips = sorted(
                [e for e in self.pool if not e.is_aircraft and e.los > 0],
                key=lambda e: e.los * LOS_FACTORS.get(e.icon_type, LOS_FACTOR_DEFAULT),
                reverse=True,
            )
            for ship in self.ships:
                for slot_i in range(ship.slot_num):
                    if estimate_los() >= los_needed:
                        break
                    if not los_equips:
                        break
                    if plans[ship.instance_id][slot_i] is not None:
                        continue
                    best = los_equips.pop(0)
                    plans[ship.instance_id][slot_i] = best.instance_id
                    use(best)

            final_los = estimate_los()
            if final_los < los_needed:
                notes.append(
                    f"LoS shortfall: achieved {final_los:.1f} / needed {los_needed}"
                )

        # ── Phase 3: Role-based + combat fill ─────────────────────────────────
        for ship in self.ships:
            for slot_i in range(ship.slot_num):
                if plans[ship.instance_id][slot_i] is not None:
                    continue
                slot_count = ship.slot_counts[slot_i] if slot_i < len(ship.slot_counts) else 0
                best = None
                # Try role-based first
                if self.knowledge:
                    roles = self.knowledge.default_roles_for_ship(ship.ship_type, slot_i, slot_count)
                    for role in roles:
                        best = self.knowledge.find_in_pool(role, self.pool, used)
                        if best:
                            break
                # Fallback to stat-based
                if best is None:
                    best = self._best_combat(ship, slot_i)
                if best:
                    plans[ship.instance_id][slot_i] = best.instance_id
                    use(best)

        # ── Build result ───────────────────────────────────────────────────
        ship_plans = [
            ShipEquipPlan(
                ship_id=s.instance_id,
                ship_name=s.name,
                slots=plans[s.instance_id],
                slot_ex=ex_plans[s.instance_id],
            )
            for s in self.ships
        ]

        final_ships = self._ships_with_plan(plans, ex_plans)
        final_air = fleet_air_power(final_ships)
        final_los = los_33(final_ships, self.hq_level)
        met = (
            (final_air >= (self.req.air_power_min or 0))
            and (final_los >= (self.req.los_min or 0))
        )

        return EquipPlan(
            ships=ship_plans,
            air_power=final_air,
            los_value=final_los,
            requirements_met=met,
            notes=notes,
        )

    def _best_combat(self, ship: Ship, slot_i: int) -> Optional[Equipment]:
        """Pick best combat equipment for a slot based on ship type (stype)."""
        stype = ship.ship_type

        # SS (13) / SSV (14): submarine torpedoes only
        if stype in (13, 14):
            cands = [e for e in self.pool if e.eq_type == 32]
            return max(cands, key=lambda e: e.torpedo) if cands else None

        # CV/CVL (11,18): only aircraft — already covered by Phase 1, skip Phase 3
        if stype in (11, 18):
            return None

        # AO (22), AS (20), LHA (17): no combat equipment
        if stype in (22, 20, 17):
            return None

        # Exclude aircraft and submarine torpedoes
        cands = [e for e in self.pool if not e.is_aircraft and e.eq_type != 32]
        if not cands:
            return None

        # DE (1) / DD (2) / CL (3) / CLT (4): prioritise torpedo
        if stype in (1, 2, 3, 4):
            return max(cands, key=lambda e: e.torpedo * 2 + e.firepower)

        # CA (5) / CAV (6) / BB (9) / FBB (8) / BBV (10) / AVS(16): firepower
        return max(cands, key=lambda e: e.firepower * 2 + e.torpedo)

    def _ships_with_plan(
        self,
        plans: dict[int, list[Optional[int]]],
        ex_plans: dict[int, Optional[int]],
    ) -> list[Ship]:
        """Return Ship objects with equipped list reflecting the current plan."""
        from copy import copy
        result = []
        for ship in self.ships:
            s = copy(ship)
            slot_ids = plans[ship.instance_id]
            s.equipped = [self._equip_index.get(eid) for eid in slot_ids if eid is not None]
            result.append(s)
        return result

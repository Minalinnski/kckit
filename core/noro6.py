"""Generate noro6 (kc-web) DeckBuilder URLs from fleet/equip plan."""
from __future__ import annotations

import json
from typing import Optional
from urllib.parse import urlencode

from .models import Equipment, GameState, Ship
from .composer import ProposedFleet
from .optimizer import EquipPlan

NORO6_BASE = "https://noro6.github.io/kc-web"


def _item(equip: Optional[Equipment]) -> Optional[dict]:
    if equip is None:
        return None
    return {"id": equip.master_id, "rf": equip.level, "mas": equip.proficiency}


def build_deck(
    proposed: ProposedFleet,
    equip_plan: Optional[EquipPlan],
    equip_index: dict[int, Equipment],
    hq_level: int = 120,
    fleet_num: int = 1,
) -> dict:
    """Build a noro6 DeckBuilder dict from a proposed fleet + optional equip plan."""
    ship_plan_map = {p.ship_id: p for p in equip_plan.ships} if equip_plan else {}

    fleet: dict = {"name": proposed.preset_name}
    for i, ship in enumerate(proposed.ships, start=1):
        plan = ship_plan_map.get(ship.instance_id)
        items: dict = {}
        if plan:
            for slot_i, eid in enumerate(plan.slots):
                eq = equip_index.get(eid) if eid else None
                item = _item(eq)
                if item:
                    items[str(slot_i)] = item
            if plan.slot_ex:
                ex = equip_index.get(plan.slot_ex)
                if ex:
                    items["ex"] = _item(ex)
        else:
            for slot_i, eq in enumerate(ship.equipped):
                item = _item(eq)
                if item:
                    items[str(slot_i)] = item
            if ship.equipped_ex:
                ex_item = _item(ship.equipped_ex)
                if ex_item:
                    items["ex"] = ex_item

        fleet[f"s{i}"] = {
            "id": ship.master_id,
            "lv": ship.level,
            "luck": -1,
            "items": items,
        }

    return {
        "version": 4,
        "hqlv": hq_level,
        f"f{fleet_num}": fleet,
    }


def deck_to_url(deck: dict) -> str:
    return f"{NORO6_BASE}?" + urlencode({"predeck": json.dumps(deck, ensure_ascii=False)})

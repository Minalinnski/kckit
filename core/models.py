"""Game data models wrapping kcsapi DTOs received from poi."""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Optional

from .schema import ShipClass, STYPE_TO_CLASS

# ── Equipment icon type → LoS factor (33式) ───────────────────────────────────
# api_type[3] is the icon type
LOS_FACTORS: dict[int, float] = {
    9: 1.04,   # 艦上偵察機
    10: 1.00,  # 水上偵察機
    11: 1.04,  # 水上爆撃機 (seaplane bomber)
    8: 0.8,    # 艦上攻撃機
    41: 1.00,  # 大型飛行艇
}
LOS_FACTOR_DEFAULT = 0.6   # 電探、その他

# Equipment types that contribute to air power (api_type[2])
AIRCRAFT_TYPES = {
    6,   # 艦戦 (fighter)
    7,   # 艦爆 (dive bomber)
    8,   # 艦攻 (torpedo bomber)
    9,   # 艦偵 (recon)
    10,  # 水偵 (seaplane recon)
    11,  # 水爆 (seaplane bomber)
    45,  # 水戦 (seaplane fighter)
    56,  # 噴式戦闘爆撃機
    57,  # 噴式攻撃機
    58,  # 噴式偵察機
    59,  # 噴式偵察爆撃機
}


@dataclass
class Equipment:
    """Merged instance + master equipment data."""
    instance_id: int      # api_id of the instance
    master_id: int        # api_slotitem_id
    name: str
    level: int = 0        # api_level (remodel level)
    proficiency: int = 0  # api_alv (0-7)

    # From master ($equips)
    eq_type: int = 0        # api_type[2] — equipment category
    icon_type: int = 0      # api_type[3] — icon for LoS factor lookup
    firepower: int = 0      # api_houg
    torpedo: int = 0        # api_raig
    anti_air: int = 0       # api_tyku
    armor: int = 0          # api_souk
    los: int = 0            # api_sakuteki
    asw: int = 0            # api_taisen
    evasion: int = 0        # api_kaihi
    accuracy: int = 0       # api_houm (?)
    range: int = 0          # api_leng

    @property
    def is_aircraft(self) -> bool:
        return self.eq_type in AIRCRAFT_TYPES

    @property
    def los_factor(self) -> float:
        return LOS_FACTORS.get(self.icon_type, LOS_FACTOR_DEFAULT)

    @property
    def proficiency_aa_bonus(self) -> float:
        """Simplified proficiency bonus to anti-air for air power calc."""
        # Accurate table is complex; simplified per-level bonus
        BONUS = [0, 0, 2, 5, 9, 14, 14, 22]
        return BONUS[min(self.proficiency, 7)]

    @classmethod
    def from_poi(cls, instance: dict, master: dict) -> "Equipment":
        t = master.get("api_type", [0, 0, 0, 0, 0])
        return cls(
            instance_id=instance["api_id"],
            master_id=instance["api_slotitem_id"],
            name=master.get("api_name", ""),
            level=instance.get("api_level", 0),
            proficiency=instance.get("api_alv", 0),
            eq_type=t[2] if len(t) > 2 else 0,
            icon_type=t[3] if len(t) > 3 else 0,
            firepower=master.get("api_houg", 0),
            torpedo=master.get("api_raig", 0),
            anti_air=master.get("api_tyku", 0),
            armor=master.get("api_souk", 0),
            los=master.get("api_sakuteki", 0),
            asw=master.get("api_taisen", 0),
            evasion=master.get("api_kaihi", 0),
        )


@dataclass
class Ship:
    """A ship in the player's fleet roster."""
    instance_id: int       # api_id
    master_id: int         # api_ship_id
    name: str
    ship_type: int         # api_stype
    level: int             # api_lv
    now_hp: int            # api_nowhp
    max_hp: int            # api_maxhp
    morale: int            # api_cond (50+ = sparkled)
    locked: bool           # api_locked
    speed: int             # api_soku (5=slow, 10=fast, 15=fast+, 20=fast++)
    slot_ids: list[int]    # api_slot — equip instance IDs (6 slots, -1=empty)
    slot_ex_id: int        # api_slot_ex — ex-slot
    slot_counts: list[int] # api_onslot — plane counts per slot
    slot_num: int          # api_slotnum — usable slot count
    in_repair: bool        # api_ndock_time > 0
    repair_time_ms: int    # api_ndock_time

    # Stats (array format [current, max])
    firepower: int         # api_karyoku[0]
    torpedo: int           # api_raisou[0]
    anti_air: int          # api_taiku[0]
    armor: int             # api_soukou[0]
    asw: int               # api_taisen[0]
    evasion: int           # api_kaihi[0]
    los: int               # api_sakuteki[0]

    equipped: list[Optional[Equipment]] = field(default_factory=list)
    equipped_ex: Optional[Equipment] = field(default=None)

    @property
    def ship_class(self) -> Optional[ShipClass]:
        return STYPE_TO_CLASS.get(self.ship_type)

    @property
    def hp_ratio(self) -> float:
        return self.now_hp / self.max_hp if self.max_hp > 0 else 1.0

    @property
    def is_taiha(self) -> bool:
        """大破: HP ≤ 25%."""
        return self.hp_ratio <= 0.25

    @property
    def is_chuuha(self) -> bool:
        """中破: HP ≤ 50%."""
        return self.hp_ratio <= 0.50

    @property
    def is_sparkled(self) -> bool:
        return self.morale >= 50

    @property
    def is_available(self) -> bool:
        """Ship can be assigned to a fleet."""
        return not self.in_repair and not self.is_taiha

    @property
    def naked_los(self) -> int:
        """Estimate naked LoS by subtracting equipped LoS (including EX slot)."""
        equip_los = sum(e.los for e in self.equipped if e)
        if self.equipped_ex:
            equip_los += self.equipped_ex.los
        return max(0, self.los - equip_los)

    @classmethod
    def from_poi(
        cls,
        ship_data: dict,
        master_data: dict,
        equips_map: dict[int, Equipment],
    ) -> "Ship":
        slot_ids = ship_data.get("api_slot", [-1] * 6)
        slot_ex_id = ship_data.get("api_slot_ex", -1)
        slot_counts = ship_data.get("api_onslot", [0] * 6)

        equipped = [equips_map.get(sid) for sid in slot_ids]
        ex_equip = equips_map.get(slot_ex_id) if slot_ex_id and slot_ex_id > 0 else None

        def stat(key: str) -> int:
            v = ship_data.get(key, [0, 0])
            return v[0] if isinstance(v, list) else v

        return cls(
            instance_id=ship_data["api_id"],
            master_id=ship_data["api_ship_id"],
            name=master_data.get("api_name", ""),
            ship_type=master_data.get("api_stype", 0),
            level=ship_data.get("api_lv", 1),
            now_hp=ship_data.get("api_nowhp", 0),
            max_hp=ship_data.get("api_maxhp", 0),
            morale=ship_data.get("api_cond", 49),
            locked=bool(ship_data.get("api_locked", 0)),
            speed=ship_data.get("api_soku", 0),
            slot_ids=slot_ids,
            slot_ex_id=slot_ex_id,
            slot_counts=slot_counts,
            slot_num=ship_data.get("api_slotnum", 4),
            in_repair=ship_data.get("api_ndock_time", 0) > 0,
            repair_time_ms=ship_data.get("api_ndock_time", 0),
            firepower=stat("api_karyoku"),
            torpedo=stat("api_raisou"),
            anti_air=stat("api_taiku"),
            armor=stat("api_soukou"),
            asw=stat("api_taisen"),
            evasion=stat("api_kaihi"),
            los=stat("api_sakuteki"),
            equipped=equipped,
            equipped_ex=ex_equip,
        )


@dataclass
class SortieState:
    """Current sortie progress — from poi Redux state.sortie."""
    in_sortie: bool = False
    map_id: list = field(default_factory=list)   # [area, map] e.g. [5, 5]
    node_id: Optional[int] = None
    boss_id: Optional[int] = None
    combined_flag: int = 0
    escaped_pos: list = field(default_factory=list)
    fleet_id: int = 1

    @property
    def map_str(self) -> str:
        if len(self.map_id) >= 2:
            return f"{self.map_id[0]}-{self.map_id[1]}"
        return ""

    @property
    def is_boss_node(self) -> bool:
        return self.node_id is not None and self.node_id == self.boss_id

    @classmethod
    def from_poi(cls, d: dict) -> "SortieState":
        return cls(
            in_sortie=bool(d.get("in_sortie", False)),
            map_id=d.get("map_id") or [],
            node_id=d.get("node_id"),
            boss_id=d.get("boss_id"),
            combined_flag=d.get("combined_flag", 0),
            escaped_pos=d.get("escaped_pos") or [],
            fleet_id=d.get("fleet_id", 1),
        )


@dataclass
class RepairDock:
    dock_id: int
    state: int           # 0=empty, 1=repairing
    ship_id: int         # 0 if empty
    complete_time_ms: int # 0 if empty

    @property
    def is_empty(self) -> bool:
        return self.state == 0

    @property
    def complete_dt(self) -> Optional["datetime"]:
        from datetime import datetime
        if self.complete_time_ms and self.complete_time_ms > 0:
            return datetime.fromtimestamp(self.complete_time_ms / 1000)
        return None

    @classmethod
    def from_poi(cls, d: dict) -> "RepairDock":
        return cls(
            dock_id=d.get("api_id", 0),
            state=d.get("api_state", 0),
            ship_id=d.get("api_ship_id", 0),
            complete_time_ms=d.get("api_complete_time", 0),
        )


@dataclass
class Construction:
    dock_id: int
    state: int           # 0=empty, 2=building, 3=complete
    ship_id: int         # api_created_ship_id (0 if empty)
    complete_time_ms: int

    @property
    def is_empty(self) -> bool:
        return self.state == 0

    @property
    def is_complete(self) -> bool:
        return self.state == 3

    @property
    def complete_dt(self) -> Optional["datetime"]:
        from datetime import datetime
        if self.complete_time_ms and self.complete_time_ms > 0:
            return datetime.fromtimestamp(self.complete_time_ms / 1000)
        return None

    @classmethod
    def from_poi(cls, d: dict) -> "Construction":
        return cls(
            dock_id=d.get("api_id", 0),
            state=d.get("api_state", 0),
            ship_id=d.get("api_created_ship_id", 0) or d.get("api_item1", 0),
            complete_time_ms=d.get("api_complete_time", 0),
        )


@dataclass
class Quest:
    quest_id: int        # api_no
    category: int        # 1=comp, 2=sortie, 3=exercise, 4=expedition, 5=supply/repair, 6=factory
    quest_type: int      # 1=daily, 2=weekly, 3=monthly, 4=once
    state: int           # 1=active, 2=complete(claimable)
    progress: int        # 0=<50%, 1=50%, 2=80%
    title: str

    @property
    def is_complete(self) -> bool:
        return self.state == 2

    @property
    def progress_str(self) -> str:
        return {0: "  ", 1: "50%", 2: "80%"}[self.progress]

    @classmethod
    def from_poi(cls, d: dict) -> "Quest":
        return cls(
            quest_id=d.get("api_no", 0),
            category=d.get("api_category", 0),
            quest_type=d.get("api_type", 0),
            state=d.get("api_state", 1),
            progress=d.get("api_progress", 0),
            title=d.get("api_title", ""),
        )


@dataclass
class Fleet:
    fleet_id: int          # 1-4
    name: str
    ship_ids: list[int]    # api_ship — roster IDs, -1 = empty
    in_expedition: bool    # api_mission[0] != 0
    expedition_id: int     # api_mission[1] (0 if not on expedition)
    expedition_return_ms: int = 0   # api_mission[2] — Unix ms timestamp, 0 if not on expedition
    ships: list[Optional[Ship]] = field(default_factory=list)

    @classmethod
    def from_poi(cls, fleet_data: dict, ships_map: dict[int, Ship]) -> "Fleet":
        ship_ids = fleet_data.get("api_ship", [-1] * 6)
        mission = fleet_data.get("api_mission", [0, 0])
        ships = [ships_map.get(sid) for sid in ship_ids if sid > 0]
        return cls(
            fleet_id=fleet_data["api_id"],
            name=fleet_data.get("api_name", ""),
            ship_ids=ship_ids,
            in_expedition=mission[0] != 0,
            expedition_id=mission[1],
            expedition_return_ms=mission[2] if len(mission) > 2 else 0,
            ships=ships,
        )


@dataclass
class GameState:
    """Full game state snapshot from poi."""
    ships: dict[int, Ship] = field(default_factory=dict)
    equips: dict[int, Equipment] = field(default_factory=dict)
    fleets: dict[int, Fleet] = field(default_factory=dict)
    resources: dict[str, int] = field(default_factory=dict)
    repair_docks: list[RepairDock] = field(default_factory=list)
    constructions: list[Construction] = field(default_factory=list)
    quests: list[Quest] = field(default_factory=list)
    sortie: SortieState = field(default_factory=SortieState)
    last_event: str = ""
    hq_level: int = 120
    timestamp: int = 0

    def available_ships(self) -> list[Ship]:
        """Ships not in repair, not in expedition, not taiha."""
        in_expedition_ids = {
            sid
            for f in self.fleets.values()
            if f.in_expedition
            for sid in f.ship_ids
            if sid > 0
        }
        return [
            s for s in self.ships.values()
            if s.is_available and s.instance_id not in in_expedition_ids
        ]

    @classmethod
    def from_snapshot(cls, path: str = None) -> "GameState":
        """Load a box_snapshot.json written by poi-plugin-kckit-bridge."""
        if path is None:
            path = os.path.expanduser("~/.kckit/box_snapshot.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        equips_map: dict[int, Equipment] = {}
        for raw in data.get("equips", {}).values():
            master = raw.get("$master", {})
            equip = Equipment.from_poi(raw, master)
            equips_map[equip.instance_id] = equip

        ships_map: dict[int, Ship] = {}
        for raw in data.get("ships", {}).values():
            master = raw.get("$master", {})
            ship = Ship.from_poi(raw, master, equips_map)
            ships_map[ship.instance_id] = ship

        fleets: dict[int, Fleet] = {}
        for raw in data.get("fleets", {}).values():
            fleet = Fleet.from_poi(raw, ships_map)
            fleets[fleet.fleet_id] = fleet

        # Parse repair docks
        raw_repairs = data.get("repairs") or []
        if isinstance(raw_repairs, dict):
            raw_repairs = list(raw_repairs.values())
        repair_docks = [RepairDock.from_poi(r) for r in raw_repairs if isinstance(r, dict)]

        # Parse constructions (may be absent from older snapshots)
        raw_constructions = data.get("constructions") or []
        if isinstance(raw_constructions, dict):
            raw_constructions = list(raw_constructions.values())
        constructions = [Construction.from_poi(c) for c in raw_constructions if isinstance(c, dict)]

        # Parse quests (may be absent; snapshot stores as dict keyed by quest_id)
        raw_quests = data.get("quests") or {}
        if isinstance(raw_quests, dict):
            raw_quests_list = list(raw_quests.values())
        else:
            raw_quests_list = list(raw_quests)
        quests = [Quest.from_poi(q) for q in raw_quests_list if isinstance(q, dict)]

        sortie = SortieState.from_poi(data.get("sortie") or {})

        return cls(
            ships=ships_map,
            equips=equips_map,
            fleets=fleets,
            resources=data.get("resources", {}),
            repair_docks=repair_docks,
            constructions=constructions,
            quests=quests,
            sortie=sortie,
            last_event=data.get("last_event", ""),
            hq_level=data.get("hq_level", 120),
            timestamp=data.get("timestamp", 0),
        )

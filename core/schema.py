"""Strategy YAML schema — data contract for the entire system."""
from __future__ import annotations

import math
from enum import Enum
from pathlib import Path
from typing import Optional
from datetime import date

import yaml
from pydantic import BaseModel, Field, model_validator


class AirState(str, Enum):
    SUPREMACY = "supremacy"    # 制空確保: need >= enemy * 3
    SUPERIORITY = "superiority"  # 制空優勢: need >= ceil(enemy * 1.5)
    PARITY = "parity"          # 制空均衡
    NONE = "none"              # 制空不要

    def min_power_needed(self, enemy_air: int) -> int:
        if self == AirState.SUPREMACY:
            return enemy_air * 3
        if self == AirState.SUPERIORITY:
            return math.ceil(enemy_air * 1.5)
        return 0


class FleetType(str, Enum):
    SINGLE = "single"
    CARRIER_TASK = "carrier_task"   # 空母機動部隊
    SURFACE_TASK = "surface_task"   # 水上打撃部隊
    TRANSPORT = "transport"         # 輸送護衛部隊


class ShipClass(str, Enum):
    DE = "DE"      # 1  海防艦
    DD = "DD"      # 2  駆逐艦
    CL = "CL"      # 3  軽巡洋艦
    CLT = "CLT"    # 4  重雷装巡洋艦
    CA = "CA"      # 5  重巡洋艦
    CAV = "CAV"    # 6  航空巡洋艦
    CVL = "CVL"    # 7  軽空母
    BB = "BB"      # 8  戦艦
    FBB = "FBB"    # 6  高速戦艦
    BBV = "BBV"    # 10 航空戦艦
    CV = "CV"      # 11 正規空母
    SS = "SS"      # 13 潜水艦
    SSV = "SSV"    # 14 潜水空母
    AV = "AV"      # 16 水上機母艦
    LHA = "LHA"    # 17 揚陸艦
    AS = "AS"      # 19 工作艦
    AO = "AO"      # 22 補給艦


# api_stype → ShipClass mapping
STYPE_TO_CLASS: dict[int, ShipClass] = {
    1: ShipClass.DE,
    2: ShipClass.DD,
    3: ShipClass.CL,
    4: ShipClass.CLT,
    5: ShipClass.CA,
    6: ShipClass.CAV,
    7: ShipClass.CVL,
    8: ShipClass.BB,
    9: ShipClass.BB,   # 戦艦 (low speed)
    10: ShipClass.BBV,
    11: ShipClass.CV,
    13: ShipClass.SS,
    14: ShipClass.SSV,
    16: ShipClass.AV,
    17: ShipClass.LHA,
    19: ShipClass.AS,
    22: ShipClass.AO,
}


class SlotRequirement(BaseModel):
    ship_class: list[ShipClass]
    count: int = 1
    min_level: Optional[int] = None
    min_asw: Optional[int] = None     # 対潜値 minimum (api_taisen[0])
    speed: Optional[str] = None       # "high" | "standard" | "slow"
    specific_ships: Optional[list[str]] = None   # 固定艦名（日文）
    notes: Optional[str] = None
    position: Optional[int] = None    # required fleet position (1-6), e.g. Nelson Touch needs flagship=pos1
    role: Optional[str] = None        # role in special attack, e.g. "yamato_main", "nelson_wing"


class SpecialAttack(BaseModel):
    type: str                                              # "yamato_salvo" | "nagato_touch" | "nelson_touch" | "torpedo_ci" | "main_torpedo_ci" | etc.
    ships: list[str] = Field(default_factory=list)        # required ship names for this attack
    flagship: Optional[str] = None                        # ship name that must be flagship
    positions: Optional[list[int]] = None                 # 1-indexed slot positions (e.g. [1,3,5] for Nelson Touch)
    equip_hint: Optional[str] = None                      # equipment notes (e.g. "双主炮+测距仪")
    notes: Optional[str] = None


class FleetRequirement(BaseModel):
    type: FleetType = FleetType.SINGLE
    slots: list[SlotRequirement]

    @model_validator(mode="after")
    def total_count_le_six(self) -> "FleetRequirement":
        total = sum(s.count for s in self.slots)
        if total > 6:
            raise ValueError(f"Fleet slot total {total} exceeds 6")
        return self


class Requirements(BaseModel):
    air_state: AirState = AirState.NONE
    air_power_min: Optional[int] = None    # explicit override; auto-calc if None
    enemy_air_power: Optional[int] = None  # for auto-calculating air_power_min
    los_formula: Optional[int] = 33
    los_min: Optional[float] = None
    routing_nodes: Optional[list[str]] = None   # expected route e.g. ["B","D","F","H"]
    avoid_nodes: Optional[list[str]] = None     # nodes to avoid

    @model_validator(mode="after")
    def resolve_air_power(self) -> "Requirements":
        if self.air_power_min is None and self.enemy_air_power is not None:
            self.air_power_min = self.air_state.min_power_needed(self.enemy_air_power)
        return self


class ExampleShip(BaseModel):
    ship: str          # 艦名（日文）
    equips: list[str]  # 装備名リスト（最大6）


class PresetConfig(BaseModel):
    name: str
    fleet: FleetRequirement
    requirements: Requirements = Field(default_factory=Requirements)
    equip_notes: Optional[str] = None
    example: Optional[list[ExampleShip]] = None
    tags: list[str] = Field(default_factory=list)   # e.g. ["low_cost", "fast"]
    special_attacks: list[SpecialAttack] = Field(default_factory=list)


class MapStrategy(BaseModel):
    map: str           # e.g. "5-4"
    source: Optional[str] = None
    imported_at: Optional[str] = None
    notes: Optional[str] = None
    presets: list[PresetConfig]

    def get_preset(self, name: str) -> Optional[PresetConfig]:
        return next((p for p in self.presets if p.name == name), None)

    def get_presets_for_quest(self, quest_id: str) -> list[PresetConfig]:
        return [p for p in self.presets if quest_id in p.tags]


# ── Quest index ───────────────────────────────────────────────────────────────

class QuestMapRec(BaseModel):
    map_id: str
    fleet_hint: Optional[str] = None   # solo fleet; None = same as synergy partner's fleet
    notes: Optional[str] = None
    synergy_quests: list[str] = Field(default_factory=list)
    # fleet variant to use on this map when also running a synergy quest simultaneously
    # key = synergy quest_id, value = compact fleet hint for that combo
    combo_fleets: Optional[dict[str, str]] = None


class QuestEntry(BaseModel):
    quest_id: str       # e.g. "Bw6"
    quest_name: str     # Japanese quest name
    category: str       # "daily" | "weekly" | "monthly" | "quarterly" | "yearly"
    requirement: str    # what the quest requires
    maps: list[QuestMapRec] = Field(default_factory=list)


class QuestIndex(BaseModel):
    source: Optional[str] = None
    quests: list[QuestEntry]

    def get_quest(self, quest_id: str) -> Optional[QuestEntry]:
        return next((q for q in self.quests if q.quest_id == quest_id), None)

    def quests_for_map(self, map_id: str) -> list[QuestEntry]:
        return [q for q in self.quests if any(m.map_id == map_id for m in q.maps)]


# ── YAML helpers ──────────────────────────────────────────────────────────────

def load_strategy(path: str) -> MapStrategy:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return MapStrategy.model_validate(data)


def save_strategy(strategy: MapStrategy, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(
            strategy.model_dump(mode="json", exclude_none=True),
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


STRATEGIES_ROOT = Path(__file__).parent.parent / "strategies"
MAPS_DIR = STRATEGIES_ROOT / "maps"
QUESTS_DIR = STRATEGIES_ROOT / "quests"

# World area names for directory labels
_WORLD_DIR: dict[str, str] = {
    "1": "world1_镇守府海域",
    "2": "world2_南西群岛海域",
    "3": "world3_北方海域",
    "4": "world4_西方海域",
    "5": "world5_南方海域",
    "6": "world6_中部海域",
    "7": "world7_南西海域",
}

# Map area names for file labels
_MAP_NAME: dict[str, str] = {
    "1-1": "鎮守府正面海域", "1-2": "南西諸島沖", "1-3": "製油所地帯沿岸",
    "1-4": "南西諸島防衛線", "1-5": "鎮守府近海", "1-6": "鎮守府近海航路",
    "2-1": "南西諸島近海", "2-2": "バシー海峡", "2-3": "東部オリョール海",
    "2-4": "沖ノ島海域", "2-5": "沖ノ島沖",
    "3-1": "モーレイ海", "3-2": "キス島沖", "3-3": "アルフォンシーノ方面",
    "3-4": "北方海域全域", "3-5": "北方AL海域",
    "4-1": "ジャム島沖", "4-2": "カレー洋海域", "4-3": "リランカ島",
    "4-4": "カスガダマ島", "4-5": "カレー洋リランカ島沖",
    "5-1": "南方海域前面", "5-2": "珊瑚諸島沖", "5-3": "サブ島沖海域",
    "5-4": "サーモン海域", "5-5": "サーモン海域北方",
    "6-1": "中部海域哨戒線", "6-2": "MS諸島沖", "6-3": "グアノ環礁沖海域",
    "6-4": "中部北海域ピーコック島沖", "6-5": "KW環礁沖海域",
    "7-1": "ブルネイ泊地沖", "7-2": "タウイタウイ泊地沖", "7-3": "ペナン島沖",
    "7-4": "昭南本土航路", "7-5": "ジャワ島沖",
}

_QUEST_CATEGORY_DIR: dict[str, str] = {
    "daily": "daily",
    "weekly": "weekly",
    "monthly": "monthly",
    "quarterly": "quarterly",
    "yearly": "yearly",
}


def _quest_filename(quest_id: str, quest_name: str) -> str:
    """Build a safe filename from quest id + name, e.g. 'Bw6_敵東方艦隊を撃滅せよ！'."""
    safe = quest_name.replace("/", "").replace("\\", "").replace("\x00", "")
    return f"{quest_id}_{safe}"


def quest_yaml_path(quest_id: str, quest_name: str = "", category: str = "") -> Path:
    """Return canonical path, e.g. strategies/quests/weekly/Bw6_敵東方艦隊を撃滅せよ！.yaml"""
    subdir = _QUEST_CATEGORY_DIR.get(category, "other")
    filename = _quest_filename(quest_id, quest_name) if quest_name else quest_id
    return QUESTS_DIR / subdir / f"{filename}.yaml"


def map_yaml_path(map_id: str) -> Path:
    """Return canonical path, e.g. '3-5' → strategies/maps/world3_北方海域/3-5_北方AL海域.yaml"""
    world = map_id.split("-")[0]
    world_dir = _WORLD_DIR.get(world, f"world{world}")
    map_name = _MAP_NAME.get(map_id, "")
    filename = f"{map_id}_{map_name}" if map_name else map_id.replace("-", "_")
    return MAPS_DIR / world_dir / f"{filename}.yaml"


def load_quests(path: str) -> QuestIndex:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return QuestIndex.model_validate(data)


def save_quests(qi: QuestIndex, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(
            qi.model_dump(mode="json", exclude_none=True),
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


def load_quest(quest_id: str) -> QuestEntry:
    """Load quest by ID, searching the category subdirectories."""
    matches = list(QUESTS_DIR.rglob(f"{quest_id}_*.yaml"))
    if not matches:
        # fallback: flat file (legacy)
        matches = list(QUESTS_DIR.glob(f"{quest_id}.yaml"))
    if not matches:
        raise FileNotFoundError(f"Quest {quest_id} not found under {QUESTS_DIR}")
    with open(matches[0], encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return QuestEntry.model_validate(data)


def save_quest(entry: QuestEntry, path: Optional[str] = None) -> None:
    if path is None:
        p = quest_yaml_path(entry.quest_id, entry.quest_name, entry.category)
    else:
        p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        yaml.dump(
            entry.model_dump(mode="json", exclude_none=True),
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )


def merge_strategy(existing_path: str, new_strategy: MapStrategy) -> MapStrategy:
    """Merge presets from new_strategy into existing file.
    Existing presets with the same name are replaced; new ones are appended.
    """
    try:
        existing = load_strategy(existing_path)
    except FileNotFoundError:
        return new_strategy

    existing_by_name = {p.name: i for i, p in enumerate(existing.presets)}
    for preset in new_strategy.presets:
        if preset.name in existing_by_name:
            existing.presets[existing_by_name[preset.name]] = preset
        else:
            existing.presets.append(preset)
    return existing

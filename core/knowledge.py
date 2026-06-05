"""Equipment knowledge base — master stats + substitution tiers.
Also contains KanColle special attack rules sourced from zh.kcwiki.cn.
"""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional
from rapidfuzz import process, fuzz
from .models import Equipment


class KnowledgeBase:
    """Equipment knowledge base: master stats + role-based substitution tiers."""

    def __init__(self, equip_db_path: str, subs_path: str):
        with open(equip_db_path, encoding="utf-8") as f:
            raw = json.load(f)
        # keys are strings in JSON; convert to int
        self.equip_db: dict[int, dict] = {int(k): v for k, v in raw.items()}
        with open(subs_path, encoding="utf-8") as f:
            self.subs: dict[str, list[int]] = json.load(f)

        # name → master_id index
        self._name_to_id: dict[str, int] = {v["name"]: k for k, v in self.equip_db.items()}
        self._all_names: list[str] = list(self._name_to_id.keys())

        # master_id → roles that contain it
        self._id_to_roles: dict[int, list[str]] = {}
        for role, ids in self.subs.items():
            for mid in ids:
                self._id_to_roles.setdefault(mid, []).append(role)

    def master_id_for_name(self, name: str) -> Optional[int]:
        if name in self._name_to_id:
            return self._name_to_id[name]
        result = process.extractOne(name, self._all_names, scorer=fuzz.WRatio)
        if result and result[1] >= 85:
            return self._name_to_id[result[0]]
        return None

    def find_in_pool(self, role: str, pool: list[Equipment], used: set[int]) -> Optional[Equipment]:
        tier = self.subs.get(role, [])
        pool_by_master: dict[int, list[Equipment]] = {}
        for e in pool:
            if e.instance_id not in used:
                pool_by_master.setdefault(e.master_id, []).append(e)
        for mid in tier:
            if mid in pool_by_master:
                return pool_by_master[mid][0]
        return None

    def find_substitute(self, equip_name: str, pool: list[Equipment], used: set[int]) -> Optional[Equipment]:
        # 1. Try exact name match in pool
        for e in pool:
            if e.instance_id not in used and e.name == equip_name:
                return e
        # 2. Fuzzy master_id lookup → role → pool
        mid = self.master_id_for_name(equip_name)
        if mid is not None:
            # Try exact master_id first
            for e in pool:
                if e.instance_id not in used and e.master_id == mid:
                    return e
            # Try roles
            for role in self._id_to_roles.get(mid, []):
                found = self.find_in_pool(role, pool, used)
                if found:
                    return found
        # 3. Fuzzy search directly in pool by name
        pool_names = {e.name: e for e in pool if e.instance_id not in used}
        if pool_names:
            result = process.extractOne(equip_name, list(pool_names.keys()), scorer=fuzz.WRatio)
            if result and result[1] >= 80:
                return pool_names[result[0]]
        return None

    def default_roles_for_ship(self, stype: int, slot_index: int, slot_count: int) -> list[str]:
        """Return ordered role list to try for this ship type + slot position."""
        if slot_count == 0:
            return []
        # stype values from api_mst_stype
        # 1=DE, 2=DD, 3=CL, 4=CLT, 5=CA, 6=CAV, 7=CVL, 8/9=BB/FBB, 10=BBV, 11=CV, 13=SS, 14=SSV, 16=AV, 22=AO
        roles_by_stype: dict[int, list[list[str]]] = {
            1:  [["dd_gun_d"], ["sonar"], ["depth_charge"], ["depth_charge"]],      # DE
            2:  [["dd_gun_d"], ["torp_surface"], ["torp_surface"], ["sonar"]],      # DD
            3:  [["cl_gun"], ["cl_gun"], ["torp_surface"], ["radar_small_los"]],    # CL
            4:  [["cl_gun"], ["torp_surface"], ["torp_surface"], ["radar_small_los"]],  # CLT
            5:  [["ca_gun"], ["ca_gun"], ["ap_shell"], ["seaplane_recon"]],         # CA
            6:  [["ca_gun"], ["ca_gun"], ["seaplane_recon"], ["seaplane_recon"]],   # CAV
            8:  [["bb_gun_std", "bb_gun_super"], ["bb_gun_std", "bb_gun_super"], ["ap_shell"], ["seaplane_night"]],  # BB
            9:  [["bb_gun_std", "bb_gun_super"], ["bb_gun_std", "bb_gun_super"], ["ap_shell"], ["seaplane_night"]],  # BB(low speed)
            10: [["bb_gun_std"], ["bb_gun_std"], ["seaplane_recon"], ["seaplane_recon"]],  # BBV
            13: [["torp_sub"], ["torp_sub"], ["torp_sub"], ["sonar"]],              # SS
            14: [["torp_sub"], ["torp_sub"], ["torp_sub"], ["sonar"]],              # SSV
        }
        slot_roles = roles_by_stype.get(stype, [])
        if slot_index >= len(slot_roles):
            return []
        return slot_roles[slot_index]


# ── KanColle special attack rules ────────────────────────────────────────────

# Each entry documents trigger conditions, valid ship combos, equipment bonuses,
# and damage multipliers for the named special attack.
#
# Field conventions:
#   flagship_ships        — exact Japanese ship names valid as flagship (slot 1)
#   slot_requirements     — slot index (1-based) → description or list of valid ships
#   valid_partners        — {ship_name: extra_multiplier} for 2-slot attacks
#   valid_pairs           — list of {slot2, slot3, interchangeable} for 3-slot attacks
#   base_multipliers      — damage multiplier per firing sub-attack (varies by partner for some)
#   equipment_bonuses     — {equip_name: multiplier}; stack multiplicatively
#   formation_single      — required formation for single fleet
#   formation_combined    — required formation for combined fleet
#   day_battle            — True if can trigger during day battle
#   night_battle          — True if can trigger during night battle
#   notes                 — important clarifications

SPECIAL_ATTACKS: dict[str, dict] = {

    # ── Nelson Touch ─────────────────────────────────────────────────────────
    "nelson_touch": {
        "name_ja": "ネルソンタッチ",
        "name_zh": "纳尔逊触发（nelson touch）",
        "flagship_ships": ["ネルソン", "ロドニー"],
        "slot_requirements": {
            1: "ネルソン or ロドニー (小破以下)",
            3: "non-carrier (小破以下)",
            5: "non-carrier (小破以下)",
        },
        # Fleet must be exactly 6 ships
        "fleet_size": 6,
        "valid_partners": None,   # no fixed partner — slots 3 & 5 fire too
        "valid_pairs": None,
        # Base multiplier: 2.5× on T-disadvantage (T不利), 2.0× otherwise
        # If BOTH slot 3 & 5 are Nelson-class, each sub-attack gains +1.2×
        "base_multipliers": {
            "default": 2.0,
            "t_disadvantage": 2.5,
            "nelson_class_bonus": 1.2,  # extra per slot if slot is Nelson-class
        },
        "equipment_bonuses": {},   # no specific equipment required
        "formation_single": "複縦陣",
        "formation_combined": "第二警戒航行序列",
        "day_battle": True,
        "night_battle": False,
        "notes": (
            "Fires 3 sub-attacks at slots 1, 3, 5. "
            "Flagship must be 小破以下; slots 3 and 5 must be non-carriers. "
            "Nelson-class ship in slot 3 or 5 each add ×1.2 to that sub-attack."
        ),
    },

    # ── Nagato Touch (長門型改二特殊砲撃) ────────────────────────────────────
    "nagato_touch": {
        "name_ja": "長門型改二特殊砲撃",
        "name_zh": "长门改二特殊炮击（长门touch）",
        "flagship_ships": ["長門改二", "陸奥改二"],
        "slot_requirements": {
            1: "長門改二 or 陸奥改二 (小破以下)",
            2: "any BB or BBV (中破以下)",
        },
        "valid_partners": None,   # any BB/BBV allowed; multiplier varies by specific ship
        "valid_pairs": None,
        # Multipliers depend on which ship is in slot 2:
        #   slot2=長門改二 or 陸奥改二 (the other one): atk1=atk2=1.68, atk3=1.68
        #   slot2=長門改  or 陸奥改  (non-改二):       atk1=atk2=1.61, atk3=1.62
        #   slot2=ネルソン (only when 長門改二 is FS): atk1=atk2=1.54, atk3=1.50
        #   slot2=other BB/BBV:                        atk1=atk2=1.40, atk3=1.20
        "base_multipliers": {
            "nagato_kai2_or_mutsu_kai2": {"atk1": 1.68, "atk2": 1.68, "atk3": 1.68},
            "nagato_kai_or_mutsu_kai":   {"atk1": 1.61, "atk2": 1.61, "atk3": 1.62},
            "nelson_slot2":              {"atk1": 1.54, "atk2": 1.54, "atk3": 1.50},
            "other_bb_bbv":              {"atk1": 1.40, "atk2": 1.40, "atk3": 1.20},
        },
        "equipment_bonuses": {
            "電探(索敵≥5)": 1.15,
            "徹甲弾": 1.35,
        },
        "formation_single": "梯形陣",
        "formation_combined": "第二警戒航行序列",
        "day_battle": True,
        "night_battle": True,
        "notes": (
            "Slot 2 ship determines the multiplier tier. "
            "ネルソン as slot-2 partner applies only when 長門改二 is flagship. "
            "Equipment bonuses stack multiplicatively."
        ),
    },

    # ── Yamato 2-ship (大和型改二特殊砲撃 2艦) ───────────────────────────────
    "yamato_2ship": {
        "name_ja": "大和型改二特殊砲撃（2艦）",
        "name_zh": "大和改二重特殊炮击（2舰）",
        # 大和改二(重) — both 大和改二重 and 大和改二 can trigger
        "flagship_ships": ["大和改二重", "大和改二"],
        "slot_requirements": {
            1: "大和改二重 or 大和改二 (旗舰)",
            2: "one of the valid partner ships",
        },
        "valid_partners": {
            "武蔵改二":     1.20,
            "Iowa改":       1.10,
            "Richelieu改":  1.10,
            "Richelieu Deux": 1.10,
            "Jean Bart改":  1.10,
            "Bismarck drei": 1.10,
        },
        "valid_pairs": None,
        "base_multipliers": {
            "slot1": 1.40,
            "slot2": 1.55,
            # Final damage = base × partner_extra × equip_bonus × ammo_modifier
        },
        "equipment_bonuses": {
            "徹甲弾": 1.35,
            "15m二重測距儀+21号電探改二": 1.265,
            "電探(索敵≥5)": 1.15,
        },
        "ammo_modifier": 1.6,
        "formation_single": "梯形陣",
        "formation_combined": "第四警戒航行序列",
        "day_battle": True,
        "night_battle": True,
        "notes": (
            "大和改二(重) — both 大和改二重 and 大和改二 trigger this attack. "
            "The two forms represent different tactical modes (改二重=slow/heavy, "
            "改二=fast) and should be separate presets. Partner ship extra "
            "multiplier applies to all sub-attacks."
        ),
    },

    # ── Yamato 3-ship (大和型改二特殊砲撃 3艦) ───────────────────────────────
    "yamato_3ship": {
        "name_ja": "大和型改二特殊砲撃（3艦）",
        "name_zh": "大和改二重特殊炮击（3舰）",
        # 大和改二(重) — both 大和改二重 and 大和改二 can trigger
        "flagship_ships": ["大和改二重", "大和改二"],
        "slot_requirements": {
            1: "大和改二重 or 大和改二 (旗舰)",
            2: "first ship of a valid pair",
            3: "second ship of the same valid pair",
        },
        # Valid (slot2, slot3) pairs; interchangeable=True means slot2/slot3 can swap
        "valid_pairs": [
            {"slot2": "武蔵改二",   "slot3": "長門改二",   "interchangeable": False},
            {"slot2": "武蔵改二",   "slot3": "陸奥改二",   "interchangeable": False},
            {"slot2": "長門改二",   "slot3": "陸奥改二",   "interchangeable": True},
            {"slot2": "伊勢改二",   "slot3": "日向改二",   "interchangeable": True},
            {"slot2": "山城改二",   "slot3": "扶桑改二",   "interchangeable": True},
            {"slot2": "ネルソン改", "slot3": "ロドニー改", "interchangeable": True},
            {"slot2": "ネルソン改", "slot3": "ウォースパイト改", "interchangeable": True},
            {"slot2": "ウォースパイト改", "slot3": "Valiant改", "interchangeable": True},
            {"slot2": "Roma",       "slot3": "Italia",     "interchangeable": True},
            {"slot2": "比叡改二丙", "slot3": "霧島改二丙", "interchangeable": True},
            {"slot2": "South Dakota", "slot3": "Washington", "interchangeable": True},
            {"slot2": "コロラド改", "slot3": "メリーランド改", "interchangeable": True},
            {"slot2": "Richelieu改", "slot3": "Jean Bart改", "interchangeable": True},
        ],
        "valid_partners": None,
        "base_multipliers": {
            "slot1": 1.50,
            "slot2": 1.50,
            "slot3": 1.65,
        },
        # Per-ship extra multipliers applied on top of the base
        "ship_extra_multipliers": {
            "武蔵改二":  {"slot2": 1.21},
            "長門改二":  {"any_slot": 1.10},
            "陸奥改二":  {"any_slot": 1.10},
            "伊勢改二":  {"slot2": 1.05},
            "日向改二":  {"slot2": 1.05},   # when used as slot2 in its own pair
        },
        "equipment_bonuses": {
            "徹甲弾": 1.35,
            "15m二重測距儀+21号電探改二": 1.265,
            "電探(索敵≥5)": 1.15,
        },
        "ammo_modifier": 1.8,
        "formation_single": "梯形陣",
        "formation_combined": "第四警戒航行序列",
        "day_battle": True,
        "night_battle": True,
        "notes": (
            "大和改二(重) — both 大和改二重 and 大和改二 trigger this. "
            "武蔵改二 in slot 2 always pairs with 長門改二 or 陸奥改二 in slot 3 "
            "(not interchangeable: 武蔵改二 must be slot 2). "
            "For all other pairs marked interchangeable, the two ships may swap slots."
        ),
    },

    # ── Colorado Special (コロラド特殊砲撃) ──────────────────────────────────
    "colorado_special": {
        "name_ja": "コロラド特殊砲撃",
        "name_zh": "科罗拉多特殊炮击",
        "flagship_ships": ["コロラド", "メリーランド"],
        "slot_requirements": {
            1: "コロラド or メリーランド (旗舰)",
            2: "BB or BBV",
            3: "BB or BBV",
        },
        # BIG7 ships receive higher sub-attack multipliers
        "big7_ships": [
            "長門", "陸奥", "ネルソン", "ロドニー",
            "コロラド", "メリーランド", "大和", "武蔵",
        ],
        "valid_partners": None,
        "valid_pairs": None,
        "base_multipliers": {
            # Multiplier is higher when slot-2/slot-3 ships are BIG7 members
            "big7_partner": "higher",
            "non_big7_partner": "lower",
        },
        "equipment_bonuses": {
            "電探": 1.15,
            "SG Radar Late Model": 1.3225,
            "徹甲弾": 1.35,
        },
        "formation_single": "梯形陣",
        "formation_combined": "第二警戒航行序列",
        "day_battle": True,
        "night_battle": True,
        "notes": (
            "Fires 3 sub-attacks (slots 1, 2, 3). "
            "BIG7 ships in slots 2 and 3 each grant a higher sub-attack multiplier. "
            "SG Radar Late Model stacks with 電探 bonus."
        ),
    },

    # ── Kongou Night Raid (僚艦夜戦突撃) ─────────────────────────────────────
    "kongou_night_raid": {
        "name_ja": "僚艦夜戦突撃",
        "name_zh": "金刚级僚舰夜战突击",
        # Flagship determines which partners are valid
        "flagship_ships": [
            "金剛改二丙", "比叡改二丙", "榛名改二乙", "榛名改二丙", "霧島改二丙",
        ],
        "slot_requirements": {
            1: "one of the valid flagship ships (旗舰)",
            2: "one of the valid partner ships for the chosen flagship",
        },
        # flagship → list of valid slot-2 partners
        "valid_partners_by_flagship": {
            "金剛改二丙": ["比叡改二丙", "榛名改二乙", "榛名改二丙", "ウォースパイト"],
            "比叡改二丙": ["金剛改二丙", "榛名改二乙", "榛名改二丙", "霧島改二"],
            "榛名改二乙": ["金剛改二丙", "比叡改二丙"],
            "榛名改二丙": ["金剛改二丙", "比叡改二丙"],
            "霧島改二丙": ["金剛改二丙", "比叡改二丙", "South Dakota改"],
        },
        "valid_partners": None,   # see valid_partners_by_flagship above
        "valid_pairs": None,
        "base_multipliers": {
            "normal": 2.4,
            "t_advantage": 3.0,
            "t_disadvantage": 1.92,
        },
        "equipment_bonuses": {},
        "ammo_modifier": 1.2,
        "formation_single": ["単縦陣", "梯形陣"],
        "formation_combined": ["第二警戒航行序列", "第四警戒航行序列"],
        "day_battle": False,
        "night_battle": True,
        "notes": (
            "Night battle only. "
            "Flagship determines which ships are valid slot-2 partners. "
            "霧島改二 (non-丙) is a valid partner for 比叡改二丙, "
            "but 霧島改二丙 is a flagship (not a partner). "
            "South Dakota改 can partner with 霧島改二丙."
        ),
    },

    # ── Richelieu Special (リシュリュー改特殊砲撃) ────────────────────────────
    "richelieu_special": {
        "name_ja": "リシュリュー改特殊砲撃",
        "name_zh": "黎塞留改特殊炮击",
        "flagship_ships": ["Richelieu改", "Richelieu Deux", "Jean Bart改"],
        "slot_requirements": {
            1: "Richelieu改, Richelieu Deux, or Jean Bart改 (旗舰)",
            2: "the other ship from the valid flagship pair",
        },
        # Each flagship pairs with a specific ship:
        #   Richelieu改 or Richelieu Deux ↔ Jean Bart改
        "valid_pairs": [
            {"slot2": "Richelieu改",   "slot3": "Jean Bart改",  "interchangeable": True},
            {"slot2": "Richelieu Deux", "slot3": "Jean Bart改", "interchangeable": True},
        ],
        "valid_partners": None,
        "base_multipliers": {},   # exact values not specified in kcwiki excerpt
        "equipment_bonuses": {
            "徹甲弾": 1.35,
            "電探": 1.15,
        },
        "formation_single": "複縦陣",
        "formation_combined": "第二警戒航行序列",
        "day_battle": True,
        "night_battle": False,
        "notes": (
            "Richelieu改 and Richelieu Deux are effectively the same ship for "
            "triggering purposes (different remodel names). Pair with Jean Bart改."
        ),
    },

    # ── Warspite–Valiant (ウォースパイト改特殊砲撃) ───────────────────────────
    "warspite_valiant": {
        "name_ja": "ウォースパイト改特殊砲撃",
        "name_zh": "厌战改特殊炮击",
        "flagship_ships": ["ウォースパイト改", "Valiant改"],
        "slot_requirements": {
            1: "ウォースパイト改 or Valiant改 (旗舰)",
            2: "the other ship of the pair",
        },
        "valid_pairs": [
            {"slot2": "ウォースパイト改", "slot3": "Valiant改", "interchangeable": True},
        ],
        "valid_partners": None,
        "base_multipliers": {},   # exact values not specified in kcwiki excerpt
        "equipment_bonuses": {
            "徹甲弾": 1.35,
            "電探": 1.15,
        },
        "formation_single": "梯形陣",
        "formation_combined": "第二警戒航行序列",
        "day_battle": True,
        "night_battle": False,
        "notes": (
            "ウォースパイト改 and Valiant改 can each serve as flagship; "
            "the other must be in slot 2."
        ),
    },
}


# ── Convenience helpers ───────────────────────────────────────────────────────

def flagship_to_attack_types(ship_name: str) -> list[str]:
    """Return all special attack type keys where *ship_name* is a valid flagship."""
    return [
        key for key, data in SPECIAL_ATTACKS.items()
        if ship_name in data.get("flagship_ships", [])
    ]


def all_flagship_ships() -> set[str]:
    """Return the union of all valid flagship ship names across all attacks."""
    result: set[str] = set()
    for data in SPECIAL_ATTACKS.values():
        result.update(data.get("flagship_ships", []))
    return result


# Compact type list exposed for prompt injection
SPECIAL_ATTACK_TYPES: list[str] = list(SPECIAL_ATTACKS.keys())

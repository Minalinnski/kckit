#!/usr/bin/env python3
"""
Build master knowledge databases from poi's navy-album master.json (api_start2).

Outputs:
  data/equip_db.json   — full equipment stats, keyed by id (int)
  data/ship_db.json    — ship master stats, keyed by id (int)
  data/equip_subs.json — substitution tiers: role → ordered list of equip ids
"""
from __future__ import annotations
import json, sys
from pathlib import Path

MASTER = Path.home() / "Library/Application Support/poi/navy-album/master.json"
DATA = Path(__file__).parent.parent / "data"

# ── LoS multipliers by api_type[3] (icon_type) ────────────────────────────────
# Matches core/models.py LOS_FACTORS exactly (33-shiki formula)
LOS_FACTORS: dict[int, float] = {
    9:  1.04,  # 艦上偵察機 (carrier recon)
    10: 1.00,  # 水上偵察機 (seaplane recon)
    11: 1.04,  # 水上爆撃機 (seaplane bomber)
    8:  0.8,   # 艦上攻撃機 (torpedo bomber — rarely has LoS)
    41: 1.00,  # 大型飛行艇
}
LOS_DEFAULT = 0.6  # 電探・その他

# Aircraft types by api_type[2] — matches core/models.py AIRCRAFT_TYPES
AIRCRAFT_ICON_TYPES = {
    6,   # 艦戦
    7,   # 艦爆
    8,   # 艦攻
    9,   # 艦偵
    10,  # 水偵
    11,  # 水爆
    45,  # 水戦
    56,  # 噴式戦闘爆撃機
    57,  # 噴式攻撃機
    58,  # 噴式偵察機
    59,  # 噴式偵察爆撃機
}


def build_equip_db(master: dict) -> dict[int, dict]:
    db: dict[int, dict] = {}
    for e in master["api_mst_slotitem"]:
        eid = e["api_id"]
        eq_type   = e["api_type"][2]   # api_type[2] = equipment category (AIRCRAFT_TYPES check)
        icon_type = e["api_type"][3]   # api_type[3] = icon type (LoS factor lookup)
        db[eid] = {
            "id":       eid,
            "name":     e["api_name"],
            "type":     eq_type,        # eq_type (api_type[2]) — same convention as models.py
            "icon":     icon_type,      # icon_type (api_type[3]) — for LOS_FACTORS lookup
            "fire":     e.get("api_houg", 0),
            "torpedo":  e.get("api_raig", 0),
            "bomb":     e.get("api_baku", 0),
            "aa":       e.get("api_tyku", 0),
            "asw":      e.get("api_tais", 0),
            "los":      e.get("api_saku", 0),
            "armor":    e.get("api_souk", 0),
            "evasion":  e.get("api_houk", 0),
            "range":    e.get("api_leng", 0),
            "rare":     e.get("api_rare", 0),
            "los_mult": LOS_FACTORS.get(icon_type, LOS_DEFAULT),
            "is_aircraft": eq_type in AIRCRAFT_ICON_TYPES,
        }
    return db


def build_ship_db(master: dict) -> dict[int, dict]:
    # stype name map
    stype_names = {s["api_id"]: s["api_name"] for s in master["api_mst_stype"]}
    db: dict[int, dict] = {}
    for s in master["api_mst_ship"]:
        sid = s["api_id"]
        db[sid] = {
            "id":       sid,
            "name":     s["api_name"],
            "stype":    s.get("api_stype", 0),
            "stype_name": stype_names.get(s.get("api_stype", 0), ""),
            "ctype":    s.get("api_ctype", 0),
            "slot_num": s.get("api_slot_num", 0),
            "slots":    s.get("api_maxeq", []),
            "speed":    s.get("api_soku", 0),   # 5=low, 10=medium, 15=fast, 20=fastest
            # base stats [min, max] — max = fully married remodel max
            "fire":     s.get("api_houg", [0, 0]),
            "torpedo":  s.get("api_raig", [0, 0]),
            "aa":       s.get("api_tyku", [0, 0]),
            "asw":      s.get("api_tais", [0, 0]) if s.get("api_tais") else [0, 0],
            "los":      s.get("api_saku", [0, 0]) if s.get("api_saku") else [0, 0],
            "armor":    s.get("api_souk", [0, 0]),
        }
    return db


def build_equip_subs(equip_db: dict[int, dict]) -> dict[str, list[int]]:
    """
    Substitution tiers: role_name → [equip_id, ...] ordered best→worst.
    When composer needs equipment for a role, it picks the first id the player owns.
    """
    def ids_sorted(names: list[str]) -> list[int]:
        """Return IDs for given names in the order listed (best first)."""
        name_to_id = {v["name"]: k for k, v in equip_db.items()}
        return [name_to_id[n] for n in names if n in name_to_id]

    subs: dict[str, list[int]] = {}

    # ── 大口径主砲 (BB main guns) ──────────────────────────────────────────────
    subs["bb_gun_super"] = ids_sorted([
        "試製51cm三連装砲",      # id=465, fire=36
        "51cm連装砲",            # id=281, fire=32
        "試製51cm連装砲",        # id=128, fire=30
    ])
    subs["bb_gun_std"] = ids_sorted([
        "46cm三連装砲改",        # id=276, fire=27
        "46cm三連装砲",          # id=9,   fire=26
        "16inch三連装砲 Mk.7+GFCS",  # id=183, fire=24
        "41cm三連装砲改二",      # id=290, fire=23
        "41cm三連装砲改",        # id=236, fire=22
        "41cm連装砲改",          # id=78,  fire=20
        "41cm連装砲",            # id=11
        "38cm四連装砲改 deux",   # id=468, fire=24
        "38cm四連装砲改",        # id=246, fire=22
        "35.6cm三連装砲改",      # id=328, fire=20
    ])
    subs["bb_gun_foreign"] = ids_sorted([
        "16inch三連装砲 Mk.7+GFCS",
        "16inch三連装砲 Mk.7",
        "16inch Mk.I三連装砲改+FCR type284",
        "16inch三連装砲 Mk.6+GFCS",
        "16inch三連装砲 Mk.6 mod.2",
        "16inch三連装砲 Mk.6",
        "Bismarck drei主砲",
        "38cm四連装砲改 deux",
        "38cm四連装砲改",
        "38cm連装砲改",
    ])

    # ── 徹甲弾 (AP shells) ─────────────────────────────────────────────────────
    subs["ap_shell"] = ids_sorted([
        "一式徹甲弾改",   # id=365, fire+11
        "一式徹甲弾",     # id=116, fire+9
        "九一式徹甲弾",   # id=36,  fire+8
    ])

    # ── 中口径主砲 (CA/CAV guns) ──────────────────────────────────────────────
    subs["ca_gun"] = ids_sorted([
        "試製20.3cm(4号)連装砲",   # id=520, fire=11
        "20.3cm(3号)連装砲",       # id=50,  fire=10
        "20.3cm(2号)連装砲",       # id=90,  fire=9
        "SKC34 20.3cm連装砲",      # id=123, fire=10
        "20.3cm連装砲",            # id=14
        "8inch三連装砲 Mk.9 mod.2",# id=357, fire=12 (foreign)
        "Zara due主砲",
    ])

    # ── 小口径主砲 DD guns ─────────────────────────────────────────────────────
    subs["dd_gun_d"] = ids_sorted([
        "12.7cm連装砲D型改三",   # id=366 (D型最高 fire=3 aa=4 asw=2)
        "12.7cm連装砲D型改二",   # id=267
        "12.7cm連装砲D型改",     # id=220
        "12.7cm連装砲D型",       # id=170
        "12.7cm連装砲B型改四",   # id=289
        "12.7cm連装砲B型改三",   # id=174
        "12.7cm連装砲B型改二",   # id=104
    ])
    subs["dd_gun_aa"] = ids_sorted([
        "5inch単装砲 Mk.30改+GFCS Mk.37",  # id=313 高AA US DD
        "5inch単装砲 Mk.30改",             # id=308
        "5inch単装砲 Mk.30",               # id=284
        "12.7cm連装砲C型改三",             # id=470 C型 (asw重視)
        "10cm連装高角砲+高射装置",          # id=266
        "10cm連装高角砲改+増設機銃",        # id=275
    ])
    subs["cl_gun"] = ids_sorted([
        "15.2cm連装砲改二",              # id=381 矢矧用
        "15.2cm連装砲改",                # id=135
        "15.2cm連装砲",                  # id=11...
        "15.5cm三連装砲改",              # id=235
        "Bofors 15cm連装速射砲 Mk.9改+単装速射砲 Mk.10改 Model 1938", # id=361
    ])

    # ── 魚雷 surface torpedoes ────────────────────────────────────────────────
    subs["torp_surface"] = ids_sorted([
        "試製61cm六連装(酸素)魚雷",           # id=179, torp=14
        "61cm五連装(酸素)魚雷",               # id=58,  torp=12
        "61cm四連装(酸素)魚雷後期型",          # id=286, torp=11
        "533mm五連装魚雷(後期型)",             # id=376
        "試製61cm三連装(酸素)魚雷",
        "61cm四連装(酸素)魚雷",               # id=68
        "61cm三連装(酸素)魚雷",
        "533mm 三連装魚雷(53-39型)",           # id=360
    ])
    subs["torp_sub"] = ids_sorted([
        "後期型53cm艦首魚雷(8門)",            # id=383, torp=19
        "潜水艦53cm艦首魚雷(8門)",            # id=95,  torp=16
        "熟練聴音員+後期型艦首魚雷(6門)",      # id=214, torp=15
        "後期型艦首魚雷(6門)",                # id=213, torp=15
        "21inch艦首魚雷発射管6門(後期型)",     # id=441
        "試製53cm艦首魚雷(8門)",
        "潜水艦後期型艦首魚雷(6門)",
        "艦首魚雷(6門)",                      # id=75
    ])

    # ── 対潜装備 ASW ──────────────────────────────────────────────────────────
    subs["sonar"] = ids_sorted([
        "HF/DF + Type144/147 ASDIC",        # id=431 best ASW sonar
        "Type144/147 ASDIC",                # id=432
        "三式水中探信儀改",                  # id=132
        "四式水中聴音機",                   # id=149
        "三式水中探信儀",                   # id=46
        "九三式水中聴音機",                  # id=47
    ])
    subs["depth_charge"] = ids_sorted([
        "対潜短魚雷(試作初期型)",            # id=378, asw=20
        "Mk.32 対潜魚雷(Mk.2落射機)",        # id=472
        "Hedgehog(初期型)",                 # id=439
        "RUR-4A Weapon Alpha改",             # id=377
        "試製15cm9連装対潜噴進砲改",
        "試製15cm9連装対潜噴進砲",
        "九四式爆雷投射機",                  # id=44
        "三式爆雷投射機",                    # id=45
    ])
    subs["dc_projector"] = ids_sorted([
        "試製15cm9連装対潜噴進砲改",
        "試製15cm9連装対潜噴進砲",
        "九四式爆雷投射機",
        "三式爆雷投射機",
        "二式爆雷",
        "爆雷",
    ])

    # ── 水上偵察機 recon seaplanes ───────────────────────────────────────────
    subs["seaplane_recon"] = ids_sorted([
        "零式水上偵察機11型乙(熟練)",         # id=239, los=8
        "零式水上偵察機11型乙",               # id=238, los=6
        "零式水上観測機",                     # id=59,  los=6
        "Swordfish Mk.II改(水偵型)",          # id=370
        "Fairey Seafox改",                   # id=371
        "零式水上偵察機",                     # id=25
        "九八式水上偵察機(夜偵)",             # id=102, night contact
    ])
    subs["seaplane_night"] = ids_sorted([
        "零式水上偵察機11型乙改(夜偵)",       # id=469, best night recon
        "Sea Otter",                         # id=515
        "Walrus",                            # id=510
        "Loire 130M",                        # id=471
        "九八式水上偵察機(夜偵)",             # id=102
    ])
    subs["seaplane_fighter"] = ids_sorted([
        "強風改",                            # id=233
        "二式水戦改(熟練)",                   # id=257 AA+制空
        "二式水戦改",                        # id=172
        "瑞雲(六三四空/熟練)",               # id=333
        "瑞雲(六三四空)",                    # id=322
        "瑞雲12型",                          # id=154
        "瑞雲",                              # id=26
    ])

    # ── 艦上戦闘機 carrier fighters ─────────────────────────────────────────
    subs["cv_fighter"] = ids_sorted([
        "烈風改二(一航戦/熟練)",             # id=337, aa=14
        "震電改",                            # id=56,  aa=15
        "烈風改二",                          # id=336, aa=13
        "試製 陣風",                         # id=437, aa=13
        "Corsair Mk.II(Ace)",               # id=435
        "烈風 一一型",                       # id=420
        "烈風改",                            # id=20
        "烈風",                              # id=55
        "F6F-5",                            # id=223
        "F4U-1D",                           # id=224
        "零式艦戦52型甲(六〇一空)",          # id=315...
        "零式艦戦52型甲",
        "零式艦戦21型(熟練)",
    ])
    subs["cv_torpedo"] = ids_sorted([
        "流星改(一航戦/熟練)",               # id=343, torp=15
        "天山一二型(村田隊)",                 # id=144, torp=15
        "流星改(熟練)",                      # id=466, torp=13
        "天山一二型(友永隊)",
        "流星改(一航戦)",                    # id=342, torp=14
        "天山",                              # id=57
        "流星",                              # id=18
    ])
    subs["cv_bomber"] = ids_sorted([
        "彗星一二型甲",                      # id=291
        "彗星二二型甲",                      # id=292
        "彗星(六〇一空)",
        "彗星改",
        "彗星",                              # id=24
        "九九式艦爆(江草隊)",                 # id=57...
    ])
    subs["cv_night_fighter"] = ids_sorted([
        "夜間戦闘機 月光一一型(熟練)",        # id=496
        "夜間戦闘機 月光一一型",              # id=494
        "F6F-3N",                           # id=404
        "F6F-5N",                           # id=410
        "Corsair Mk.II(Ace)",
    ])
    subs["cv_night_attacker"] = ids_sorted([
        "流星改(一航戦/熟練)【夜攻】",
        "Swordfish Mk.III(熟練)",            # id=244
        "TBF",                              # id=240
        "TBM-3D",                           # id=412
    ])

    # ── 電探 radar ───────────────────────────────────────────────────────────
    subs["radar_small_los"] = ids_sorted([
        "SG レーダー(後期型)",               # id=456, los=9
        "SG レーダー(初期型)",               # id=315, los=8
        "22号対水上電探改四",                # id=88,  los=8
        "13号対空電探改",                    # id=106, los=5 aa=4
        "33号対水上電探",                    # id=247, los=7
    ])
    subs["radar_large_los"] = ids_sorted([
        "SK+SG レーダー",                   # id=279, los=12 aa=9
        "SK レーダー",                       # id=278, los=10 aa=8
        "FuMO25 レーダー",                   # id=124, los=9  aa=7
        "42号対空電探改二",                  # id=411, los=6  aa=7
        "21号対空電探改二",                  # id=410, los=7  aa=7
        "21号対空電探改",                    # id=131
    ])

    # ── 発煙装置 smoke ────────────────────────────────────────────────────────
    subs["smoke"] = ids_sorted([
        "発煙装置改(煙幕)",                  # id=507 (DD/AO携行)
        "発煙装置(煙幕)",                    # id=500
    ])

    # ── 大型飛行艇 / 補給物資 ─────────────────────────────────────────────────
    subs["supply_item"] = ids_sorted([
        "洋上補給(物資)",
        "洋上補給",
    ])
    subs["daihatsu"] = ids_sorted([
        "大発動艇(八九式中戦車&陸戦隊)",     # best TP/resource
        "大発動艇(II号戦車/北アフリカ仕様)",
        "特大発動艇",
        "大発動艇",
    ])

    return subs


def main():
    if not MASTER.exists():
        print(f"ERROR: {MASTER} not found. Open poi first to generate it.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {MASTER}…")
    d = json.load(open(MASTER, encoding="utf-8"))

    equip_db = build_equip_db(d)
    ship_db  = build_ship_db(d)
    subs     = build_equip_subs(equip_db)

    DATA.mkdir(exist_ok=True)

    out_equip = DATA / "equip_db.json"
    with open(out_equip, "w", encoding="utf-8") as f:
        json.dump(equip_db, f, ensure_ascii=False, separators=(",", ":"))
    print(f"✓ equip_db: {len(equip_db)} items → {out_equip}")

    out_ship = DATA / "ship_db.json"
    with open(out_ship, "w", encoding="utf-8") as f:
        json.dump(ship_db, f, ensure_ascii=False, separators=(",", ":"))
    print(f"✓ ship_db:  {len(ship_db)} ships → {out_ship}")

    out_subs = DATA / "equip_subs.json"
    with open(out_subs, "w", encoding="utf-8") as f:
        json.dump(subs, f, ensure_ascii=False, indent=2)
    print(f"✓ equip_subs: {len(subs)} roles → {out_subs}")

    # Summary of unresolved names in subs
    all_ids_in_subs = set(i for v in subs.values() for i in v)
    print(f"\nSub role coverage: {len(all_ids_in_subs)} unique equips across {len(subs)} roles")

    # Warn about names not found
    name_to_id = {v["name"]: k for k, v in equip_db.items()}
    for role, role_ids in subs.items():
        if not role_ids:
            print(f"  WARNING: role {role!r} resolved to 0 ids (all names missing from DB)")


if __name__ == "__main__":
    main()

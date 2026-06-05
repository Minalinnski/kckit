"""Unit tests for optimizer.py — pure math, no game connection needed."""
import math
import pytest
from core.optimizer import slot_air_power, fleet_air_power, los_33
from core.models import Ship, Equipment, AIRCRAFT_TYPES


def make_equip(
    instance_id=1, master_id=1, name="テスト装備",
    eq_type=6, icon_type=6, anti_air=9, los=0, proficiency=0,
) -> Equipment:
    return Equipment(
        instance_id=instance_id, master_id=master_id, name=name,
        eq_type=eq_type, icon_type=icon_type, anti_air=anti_air,
        los=los, proficiency=proficiency,
    )


def make_ship(
    instance_id=1, slot_counts=None, equipped=None,
    ship_type=5, los=50, now_hp=40, max_hp=40,
) -> Ship:
    if slot_counts is None:
        slot_counts = [18, 12, 6, 0]
    if equipped is None:
        equipped = []
    return Ship(
        instance_id=instance_id, master_id=100, name="テスト艦",
        ship_type=ship_type, level=99, now_hp=now_hp, max_hp=max_hp,
        morale=49, locked=True, speed=10,
        slot_ids=[-1]*6, slot_ex_id=-1,
        slot_counts=slot_counts, slot_num=4,
        in_repair=False, repair_time_ms=0,
        firepower=60, torpedo=60, anti_air=40, armor=55,
        asw=0, evasion=40, los=los, equipped=equipped,
    )


class TestSlotAirPower:
    def test_zero_slot_count(self):
        assert slot_air_power(0, 9) == 0

    def test_basic(self):
        # sqrt(18) * 9 = 4.24... * 9 ≈ 38.18 → floor = 38
        result = slot_air_power(18, 9)
        assert result == math.floor(math.sqrt(18)) * 9

    def test_with_proficiency(self):
        base = math.floor(math.sqrt(18)) * 9
        bonus_lv7 = 22
        assert slot_air_power(18, 9, proficiency=7) == base + bonus_lv7

    def test_single_plane(self):
        # sqrt(1) * 9 = 9
        assert slot_air_power(1, 9) == 9


class TestFleetAirPower:
    def test_no_aircraft(self):
        ship = make_ship(equipped=[])
        assert fleet_air_power([ship]) == 0.0

    def test_single_aircraft_slot(self):
        aircraft = make_equip(eq_type=6, anti_air=9, icon_type=6)
        ship = make_ship(slot_counts=[18, 0, 0, 0], equipped=[aircraft])
        expected = math.floor(math.sqrt(18)) * 9
        assert fleet_air_power([ship]) == expected

    def test_multiple_ships(self):
        aircraft = make_equip(eq_type=6, anti_air=9)
        s1 = make_ship(instance_id=1, slot_counts=[18, 0, 0, 0], equipped=[aircraft])
        s2 = make_ship(instance_id=2, slot_counts=[12, 0, 0, 0], equipped=[aircraft])
        result = fleet_air_power([s1, s2])
        expected = (
            math.floor(math.sqrt(18)) * 9
            + math.floor(math.sqrt(12)) * 9
        )
        assert result == expected


class TestLoS33:
    def test_empty_fleet(self):
        result = los_33([], hq_level=120)
        # 0 + 2*0 - 0.4*ceil(120) = -48
        assert result == pytest.approx(-48.0, abs=0.1)

    def test_with_radar(self):
        # Radar: icon_type default (not in LOS_FACTORS) → factor 0.6
        radar = make_equip(instance_id=10, eq_type=12, icon_type=12, anti_air=0, los=5)
        # icon_type 12 not in LOS_FACTORS, so factor = 0.6
        ship = make_ship(los=40, equipped=[radar])
        # naked_los = 40 - 5 = 35; equip_los contribution = 5 * 0.6 = 3.0
        result = los_33([ship], hq_level=120)
        expected = 3.0 + 35 * 0.1 + 2 * 1 - 0.4 * math.ceil(120)
        assert result == pytest.approx(expected, abs=0.1)

    def test_seaplane_recon_factor(self):
        # icon_type 10 = 水上偵察機, factor = 1.00
        from core.models import LOS_FACTORS
        assert LOS_FACTORS[10] == 1.00

    def test_carrier_recon_factor(self):
        from core.models import LOS_FACTORS
        assert LOS_FACTORS[9] == 1.04


class TestTaiha:
    def test_taiha_threshold(self):
        ship = make_ship(now_hp=10, max_hp=40)   # 25% exactly
        assert ship.is_taiha is True

    def test_just_above_taiha(self):
        ship = make_ship(now_hp=11, max_hp=40)   # 27.5%
        assert ship.is_taiha is False

    def test_full_hp(self):
        ship = make_ship(now_hp=40, max_hp=40)
        assert ship.is_taiha is False

    def test_zero_hp(self):
        ship = make_ship(now_hp=0, max_hp=40)
        assert ship.is_taiha is True

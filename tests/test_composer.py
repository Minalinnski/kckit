"""Unit tests for fleet composer."""
import pytest
from core.composer import FleetComposer
from core.models import GameState, Ship
from core.schema import (
    FleetRequirement, FleetType, PresetConfig,
    Requirements, ShipClass, SlotRequirement,
)


def make_ship(
    instance_id, name, stype, level=99, now_hp=40, max_hp=40,
    morale=49, speed=10, asw=0, in_repair=False, master_id=100,
) -> Ship:
    return Ship(
        instance_id=instance_id, master_id=master_id, name=name,
        ship_type=stype, level=level, now_hp=now_hp, max_hp=max_hp,
        morale=morale, locked=True, speed=speed,
        slot_ids=[-1]*6, slot_ex_id=-1,
        slot_counts=[0]*6, slot_num=4,
        in_repair=in_repair, repair_time_ms=0,
        firepower=30, torpedo=70, anti_air=20, armor=15,
        asw=asw, evasion=70, los=30, equipped=[],
    )


def make_state(ships: list[Ship]) -> GameState:
    return GameState(ships={s.instance_id: s for s in ships})


def make_preset(slots) -> PresetConfig:
    return PresetConfig(
        name="テスト",
        fleet=FleetRequirement(type=FleetType.SINGLE, slots=slots),
        requirements=Requirements(),
    )


class TestComposer:
    def test_selects_correct_class(self):
        ships = [
            make_ship(1, "DD艦", stype=2),   # DD
            make_ship(2, "CA艦", stype=5),   # CA
        ]
        state = make_state(ships)
        preset = make_preset([SlotRequirement(ship_class=[ShipClass.DD], count=1)])
        result = FleetComposer(state).compose(preset)
        assert len(result.ships) == 1
        assert result.ships[0].name == "DD艦"

    def test_skips_in_repair(self):
        ships = [
            make_ship(1, "入渠中", stype=2, in_repair=True),
            make_ship(2, "健在艦", stype=2),
        ]
        result = FleetComposer(make_state(ships)).compose(
            make_preset([SlotRequirement(ship_class=[ShipClass.DD], count=1)])
        )
        assert result.ships[0].name == "健在艦"

    def test_skips_taiha(self):
        ships = [
            make_ship(1, "大破艦", stype=2, now_hp=10, max_hp=40),
            make_ship(2, "健在艦", stype=2),
        ]
        result = FleetComposer(make_state(ships)).compose(
            make_preset([SlotRequirement(ship_class=[ShipClass.DD], count=1)])
        )
        assert result.ships[0].name == "健在艦"

    def test_insufficient_ships_gives_error_warning(self):
        ships = [make_ship(1, "DD艦", stype=2)]
        result = FleetComposer(make_state(ships)).compose(
            make_preset([SlotRequirement(ship_class=[ShipClass.DD], count=3)])
        )
        assert any("ERROR" in w for w in result.warnings)
        assert not result.is_valid

    def test_prefers_higher_level(self):
        ships = [
            make_ship(1, "低レベル", stype=2, level=50, master_id=100),
            make_ship(2, "高レベル", stype=2, level=99, master_id=100),
        ]
        result = FleetComposer(make_state(ships)).compose(
            make_preset([SlotRequirement(ship_class=[ShipClass.DD], count=1)])
        )
        assert result.ships[0].name == "高レベル"

    def test_specific_ships_pinned(self):
        ships = [
            make_ship(1, "雪風改", stype=2, level=170, master_id=300),
            make_ship(2, "時雨改二", stype=2, level=155, master_id=290),
        ]
        result = FleetComposer(make_state(ships)).compose(
            make_preset([
                SlotRequirement(
                    ship_class=[ShipClass.DD],
                    count=1,
                    specific_ships=["時雨改二"],
                )
            ])
        )
        assert result.ships[0].name == "時雨改二"

    def test_min_level_filter(self):
        ships = [
            make_ship(1, "低レベル", stype=2, level=30),
            make_ship(2, "高レベル", stype=2, level=80),
        ]
        result = FleetComposer(make_state(ships)).compose(
            make_preset([SlotRequirement(ship_class=[ShipClass.DD], count=1, min_level=50)])
        )
        assert len(result.ships) == 1
        assert result.ships[0].name == "高レベル"

    def test_multiple_slots_no_duplicate(self):
        ships = [make_ship(i, f"DD{i}", stype=2) for i in range(1, 7)]
        result = FleetComposer(make_state(ships)).compose(
            make_preset([
                SlotRequirement(ship_class=[ShipClass.DD], count=3),
                SlotRequirement(ship_class=[ShipClass.DD], count=3),
            ])
        )
        ids = [s.instance_id for s in result.ships]
        assert len(ids) == len(set(ids)), "Duplicate ships in fleet"

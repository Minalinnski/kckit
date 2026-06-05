"""Unit tests for schema.py."""
import pytest
from pydantic import ValidationError
from core.schema import (
    AirState, FleetRequirement, FleetType, MapStrategy,
    PresetConfig, Requirements, ShipClass, SlotRequirement,
    load_strategy, save_strategy,
)
import tempfile, os


def make_preset(name="テスト", ship_classes=None, count=6):
    if ship_classes is None:
        ship_classes = [ShipClass.DD]
    return PresetConfig(
        name=name,
        fleet=FleetRequirement(
            type=FleetType.SINGLE,
            slots=[SlotRequirement(ship_class=ship_classes, count=count)],
        ),
        requirements=Requirements(),
    )


class TestSlotRequirement:
    def test_valid(self):
        s = SlotRequirement(ship_class=[ShipClass.DD, ShipClass.CL], count=3)
        assert s.count == 3

    def test_with_speed(self):
        s = SlotRequirement(ship_class=[ShipClass.DD], count=6, speed="fast")
        assert s.speed == "fast"


class TestFleetRequirement:
    def test_exceeds_six_raises(self):
        with pytest.raises(ValidationError):
            FleetRequirement(
                type=FleetType.SINGLE,
                slots=[SlotRequirement(ship_class=[ShipClass.DD], count=7)],
            )

    def test_exactly_six(self):
        f = FleetRequirement(
            type=FleetType.SINGLE,
            slots=[SlotRequirement(ship_class=[ShipClass.DD], count=6)],
        )
        assert sum(s.count for s in f.slots) == 6

    def test_multiple_slots_totalling_six(self):
        f = FleetRequirement(
            type=FleetType.SINGLE,
            slots=[
                SlotRequirement(ship_class=[ShipClass.CA], count=2),
                SlotRequirement(ship_class=[ShipClass.DD], count=4),
            ],
        )
        assert sum(s.count for s in f.slots) == 6


class TestRequirements:
    def test_auto_air_power_min(self):
        r = Requirements(
            air_state=AirState.SUPERIORITY,
            enemy_air_power=100,
        )
        # superiority: ceil(100 * 1.5) = 150
        assert r.air_power_min == 150

    def test_supremacy_formula(self):
        r = Requirements(
            air_state=AirState.SUPREMACY,
            enemy_air_power=60,
        )
        assert r.air_power_min == 180   # 60 * 3

    def test_none_air_state(self):
        r = Requirements(air_state=AirState.NONE)
        assert r.air_power_min is None


class TestMapStrategy:
    def test_valid(self):
        s = MapStrategy(map="5-4", presets=[make_preset()])
        assert s.map == "5-4"

    def test_get_preset(self):
        s = MapStrategy(map="5-4", presets=[make_preset("A"), make_preset("B")])
        assert s.get_preset("A").name == "A"
        assert s.get_preset("Z") is None


class TestYamlIO:
    def test_roundtrip(self):
        strategy = MapStrategy(
            map="1-1",
            notes="test",
            presets=[make_preset("低コスト")],
        )
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False, mode="w") as f:
            path = f.name
        try:
            save_strategy(strategy, path)
            loaded = load_strategy(path)
            assert loaded.map == "1-1"
            assert loaded.presets[0].name == "低コスト"
        finally:
            os.unlink(path)

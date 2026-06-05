"""Unit tests for poi_client state parsing — no live connection required."""
import pytest
from core.poi_client import PoiClient


def make_raw_state(
    *,
    ships=None, equips=None, fleets=None,
    hq_level=120, fuel=1000,
):
    """Build a minimal raw state dict matching the plugin's buildState() output."""
    return {
        "ships":  ships  or {},
        "equips": equips or {},
        "fleets": fleets or {},
        "repairs": {},
        "resources": {"fuel": fuel, "ammo": 0, "steel": 0, "bauxite": 0},
        "hq_level": hq_level,
        "timestamp": 0,
    }


def make_ship_entry(instance_id=1, ship_id=100, name="テスト艦", stype=2,
                    now_hp=40, max_hp=40, lv=99, cond=49):
    return {
        str(instance_id): {
            "api_id": instance_id,
            "api_ship_id": ship_id,
            "api_lv": lv,
            "api_nowhp": now_hp,
            "api_maxhp": max_hp,
            "api_cond": cond,
            "api_soku": 10,
            "api_locked": 1,
            "api_slot": [-1, -1, -1, -1, -1, -1],
            "api_slot_ex": -1,
            "api_onslot": [0, 0, 0, 0, 0, 0],
            "api_slotnum": 4,
            "api_ndock_time": 0,
            "api_karyoku": [30, 30],
            "api_raisou": [70, 70],
            "api_taiku": [20, 20],
            "api_soukou": [15, 15],
            "api_taisen": [60, 60],
            "api_kaihi": [70, 70],
            "api_sakuteki": [30, 30],
            "$master": {
                "api_name": name,
                "api_stype": stype,
            },
        }
    }


def make_fleet_entry(fleet_id=1, ship_ids=None):
    return {
        str(fleet_id): {
            "api_id": fleet_id,
            "api_name": f"第{fleet_id}艦隊",
            "api_ship": (ship_ids or [1, -1, -1, -1, -1, -1]),
            "api_mission": [0, 0, 0, 0],
        }
    }


class TestApplyState:
    def test_hq_level_parsed(self):
        client = PoiClient.__new__(PoiClient)
        import threading
        client._state = None
        client._state_lock = threading.Lock()

        raw = make_raw_state(hq_level=137)
        client._apply_state(raw)
        assert client._state.hq_level == 137

    def test_resources_parsed(self):
        client = PoiClient.__new__(PoiClient)
        import threading
        client._state = None
        client._state_lock = threading.Lock()

        raw = make_raw_state(fuel=12345)
        client._apply_state(raw)
        assert client._state.resources["fuel"] == 12345

    def test_ship_parsed(self):
        client = PoiClient.__new__(PoiClient)
        import threading
        client._state = None
        client._state_lock = threading.Lock()

        ships = make_ship_entry(instance_id=5, name="雪風改", now_hp=32, max_hp=40)
        raw = make_raw_state(ships=ships)
        client._apply_state(raw)

        assert 5 in client._state.ships
        ship = client._state.ships[5]
        assert ship.name == "雪風改"
        assert ship.now_hp == 32
        assert ship.is_taiha is False

    def test_taiha_ship_parsed(self):
        client = PoiClient.__new__(PoiClient)
        import threading
        client._state = None
        client._state_lock = threading.Lock()

        ships = make_ship_entry(instance_id=3, name="大破艦", now_hp=10, max_hp=40)
        raw = make_raw_state(ships=ships)
        client._apply_state(raw)

        assert client._state.ships[3].is_taiha is True

    def test_fleet_parsed(self):
        client = PoiClient.__new__(PoiClient)
        import threading
        client._state = None
        client._state_lock = threading.Lock()

        ships = make_ship_entry(instance_id=1)
        fleets = make_fleet_entry(fleet_id=1, ship_ids=[1, -1, -1, -1, -1, -1])
        raw = make_raw_state(ships=ships, fleets=fleets)
        client._apply_state(raw)

        assert 1 in client._state.fleets
        fleet = client._state.fleets[1]
        assert fleet.fleet_id == 1
        assert len(fleet.ships) == 1

    def test_empty_state_does_not_raise(self):
        client = PoiClient.__new__(PoiClient)
        import threading
        client._state = None
        client._state_lock = threading.Lock()

        client._apply_state(make_raw_state())
        assert client._state is not None
        assert client._state.hq_level == 120

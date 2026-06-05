"""Unit tests for safety.py — the absolute red line."""
import pytest
from core.safety import (
    TaihaAdvanceError,
    bezier_points,
    check_taiha,
    is_taiha,
    jitter_point,
    taiha_ships,
)
from core.models import Ship


def make_ship(now_hp: int, max_hp: int, name: str = "テスト艦") -> Ship:
    return Ship(
        instance_id=1, master_id=100, name=name,
        ship_type=2, level=99, now_hp=now_hp, max_hp=max_hp,
        morale=49, locked=True, speed=10,
        slot_ids=[-1]*6, slot_ex_id=-1,
        slot_counts=[0]*6, slot_num=4,
        in_repair=False, repair_time_ms=0,
        firepower=30, torpedo=70, anti_air=20, armor=15,
        asw=60, evasion=70, los=30, equipped=[],
    )


class TestIsTaiha:
    def test_exactly_25pct(self):
        assert is_taiha(make_ship(10, 40)) is True

    def test_below_25pct(self):
        assert is_taiha(make_ship(1, 40)) is True

    def test_above_25pct(self):
        assert is_taiha(make_ship(11, 40)) is False

    def test_full_hp(self):
        assert is_taiha(make_ship(40, 40)) is False

    def test_zero_max_hp(self):
        s = make_ship(0, 0)
        assert is_taiha(s) is False   # guard against division by zero


class TestCheckTaiha:
    def test_no_taiha_passes(self):
        ships = [make_ship(40, 40), make_ship(30, 40)]
        check_taiha(ships)   # should not raise

    def test_taiha_raises(self):
        ships = [make_ship(40, 40), make_ship(10, 40, name="大破艦")]
        with pytest.raises(TaihaAdvanceError) as exc_info:
            check_taiha(ships, context="test")
        assert "大破艦" in str(exc_info.value)
        assert "TAIHA ADVANCE BLOCKED" in str(exc_info.value)

    def test_all_taiha_raises(self):
        ships = [make_ship(5, 40), make_ship(8, 40)]
        with pytest.raises(TaihaAdvanceError):
            check_taiha(ships)

    def test_none_in_list(self):
        ships = [make_ship(40, 40), None, make_ship(30, 40)]
        check_taiha(ships)   # None should be skipped

    def test_cannot_be_suppressed_silently(self):
        """Verify TaihaAdvanceError is a real exception that propagates."""
        ships = [make_ship(10, 40)]
        raised = False
        try:
            check_taiha(ships)
        except TaihaAdvanceError:
            raised = True
        assert raised, "TaihaAdvanceError must be raised"


class TestBezierPoints:
    def test_returns_n_plus_one_points(self):
        pts = bezier_points((0, 0), (100, 100), n=30)
        assert len(pts) == 31

    def test_start_and_end(self):
        pts = bezier_points((10, 20), (200, 300), n=20)
        assert pts[0] == pytest.approx((10, 20), abs=0.1)
        assert pts[-1] == pytest.approx((200, 300), abs=0.1)

    def test_not_straight_line(self):
        import random
        random.seed(42)
        pts = bezier_points((0, 0), (100, 0), n=20)
        # At least some points should deviate from y=0
        y_values = [p[1] for p in pts[1:-1]]
        assert any(abs(y) > 0.1 for y in y_values)


class TestJitterPoint:
    def test_within_radius(self):
        import random
        random.seed(0)
        for _ in range(100):
            jx, jy = jitter_point(100, 100, radius=5)
            assert abs(jx - 100) <= 5
            assert abs(jy - 100) <= 5

    def test_returns_int(self):
        jx, jy = jitter_point(100, 100)
        assert isinstance(jx, int)
        assert isinstance(jy, int)

"""
Sortie executor — controls poi via virtual mouse.
Fully event-driven: waits for kcsapi events instead of sleeping.
Every advance decision MUST pass through safety.check_taiha().
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import pyautogui
import mss
import mss.tools
from PIL import Image
import numpy as np
import cv2
import yaml

from .poi_client import PoiClient
from .safety import (
    TaihaAdvanceError,
    action_delay,
    bezier_points,
    check_taiha,
    click_delay,
    jitter_point,
    random_delay,
)

log = logging.getLogger(__name__)

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.05

# API event paths we care about
_BATTLE_RESULT_PATHS = {
    "/kcsapi/api_req_sortie/battleresult",
    "/kcsapi/api_req_combined_battle/battleresult",
}
_BATTLE_START_PATHS = {
    "/kcsapi/api_req_sortie/battle",
    "/kcsapi/api_req_sortie/airbattle",
    "/kcsapi/api_req_sortie/ld_airbattle",
    "/kcsapi/api_req_combined_battle/battle",
    "/kcsapi/api_req_combined_battle/airbattle",
}
_MAP_START = "/kcsapi/api_req_map/start"
_MAP_NEXT = "/kcsapi/api_req_map/next"
_PORT = "/kcsapi/api_port/port"

_LAYOUT_PATH = Path(__file__).parent.parent / "config" / "screen_layout.yaml"
_WINDOW_PATH = Path(__file__).parent.parent / "config" / "poi_window.yaml"


class SortieResult(Enum):
    S_RANK = auto()
    A_RANK = auto()
    B_RANK = auto()
    RETREAT_TAIHA = auto()
    RETREAT_MANUAL = auto()
    ERROR = auto()
    NOT_CALIBRATED = auto()


@dataclass
class CanvasConfig:
    """Screen pixel position of the KanColle game canvas."""
    x: int = 0
    y: int = 0
    w: int = 800
    h: int = 480

    @classmethod
    def load(cls, path: Path = _WINDOW_PATH) -> "CanvasConfig":
        if path.exists():
            with open(path) as f:
                d = yaml.safe_load(f)
            c = d.get("canvas", {})
            return cls(x=c.get("x", 0), y=c.get("y", 0),
                       w=c.get("w", 800), h=c.get("h", 480))
        return cls()

    @property
    def is_calibrated(self) -> bool:
        return self.x > 0 or self.y > 0

    def px(self, fx: float, fy: float) -> tuple[int, int]:
        """Convert fractional canvas coords to screen pixels."""
        return (int(self.x + fx * self.w), int(self.y + fy * self.h))


class ScreenLayout:
    """Loads screen_layout.yaml and resolves element pixel positions."""

    def __init__(self, canvas: CanvasConfig, path: Path = _LAYOUT_PATH):
        self._canvas = canvas
        with open(path) as f:
            data = yaml.safe_load(f)
        self._screens = data.get("screens", {})

    def px(self, screen: str, element: str) -> Optional[tuple[int, int]]:
        """Return screen pixel center of a named element."""
        elem = self._screens.get(screen, {}).get("elements", {}).get(element)
        if elem is None:
            return None
        return self._canvas.px(elem["x"], elem["y"])


class MouseController:
    """Safe mouse controller with Bézier movement and click jitter."""

    def move_to(self, x: int, y: int, duration: float = 0.4) -> None:
        start = pyautogui.position()
        points = bezier_points(start, (x, y), n=max(20, int(duration * 60)))
        t_per_step = duration / len(points)
        for px, py in points:
            pyautogui.moveTo(int(px), int(py), _pause=False)
            time.sleep(t_per_step)

    def click(self, x: int, y: int, jitter: int = 4) -> None:
        jx, jy = jitter_point(x, y, jitter)
        self.move_to(jx, jy)
        click_delay()
        pyautogui.click(jx, jy)
        log.debug("Click (%d, %d)", jx, jy)

    def click_element(self, pos: Optional[tuple[int, int]]) -> bool:
        """Click a (x,y) position if not None. Returns False if pos is None."""
        if pos is None:
            log.warning("click_element: position is None (layout not calibrated?)")
            return False
        self.click(*pos)
        return True


class SortieExecutor:
    """
    Event-driven sortie executor.
    Waits for kcsapi events instead of fixed sleeps.
    Reads HP from poi state after each battleresult.
    """

    def __init__(
        self,
        poi: PoiClient,
        canvas: Optional[CanvasConfig] = None,
        fleet_id: int = 1,
        formation: int = 4,      # 梯形陣 default
        night_battle: bool = False,
        dry_run: bool = False,   # log actions but don't actually click
    ):
        self.poi = poi
        self.canvas = canvas or CanvasConfig.load()
        self.fleet_id = fleet_id
        self.formation = formation
        self.night_battle = night_battle
        self.dry_run = dry_run
        self.mouse = MouseController()
        self._layout = ScreenLayout(self.canvas) if _LAYOUT_PATH.exists() else None

    def run_sortie(self, map_id: str) -> SortieResult:
        if not self.canvas.is_calibrated and not self.dry_run:
            log.error("Canvas not calibrated. Run tools/calibrate.py first.")
            return SortieResult.NOT_CALIBRATED

        log.info("Sortie start: map=%s fleet=%d formation=%d night=%s dry_run=%s",
                 map_id, self.fleet_id, self.formation, self.night_battle, self.dry_run)

        try:
            # Check if we're already in sortie (user may have navigated there manually)
            current = self.poi.state.sortie
            if current.in_sortie and current.map_str == map_id:
                log.info("Already in sortie on %s (node %s), joining loop", map_id, current.node_id)
                ev = {"body": {}}  # synthetic — we're already past map/start
            else:
                # Navigate from port to the map start
                self._navigate_to_map(map_id)

                # Wait for map/start event (formation select appears)
                log.info("Waiting for map/start event…")
                ev = self.poi.wait_for_event(_MAP_START, timeout=60)
                if ev is None:
                    log.error("Timeout waiting for map/start")
                    return SortieResult.ERROR

            node_count = 0
            while True:
                node_count += 1
                log.info("Node %d: selecting formation", node_count)

                # ── HARD GATE: check 大破 before every advance ──
                state = self.poi.state
                fleet = state.fleets.get(self.fleet_id)
                if fleet:
                    check_taiha(fleet.ships, context=f"map={map_id} node={node_count}")

                self._select_formation()
                action_delay()

                # Wait for battle to start, then wait for result
                log.info("Waiting for battle result…")
                result_ev = self.poi.wait_for_any_event(
                    list(_BATTLE_RESULT_PATHS), timeout=180
                )
                if result_ev is None:
                    log.error("Timeout waiting for battle result at node %d", node_count)
                    return SortieResult.ERROR

                path, payload = result_ev
                rank = payload.get("body", {}).get("api_win_rank", "?")
                log.info("Battle result: %s", rank)

                # Post-result HP check (state updated by bridge after battleresult)
                action_delay()
                state = self.poi.state
                fleet = state.fleets.get(self.fleet_id)
                if fleet:
                    check_taiha(fleet.ships, context=f"map={map_id} node={node_count} post-battle")

                # Handle night battle option
                if self.night_battle:
                    self._click("night_battle_select", "night_battle")
                    random_delay(1.5, 0.5)
                    # Wait for night battleresult
                    night_ev = self.poi.wait_for_any_event(
                        list(_BATTLE_RESULT_PATHS), timeout=120
                    )
                    if night_ev:
                        nb_path, nb_payload = night_ev
                        rank = nb_payload.get("body", {}).get("api_win_rank", rank)
                        log.info("Night battle result: %s", rank)
                    action_delay()

                # Click past the battle result screen
                self._click("battle_result", "next")
                random_delay(1.5, 0.5)

                # Wait for next node event
                next_ev = self.poi.wait_for_any_event(
                    [_MAP_NEXT, _PORT], timeout=30
                )
                if next_ev is None or next_ev[0] == _PORT:
                    # Returned to port — sortie complete
                    log.info("Returned to port after node %d", node_count)
                    break

                # Still on the map — check if this is boss node from event payload
                next_path, next_payload = next_ev
                next_node = next_payload.get("body", {}).get("api_no")
                boss_node = next_payload.get("body", {}).get("api_event_id") in (5, 6)

                if boss_node:
                    # Boss node: advance into it (will loop back for formation + battle)
                    log.info("Boss node detected, advancing")
                    self._click("post_battle", "advance")
                    action_delay()
                else:
                    # Non-boss intermediate node: advance
                    self._click("post_battle", "advance")
                    action_delay()

            return self._rank_to_result(rank)

        except TaihaAdvanceError as e:
            log.warning("Taiha detected — retreating: %s", e)
            self._click("post_battle", "retreat")
            action_delay()
            return SortieResult.RETREAT_TAIHA

        except Exception as e:
            log.error("Sortie error: %s", e, exc_info=True)
            return SortieResult.ERROR

    def supply_fleet(self) -> None:
        log.info("Supplying fleet %d", self.fleet_id)
        self._click("port", "supply_nav")
        random_delay(1.0, 0.3)
        tab = f"fleet_tab_{self.fleet_id}"
        self._click("supply", tab)
        random_delay(0.5, 0.2)
        self._click("supply", "supply_all")
        random_delay(0.8, 0.2)
        self._click("supply", "back")
        random_delay(0.8, 0.2)

    def collect_expedition_result(self, fleet_id: int) -> bool:
        log.info("Collecting expedition result for fleet %d", fleet_id)
        self._click("port", f"fleet_tab_{fleet_id}")
        random_delay(0.5, 0.2)
        self._click("expedition_result", "confirm")
        ev = self.poi.wait_for_event("/kcsapi/api_req_mission/result", timeout=15)
        if ev is None:
            log.warning("Timeout waiting for api_req_mission/result (fleet %d)", fleet_id)
            return False
        return True

    def resend_expedition(self, fleet_id: int, exp_id: int) -> bool:
        log.info("Resending fleet %d on expedition %d", fleet_id, exp_id)
        self._click("port", f"fleet_tab_{fleet_id}")
        random_delay(0.8, 0.3)
        self._click("port", "expedition_btn")
        random_delay(1.0, 0.4)
        if fleet_id in (2, 3, 4):
            self._click("expedition_select", f"fleet_tab_{fleet_id}")
            random_delay(0.5, 0.2)
        area = self._expedition_area(exp_id)
        self._click("expedition_select", f"area_{area}")
        random_delay(0.5, 0.2)
        pos = self._expedition_pos_in_area(exp_id)
        if pos <= 7:
            self._click("expedition_select", f"exp_item_{pos}")
        else:
            for _ in range(pos - 7):
                self._click("expedition_select", "scroll_down")
                random_delay(0.3, 0.1)
            self._click("expedition_select", "exp_item_7")
        random_delay(0.5, 0.2)
        self._click("expedition_select", "go_btn")
        ev = self.poi.wait_for_event("/kcsapi/api_req_mission/start", timeout=15)
        if ev is None:
            log.warning("Timeout waiting for api_req_mission/start (fleet %d exp %d)", fleet_id, exp_id)
            return False
        return True

    def navigate_to_repair(self) -> None:
        log.debug("Navigating to repair screen")
        self._click("port", "repair_nav")
        random_delay(1.0, 0.3)

    def start_repair_ship(self, dock_id: int) -> bool:
        log.info("Starting repair in dock %d", dock_id)
        self._click("repair", f"dock_{dock_id}")
        random_delay(0.8, 0.3)
        self._click("repair_ship_select", "ship_item_1")
        random_delay(0.5, 0.2)
        self._click("repair_ship_select", "confirm")
        ev = self.poi.wait_for_event("/kcsapi/api_req_nyukyo/start", timeout=10)
        if ev is None:
            log.warning("Timeout waiting for api_req_nyukyo/start (dock %d)", dock_id)
            return False
        return True

    @staticmethod
    def _expedition_area(exp_id: int) -> int:
        if exp_id <= 9:
            return 1
        elif exp_id <= 20:
            return 2
        elif exp_id <= 30:
            return 3
        else:
            return 4

    @staticmethod
    def _expedition_pos_in_area(exp_id: int) -> int:
        if exp_id <= 9:
            return exp_id
        elif exp_id <= 20:
            return exp_id - 10
        elif exp_id <= 30:
            return exp_id - 20
        else:
            return exp_id - 30

    # ── Internal helpers ────────────────────────────────────────────────────

    def _click(self, screen: str, element: str) -> bool:
        if self._layout is None:
            log.warning("No screen layout loaded, skipping click %s.%s", screen, element)
            return False
        pos = self._layout.px(screen, element)
        if pos is None:
            log.warning("Element not found in layout: %s.%s", screen, element)
            return False
        if self.dry_run:
            log.info("[DRY-RUN] click %s.%s @ %s", screen, element, pos)
            return True
        return self.mouse.click_element(pos)

    def _navigate_to_map(self, map_id: str) -> None:
        """Click through port → sortie → area select → fleet select."""
        log.debug("Navigating to map %s", map_id)
        self._click("port", "sortie_nav")
        random_delay(1.2, 0.4)
        # TODO: select specific sea area + map node based on map_id
        # For now click the sortie confirm — needs per-map coordinates
        log.warning("_navigate_to_map: map-specific node clicks not yet implemented for %s", map_id)

    def _select_formation(self) -> None:
        elem = f"formation_{self.formation}"
        self._click("formation_select", elem)
        click_delay()

    @staticmethod
    def _rank_to_result(rank: str) -> SortieResult:
        return {
            "S": SortieResult.S_RANK,
            "A": SortieResult.A_RANK,
            "B": SortieResult.B_RANK,
        }.get(rank, SortieResult.A_RANK)

"""
Screen state detector.
Primary method: infer from poi WebSocket events and game state.
Secondary: template matching (requires calibrated templates in data/templates/).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger(__name__)

LAYOUT_PATH = Path(__file__).parent.parent / "config" / "screen_layout.yaml"


@dataclass
class ScreenState:
    name: str          # e.g. "port", "supply", "battle_result"
    display_name: str  # Japanese name e.g. "母港"
    confidence: float  # 0.0–1.0; 1.0 = certain from API event
    detected_at: float = 0.0  # time.time()

    def __post_init__(self):
        if not self.detected_at:
            self.detected_at = time.time()

    def __str__(self) -> str:
        return f"{self.name}({self.display_name}) conf={self.confidence:.2f}"


# API event → screen it transitions TO
_EVENT_TO_SCREEN: dict[str, str] = {
    "/kcsapi/api_port/port":                           "port",
    "/kcsapi/api_req_mission/result":                  "expedition_result",
    "/kcsapi/api_req_mission/start":                   "port",
    "/kcsapi/api_req_map/start":                       "formation_select",
    "/kcsapi/api_req_map/next":                        "formation_select",
    "/kcsapi/api_req_sortie/battleresult":             "battle_result",
    "/kcsapi/api_req_combined_battle/battleresult":    "battle_result",
    "/kcsapi/api_req_nyukyo/start":                    "repair",
    "/kcsapi/api_req_nyukyo/speedchange":              "repair",
    "/kcsapi/api_req_hokyu/charge":                    "supply",
    "/kcsapi/api_req_kousyou/createship":              "factory",
    "/kcsapi/api_req_kousyou/getship":                 "factory",
    "/kcsapi/api_get_member/questlist":                "port",
}


def detect_from_last_event(last_event: str) -> Optional[ScreenState]:
    """Infer current screen from the last received API event."""
    if not last_event:
        return None
    screen = _EVENT_TO_SCREEN.get(last_event)
    if screen:
        return ScreenState(name=screen, display_name=_screen_display_name(screen), confidence=0.9)
    return None


def detect_from_spy(screen_name: str) -> Optional[ScreenState]:
    """Build a ScreenState from the screen spy's output (ground truth).

    screen_name is set by the injected MutationObserver in the KC webview,
    derived from DOM visibility and KC globals — not from API events.
    This is the most reliable source since it works even when KC caches responses.
    """
    if not screen_name:
        return None
    # Strip "kc:" prefix from KC-internal names
    name = screen_name.removeprefix("kc:")
    return ScreenState(name=name, display_name=_screen_display_name(name), confidence=1.0)


def detect_from_poi(poi_client) -> ScreenState:
    """
    Detect current screen using best available source from poi client.

    Priority:
      1. Scene tree / screen spy (scene_tree = perception v2 atlas-prefix
         classification, confidence=0.95; other spy sources claim 1.0)
      2. Last API event (unreliable if KC caches the response, confidence=0.9)
      3. Game state heuristics (confidence≤0.6)
    """
    # 1. Scene tree / spy — no API-event dependency
    spy_screen = getattr(poi_client, "current_screen", None)
    if spy_screen:
        result = detect_from_spy(spy_screen)
        if result:
            if getattr(poi_client, "current_screen_source", None) == "scene_tree":
                result.confidence = 0.95
            return result
        return _fallback_from_state(poi_client)

    # 2. API event inference
    try:
        state = poi_client.state
        if hasattr(state, "last_event") and state.last_event:
            result = detect_from_last_event(state.last_event)
            if result:
                return result
        return _fallback_from_state(poi_client)
    except RuntimeError:
        return ScreenState(name="unknown", display_name="不明", confidence=0.0)


def _fallback_from_state(poi_client) -> ScreenState:
    try:
        state = poi_client.state
    except RuntimeError:
        return ScreenState(name="unknown", display_name="不明", confidence=0.0)
    return detect_from_state(state)


def detect_from_state(state) -> ScreenState:
    """
    Infer screen from GameState via API events and heuristics.
    Use detect_from_poi() instead when a PoiClient is available.
    Falls back to 'port' if nothing else matches.
    """
    if hasattr(state, "last_event") and state.last_event:
        result = detect_from_last_event(state.last_event)
        if result:
            return result

    if hasattr(state, "fleets"):
        fleets = state.fleets if isinstance(state.fleets, dict) else {}
        if fleets:
            return ScreenState(name="port", display_name="母港", confidence=0.6)

    return ScreenState(name="unknown", display_name="不明", confidence=0.0)


def _screen_display_name(name: str) -> str:
    names = {
        "port": "母港",
        "supply": "補給",
        "equipment": "改装",
        "repair": "入渠",
        "factory": "工廠",
        "expedition_result": "遠征帰投",
        "expedition_select": "遠征選択",
        "sortie_area_select": "出撃海域選択",
        "formation_select": "陣形選択",
        "battle": "戦闘中",
        "night_battle_select": "夜戦選択",
        "battle_result": "戦闘結果",
        "post_battle": "戦闘後",
    }
    return names.get(name, name)


class ScreenLayout:
    """Loads screen_layout.yaml and provides element lookup."""

    def __init__(self, path: Path = LAYOUT_PATH):
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        self._screens: dict[str, dict] = data.get("screens", {})

    def get_elements(self, screen_name: str) -> dict[str, dict]:
        return self._screens.get(screen_name, {}).get("elements", {})

    def get_element(self, screen_name: str, element_name: str) -> Optional[dict]:
        return self.get_elements(screen_name).get(element_name)

    def to_pixel(self, coord: dict, canvas_x: int, canvas_y: int, canvas_w: int, canvas_h: int) -> tuple[int, int]:
        """Convert fractional coord to absolute screen pixel (center of element)."""
        x = int(canvas_x + coord["x"] * canvas_w)
        y = int(canvas_y + coord["y"] * canvas_h)
        return (x, y)

    def all_screen_names(self) -> list[str]:
        return list(self._screens.keys())

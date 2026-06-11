"""
kckit Executor
==============
Event-driven virtual mouse executor for daily KanColle automation.

Architecture:
  - Connects to kckit Simulator WS (:8765/ws) to receive game events in real-time
  - Sends clicks via Simulator REST API (/api/click), which proxies to poi plugin
  - poi plugin uses wc.sendInputEvent() — goes through Electron's normal rendering pipeline
  - NEVER forges HTTP requests to /kcsapi

Every action has an expected confirmation event + timeout.
On timeout/failure: abort to port (ground-truth anchor via api_port/port).

Usage:
    async with Executor(dry_run=True) as ex:
        await ex.supply_fleet(1)
        await ex.send_expedition(2, 5)
        await ex.run_daily_routine()
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

ROOT = Path(__file__).parent.parent
LAYOUT_PATH = ROOT / "config" / "screen_layout.yaml"
SNAPSHOT_PATH = Path.home() / ".kckit" / "box_snapshot.json"


# ── Layout element lookup ──────────────────────────────────────────────────

_layout_cache: dict | None = None


def _get_layout() -> dict:
    global _layout_cache
    if _layout_cache is None:
        import yaml
        with open(LAYOUT_PATH) as f:
            _layout_cache = yaml.safe_load(f)
    return _layout_cache


def _element_xy(screen: str, key: str) -> tuple[float, float]:
    """Return (rx, ry) center fractions of a named element from screen_layout.yaml.
    Searches: common → left_nav → screen-specific → popups.
    """
    layout = _get_layout()
    for section in [
        (layout.get("common") or {}).get("elements") or {},
        (layout.get("left_nav") or {}).get("elements") or {},
        ((layout.get("screens") or {}).get(screen) or {}).get("elements") or {},
        ((layout.get("popups") or {}).get("list_dismiss") or {}).get("elements") or {},
        ((layout.get("popups") or {}).get("confirm_dismiss") or {}).get("elements") or {},
    ]:
        if key in section:
            el = section[key]
            return float(el["x"]), float(el["y"])
    raise KeyError(f"Element '{key}' not found for screen '{screen}'")


# ── Expedition position helpers ────────────────────────────────────────────

def _expedition_area(exp_id: int) -> int:
    """Return area number (1-4) for an expedition id."""
    if exp_id <= 9:   return 1
    if exp_id <= 20:  return 2
    if exp_id <= 30:  return 3
    return 4


def _expedition_pos_in_area(exp_id: int) -> int:
    """1-based sequential position within the area list (before any scroll)."""
    if exp_id <= 9:   return exp_id
    if exp_id <= 20:  return exp_id - 10
    if exp_id <= 30:  return exp_id - 20
    return exp_id - 30


# ── Navigation knowledge ───────────────────────────────────────────────────

# Multi-step paths from port to each target screen.
# Each step: (element_key_to_click_on_current_screen, resulting_screen_after_click)
_PORT_NAV: dict[str, list[tuple[str, str]]] = {
    "supply":            [("supply_nav",     "supply")],
    "repair":            [("repair_nav",     "repair")],
    "hensei":            [("hensei_nav",     "hensei")],
    "equipment":         [("equipment_nav",  "equipment")],
    "factory":           [("factory_nav",    "factory")],
    "sortie_type":       [("sortie_nav",     "sortie_type")],
    "expedition_select": [("sortie_nav",     "sortie_type"),
                          ("expedition_btn", "expedition_select")],
    "sortie_world":      [("sortie_nav",     "sortie_type"),
                          ("sortie_btn",     "sortie_world")],
    "practice":          [("sortie_nav",     "sortie_type"),
                          ("exercise_btn",   "practice")],
}

# API events that confirm arrival at a screen (ground-truth, always fire on entry).
# Screens without an entry here have no reliable API → trust navigation path + sleep.
_SCREEN_CONFIRM_API: dict[str, str] = {
    "port":              "api_port/port",
    "repair":            "api_get_member/ndock",
    "hensei":            "api_get_member/deck",
    "expedition_select": "api_get_member/mission",
    "sortie_world":      "api_get_member/mapinfo",
    "practice":          "api_get_member/practice",
    "quest_list":        "api_get_member/questlist",
    # supply, sortie_type, equipment, factory: NO reliable entry API (KC caches them)
}

# Screens where the left sidebar (編成/補給/改装/入渠/工廠 + 母港) is visible.
_LEFT_NAV_SCREENS = frozenset({
    "supply", "repair", "hensei", "equipment", "factory", "equipment_other",
    "repair_ship_select", "repair_ship_confirm", "repair_confirm",
    "hensei_ship_select", "hensei_ship_confirm",
    "equipment_select", "equipment_confirm",
})

# Left-nav element keys for direct screen jumps from any left-nav screen.
_LEFT_NAV_ELEM: dict[str, str] = {
    "supply":    "left_nav_supply",
    "repair":    "left_nav_repair",
    "hensei":    "left_nav_hensei",
    "equipment": "left_nav_equip",
    "factory":   "left_nav_factory",
    "port":      "left_nav_home",
}


# ── Error type ─────────────────────────────────────────────────────────────

class ExecutionError(Exception):
    """Raised when an action fails to produce expected outcome within timeout."""


# ── Main Executor class ────────────────────────────────────────────────────

class Executor:
    """
    Async event-driven executor. Uses virtual mouse clicks via poi plugin.

    Parameters:
        base_url: kckit Simulator REST base URL (default http://localhost:8765)
        ws_url:   kckit Simulator WS URL        (default ws://localhost:8765/ws)
        dry_run:  True = log actions without sending real clicks (default True for safety)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8765",
        ws_url:   str = "ws://localhost:8765/ws",
        dry_run:  bool = True,
    ):
        self.base_url = base_url
        self.ws_url   = ws_url
        self.dry_run  = dry_run
        self._ws      = None
        self._screen: str | None = None
        self._event_q: asyncio.Queue = asyncio.Queue()
        self._listener: asyncio.Task | None = None

    async def __aenter__(self) -> "Executor":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()

    # ── Connection ─────────────────────────────────────────────────────────

    async def connect(self) -> None:
        import websockets  # type: ignore
        self._ws = await websockets.connect(self.ws_url)
        self._listener = asyncio.create_task(self._listen_loop())
        log.info("Executor connected to %s (dry_run=%s)", self.ws_url, self.dry_run)

    async def disconnect(self) -> None:
        if self._listener:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()

    async def _listen_loop(self) -> None:
        """Background task: read simulator WS, push to event queue, update screen state."""
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                    msg["_recv_at"] = asyncio.get_event_loop().time()
                    await self._event_q.put(msg)
                    if msg.get("type") == "screen_change" and msg.get("screen"):
                        prev = self._screen
                        self._screen = msg["screen"]
                        if prev != self._screen:
                            log.debug("screen: %s → %s  (src=%s)",
                                      prev, self._screen, msg.get("source"))
                except Exception:
                    pass
        except Exception as e:
            log.debug("WS listener ended: %s", e)

    # ── Core primitives ─────────────────────────────────────────────────────

    async def click(self, rx: float, ry: float, label: str = "") -> dict:
        """Send a virtual click to KC2 canvas at (rx, ry) [0-1] fractions.
        Adds a small random pre-click pause for human-like timing."""
        await asyncio.sleep(random.uniform(0.07, 0.15))
        if self.dry_run:
            log.info("[DRY RUN] click (%.3f, %.3f)  %s", rx, ry, label)
            return {"ok": True, "dry_run": True}
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.base_url}/api/click",
                params={"rx": rx, "ry": ry, "label": label},
                timeout=6.0,
            )
            r.raise_for_status()
        result = r.json()
        log.debug("click (%.3f, %.3f)  %s → %s", rx, ry, label, result)
        return result

    async def click_element(self, screen: str, key: str) -> dict:
        """Click a named element from screen_layout.yaml (looks up rx/ry automatically)."""
        rx, ry = _element_xy(screen, key)
        return await self.click(rx, ry, label=f"{screen}.{key}")

    async def wait_for_event(
        self, api_pattern: str, timeout: float = 8.0
    ) -> dict | None:
        """
        Wait for a game_event whose 'event' field contains api_pattern,
        or a screen_change whose 'api' field matches.

        Events received BEFORE this call began are ignored (timestamp guard).
        Returns the matching message dict, or None on timeout.
        """
        t_start = asyncio.get_event_loop().time()
        deadline = t_start + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                log.warning("wait_for_event('%s'): timeout after %.1fs", api_pattern, timeout)
                return None
            try:
                msg = await asyncio.wait_for(
                    self._event_q.get(), timeout=min(remaining, 1.0)
                )
            except asyncio.TimeoutError:
                continue
            if msg.get("_recv_at", 0) < t_start:
                continue  # stale event, pre-dates this action
            t = msg.get("type")
            if t == "game_event" and api_pattern in (msg.get("event") or ""):
                return msg
            if t == "screen_change" and api_pattern in (msg.get("api") or ""):
                return msg

    async def _pause(self, lo: float = 0.35, hi: float = 0.75) -> None:
        await asyncio.sleep(random.uniform(lo, hi))

    # ── Screen state ────────────────────────────────────────────────────────

    @property
    def screen(self) -> str | None:
        return self._screen

    async def back_to_port(self) -> bool:
        """
        Ground-truth anchor navigation. Tries back_port (teal circle) then
        left_nav_home, waits for api_port/port confirmation.
        Returns True if successfully confirmed at port.
        """
        log.info("back_to_port() from screen=%s", self._screen)
        # Search for back_port / left_nav_home in multiple screen contexts
        screen_candidates = [self._screen or "supply", "supply", "port"]
        elem_candidates = ["back_port", "left_nav_home"]
        for sc in screen_candidates:
            for elem in elem_candidates:
                try:
                    rx, ry = _element_xy(sc, elem)
                    await self.click(rx, ry, label=f"back_to_port.{elem}")
                    ev = await self.wait_for_event("api_port/port", timeout=9.0)
                    if ev:
                        self._screen = "port"
                        log.info("back_to_port(): confirmed")
                        return True
                except (KeyError, Exception) as e:
                    log.debug("back_to_port try %s.%s: %s", sc, elem, e)
                    continue
        log.error("back_to_port(): FAILED — unable to reach port")
        return False

    async def ensure_at_port(self) -> None:
        """Ensure we are at port, raising ExecutionError if not possible."""
        if self._screen == "port":
            return
        if not await self.back_to_port():
            raise ExecutionError("Cannot navigate to port — check game state manually")

    # ── Navigation ─────────────────────────────────────────────────────────

    async def navigate_to(self, target: str) -> bool:
        """
        Navigate to target screen using the port-anchor strategy:
          1. Already there → return immediately
          2. On a left-nav screen + target has a sidebar shortcut → use sidebar
          3. Otherwise → go to port first, then navigate from port via nav wheel

        Returns True if confirmed at target.
        """
        if self._screen == target:
            return True

        # Left-nav shortcut (avoids port detour when already in a menu screen)
        if self._screen in _LEFT_NAV_SCREENS and target in _LEFT_NAV_ELEM:
            elem = _LEFT_NAV_ELEM[target]
            log.info("navigate_to(%s): left-nav shortcut from %s", target, self._screen)
            await self.click_element(self._screen, elem)
            confirm = _SCREEN_CONFIRM_API.get(target)
            if confirm:
                ev = await self.wait_for_event(confirm, timeout=7.0)
                if not ev:
                    log.warning("navigate_to(%s): no entry API — trusting path", target)
            else:
                await self._pause(0.6, 1.0)
            self._screen = target
            return True

        # Port-anchor path
        await self.ensure_at_port()
        if target == "port":
            return True

        if target not in _PORT_NAV:
            raise ExecutionError(f"No navigation path to '{target}' defined in _PORT_NAV")

        log.info("navigate_to(%s) via port", target)
        current = "port"
        for elem, next_screen in _PORT_NAV[target]:
            await self.click_element(current, elem)
            confirm = _SCREEN_CONFIRM_API.get(next_screen)
            if confirm:
                ev = await self.wait_for_event(confirm, timeout=7.0)
                if not ev:
                    log.warning("navigate step to '%s': no API — trusting path", next_screen)
            else:
                await self._pause(0.65, 1.1)
            current = next_screen
            self._screen = current

        return self._screen == target

    # ── Game state helpers ──────────────────────────────────────────────────

    async def get_state(self) -> dict:
        """GET /api/state — curated snapshot (resources/fleets/repairs)."""
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}/api/state", timeout=5.0)
            r.raise_for_status()
        return r.json()

    async def get_ships(self) -> list[dict]:
        """GET /api/ships — full ship list with list_page/list_row for popup navigation."""
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}/api/ships", timeout=5.0)
            r.raise_for_status()
        return r.json().get("ships", [])

    def _load_snapshot(self) -> dict:
        if SNAPSHOT_PATH.exists():
            return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
        return {}

    # ── Actions ─────────────────────────────────────────────────────────────

    async def supply_fleet(self, fleet_id: int) -> bool:
        """
        Supply all ships in fleet_id (1-4) with fuel and ammo.
        Workflow: navigate to supply → select fleet tab → 一键全選 → 全補給
        Confirmation: api_req_hokyu/charge (absent if fleet already full — not an error)
        Note: supply screen has no entry API; navigation path is trusted.
        """
        log.info("supply_fleet(fleet=%d)", fleet_id)
        await self.navigate_to("supply")

        if fleet_id != 1:
            await self.click_element("supply", f"fleet_tab_{fleet_id}")
            await self._pause(0.3, 0.5)

        await self.click_element("supply", "select_all")
        await self._pause(0.18, 0.35)
        await self.click_element("supply", "supply_all_btn")

        ev = await self.wait_for_event("api_req_hokyu/charge", timeout=5.0)
        if not ev:
            log.info("supply_fleet(%d): no charge event — fleet may already be fully supplied", fleet_id)
            return False

        log.info("supply_fleet(%d): confirmed (api_req_hokyu/charge)", fleet_id)
        return True

    async def send_expedition(self, fleet_id: int, exp_id: int) -> bool:
        """
        Send fleet_id (2-4) on expedition exp_id.
        Workflow: navigate to expedition_select → select fleet → select area → select item → 決定
        Confirmation: api_req_mission/start
        Scroll: exp_item_1..6 visible at once; scroll_down shifts list by 1 row.
          pos ≤ 6: click exp_item_{pos} directly
          pos > 6: scroll (pos-6) times, then click exp_item_6
        """
        if fleet_id == 1:
            raise ValueError("Fleet 1 cannot go on expedition")
        log.info("send_expedition(fleet=%d, exp=%d)", fleet_id, exp_id)

        await self.navigate_to("expedition_select")

        await self.click_element("expedition_select", f"fleet_tab_{fleet_id}")
        await self._pause(0.3, 0.5)

        area = _expedition_area(exp_id)
        await self.click_element("expedition_select", f"area_{area}")
        await self._pause(0.3, 0.5)

        pos = _expedition_pos_in_area(exp_id)
        if pos <= 6:
            await self.click_element("expedition_select", f"exp_item_{pos}")
        else:
            for _ in range(pos - 6):
                await self.click_element("expedition_select", "scroll_down")
                await self._pause(0.25, 0.4)
            await self.click_element("expedition_select", "exp_item_6")
        await self._pause(0.3, 0.5)

        await self.click_element("expedition_select", "go_btn")

        ev = await self.wait_for_event("api_req_mission/start", timeout=9.0)
        if not ev:
            log.error("send_expedition(fleet=%d, exp=%d): no confirmation", fleet_id, exp_id)
            await self.back_to_port()
            raise ExecutionError(
                f"Expedition start failed: no api_req_mission/start "
                f"(fleet {fleet_id} exp {exp_id})"
            )

        log.info("send_expedition(fleet=%d, exp=%d): confirmed", fleet_id, exp_id)
        self._screen = "port"
        return True

    async def collect_expedition_result(self, fleet_id: int) -> bool:
        """
        Dismiss the expedition result popup for fleet_id.
        Fires when the game sends api_req_mission/result.
        Confirmation: api_req_mission/result → click confirm
        """
        log.info("collect_expedition_result(fleet=%d)", fleet_id)
        ev = await self.wait_for_event("api_req_mission/result", timeout=8.0)
        if not ev:
            log.warning("collect_expedition_result(%d): no result event within timeout", fleet_id)
            return False

        await self._pause(0.5, 0.9)
        try:
            await self.click_element("expedition_result", "confirm")
            log.info("collect_expedition_result(%d): dismissed", fleet_id)
        except KeyError:
            log.warning("expedition_result.confirm not in layout — popup may have auto-dismissed")
        return True

    async def start_repair(
        self, dock_id: int, ship_instance_id: int, use_bucket: bool = False
    ) -> bool:
        """
        Start repair for ship_instance_id in dock_id (1-4).
        Workflow: navigate to repair → click dock → click ship row → confirm → YES
        Confirmation: api_req_nyukyo/start (or speedchange if use_bucket=True)

        Limitation: only supports page 0 (ships 1-10 in sorted order).
        Ships on pages 1+ will use row 0 as fallback with a warning.
        """
        log.info("start_repair(dock=%d, ship=%d, bucket=%s)", dock_id, ship_instance_id, use_bucket)
        await self.navigate_to("repair")

        await self.click_element("repair", f"dock_{dock_id}")
        await self._pause(0.4, 0.7)
        self._screen = "repair_ship_select"

        ships = await self.get_ships()
        ship = next((s for s in ships if s.get("id") == ship_instance_id), None)
        if not ship:
            log.error("start_repair: ship %d not found", ship_instance_id)
            await self.back_to_port()
            raise ExecutionError(f"Ship {ship_instance_id} not found in ship list")

        page = ship.get("list_page", 0)
        row  = ship.get("list_row", 0)  # 0-indexed

        if page > 0:
            log.warning(
                "start_repair: ship %d on page %d — only page 0 supported, using row 0 as fallback",
                ship_instance_id, page
            )
            row = 0

        await self.click_element("repair_ship_select", f"ship_item_{row + 1}")
        await self._pause(0.4, 0.65)
        self._screen = "repair_ship_confirm"

        if use_bucket:
            try:
                await self.click_element("repair_ship_confirm", "bucket_toggle")
                await self._pause(0.2, 0.35)
            except KeyError:
                log.warning("bucket_toggle not in layout — skipping")

        await self.click_element("repair_ship_confirm", "confirm_btn")
        await self._pause(0.3, 0.5)
        self._screen = "repair_confirm"

        await self.click_element("repair_confirm", "yes_btn")

        confirm_api = "api_req_nyukyo/speedchange" if use_bucket else "api_req_nyukyo/start"
        ev = await self.wait_for_event(confirm_api, timeout=9.0)
        if not ev:
            log.error("start_repair(dock=%d, ship=%d): no %s", dock_id, ship_instance_id, confirm_api)
            await self.back_to_port()
            raise ExecutionError(
                f"Repair start failed: no {confirm_api} "
                f"(dock {dock_id}, ship {ship_instance_id})"
            )

        log.info("start_repair(dock=%d, ship=%d): confirmed", dock_id, ship_instance_id)
        self._screen = "repair"
        return True

    # ── Daily routine ───────────────────────────────────────────────────────

    async def run_daily_routine(
        self,
        fleet_plan: dict[int, int] | None = None,
        supply_fleets: list[int] | None = None,
    ) -> dict:
        """
        Standard daily maintenance cycle:
          1. Supply all specified fleets (default: all 4)
          2. Collect returned expeditions and resend
          3. Fill empty repair docks with most-damaged ships

        Args:
            fleet_plan:    {fleet_id: exp_id} override. None = read from ezexped plugin.
            supply_fleets: fleet ids to supply. Default = [1, 2, 3, 4]

        Returns summary dict: {supply, expedition, repair, errors}
        """
        from core.expedition_manager import ExpeditionManager, load_ezexped_plan
        from core.repair_manager import RepairManager

        summary: dict[str, list[str]] = {
            "supply": [], "expedition": [], "repair": [], "errors": []
        }
        snap = self._load_snapshot()

        # 1. Supply ────────────────────────────────────────────────────────
        for fid in (supply_fleets or [1, 2, 3, 4]):
            try:
                charged = await self.supply_fleet(fid)
                status = "supplied" if charged else "already full"
                summary["supply"].append(f"fleet {fid}: {status}")
                await self._pause(0.5, 1.0)
            except ExecutionError as e:
                summary["errors"].append(f"supply fleet {fid}: {e}")
                await self.ensure_at_port()

        # 2. Expeditions ───────────────────────────────────────────────────
        if snap:
            effective_plan = fleet_plan or load_ezexped_plan()
            exp_mgr = ExpeditionManager(config={"expedition_plan": effective_plan})
            actions = exp_mgr.assess(_SnapshotExpeditionState(snap))

            for action in actions:
                fid, eid = action.fleet_id, action.expedition_id
                try:
                    if action.action == "resend":
                        await self.send_expedition(fid, eid)
                        summary["expedition"].append(f"fleet {fid}: sent exp {eid}")
                        await self._pause(1.0, 2.0)

                    elif action.action == "collect":
                        collected = await self.collect_expedition_result(fid)
                        note = "collected" if collected else "collect failed"
                        summary["expedition"].append(f"fleet {fid}: {note} exp {eid}")
                        if collected:
                            await self._pause(0.5, 1.0)
                            await self.send_expedition(fid, eid)
                            summary["expedition"].append(f"fleet {fid}: resent exp {eid}")
                            await self._pause(1.0, 2.0)

                    else:
                        summary["expedition"].append(f"fleet {fid}: {action.note}")

                except ExecutionError as e:
                    summary["errors"].append(f"expedition fleet {fid}: {e}")
                    await self.ensure_at_port()

        # 3. Repair ────────────────────────────────────────────────────────
        if snap:
            rep_mgr = RepairManager()
            rep_actions = rep_mgr.assess(_SnapshotRepairState(snap))

            for action in rep_actions:
                if action.action != "start_repair":
                    summary["repair"].append(f"dock {action.dock_id}: {action.note}")
                    continue
                try:
                    await self.start_repair(action.dock_id, action.ship_id)
                    summary["repair"].append(f"dock {action.dock_id}: {action.ship_name} started")
                    await self._pause(0.8, 1.5)
                except ExecutionError as e:
                    summary["errors"].append(f"repair dock {action.dock_id}: {e}")
                    await self.ensure_at_port()

        return summary


# ── State adapters for manager classes ─────────────────────────────────────
# ExpeditionManager.assess() and RepairManager.assess() expect objects with
# specific attributes. These adapters wrap the snapshot dict.

class _SnapshotExpeditionState:
    """Adapts box_snapshot.json to what ExpeditionManager.assess() expects."""

    def __init__(self, snap: dict):
        from dataclasses import dataclass

        @dataclass
        class Fleet:
            in_expedition: bool
            expedition_id: int | None
            expedition_return_ms: float

        self.fleets: dict[int, Fleet] = {}
        for fid, fleet in (snap.get("fleets") or {}).items():
            mission = fleet.get("api_mission") or [0, 0, 0, 0]
            self.fleets[int(fid)] = Fleet(
                in_expedition=mission[0] == 1,
                expedition_id=int(mission[1]) if mission[0] == 1 else None,
                expedition_return_ms=float(mission[2]) if mission[0] == 1 else 0.0,
            )


class _SnapshotRepairState:
    """Adapts box_snapshot.json to what RepairManager.assess() expects."""

    def __init__(self, snap: dict):
        from dataclasses import dataclass

        @dataclass
        class Dock:
            dock_id: int
            ship_id: int
            is_empty: bool
            complete_dt: Optional[datetime]

        @dataclass
        class Ship:
            instance_id: int
            name: str
            now_hp: int
            max_hp: int
            level: int
            in_repair: bool

            @property
            def hp_ratio(self) -> float:
                return self.now_hp / max(self.max_hp, 1)

            @property
            def is_taiha(self) -> bool:
                return self.hp_ratio <= 0.25

        self.repair_docks: list[Dock] = []
        in_repair_ids: set[int] = set()
        repairs_raw = snap.get("repairs") or {}
        docks_raw = repairs_raw if isinstance(repairs_raw, list) else list(repairs_raw.values())
        for dock in docks_raw:
            if not isinstance(dock, dict):
                continue
            ship_id     = dock.get("api_ship_id", 0)
            state       = dock.get("api_state", 0)
            complete_ms = dock.get("api_complete_time", 0)
            complete_dt = datetime.fromtimestamp(complete_ms / 1000) if complete_ms else None
            if ship_id:
                in_repair_ids.add(ship_id)
            self.repair_docks.append(Dock(
                dock_id=dock.get("api_id", 0),
                ship_id=ship_id,
                is_empty=(state <= 0 or ship_id == 0),
                complete_dt=complete_dt,
            ))

        self.ships: dict[int, Ship] = {}
        for sid, ship in (snap.get("ships") or {}).items():
            master  = ship.get("$master") or {}
            now_hp  = ship.get("api_nowhp", 0)
            max_hp  = ship.get("api_maxhp", 1)
            iid     = ship.get("api_id", 0)
            self.ships[int(sid)] = Ship(
                instance_id=iid,
                name=master.get("api_name", "？"),
                now_hp=now_hp,
                max_hp=max_hp,
                level=ship.get("api_lv", 1),
                in_repair=iid in in_repair_ids,
            )


# ──────────────────────────────────────────────────────────────────────────
# Legacy SortieExecutor (pyautogui screen-coordinate approach)
# Kept for reference; not used for daily routine.
# ──────────────────────────────────────────────────────────────────────────

import logging as _logging
import time as _time
from enum import Enum as _Enum, auto as _auto

try:
    import pyautogui as _pyautogui
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
    _LEGACY_DEPS = True
except ImportError:
    _LEGACY_DEPS = False

_legacy_log = _logging.getLogger(__name__ + ".legacy")

_BATTLE_RESULT_PATHS = {
    "/kcsapi/api_req_sortie/battleresult",
    "/kcsapi/api_req_combined_battle/battleresult",
}
_MAP_START = "/kcsapi/api_req_map/start"
_MAP_NEXT  = "/kcsapi/api_req_map/next"
_PORT      = "/kcsapi/api_port/port"

_WINDOW_PATH = ROOT / "config" / "poi_window.yaml"


class SortieResult(_Enum):
    S_RANK         = _auto()
    A_RANK         = _auto()
    B_RANK         = _auto()
    RETREAT_TAIHA  = _auto()
    RETREAT_MANUAL = _auto()
    ERROR          = _auto()
    NOT_CALIBRATED = _auto()


class CanvasConfig:
    def __init__(self, x: int = 0, y: int = 0, w: int = 800, h: int = 480):
        self.x, self.y, self.w, self.h = x, y, w, h

    @classmethod
    def load(cls, path: Path = _WINDOW_PATH) -> "CanvasConfig":
        if path.exists():
            import yaml
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
        return int(self.x + fx * self.w), int(self.y + fy * self.h)


class ScreenLayout:
    def __init__(self, canvas: CanvasConfig, path: Path = LAYOUT_PATH):
        self._canvas = canvas
        import yaml
        with open(path) as f:
            data = yaml.safe_load(f)
        self._screens = data.get("screens", {})

    def px(self, screen: str, element: str) -> Optional[tuple[int, int]]:
        elem = self._screens.get(screen, {}).get("elements", {}).get(element)
        return self._canvas.px(elem["x"], elem["y"]) if elem else None


class MouseController:
    def click(self, x: int, y: int, jitter: int = 4) -> None:
        if not _LEGACY_DEPS:
            return
        jx, jy = jitter_point(x, y, jitter)
        pts = bezier_points(_pyautogui.position(), (jx, jy), n=30)
        for px, py in pts:
            _pyautogui.moveTo(int(px), int(py), _pause=False)
            _time.sleep(0.013)
        click_delay()
        _pyautogui.click(jx, jy)

    def click_element(self, pos: Optional[tuple[int, int]]) -> bool:
        if pos is None:
            return False
        self.click(*pos)
        return True


class SortieExecutor:
    """Legacy sortie executor (pyautogui-based). Use async Executor for new automation."""

    def __init__(
        self,
        poi: "PoiClient",
        canvas: Optional[CanvasConfig] = None,
        fleet_id: int = 1,
        formation: int = 4,
        night_battle: bool = False,
        dry_run: bool = False,
    ):
        self.poi = poi
        self.canvas = canvas or CanvasConfig.load()
        self.fleet_id = fleet_id
        self.formation = formation
        self.night_battle = night_battle
        self.dry_run = dry_run
        self.mouse = MouseController()
        self._layout = ScreenLayout(self.canvas) if LAYOUT_PATH.exists() else None
        if _LEGACY_DEPS:
            _pyautogui.FAILSAFE = True
            _pyautogui.PAUSE = 0.05

    def run_sortie(self, map_id: str) -> SortieResult:
        if not self.canvas.is_calibrated and not self.dry_run:
            _legacy_log.error("Canvas not calibrated. Run tools/calibrate.py first.")
            return SortieResult.NOT_CALIBRATED
        _legacy_log.info("Sortie: map=%s fleet=%d dry=%s", map_id, self.fleet_id, self.dry_run)
        try:
            current = self.poi.state.sortie
            if current.in_sortie and current.map_str == map_id:
                ev = {"body": {}}
            else:
                self._navigate_to_map(map_id)
                ev = self.poi.wait_for_event(_MAP_START, timeout=60)
                if ev is None:
                    return SortieResult.ERROR

            node_count = 0
            rank = "?"
            while True:
                node_count += 1
                state = self.poi.state
                fleet = state.fleets.get(self.fleet_id)
                if fleet and _LEGACY_DEPS:
                    check_taiha(fleet.ships, context=f"map={map_id} node={node_count}")

                self._click("formation_select", f"formation_{self.formation}")
                if _LEGACY_DEPS:
                    action_delay()

                result_ev = self.poi.wait_for_any_event(list(_BATTLE_RESULT_PATHS), timeout=180)
                if result_ev is None:
                    return SortieResult.ERROR

                _, payload = result_ev
                rank = payload.get("body", {}).get("api_win_rank", "?")

                if _LEGACY_DEPS:
                    action_delay()
                state = self.poi.state
                fleet = state.fleets.get(self.fleet_id)
                if fleet and _LEGACY_DEPS:
                    check_taiha(fleet.ships, context=f"post-battle node={node_count}")

                if self.night_battle:
                    self._click("night_battle_select", "night_battle")
                    if _LEGACY_DEPS:
                        random_delay(1.5, 0.5)
                    night_ev = self.poi.wait_for_any_event(list(_BATTLE_RESULT_PATHS), timeout=120)
                    if night_ev:
                        _, nb_payload = night_ev
                        rank = nb_payload.get("body", {}).get("api_win_rank", rank)

                self._click("battle_result", "next")
                if _LEGACY_DEPS:
                    random_delay(1.5, 0.5)

                next_ev = self.poi.wait_for_any_event([_MAP_NEXT, _PORT], timeout=30)
                if next_ev is None or next_ev[0] == _PORT:
                    break

                _, next_payload = next_ev
                boss_node = next_payload.get("body", {}).get("api_event_id") in (5, 6)
                self._click("post_battle", "advance")
                if _LEGACY_DEPS:
                    action_delay()

            return {"S": SortieResult.S_RANK, "A": SortieResult.A_RANK,
                    "B": SortieResult.B_RANK}.get(rank, SortieResult.A_RANK)

        except Exception as e:
            if _LEGACY_DEPS and isinstance(e, TaihaAdvanceError):
                _legacy_log.warning("Taiha — retreating: %s", e)
                self._click("post_battle", "retreat")
                return SortieResult.RETREAT_TAIHA
            _legacy_log.error("Sortie error: %s", e, exc_info=True)
            return SortieResult.ERROR

    def _click(self, screen: str, element: str) -> bool:
        if self._layout is None:
            return False
        pos = self._layout.px(screen, element)
        if pos is None:
            _legacy_log.warning("Element not found: %s.%s", screen, element)
            return False
        if self.dry_run:
            _legacy_log.info("[DRY-RUN] click %s.%s @ %s", screen, element, pos)
            return True
        return self.mouse.click_element(pos)

    def _navigate_to_map(self, map_id: str) -> None:
        self._click("port", "sortie_nav")
        if _LEGACY_DEPS:
            random_delay(1.2, 0.4)
        _legacy_log.warning("_navigate_to_map: map node selection not implemented for %s", map_id)

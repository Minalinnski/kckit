"""WebSocket client — receives game state pushed from poi-plugin-kckit-bridge."""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Callable, Optional

import websockets
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from .models import Construction, Equipment, Fleet, GameState, Quest, RepairDock, Ship, SortieState

log = logging.getLogger(__name__)

POI_BRIDGE_URL = "ws://127.0.0.1:23456"


class PoiClient:
    """
    Thread-safe WebSocket client.
    Maintains a live GameState updated by push events from poi.
    Call `start()` to connect in a background thread.
    """

    def __init__(self, url: str = POI_BRIDGE_URL):
        self.url = url
        self._state: Optional[GameState] = None
        self._state_lock = threading.Lock()
        self._event_handlers: list[Callable[[str, dict], None]] = []
        self._screen_handlers: list[Callable[[str, str], None]] = []
        self._last_screen: Optional[str] = None
        self._last_screen_source: str = ""
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()

    # ── Public API ──────────────────────────────────────────────────────────

    def start(self, timeout: float = 10.0) -> None:
        """Connect and block until first state is received (or timeout)."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._connected_event.wait(timeout):
            raise TimeoutError(
                f"Could not connect to poi bridge at {self.url} within {timeout}s. "
                "Make sure poi is running with the kckit-bridge plugin enabled."
            )

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def state(self) -> GameState:
        with self._state_lock:
            if self._state is None:
                raise RuntimeError("No state received yet. Call start() first.")
            return self._state

    def on_event(self, handler: Callable[[str, dict], None]) -> None:
        """Register a callback for raw game events (path, payload)."""
        self._event_handlers.append(handler)

    def on_screen_change(self, handler: Callable[[str, str], None]) -> None:
        """Register a callback for screen changes (screen_name, source).

        screen_name comes from the injected spy (DOM-based, ground truth) or
        the navigation watcher. More reliable than API event inference.
        """
        self._screen_handlers.append(handler)

    @property
    def current_screen(self) -> Optional[str]:
        """Last known screen name from the screen spy, or None if unknown."""
        with self._state_lock:
            return self._last_screen

    def inject_screen_spy(self) -> None:
        """Ask the plugin to inject the persistent DOM screen spy into KC webview."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_cmd({"cmd": "inject_screen_spy"}), self._loop
            )

    def get_spy_screen(self) -> Optional[str]:
        """Return the spy's current screen. Triggers a fresh poll in background;
        returns the last known value immediately (updated asynchronously)."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_cmd({"cmd": "get_spy_screen"}), self._loop
            )
        return self._last_screen

    def probe_kc_globals(self) -> None:
        """Ask plugin to dump KC webview globals (for discovery/debugging)."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_cmd({"cmd": "probe_kc_globals"}), self._loop
            )

    def get_resource_path(self) -> None:
        """Ask plugin to report the local KC asset cache path."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._send_cmd({"cmd": "get_resource_path"}), self._loop
            )

    def request_state(self) -> GameState:
        """Ask poi to push a fresh state snapshot."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._send_cmd({"cmd": "get_state"}), self._loop)
        return self.state

    def wait_for_event(
        self,
        event_path: str,
        timeout: float = 120.0,
        predicate: Callable[[dict], bool] = None,
    ) -> Optional[dict]:
        """
        Block until a specific API event fires, return its payload dict.
        Returns None if timeout exceeded.
        predicate: optional extra condition on the payload (for disambiguation).
        """
        fired = threading.Event()
        result: dict[str, Optional[dict]] = {"payload": None}

        def handler(path: str, payload: dict) -> None:
            if path == event_path:
                if predicate is None or predicate(payload):
                    result["payload"] = payload
                    fired.set()

        self._event_handlers.append(handler)
        try:
            fired.wait(timeout=timeout)
            return result["payload"]
        finally:
            try:
                self._event_handlers.remove(handler)
            except ValueError:
                pass

    def wait_for_any_event(
        self,
        event_paths: list[str],
        timeout: float = 120.0,
    ) -> Optional[tuple[str, dict]]:
        """
        Block until any of the given event paths fires.
        Returns (path, payload) or None on timeout.
        """
        fired = threading.Event()
        result: dict[str, object] = {"path": None, "payload": None}

        def handler(path: str, payload: dict) -> None:
            if path in event_paths and not fired.is_set():
                result["path"] = path
                result["payload"] = payload
                fired.set()

        self._event_handlers.append(handler)
        try:
            fired.wait(timeout=timeout)
            if result["path"]:
                return (result["path"], result["payload"])
            return None
        finally:
            try:
                self._event_handlers.remove(handler)
            except ValueError:
                pass

    # ── Internal ────────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._connect_loop())
        self._loop.close()

    async def _connect_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.url, max_size=8*1024*1024) as ws:
                    self._ws = ws
                    log.info("Connected to poi bridge at %s", self.url)
                    await self._receive_loop(ws)
            except (ConnectionRefusedError, OSError):
                log.debug("poi bridge not available, retrying in 3s…")
                await asyncio.sleep(3)
            except (ConnectionClosedOK, ConnectionClosedError):
                log.warning("Connection closed, reconnecting…")
                await asyncio.sleep(1)

    async def _receive_loop(self, ws) -> None:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if msg.get("type") == "state":
                self._apply_state(msg["payload"])
                self._connected_event.set()

            elif msg.get("type") == "event":
                path = msg.get("event", "")
                payload = msg.get("payload", {})
                for handler in self._event_handlers:
                    try:
                        handler(path, payload)
                    except Exception as e:
                        log.error("Event handler error: %s", e)

            elif msg.get("type") == "screen_change":
                # Emitted by the screen spy or nav watcher — ground truth screen state
                screen = msg.get("screen")
                source = msg.get("source", "unknown")
                log.debug("Screen change: %s (source=%s)", screen, source)
                with self._state_lock:
                    if screen:  # only update if non-null (null = unknown transition)
                        self._last_screen = screen
                        self._last_screen_source = source
                for handler in self._screen_handlers:
                    try:
                        handler(screen, source)
                    except Exception as e:
                        log.error("Screen handler error: %s", e)

            elif msg.get("type") == "spy_screen":
                payload = msg.get("payload") or {}
                screen = payload.get("screen")
                source = payload.get("source", "spy_poll")
                if screen:
                    with self._state_lock:
                        self._last_screen = screen
                        self._last_screen_source = source
                    log.debug("Spy screen poll: %s", screen)

            elif msg.get("type") in ("kc_globals", "resource_path", "spy_result", "page_state", "canvas_info"):
                log.debug("Plugin info msg [%s]: %s", msg.get("type"), msg.get("payload"))

    async def _send_cmd(self, cmd: dict) -> None:
        if hasattr(self, "_ws") and self._ws.open:
            await self._ws.send(json.dumps(cmd))

    def _apply_state(self, raw: dict) -> None:
        """Parse raw poi state JSON into a GameState object."""
        equips = self._parse_equips(raw.get("equips", {}))
        ships = self._parse_ships(raw.get("ships", {}), equips)
        fleets = self._parse_fleets(raw.get("fleets", {}), ships)

        # Parse repair docks
        raw_repairs = raw.get("repairs") or []
        if isinstance(raw_repairs, dict):
            raw_repairs = list(raw_repairs.values())
        repair_docks = [RepairDock.from_poi(r) for r in raw_repairs if isinstance(r, dict)]

        # Parse constructions
        raw_constructions = raw.get("constructions") or []
        if isinstance(raw_constructions, dict):
            raw_constructions = list(raw_constructions.values())
        constructions = [Construction.from_poi(c) for c in raw_constructions if isinstance(c, dict)]

        # Parse quests
        raw_quests = raw.get("quests") or {}
        if isinstance(raw_quests, dict):
            raw_quests_list = list(raw_quests.values())
        else:
            raw_quests_list = list(raw_quests)
        quests = [Quest.from_poi(q) for q in raw_quests_list if isinstance(q, dict)]

        sortie = SortieState.from_poi(raw.get("sortie") or {})

        state = GameState(
            ships=ships,
            equips=equips,
            fleets=fleets,
            resources=raw.get("resources", {}),
            repair_docks=repair_docks,
            constructions=constructions,
            quests=quests,
            sortie=sortie,
            last_event=raw.get("last_event", ""),
            hq_level=raw.get("hq_level", 120),
            timestamp=raw.get("timestamp", 0),
        )

        with self._state_lock:
            self._state = state

        log.debug(
            "State updated: %d ships, %d equips, %d fleets",
            len(ships), len(equips), len(fleets),
        )

    @staticmethod
    def _parse_equips(raw_equips: dict) -> dict[int, Equipment]:
        result = {}
        for _key, data in raw_equips.items():
            master = data.get("$master", {})
            try:
                eq = Equipment.from_poi(data, master)
                result[eq.instance_id] = eq
            except (KeyError, TypeError) as e:
                log.debug("Skip equip parse: %s", e)
        return result

    @staticmethod
    def _parse_ships(raw_ships: dict, equips: dict[int, Equipment]) -> dict[int, Ship]:
        result = {}
        for _key, data in raw_ships.items():
            master = data.get("$master", {})
            try:
                ship = Ship.from_poi(data, master, equips)
                result[ship.instance_id] = ship
            except (KeyError, TypeError) as e:
                log.debug("Skip ship parse: %s", e)
        return result

    @staticmethod
    def _parse_fleets(raw_fleets: dict, ships: dict[int, Ship]) -> dict[int, Fleet]:
        result = {}
        for _key, data in raw_fleets.items():
            try:
                fleet = Fleet.from_poi(data, ships)
                result[fleet.fleet_id] = fleet
            except (KeyError, TypeError) as e:
                log.debug("Skip fleet parse: %s", e)
        return result

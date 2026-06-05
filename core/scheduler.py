"""
Task scheduler — priority-based main loop for unattended operation.

Priority queue:
  1. Expedition return (most time-sensitive, do immediately)
  2. Repair dock free slot (send damaged ships)
  3. Resupply fleets
  4. Sortie (configured map)
  5. Construction / development (when resources allow)

Anti-detection: RestWatchdog ensures ≥4h rest per 24h.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum, auto
from typing import TYPE_CHECKING, Callable, Optional

from .expedition_manager import ExpeditionManager
from .poi_client import PoiClient
from .repair_manager import RepairManager
from .safety import RestWatchdog

if TYPE_CHECKING:
    from .executor import SortieExecutor

log = logging.getLogger(__name__)


class TaskType(Enum):
    EXPEDITION_RETURN = auto()
    REPAIR = auto()
    RESUPPLY = auto()
    SORTIE = auto()
    IDLE = auto()


@dataclass(order=True)
class ScheduledTask:
    priority: int
    due_at: datetime
    task_type: TaskType = field(compare=False)
    callback: Callable[[], None] = field(compare=False, repr=False)
    description: str = field(compare=False, default="")


class Scheduler:
    """
    Event-driven scheduler.
    Blocks on the next due task rather than busy-polling.
    """

    POLL_INTERVAL = 30   # seconds between state checks when idle

    def __init__(
        self,
        poi: PoiClient,
        config: dict,
        executor: Optional["SortieExecutor"] = None,
    ):
        self.poi = poi
        self.config = config
        self._executor = executor
        self.watchdog = RestWatchdog()
        self._tasks: list[ScheduledTask] = []
        self._running = False
        self._sortie_callback: Optional[Callable[[], None]] = None
        self._expedition_fleet_ids: list[int] = config.get("expedition_fleets", [2, 3, 4])
        self._expedition_ids: dict[int, int] = config.get("expeditions", {
            2: 5,   # fleet 2 → expedition 5 (鎮守府近海対潜哨戒)
            3: 5,
            4: 37,  # fleet 4 → expedition 37 (遠洋練習航海)
        })
        self._expedition_mgr = ExpeditionManager(config)
        self._repair_mgr = RepairManager(config)

    def set_executor(self, executor: "SortieExecutor") -> None:
        self._executor = executor

    def set_sortie_callback(self, fn: Callable[[], None]) -> None:
        self._sortie_callback = fn

    def run(self) -> None:
        """Main loop. Blocks until stopped."""
        self._running = True
        log.info("Scheduler started")

        try:
            while self._running:
                self.watchdog.check_and_rest_if_needed()
                self._refresh_tasks()
                next_task = self._next_due_task()

                if next_task is None:
                    log.debug("No tasks due, sleeping %ds", self.POLL_INTERVAL)
                    time.sleep(self.POLL_INTERVAL)
                    continue

                wait_secs = (next_task.due_at - datetime.now()).total_seconds()
                if wait_secs > 0:
                    log.info(
                        "Next: [%s] %s in %.0fs",
                        next_task.task_type.name,
                        next_task.description,
                        wait_secs,
                    )
                    # Sleep in chunks so we can handle interrupts
                    self._interruptible_sleep(wait_secs)

                if not self._running:
                    break

                self._execute(next_task)
        except KeyboardInterrupt:
            log.info("Scheduler interrupted by user")
        finally:
            self._running = False
            log.info("Scheduler stopped")

    def stop(self) -> None:
        self._running = False

    # ── Task management ──────────────────────────────────────────────────────

    def _refresh_tasks(self) -> None:
        """Rebuild task queue from current game state."""
        self._tasks.clear()
        state = self.poi.state
        now = datetime.now()

        # ── Expedition returns ─────────────────────────────────────────────
        for fleet_id in self._expedition_fleet_ids:
            fleet = state.fleets.get(fleet_id)
            if not fleet or not fleet.in_expedition:
                continue
            exped_id = fleet.expedition_id
            return_ms = fleet.expedition_return_ms
            if return_ms > 0:
                due = datetime.fromtimestamp(return_ms / 1000)
            else:
                due = now + timedelta(seconds=30)
            self._tasks.append(ScheduledTask(
                priority=1,
                due_at=due,
                task_type=TaskType.EXPEDITION_RETURN,
                callback=lambda fid=fleet_id, eid=exped_id: self._handle_expedition(fid, eid),
                description=f"Fleet {fleet_id} expedition {exped_id} return",
            ))

        # ── Repair docks ──────────────────────────────────────────────────────
        repair_wake = self._repair_mgr.next_wakeup_seconds(state)
        if repair_wake != float("inf"):
            repair_due = now + timedelta(seconds=max(0, repair_wake))
        else:
            repair_due = now  # no repairs running — check immediately for idle queue
        self._tasks.append(ScheduledTask(
            priority=2,
            due_at=repair_due,
            task_type=TaskType.REPAIR,
            callback=self._handle_repair,
            description="Repair dock check",
        ))

        # ── Sortie (lowest priority, fill idle time) ───────────────────────
        if self._sortie_callback and not self._all_fleets_busy():
            self._tasks.append(ScheduledTask(
                priority=10,
                due_at=now,
                task_type=TaskType.SORTIE,
                callback=self._sortie_callback,
                description="Sortie",
            ))

        # Sort: lower number = higher priority; earlier due = run first
        self._tasks.sort()

    def _next_due_task(self) -> Optional[ScheduledTask]:
        """Return the highest-priority task that is due (or nearly due) now."""
        now = datetime.now()
        for task in self._tasks:
            if task.due_at <= now + timedelta(seconds=5):
                return task
        return self._tasks[0] if self._tasks else None

    def _execute(self, task: ScheduledTask) -> None:
        log.info("Executing: %s", task.description)
        try:
            task.callback()
        except Exception as e:
            log.error("Task failed [%s]: %s", task.task_type.name, e, exc_info=True)
        self._tasks.remove(task)

    # ── Handlers ─────────────────────────────────────────────────────────────

    def _handle_expedition(self, fleet_id: int, exped_id: int) -> None:
        log.info("Fleet %d expedition %d event fired", fleet_id, exped_id)
        state = self.poi.state
        actions = self._expedition_mgr.assess(state)
        for action in actions:
            if action.action == "collect":
                log.info(
                    "[expedition] COLLECT fleet %d exp %d — %s",
                    action.fleet_id, action.expedition_id, action.note,
                )
                if self._executor:
                    self._executor.collect_expedition_result(action.fleet_id)
            elif action.action == "resend":
                log.info(
                    "[expedition] RESEND fleet %d → exp %d — %s",
                    action.fleet_id, action.expedition_id, action.note,
                )
                if self._executor:
                    self._executor.resend_expedition(action.fleet_id, action.expedition_id)
            elif action.action == "wait":
                log.debug(
                    "[expedition] WAIT fleet %d exp %d — %s",
                    action.fleet_id, action.expedition_id, action.note,
                )
        next_wake = self._expedition_mgr.next_wakeup_seconds(state)
        if next_wake > 0:
            log.info("[expedition] next wakeup in %.0fs", next_wake)

    def _handle_repair(self) -> None:
        state = self.poi.state
        actions = self._repair_mgr.assess(state)
        start_actions = []
        for action in actions:
            if action.action == "collect":
                log.info(
                    "[repair] COLLECT dock %d ship %d — %s",
                    action.dock_id, action.ship_id, action.note,
                )
            elif action.action == "start_repair":
                log.info(
                    "[repair] START_REPAIR dock %d %s — %s",
                    action.dock_id, action.ship_name, action.note,
                )
                start_actions.append(action)
            elif action.action == "wait":
                log.debug("[repair] WAIT dock %d — %s", action.dock_id, action.note)
            elif action.action == "idle":
                log.debug("[repair] IDLE — %s", action.note)

        if start_actions and self._executor:
            self._executor.navigate_to_repair()
            for action in start_actions:
                self._executor.start_repair_ship(action.dock_id)
            self._executor._click("repair", "back")

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _all_fleets_busy(self) -> bool:
        state = self.poi.state
        fleet_1 = state.fleets.get(1)
        return fleet_1 is not None and fleet_1.in_expedition

    def _interruptible_sleep(self, seconds: float) -> None:
        """Sleep in small chunks so KeyboardInterrupt works promptly."""
        end = time.time() + seconds
        while time.time() < end and self._running:
            time.sleep(min(5.0, end - time.time()))

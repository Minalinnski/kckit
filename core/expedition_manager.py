"""
Expedition manager — determines what to do with fleet 2/3/4 expeditions.

Expedition IDs and durations (minutes):
  2=40, 3=20, 4=40, 5=90, 6=40, 7=60, 8=180, 9=240, 10=30, 11=60,
  16=180, 17=120, 21=60, 22=40, 23=20, 24=360, 25=480, 26=360,
  27=60, 28=60, 33=240, 37=270, 38=180, 40=90, 41=120, 43=120

Resource yields per hour (approximate, for display only):
  2: fuel+250, 9: ammo+300+bauxite, 11: steel+400, 37: fuel+500+steel
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

# ezexped plugin saves expedition assignments here
_EZEXPED_STATE = os.path.expanduser(
    "~/Library/Application Support/poi/ezexped/p-state.json"
)

# Expedition duration in minutes
EXPEDITION_DURATIONS: dict[int, int] = {
    2: 40, 3: 20, 4: 40, 5: 90, 6: 40, 7: 60, 8: 180,
    9: 240, 10: 30, 11: 60, 12: 60, 13: 120, 14: 240, 15: 360,
    16: 180, 17: 120, 18: 40, 19: 60, 20: 120,
    21: 60, 22: 40, 23: 20, 24: 360, 25: 480, 26: 360,
    27: 60, 28: 60, 29: 60, 30: 90, 31: 60, 32: 60,
    33: 240, 34: 240, 35: 60, 36: 60, 37: 270, 38: 180,
    40: 90, 41: 120, 43: 120, 44: 360,
}

# Fallback if ezexped state not available
_DEFAULT_PLAN: dict[int, int] = {2: 5, 3: 38, 4: 37}


def load_ezexped_plan() -> dict[int, int]:
    """
    Read expedition assignments from ezexped plugin (the authoritative source).
    selectedExpeds keys are fleet IDs, values are expedition IDs.
    Falls back to hardcoded defaults if ezexped state unavailable.
    """
    try:
        with open(_EZEXPED_STATE, encoding="utf-8") as f:
            data = json.load(f)
        selected = data.get("selectedExpeds", {})
        plan = {int(k): int(v) for k, v in selected.items()}
        # Only keep fleets 2-4 (fleet 1 is reserved for sorties)
        plan = {k: v for k, v in plan.items() if k in (2, 3, 4)}
        if plan:
            log.debug("Loaded ezexped plan: %s", plan)
            return plan
    except Exception as e:
        log.debug("Could not read ezexped state (%s), using defaults", e)
    return _DEFAULT_PLAN.copy()


@dataclass
class ExpeditionAction:
    fleet_id: int
    action: str      # "collect", "resend", "wait"
    expedition_id: int   # target expedition ID
    return_dt: Optional[datetime] = None
    wait_seconds: float = 0.0
    note: str = ""


class ExpeditionManager:
    """
    Reads game state and determines what to do with expeditions.
    Does NOT perform any UI interaction — returns action objects.
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        # Load from ezexped first, allow config override
        self.plan: dict[int, int] = {
            **load_ezexped_plan(),
            **self.config.get("expedition_plan", {}),
        }

    def assess(self, state) -> list[ExpeditionAction]:
        """
        Examine fleets 2/3/4 and return list of actions to take.
        state must have .fleets dict[int, Fleet].
        """
        actions: list[ExpeditionAction] = []
        now_ms = datetime.now().timestamp() * 1000

        for fleet_id in [2, 3, 4]:
            fleet = state.fleets.get(fleet_id)
            if fleet is None:
                continue

            if not fleet.in_expedition:
                # Fleet is at port — should be on expedition
                target_exp = self.plan.get(fleet_id, 2)
                actions.append(ExpeditionAction(
                    fleet_id=fleet_id,
                    action="resend",
                    expedition_id=target_exp,
                    note=f"fleet {fleet_id} idle → send on exp {target_exp}",
                ))
            else:
                return_ms = fleet.expedition_return_ms
                if return_ms <= now_ms:
                    # Expedition has returned — collect it
                    target_exp = fleet.expedition_id  # resend same expedition
                    actions.append(ExpeditionAction(
                        fleet_id=fleet_id,
                        action="collect",
                        expedition_id=target_exp,
                        note=f"fleet {fleet_id} exp {fleet.expedition_id} returned",
                    ))
                else:
                    # Still running — how long until return?
                    remaining_ms = return_ms - now_ms
                    remaining_s = remaining_ms / 1000
                    return_dt = datetime.fromtimestamp(return_ms / 1000)
                    actions.append(ExpeditionAction(
                        fleet_id=fleet_id,
                        action="wait",
                        expedition_id=fleet.expedition_id,
                        return_dt=return_dt,
                        wait_seconds=remaining_s,
                        note=f"fleet {fleet_id} exp {fleet.expedition_id} returns in {_fmt_duration(remaining_s)}",
                    ))

        return actions

    def next_wakeup_seconds(self, state) -> float:
        """How many seconds until the next expedition returns (minimum across fleets)."""
        actions = self.assess(state)
        waits = [a.wait_seconds for a in actions if a.action == "wait" and a.wait_seconds > 0]
        return min(waits) if waits else 0.0


def _fmt_duration(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    elif m > 0:
        return f"{m}m{s:02d}s"
    else:
        return f"{s}s"

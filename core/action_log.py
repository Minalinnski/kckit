"""Structured action logger — writes JSON lines + optional screenshot paths."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

TEMP_DIR = Path(__file__).parent.parent / "temp"


@dataclass
class ActionEntry:
    ts: float              # Unix timestamp
    action: str            # "click", "move", "wait", "detect_screen", "state_read"
    screen: str            # detected screen name
    element: str           # UI element name (empty if freeform click)
    x: int                 # screen pixel x (0 if non-click)
    y: int                 # screen pixel y (0 if non-click)
    cx: float              # canvas fraction x
    cy: float              # canvas fraction y
    dry_run: bool
    note: str = ""
    screenshot_path: str = ""

    @property
    def dt_str(self) -> str:
        return datetime.fromtimestamp(self.ts).strftime("%H:%M:%S.%f")[:-3]


class ActionLogger:
    """
    Logs all automation actions to a JSON-lines file in temp/.
    Each line is a JSON object representing one ActionEntry.
    """

    def __init__(self, session_name: str = ""):
        TEMP_DIR.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{session_name}_" if session_name else ""
        self._log_path = TEMP_DIR / f"actions_{name}{ts}.jsonl"
        self._entries: list[ActionEntry] = []
        log.info("ActionLogger → %s", self._log_path)

    @property
    def log_path(self) -> Path:
        return self._log_path

    def record(
        self,
        action: str,
        screen: str = "",
        element: str = "",
        x: int = 0,
        y: int = 0,
        cx: float = 0.0,
        cy: float = 0.0,
        dry_run: bool = True,
        note: str = "",
        screenshot_path: str = "",
    ) -> ActionEntry:
        entry = ActionEntry(
            ts=time.time(),
            action=action,
            screen=screen,
            element=element,
            x=x, y=y,
            cx=cx, cy=cy,
            dry_run=dry_run,
            note=note,
            screenshot_path=screenshot_path,
        )
        self._entries.append(entry)
        with open(self._log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry)) + "\n")
        return entry

    def summary(self) -> str:
        total = len(self._entries)
        clicks = sum(1 for e in self._entries if e.action == "click")
        dry = sum(1 for e in self._entries if e.dry_run)
        return f"{total} actions ({clicks} clicks, {dry} dry-run) → {self._log_path.name}"

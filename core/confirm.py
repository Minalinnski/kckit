"""
Human confirmation UI — CLI table display of proposed fleet + equipment plan.
This is a hard checkpoint: execution cannot proceed without explicit approval.
"""
from __future__ import annotations

import sys
from typing import Optional

from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

from .composer import ProposedFleet
from .models import Ship
from .optimizer import EquipPlan, ShipEquipPlan, fleet_air_power, los_33
from .schema import Requirements

console = Console()


def _hp_bar(ship: Ship) -> str:
    ratio = ship.hp_ratio
    if ratio > 0.75:
        color = "green"
    elif ratio > 0.50:
        color = "yellow"
    elif ratio > 0.25:
        color = "red"
    else:
        color = "bold red"
    return f"[{color}]{ship.now_hp}/{ship.max_hp}[/{color}]"


def _morale_icon(ship: Ship) -> str:
    if ship.morale >= 50:
        return "[bold yellow]✦[/bold yellow]"  # sparkled
    elif ship.morale >= 40:
        return "[green]○[/green]"
    elif ship.morale >= 30:
        return "[yellow]△[/yellow]"
    else:
        return "[red]✗[/red]"


def show_fleet_plan(
    proposed: ProposedFleet,
    equip_plan: EquipPlan,
    requirements: Requirements,
    equip_lookup: dict[int, str],
    hq_level: int = 120,
) -> None:
    """Print the full fleet + equipment plan to the terminal."""
    console.rule(f"[bold cyan]編成方案: {proposed.preset_name}[/bold cyan]")

    table = Table(box=box.ROUNDED, show_header=True, header_style="bold magenta")
    table.add_column("#", width=3, justify="right")
    table.add_column("艦名", min_width=12)
    table.add_column("HP", min_width=8)
    table.add_column("士気", width=4, justify="center")
    table.add_column("装備 1", min_width=20)
    table.add_column("装備 2", min_width=20)
    table.add_column("装備 3", min_width=20)
    table.add_column("装備 4", min_width=20)
    table.add_column("装備 EX", min_width=16)

    ship_plan_map = {p.ship_id: p for p in equip_plan.ships}

    for i, ship in enumerate(proposed.ships, start=1):
        plan = ship_plan_map.get(ship.instance_id)
        equip_names = []
        if plan:
            for eid in plan.slots:
                equip_names.append(equip_lookup.get(eid, "—") if eid else "—")
            ex_name = equip_lookup.get(plan.slot_ex, "—") if plan.slot_ex else "—"
        else:
            equip_names = ["—"] * 4
            ex_name = "—"

        # Pad to 4
        while len(equip_names) < 4:
            equip_names.append("—")

        table.add_row(
            str(i),
            f"[bold]{ship.name}[/bold]",
            _hp_bar(ship),
            _morale_icon(ship),
            *equip_names[:4],
            ex_name,
        )

    console.print(table)

    # Requirements check
    air_target = requirements.air_power_min or 0
    los_target = requirements.los_min or 0
    air_ok = equip_plan.air_power >= air_target
    los_ok = equip_plan.los_value >= los_target

    air_color = "green" if air_ok else "red"
    los_color = "green" if los_ok else "red"
    air_mark = "✓" if air_ok else "✗"
    los_mark = "✓" if los_ok else "✗"

    console.print(
        f"  制空値: [{air_color}]{equip_plan.air_power:.0f}[/{air_color}] "
        f"[{air_color}][目標 ≥{air_target}] {air_mark}[/{air_color}]   "
        f"  33式索敵: [{los_color}]{equip_plan.los_value:.1f}[/{los_color}] "
        f"[{los_color}][目標 ≥{los_target}] {los_mark}[/{los_color}]"
    )

    # Warnings
    all_warnings = proposed.warnings + equip_plan.notes
    if all_warnings:
        console.print()
        for w in all_warnings:
            color = "bold red" if "ERROR" in w else "yellow"
            console.print(f"  [{color}]⚠  {w}[/{color}]")


def confirm(
    proposed: ProposedFleet,
    equip_plan: EquipPlan,
    requirements: Requirements,
    equip_lookup: dict[int, str],
    hq_level: int = 120,
) -> bool:
    """
    Show plan and ask for human confirmation.
    Returns True if approved, False if rejected.
    This function MUST be called before any fleet modification or sortie.
    """
    show_fleet_plan(proposed, equip_plan, requirements, equip_lookup, hq_level)

    # Block on errors
    has_errors = any("ERROR" in w for w in proposed.warnings)
    if has_errors:
        console.print("\n[bold red]エラーがあるため確認不可。編成を見直してください。[/bold red]")
        return False

    console.print()
    console.print("  [A] poi に適用して出撃  [S] スキップ（手動で設定済み）  [Q] キャンセル")
    console.print()

    while True:
        try:
            choice = console.input("  選択 > ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[yellow]キャンセルしました。[/yellow]")
            return False

        if choice in ("A", "S"):
            console.print("[bold green]✓ 確認完了[/bold green]")
            return True
        elif choice == "Q":
            console.print("[yellow]キャンセルしました。[/yellow]")
            return False
        else:
            console.print("  [dim]A / S / Q を入力してください[/dim]")

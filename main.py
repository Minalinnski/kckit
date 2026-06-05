#!/usr/bin/env python3
"""
kckit — KanColle automation entry point.

Modes:
  plan MAP_OR_QUEST [--preset NAME]   Offline fleet plan from snapshot
  compose MAP [--preset NAME]         Compose fleet + equipment, confirm, apply to poi
  sortie                              Start sortie loop (requires prior compose)
  schedule                            Full automated loop (expeditions + sortie)
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _run_plan_for_preset(gs, preset, map_id: str, open_browser: bool = False) -> None:
    from core.composer import FleetComposer
    from core.optimizer import EquipPlan, ShipEquipPlan, fleet_air_power, los_33
    from core.confirm import show_fleet_plan
    from core.noro6 import build_deck, deck_to_url

    composer = FleetComposer(gs)
    proposed = composer.compose(preset)

    # plan mode: show current equipment as-is, let noro6 do the math
    ship_plans = [
        ShipEquipPlan(
            ship_id=s.instance_id,
            ship_name=s.name,
            slots=[e.instance_id if e else None for e in s.equipped],
            slot_ex=s.equipped_ex.instance_id if s.equipped_ex else None,
        )
        for s in proposed.ships
    ]
    air = fleet_air_power(proposed.ships)
    los = los_33(proposed.ships, gs.hq_level)
    equip_plan = EquipPlan(
        ships=ship_plans,
        air_power=air,
        los_value=los,
        requirements_met=(
            air >= (preset.requirements.air_power_min or 0)
            and los >= (preset.requirements.los_min or 0)
        ),
        notes=[],
    )
    equip_lookup = {e.instance_id: e.name for e in gs.equips.values()}

    show_fleet_plan(
        proposed=proposed,
        equip_plan=equip_plan,
        requirements=preset.requirements,
        equip_lookup=equip_lookup,
        hq_level=gs.hq_level,
    )

    deck = build_deck(proposed, None, gs.equips, hq_level=gs.hq_level)
    url = deck_to_url(deck)
    print(f"  noro6: {url}\n")
    if open_browser:
        import webbrowser
        webbrowser.open(url)


def cmd_plan(args: argparse.Namespace) -> None:
    from core.models import GameState
    from core.schema import load_strategy, map_yaml_path, load_quest

    try:
        gs = GameState.from_snapshot()
    except FileNotFoundError:
        print("No snapshot found. Launch poi with kckit-bridge plugin and enter port.")
        sys.exit(1)

    target = args.target

    if re.match(r'^\d+-\d+$', target):
        # Map mode
        path = map_yaml_path(target)
        if not path.exists():
            print(f"No strategy file: {path}")
            sys.exit(1)
        strategy = load_strategy(str(path))
        presets = ([strategy.get_preset(args.preset)] if args.preset
                   else strategy.presets)
        for preset in presets:
            if preset:
                _run_plan_for_preset(gs, preset, target, open_browser=args.open)
    else:
        # Quest mode
        try:
            quest = load_quest(target)
        except FileNotFoundError:
            print(f"Quest {target} not found under strategies/quests/")
            sys.exit(1)

        print(f"\n{quest.quest_id} {quest.quest_name}  [{quest.category}]")
        print(f"  {quest.requirement}\n")

        if not quest.maps:
            print("  (no specific map required)")
            return

        for map_rec in quest.maps:
            path = map_yaml_path(map_rec.map_id)
            if not path.exists():
                print(f"  [{map_rec.map_id}] no strategy file, skipping")
                continue
            strategy = load_strategy(str(path))
            # Prefer presets tagged for this quest, else fleet_hint name, else first
            tagged = strategy.get_presets_for_quest(target)
            if tagged:
                presets_to_show = tagged
            elif map_rec.fleet_hint:
                p = strategy.get_preset(map_rec.fleet_hint)
                presets_to_show = [p] if p else strategy.presets[:1]
            else:
                presets_to_show = strategy.presets[:1]
            for preset in presets_to_show:
                _run_plan_for_preset(gs, preset, map_rec.map_id, open_browser=args.open)


def cmd_status(args: argparse.Namespace) -> None:
    """Print current game state from poi snapshot or live bridge."""
    from core.models import GameState

    if args.live:
        from core.poi_client import PoiClient
        poi = PoiClient()
        print("Connecting to poi bridge…")
        poi.start(timeout=15)
        state = poi.state
        poi.stop()
    else:
        try:
            state = GameState.from_snapshot()
        except FileNotFoundError:
            print("No snapshot found. Launch poi with kckit-bridge plugin and enter port.")
            print("Or use --live to connect directly.")
            sys.exit(1)

    try:
        from tools.viewer import _print_status
        _print_status(state)
    except ImportError:
        _minimal_status(state)


def _minimal_status(state) -> None:
    from datetime import datetime
    now = datetime.now()
    print(f"\n母港状态  HQ Lv{state.hq_level}")
    r = state.resources
    print(f"  资源: 油{r.get('fuel',0):,} 弹{r.get('ammo',0):,} 钢{r.get('steel',0):,} 铝{r.get('bauxite',0):,}")
    print(f"  高速修复材: {r.get('bucket',0)}")
    for fid, fleet in sorted(state.fleets.items()):
        if fleet.in_expedition:
            ret_ms = getattr(fleet, "expedition_return_ms", 0)
            if ret_ms:
                remaining = (datetime.fromtimestamp(ret_ms/1000) - now).total_seconds()
                status = f"远征{fleet.expedition_id} 还剩{int(remaining//60)}分" if remaining > 0 else f"远征{fleet.expedition_id} ★已返回"
            else:
                status = f"远征{fleet.expedition_id}"
        else:
            names = [s.name for s in fleet.ships if s][:3]
            status = f"母港 [{', '.join(names)}]"
        print(f"  第{fid}舰队: {status}")
    dock_list = getattr(state, "repair_docks", None) or getattr(state, "repair_dock", [])
    for dock in dock_list:
        if hasattr(dock, "is_empty"):
            empty = dock.is_empty
            dock_id = dock.dock_id
        else:
            empty = dock.get("api_state", 0) == 0
            dock_id = dock.get("api_id", "?")
        print(f"  入渠{dock_id}: {'空' if empty else '修复中'}")


def cmd_compose(args: argparse.Namespace) -> None:
    from core.poi_client import PoiClient
    from core.schema import load_strategy, map_yaml_path
    from core.composer import FleetComposer
    from core.optimizer import EquipOptimizer
    from core.confirm import confirm
    from core.knowledge import KnowledgeBase

    poi = PoiClient()
    print("Connecting to poi bridge…")
    poi.start(timeout=15)
    state = poi.state

    strategy_path = map_yaml_path(args.map)
    if not strategy_path.exists():
        print(f"ERROR: No strategy file for {args.map}. Run import_nga.py first.")
        sys.exit(1)

    strategy = load_strategy(str(strategy_path))
    preset_name = args.preset or strategy.presets[0].name
    preset = strategy.get_preset(preset_name)
    if preset is None:
        print(f"ERROR: Preset '{preset_name}' not found in {strategy_path}")
        print("Available:", [p.name for p in strategy.presets])
        sys.exit(1)

    print(f"\nComposing fleet for: {args.map} / {preset.name}")

    # Compose
    composer = FleetComposer(state)
    proposed = composer.compose(preset)

    # Optimize equipment
    available_equips = list(state.equips.values())
    _equip_db = Path(__file__).parent / "data" / "equip_db.json"
    _subs_db = Path(__file__).parent / "data" / "equip_subs.json"
    kb = KnowledgeBase(str(_equip_db), str(_subs_db)) if _equip_db.exists() and _subs_db.exists() else None
    optimizer = EquipOptimizer(
        ships=proposed.ships,
        available_equips=available_equips,
        requirements=preset.requirements,
        hq_level=state.hq_level,
        knowledge=kb,
        preset=preset,
    )
    equip_plan = optimizer.optimize()

    # Build equip name lookup
    equip_lookup = {e.instance_id: e.name for e in state.equips.values()}

    # Human confirmation (hard gate)
    approved = confirm(
        proposed=proposed,
        equip_plan=equip_plan,
        requirements=preset.requirements,
        equip_lookup=equip_lookup,
    )

    if not approved:
        print("Aborted.")
        poi.stop()
        return

    print("\nFleet confirmed. Apply equipment changes in poi manually or use executor.")
    poi.stop()


def cmd_sortie(args: argparse.Namespace) -> None:
    from core.poi_client import PoiClient
    from core.executor import SortieExecutor, CanvasConfig
    from core.safety import RestWatchdog

    poi = PoiClient()
    print("Connecting to poi bridge…")
    poi.start(timeout=15)

    canvas = CanvasConfig.load()
    executor = SortieExecutor(poi, canvas, fleet_id=1)
    watchdog = RestWatchdog()

    map_id = args.map or "5-4"
    print(f"Starting sortie loop on {map_id}. Ctrl+C to stop.")

    count = 0
    try:
        while True:
            watchdog.check_and_rest_if_needed()
            result = executor.run_sortie(map_id)
            count += 1
            print(f"  Sortie #{count}: {result.name}")
            executor.supply_fleet()
    except KeyboardInterrupt:
        print(f"\nStopped after {count} sorties.")
    finally:
        poi.stop()


def cmd_schedule(args: argparse.Namespace) -> None:
    from core.poi_client import PoiClient
    from core.scheduler import Scheduler
    from core.executor import SortieExecutor, CanvasConfig
    import yaml as _yaml

    config = {}
    config_path = Path("config.yaml")
    if config_path.exists():
        with open(config_path) as f:
            config = _yaml.safe_load(f) or {}

    poi = PoiClient()
    print("Connecting to poi bridge…")
    poi.start(timeout=15)

    canvas = CanvasConfig.load()
    dry_run = args.dry_run if hasattr(args, "dry_run") else not canvas.is_calibrated
    if dry_run and not canvas.is_calibrated:
        print("Canvas not calibrated — running in dry-run mode. Run tools/calibrate.py first.")
    executor = SortieExecutor(poi, canvas, dry_run=dry_run)

    scheduler = Scheduler(poi, config, executor=executor)
    print("Scheduler running. Ctrl+C to stop.")
    scheduler.run()
    poi.stop()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(prog="kckit")
    sub = parser.add_subparsers(dest="cmd")

    p_status = sub.add_parser("status", help="Show current game state from snapshot or live poi")
    p_status.add_argument("--live", action="store_true", help="Connect to poi live instead of reading snapshot")

    p_plan = sub.add_parser("plan", help="Offline fleet plan from snapshot (no poi needed)")
    p_plan.add_argument("target", help="Map ID (e.g. 5-4) or quest ID (e.g. Bw6)")
    p_plan.add_argument("--preset", help="Preset name (default: all matching presets)")
    p_plan.add_argument("--open", action="store_true", help="Open noro6 URL in browser")

    p_compose = sub.add_parser("compose", help="Compose fleet + equipment for a map")
    p_compose.add_argument("map", help="Map ID e.g. 5-4")
    p_compose.add_argument("--preset", help="Preset name (default: first preset)")

    p_sortie = sub.add_parser("sortie", help="Run sortie loop")
    p_sortie.add_argument("--map", default="5-4")

    p_sched = sub.add_parser("schedule", help="Full automated loop")
    p_sched.add_argument("--dry-run", action="store_true", help="Log actions without clicking")

    args = parser.parse_args()

    if args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "plan":
        cmd_plan(args)
    elif args.cmd == "compose":
        cmd_compose(args)
    elif args.cmd == "sortie":
        cmd_sortie(args)
    elif args.cmd == "schedule":
        cmd_schedule(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

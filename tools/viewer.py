#!/usr/bin/env python3
"""
tools/inspect.py — Visual state inspection tool.

Usage:
  python tools/inspect.py [--snapshot PATH] [--no-screenshot] [--canvas X Y W H]

Output:
  - Text summary to stdout
  - Annotated PNG saved to temp/inspect_YYYYMMDD_HHMMSS.png (if screenshot taken)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Allow running from project root or tools/ directory
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from core.models import GameState
from core.expedition_manager import ExpeditionManager, _fmt_duration
from core.repair_manager import RepairManager

TEMP_DIR = ROOT / "temp"
SNAPSHOT_PATH = Path.home() / ".kckit" / "box_snapshot.json"


def _load_state(snapshot_path: Path) -> GameState:
    if not snapshot_path.exists():
        print(f"ERROR: snapshot not found at {snapshot_path}")
        print("  Make sure poi is running with kckit-bridge plugin enabled.")
        sys.exit(1)
    return GameState.from_snapshot(str(snapshot_path))


def _print_status(state: GameState) -> None:
    """Print human-readable game state summary."""
    now = datetime.now()
    ts = datetime.fromtimestamp(state.timestamp / 1000) if hasattr(state, 'timestamp') else now

    print(f"\n{'═'*60}")
    print(f"  母港状态  {ts.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'═'*60}")

    # Resources
    r = state.resources
    print(f"\n  资源:")
    print(f"    油 {r.get('fuel',0):>7,}  弹 {r.get('ammo',0):>7,}  钢 {r.get('steel',0):>7,}  铝 {r.get('bauxite',0):>7,}")
    print(f"    高速建造材 {r.get('fast_build',0):>5,}  高速修复材 {r.get('bucket',0):>5,}  开发资材 {r.get('dev_mat',0):>5,}  改修资材 {r.get('improve_mat',0):>5,}")

    # Fleets + Expeditions
    print(f"\n  舰队:")
    for fleet_id in [1, 2, 3, 4]:
        fleet = state.fleets.get(fleet_id)
        if fleet is None:
            continue
        if fleet.in_expedition:
            ret_ms = fleet.expedition_return_ms
            if ret_ms > 0:
                ret_dt = datetime.fromtimestamp(ret_ms / 1000)
                remaining = (ret_dt - now).total_seconds()
                if remaining > 0:
                    print(f"    第{fleet_id}舰队  远征{fleet.expedition_id:2d}  {ret_dt.strftime('%H:%M:%S')} 归还  (还剩 {_fmt_duration(remaining)})")
                else:
                    print(f"    第{fleet_id}舰队  远征{fleet.expedition_id:2d}  ★ 已返回，待收取")
            else:
                print(f"    第{fleet_id}舰队  远征{fleet.expedition_id:2d}  (时间未知)")
        else:
            ship_names = [s.name for s in fleet.ships if s][:3]
            print(f"    第{fleet_id}舰队  母港中  [{', '.join(ship_names)}{'...' if len(fleet.ships)>3 else ''}]")

    # Repair docks
    print(f"\n  入渠:")
    if not state.repair_docks:
        print("    (无数据)")
    for dock in state.repair_docks:
        if dock.is_empty:
            print(f"    第{dock.dock_id}船坞  空")
        else:
            ship = state.ships.get(dock.ship_id)
            name = ship.name if ship else f"ship#{dock.ship_id}"
            if dock.complete_dt:
                remaining = (dock.complete_dt - now).total_seconds()
                if remaining > 0:
                    print(f"    第{dock.dock_id}船坞  {name}  {dock.complete_dt.strftime('%H:%M:%S')} 完成  (还剩 {_fmt_duration(remaining)})")
                else:
                    print(f"    第{dock.dock_id}船坞  {name}  ★ 修复完成")
            else:
                print(f"    第{dock.dock_id}船坞  {name}  修复中")

    # Constructions
    if state.constructions:
        print(f"\n  工廠:")
        for dock in state.constructions:
            if dock.is_empty:
                print(f"    第{dock.dock_id}工廠  空")
            elif dock.is_complete:
                print(f"    第{dock.dock_id}工廠  ★ 建造完成 (ship#{dock.ship_id})")
            else:
                if dock.complete_dt:
                    remaining = (dock.complete_dt - now).total_seconds()
                    print(f"    第{dock.dock_id}工廠  建造中  {dock.complete_dt.strftime('%H:%M:%S')} 完成  (还剩 {_fmt_duration(remaining)})")
                else:
                    print(f"    第{dock.dock_id}工廠  建造中")

    # Quests
    if state.quests:
        print(f"\n  任务 ({len(state.quests)}件):")
        for q in sorted(state.quests, key=lambda x: x.quest_id)[:10]:
            status = "★完成" if q.is_complete else f" {q.progress_str}"
            type_str = {1: '日', 2: '周', 3: '月', 4: '单'}.get(q.quest_type, '?')
            print(f"    [{type_str}] {q.quest_id:4d}  {status}  {q.title[:20]}")

    # First fleet ship details
    fleet1 = state.fleets.get(1)
    if fleet1 and fleet1.ships:
        print(f"\n  第一舰队详情:")
        for ship in fleet1.ships:
            if ship is None:
                continue
            hp_bar = _hp_bar(ship.now_hp, ship.max_hp)
            cond = f"Cond{ship.morale:3d}"
            taiha = " ★大破" if ship.is_taiha else ("  中破" if ship.is_chuuha else "")
            equip_str = ", ".join(e.name for e in ship.equipped if e)[:40]
            print(f"    {ship.name:12s}  HP{hp_bar} {cond}{taiha}")
            if equip_str:
                print(f"              装备: {equip_str}")

    print(f"\n{'═'*60}\n")


def _hp_bar(now: int, max_hp: int, width: int = 8) -> str:
    if max_hp <= 0:
        return f"{'?':>{width}}"
    ratio = now / max_hp
    filled = int(ratio * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{bar} {now:3d}/{max_hp:3d}"


def _take_screenshot_and_annotate(state: GameState, canvas: tuple, out_path: Path) -> bool:
    """
    Take screenshot, draw overlay, save to out_path.
    canvas = (x, y, w, h) in screen pixels.
    Returns True if successful.
    """
    try:
        import mss
        import mss.tools
        from PIL import Image, ImageDraw, ImageFont

        cx, cy, cw, ch = canvas

        with mss.mss() as sct:
            region = {"left": cx, "top": cy, "width": cw, "height": ch}
            img_raw = sct.grab(region)
            img = Image.frombytes("RGB", img_raw.size, img_raw.bgra, "raw", "BGRX")

        draw = ImageDraw.Draw(img, "RGBA")

        # Try to load a font; fall back to default
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 14)
            font_sm = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 11)
        except Exception:
            font = ImageFont.load_default()
            font_sm = font

        # Load screen layout and draw element boxes (optional — skip if not available)
        try:
            from core.screen_detector import ScreenLayout, detect_from_state
            layout = ScreenLayout()
            screen = detect_from_state(state)
            elements = layout.get_elements(screen.name)
            for name, elem in elements.items():
                ex = int(elem["x"] * cw)
                ey = int(elem["y"] * ch)
                ew = int(elem.get("w", 0.05) * cw)
                eh = int(elem.get("h", 0.05) * ch)
                draw.rectangle([ex, ey, ex+ew, ey+eh], outline=(0, 200, 255, 200), width=1)
                draw.text((ex+2, ey+2), elem.get("label", name), fill=(0, 200, 255, 220), font=font_sm)
            screen_name = screen.display_name
            screen_conf = screen.confidence
        except Exception:
            screen_name = "unknown"
            screen_conf = 0.0

        # Overlay text: screen name + timestamp
        overlay_text = f"Screen: {screen_name}  conf={screen_conf:.1f}  {datetime.now().strftime('%H:%M:%S')}"
        draw.rectangle([0, 0, img.width, 20], fill=(0, 0, 0, 160))
        draw.text((4, 3), overlay_text, fill=(255, 255, 100, 255), font=font)

        # Draw expedition fleet status on right side
        y_off = 25
        now = datetime.now()
        for fleet_id in [2, 3, 4]:
            fleet = state.fleets.get(fleet_id)
            if fleet and fleet.in_expedition:
                ret_ms = fleet.expedition_return_ms
                if ret_ms > 0:
                    remaining = (datetime.fromtimestamp(ret_ms/1000) - now).total_seconds()
                    color = (255, 100, 100, 220) if remaining <= 0 else (100, 255, 100, 220)
                    text = f"F{fleet_id} Exp{fleet.expedition_id}: {'★DONE' if remaining<=0 else _fmt_duration(remaining)}"
                    draw.rectangle([img.width-150, y_off, img.width, y_off+16], fill=(0, 0, 0, 140))
                    draw.text((img.width-148, y_off+1), text, fill=color, font=font_sm)
                    y_off += 18

        TEMP_DIR.mkdir(exist_ok=True)
        img.save(str(out_path))
        return True

    except ImportError as e:
        print(f"  Screenshot skipped (missing dependency: {e})")
        return False
    except Exception as e:
        print(f"  Screenshot failed: {e}")
        return False


def _detect_canvas() -> Optional[tuple]:
    """Try to load canvas position from config/poi_window.yaml."""
    config_path = ROOT / "config" / "poi_window.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                data = yaml.safe_load(f)
            c = data.get("canvas", {})
            if all(k in c for k in ("x", "y", "w", "h")):
                return (c["x"], c["y"], c["w"], c["h"])
        except Exception:
            pass
    return None


def main():
    parser = argparse.ArgumentParser(description="kckit visual state inspector")
    parser.add_argument("--snapshot", default=str(SNAPSHOT_PATH), help="Path to box_snapshot.json")
    parser.add_argument("--no-screenshot", action="store_true", help="Skip screenshot capture")
    parser.add_argument("--canvas", nargs=4, type=int, metavar=("X", "Y", "W", "H"),
                        help="Game canvas position in screen pixels (skip auto-detect)")
    args = parser.parse_args()

    state = _load_state(Path(args.snapshot))
    _print_status(state)

    # Expedition recommendations
    mgr = ExpeditionManager()
    actions = mgr.assess(state)
    print("  远征建议:")
    for a in actions:
        print(f"    {a.note}")

    # Repair recommendations
    repair_mgr = RepairManager()
    repair_actions = repair_mgr.assess(state)
    needs_action = [a for a in repair_actions if a.action in ("collect", "start_repair")]
    if needs_action:
        print("\n  入渠建议:")
        for a in needs_action:
            print(f"    {a.note}")

    if args.no_screenshot:
        return

    # Screenshot
    canvas = tuple(args.canvas) if args.canvas else _detect_canvas()
    if canvas:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = TEMP_DIR / f"inspect_{ts}.png"
        print(f"\n  截图保存中... ", end="", flush=True)
        if _take_screenshot_and_annotate(state, canvas, out_path):
            print(f"-> {out_path}")
        else:
            print("失败")
    else:
        print("\n  提示: 使用 --canvas X Y W H 指定游戏画布位置以生成截图")
        print("  或运行 python tools/calibrate.py 自动标定坐标")


if __name__ == "__main__":
    main()

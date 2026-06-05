#!/usr/bin/env python3
"""
tools/calibrate.py — Auto-detect KanColle canvas position via poi bridge.

poi (Electron app) knows exactly where the game webview is on screen.
We ask it via WebSocket and save the result to config/poi_window.yaml.

Usage:
  python tools/calibrate.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

CONFIG_DIR = ROOT / "config"
POI_WINDOW_PATH = CONFIG_DIR / "poi_window.yaml"
POI_BRIDGE_URL = "ws://127.0.0.1:23456"


async def fetch_canvas_info() -> dict:
    import websockets
    async with websockets.connect(POI_BRIDGE_URL, max_size=2 * 1024 * 1024) as ws:
        # Wait for initial state push (confirms connection is alive)
        raw = await asyncio.wait_for(ws.recv(), timeout=10)
        msg = json.loads(raw)
        if msg.get("type") != "state":
            raise RuntimeError(f"Expected state msg, got: {msg.get('type')}")

        # Request canvas info
        await ws.send(json.dumps({"cmd": "get_canvas_info"}))
        raw = await asyncio.wait_for(ws.recv(), timeout=5)
        msg = json.loads(raw)
        if msg.get("type") != "canvas_info":
            raise RuntimeError(f"Expected canvas_info, got: {msg}")
        return msg["payload"]


def main():
    print("=" * 60)
    print("  kckit 坐标自动标定")
    print("=" * 60)
    print("\n  正在连接 poi bridge…")

    try:
        info = asyncio.run(fetch_canvas_info())
    except (ConnectionRefusedError, OSError):
        print("\n  ERROR: 无法连接 poi bridge (ws://127.0.0.1:23456)")
        print("  请确认 poi 已启动且 kckit-bridge 插件已启用。")
        sys.exit(1)
    except asyncio.TimeoutError:
        print("\n  ERROR: 连接超时。")
        sys.exit(1)
    except Exception as e:
        print(f"\n  ERROR: {e}")
        sys.exit(1)

    if not info.get("found"):
        print(f"\n  ERROR: poi 内未找到游戏画布元素。")
        print(f"  原因: {info.get('reason', 'unknown')}")
        print("  请确认游戏已加载（进入母港界面后重试）。")
        sys.exit(1)

    x, y, w, h = info["x"], info["y"], info["w"], info["h"]
    dpr = info.get("dpr", 1)

    print(f"\n  检测到游戏画布:")
    print(f"    位置: ({x}, {y})")
    print(f"    尺寸: {w} × {h}  (标准: 800 × 480)")
    print(f"    设备像素比: {dpr}")

    scale_x = w / 800
    scale_y = h / 480
    print(f"    缩放: {scale_x:.3f}×")
    if abs(scale_x - scale_y) > 0.05:
        print("  警告: 横纵缩放比例差异较大，请确认游戏画面未变形。")

    config_str = f"""# KanColle game canvas position on screen.
# Auto-detected by tools/calibrate.py on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Re-run calibrate.py if poi window is moved or resized.

canvas:
  x: {x}    # screen pixel x of top-left corner
  y: {y}    # screen pixel y of top-left corner
  w: {w}   # canvas width in pixels
  h: {h}   # canvas height in pixels
  # device_pixel_ratio: {dpr}
  # scale_x: {scale_x:.4f}
  # scale_y: {scale_y:.4f}
"""

    CONFIG_DIR.mkdir(exist_ok=True)
    with open(POI_WINDOW_PATH, "w") as f:
        f.write(config_str)

    print(f"\n  已保存到 {POI_WINDOW_PATH}")
    print("\n  现在可以运行:")
    print("    python main.py schedule --dry-run   # 验证调度逻辑")
    print("    python main.py schedule              # 开始自动运行")


if __name__ == "__main__":
    main()

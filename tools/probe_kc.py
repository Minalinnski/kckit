"""
KC webview discovery tool.

Connects to the kckit-bridge plugin and probes the KC game's internal state:
  - Dumps all KC-namespaced globals from the webview
  - Shows current DOM screen indicators
  - Reports local asset cache path
  - Monitors screen_change events in real time

Usage:
  # One-shot dump (run while game is open on a specific screen):
  python tools/probe_kc.py globals

  # Monitor screen changes for 60s (navigate around in the game while running):
  python tools/probe_kc.py monitor [--duration 60]

  # Show local KC asset cache path:
  python tools/probe_kc.py cache-path

  # Inject screen spy and print its output:
  python tools/probe_kc.py inject-spy
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

POI_BRIDGE_URL = "ws://127.0.0.1:23456"


async def send_and_wait(url: str, cmd: dict, response_type: str, timeout: float = 10.0) -> dict:
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as ws:
        # Consume initial state push
        await asyncio.wait_for(ws.recv(), timeout=5.0)
        await ws.send(json.dumps(cmd))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                msg = json.loads(raw)
                if msg.get("type") == response_type:
                    return msg.get("payload") or {}
            except asyncio.TimeoutError:
                continue
    return {}


async def cmd_globals(args) -> None:
    print("Probing KC webview globals…")
    result = await send_and_wait(
        POI_BRIDGE_URL,
        {"cmd": "probe_kc_globals"},
        "kc_globals",
    )
    if not result:
        print("No response (is poi running with kckit-bridge enabled?)")
        return

    print(f"\n── URL: {result.get('href', '?')}")
    print(f"── Hash: {result.get('hash', '?')}")
    print(f"── Spy screen: {result.get('spyScreen', 'not injected')}")

    kc_globals = result.get("kcGlobals", {})
    if kc_globals:
        print(f"\n── KC globals ({len(kc_globals)} found):")
        for name, info in sorted(kc_globals.items()):
            if info.get("type") == "object":
                keys = info.get("keys", [])
                print(f"  {name}  (object, keys: {keys[:10]})")
            else:
                print(f"  {name}  ({info.get('type')}) = {str(info.get('value', ''))[:80]}")
    else:
        print("\n── No KC globals found (game may not be loaded)")

    visible = result.get("visibleDivs", [])
    if visible:
        print(f"\n── Visible screen divs ({len(visible)}):")
        for d in visible:
            print(f"  #{d['id']}  cls: {d['cls'][:60]}")

    all_ids = result.get("allIds", [])
    kc_ids = [i for i in all_ids if any(k in i.lower() for k in ("port", "supply", "ndock", "kdock", "kaisou", "mission", "quest", "sortie", "practice", "area", "game"))]
    if kc_ids:
        print(f"\n── KC-related element IDs ({len(kc_ids)}):")
        for id_ in kc_ids[:40]:
            print(f"  #{id_}")

    # Save full dump for analysis
    out = Path("temp/kc_globals_dump.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nFull dump saved to {out}")


async def cmd_inject_spy(args) -> None:
    print("Injecting screen spy into KC webview…")
    result = await send_and_wait(
        POI_BRIDGE_URL,
        {"cmd": "inject_screen_spy"},
        "spy_result",
    )
    if result.get("ok"):
        screen = result.get("initialScreen") or result.get("already") and "(already active)"
        print(f"Spy active! Initial screen: {screen}")
    else:
        print(f"Injection failed: {result}")


async def cmd_monitor(args) -> None:
    duration = args.duration
    print(f"Monitoring screen changes for {duration}s — navigate around in KC…")

    async with websockets.connect(POI_BRIDGE_URL, max_size=8 * 1024 * 1024) as ws:
        # Consume initial state
        await asyncio.wait_for(ws.recv(), timeout=5.0)

        # Inject spy first
        await ws.send(json.dumps({"cmd": "inject_screen_spy"}))

        start = time.monotonic()
        last_screen = None
        event_count = 0

        while time.monotonic() - start < duration:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                msg = json.loads(raw)
                t = msg.get("type")

                if t == "spy_result":
                    p = msg.get("payload", {})
                    if p.get("ok"):
                        init = p.get("initialScreen", "?")
                        print(f"[{_ts()}] Spy injected, initial screen: {init}")
                    else:
                        print(f"[{_ts()}] Spy injection failed: {p}")

                elif t == "screen_change":
                    screen = msg.get("screen")
                    source = msg.get("source", "?")
                    url = msg.get("url", "")
                    hash_ = msg.get("hash", "")
                    if screen != last_screen:
                        last_screen = screen
                        event_count += 1
                        extra = f"  url={url}" if url else f"  hash={hash_}" if hash_ else ""
                        print(f"[{_ts()}] Screen → {screen or '(unknown)'}  [{source}]{extra}")

                elif t == "event":
                    path = msg.get("event", "")
                    if path:
                        print(f"[{_ts()}] API: {path}")

            except asyncio.TimeoutError:
                # Periodically poll spy to refresh
                try:
                    await ws.send(json.dumps({"cmd": "get_spy_screen"}))
                except Exception:
                    break
            except Exception as e:
                print(f"Error: {e}")
                break

        print(f"\nDone. Captured {event_count} screen transitions.")


async def cmd_reload(args) -> None:
    """Copy updated plugin to poi, then send reload command via WebSocket."""
    import shutil, subprocess

    src = Path(__file__).parent.parent / "poi-plugin" / "index.js"
    dst = Path.home() / "Library/Application Support/poi/plugins/node_modules/poi-plugin-kckit-bridge/index.js"

    if not src.exists():
        print(f"ERROR: source not found: {src}")
        return
    if not dst.parent.exists():
        print(f"ERROR: poi plugin dir not found: {dst.parent}")
        return

    shutil.copy2(src, dst)
    print(f"Copied {src.name} → {dst}")

    # Send reload command to currently-running plugin
    print("Sending reload command to poi bridge…")
    try:
        async with websockets.connect(POI_BRIDGE_URL, max_size=4 * 1024 * 1024) as ws:
            await asyncio.wait_for(ws.recv(), timeout=3.0)  # consume state push
            await ws.send('{"cmd":"reload"}')
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                msg = json.loads(raw)
                if msg.get("type") == "reload_ack":
                    print("Plugin acknowledged reload — reconnecting in 2s…")
                else:
                    print(f"Unexpected response: {msg}")
            except asyncio.TimeoutError:
                print("No ack received (old plugin may not support reload yet)")
    except Exception as e:
        print(f"Could not connect to poi bridge: {e}")
        print("→ Plugin file is copied. Reload poi's kckit-bridge plugin manually once.")
        return

    # Wait for plugin to restart its WS server
    await asyncio.sleep(2.0)
    try:
        async with websockets.connect(POI_BRIDGE_URL, max_size=4 * 1024 * 1024) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
            msg = json.loads(raw)
            if msg.get("type") == "state":
                print("✓ Plugin reloaded and responding")
    except Exception as e:
        print(f"Plugin not responding after reload: {e}")


async def cmd_cache_path(args) -> None:
    result = await send_and_wait(
        POI_BRIDGE_URL,
        {"cmd": "get_resource_path"},
        "resource_path",
    )
    if result.get("error"):
        print(f"Error: {result['error']}")
        return

    print("KC local asset cache paths:")
    for key, path in result.items():
        if key != "error":
            p = Path(path)
            exists = p.exists()
            size = _dir_size(p) if exists else None
            size_str = f"  ({_fmt_size(size)})" if size else "  (empty or not cached yet)"
            print(f"  {key:12} {path}{size_str if exists else '  (not found)'}")


def _ts() -> str:
    return time.strftime("%H:%M:%S")


def _dir_size(p: Path) -> int:
    try:
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except Exception:
        return 0


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def main() -> None:
    parser = argparse.ArgumentParser(description="KC webview discovery tool")
    sub = parser.add_subparsers(dest="subcmd")

    sub.add_parser("globals", help="Dump KC webview globals and DOM structure")
    sub.add_parser("inject-spy", help="Inject persistent screen spy into webview")

    p_monitor = sub.add_parser("monitor", help="Monitor screen changes in real time")
    p_monitor.add_argument("--duration", type=float, default=120.0, metavar="SEC",
                           help="How long to monitor (default: 120s)")

    sub.add_parser("cache-path", help="Show KC local asset cache path")
    sub.add_parser("reload", help="Copy plugin to poi and hot-reload it (no poi UI needed)")

    args = parser.parse_args()

    dispatch = {
        "globals": cmd_globals,
        "inject-spy": cmd_inject_spy,
        "monitor": cmd_monitor,
        "cache-path": cmd_cache_path,
        "reload": cmd_reload,
    }

    fn = dispatch.get(args.subcmd)
    if fn is None:
        parser.print_help()
        sys.exit(1)

    asyncio.run(fn(args))


if __name__ == "__main__":
    main()

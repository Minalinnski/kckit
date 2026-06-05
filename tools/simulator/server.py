#!/usr/bin/env python3
"""
kckit Simulator Server
Serves game state + operation plans to the browser UI.
Run: python -m tools.simulator.server  (from kckit root)
"""
from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
import uvicorn

from tools.simulator.plan import generate_sortie_plan

SNAPSHOT_PATH = Path.home() / ".kckit" / "box_snapshot.json"
STRATEGIES_DIR = ROOT / "strategies" / "maps"
LAYOUT_PATH = ROOT / "config" / "screen_layout.yaml"
POI_WS_URL = "ws://127.0.0.1:23456"
POI_SCREENSHOT_URL = "http://127.0.0.1:23457/screenshot"

# ── Browser client registry ────────────────────────────────────────────────

_browser_clients: set[WebSocket] = set()

# Maps short api path → screen name for UI detection
_EVENT_SCREEN: dict[str, str] = {
    # ── Port / return ──────────────────────────────────────────────────────
    "api_port/port":                          "port",
    # ── Navigation events (fire on screen entry, not on action) ───────────
    "api_get_member/deck":                    "hensei",
    "api_get_member/ship2":                   "supply",
    "api_get_member/ndock":                   "repair",
    "api_get_member/kdock":                   "factory",
    "api_get_member/questlist":               "quest_list",
    "api_get_member/mapinfo":                 "sortie_world",    # 出撃 → 海域選択
    "api_get_member/practice":                "practice",        # 演習
    "api_get_member/mission":                 "expedition_select",
    # ── 改装 entry signals ────────────────────────────────────────────────
    "api_get_member/preset_dev_items":        "equipment",   # 改装 equipment preset list
    "api_req_kaisou/can_preset_slot_select":  "equipment",   # 改装 slot preset check
    # ── 編成 entry signals ─────────────────────────────────────────────────
    "api_get_member/preset_deck":             "hensei",      # 編成 fleet preset list
    # ── Action-completion events ───────────────────────────────────────────
    "api_req_hokyu/charge":                   "supply",
    "api_req_nyukyo/start":                   "repair",
    "api_req_nyukyo/speedchange":             "repair",
    "api_req_kousyou/createship":             "factory",
    "api_req_kousyou/getship":                "construction_result",
    "api_req_kousyou/createitem":             "factory",
    "api_req_kaisou/powerup":                 "modernize_result",
    "api_req_kaisou/remodel_slot":            "equipment",
    "api_req_hensei/change":                  "hensei",
    "api_req_hensei/preset_select":           "hensei",
    "api_req_mission/start":                  "expedition_select",
    "api_req_mission/result":                 "expedition_result",
    "api_req_quest/clearitemget":             "quest_list",
    "api_req_quest/start":                    "quest_list",
    # ── Practice ───────────────────────────────────────────────────────────
    "api_req_member/get_practice_enemyinfo":  "practice",
    "api_req_practice/battle":               "battle",
    "api_req_practice/midnight_battle":      "night_battle",
    "api_req_practice/battle_result":        "battle_result",
    # ── Sortie ─────────────────────────────────────────────────────────────
    "api_req_map/start":                      "formation_select",
    "api_req_map/next":                       "formation_select",
    "api_req_sortie/battle":                  "battle",
    "api_req_sortie/battleresult":            "battle_result",
    "api_req_battle_midnight/battle":         "night_battle",
    "api_req_battle_midnight/sp_midnight":    "night_battle",
    # ── Combined fleet sortie ──────────────────────────────────────────────
    "api_req_combined_battle/battle":         "battle",
    "api_req_combined_battle/each_battle":    "battle",
    "api_req_combined_battle/battleresult":   "battle_result",
    "api_req_combined_battle/midnight_battle":"night_battle",
    "api_req_combined_battle/sp_midnight":    "night_battle",
}


async def _broadcast(msg: dict) -> None:
    if not _browser_clients:
        return
    payload = json.dumps(msg)
    dead: set[WebSocket] = set()
    for ws in list(_browser_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    for ws in dead:
        _browser_clients.discard(ws)


# ── poi WebSocket bridge ───────────────────────────────────────────────────

_bridge_task: asyncio.Task | None = None
_poi_connected = False


async def _poi_bridge() -> None:
    """Persistent connection to the poi plugin WebSocket. Forwards game events
    to all connected browser clients. Uses exponential backoff on failure."""
    import websockets as _ws  # type: ignore
    global _poi_connected

    delay = 2.0
    while True:
        try:
            async with _ws.connect(POI_WS_URL, ping_interval=20, ping_timeout=10,
                                   max_size=16 * 1024 * 1024) as poi_ws:
                _poi_connected = True
                delay = 2.0
                print("[bridge] poi WS connected")
                await _broadcast({"type": "poi_status", "connected": True})
                # Auto-inject spy after a short delay (KC2 may not be fully loaded yet)
                asyncio.create_task(_auto_inject_spy())
                async for raw in poi_ws:
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    msg_type = msg.get("type")

                    # KC webview navigation — no API call, direct screen change
                    # source values: navigation, hashchange, spy_poll, kc2_xhr, spy_init
                    if msg_type == "screen_change":
                        await _broadcast({
                            "type":   "screen_change",
                            "screen": msg.get("screen"),
                            "source": msg.get("source", "navigation"),
                            "url":    msg.get("url", ""),
                            "hash":   msg.get("hash", ""),
                            "api":    msg.get("api", ""),   # set when source=kc2_xhr
                        })
                        continue

                    # Forward spy/probe/scene responses to browser
                    if msg_type in ("spy_result", "spy_screen", "kc_globals", "resource_path",
                                    "screenshot_needed", "pixi_stage", "kc2_frame", "frame_list",
                                    "exec_kc2_result", "unknown_api"):
                        await _broadcast(msg)
                        continue

                    if msg_type != "event":
                        continue

                    path: str = msg.get("event", "")
                    short = path[8:] if path.startswith("/kcsapi/") else path.lstrip("/")
                    screen = _EVENT_SCREEN.get(short)

                    # payload.body can be a list for many KanColle APIs — guard carefully
                    _body = (msg.get("payload") or {}).get("body")
                    midnight_flag = _body.get("api_midnight_flag") if isinstance(_body, dict) else None
                    event_id = _body.get("api_event_id") if isinstance(_body, dict) else None

                    # Map/start and map/next: screen depends on event_id
                    # event_id 4=air raid, 5=battle, 6=boss, 7=night_start → formation_select
                    # event_id 0=nothing, 2=resource, 3=resource+item       → sortie_routing
                    if short in ("api_req_map/start", "api_req_map/next") and event_id is not None:
                        screen = "formation_select" if event_id in {4, 5, 6, 7} else "sortie_routing"

                    await _broadcast({
                        "type":          "game_event",
                        "event":         short,
                        "screen":        screen,
                        "midnight_flag": midnight_flag,
                        "event_id":      event_id,
                    })
        except Exception as e:
            if _poi_connected:
                _poi_connected = False
                await _broadcast({"type": "poi_status", "connected": False})
            print(f"[bridge] poi WS disconnected ({e}), retry in {delay:.0f}s")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 30.0)  # backoff up to 30s max


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _bridge_task
    _bridge_task = asyncio.create_task(_poi_bridge())
    yield
    if _bridge_task:
        _bridge_task.cancel()


app = FastAPI(title="kckit Simulator", lifespan=lifespan)


# ── Bridge control endpoints ───────────────────────────────────────────────

async def _auto_inject_spy() -> None:
    """Auto-inject screen spy after poi connects. Retries a few times if KC2 not loaded yet."""
    for attempt in range(4):
        await asyncio.sleep(3.0 if attempt == 0 else 5.0)
        result = await _poi_cmd('{"cmd":"inject_screen_spy"}', "spy_result", timeout=6.0)
        if result.get("ok"):
            print(f"[bridge] spy auto-injected (attempt {attempt+1}), screen={result.get('initialScreen')}")
            await _broadcast({"type": "spy_result", "payload": result, "auto": True})
            return
        print(f"[bridge] spy auto-inject attempt {attempt+1} failed: {result}")
    print("[bridge] spy auto-inject gave up — use the Inject Spy button when KC2 is loaded")


@app.post("/api/bridge/connect")
async def bridge_connect():
    global _bridge_task, _poi_connected
    if _bridge_task and not _bridge_task.done():
        _bridge_task.cancel()
        await asyncio.sleep(0.1)
    _poi_connected = False
    _bridge_task = asyncio.create_task(_poi_bridge())
    return {"status": "connecting"}


@app.post("/api/bridge/disconnect")
async def bridge_disconnect():
    global _bridge_task, _poi_connected
    if _bridge_task and not _bridge_task.done():
        _bridge_task.cancel()
    _poi_connected = False
    await _broadcast({"type": "poi_status", "connected": False})
    return {"status": "disconnected"}


async def _poi_cmd(cmd: str, response_type: str, timeout: float = 5.0) -> dict:
    """Open a temp WS to poi, send a command, wait for a specific response type."""
    import websockets as _ws
    try:
        async with _ws.connect(POI_WS_URL, max_size=8*1024*1024) as ws:
            # consume initial state push
            await asyncio.wait_for(ws.recv(), timeout=3.0)
            await ws.send(cmd)
            deadline = asyncio.get_event_loop().time() + timeout
            while asyncio.get_event_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    msg = json.loads(raw)
                    if msg.get("type") == response_type:
                        return msg.get("payload") or {}
                except asyncio.TimeoutError:
                    break
    except Exception as e:
        return {"error": str(e)}
    return {"error": "no response"}


@app.post("/api/probe_page_state")
async def probe_page_state():
    return await _poi_cmd('{"cmd":"get_page_state"}', "page_state")


@app.post("/api/probe_kc_globals")
async def probe_kc_globals():
    """Ask the poi plugin to dump KC webview globals + visible DOM elements."""
    return await _poi_cmd('{"cmd":"probe_kc_globals"}', "kc_globals")


@app.post("/api/inject_spy")
async def inject_spy():
    """Inject the persistent screen spy into KC webview."""
    return await _poi_cmd('{"cmd":"inject_screen_spy"}', "spy_result")


@app.get("/api/bridge/status")
async def bridge_status():
    task_exception = None
    if _bridge_task and _bridge_task.done() and not _bridge_task.cancelled():
        try:
            task_exception = str(_bridge_task.exception())
        except Exception:
            pass
    return {
        "poi_connected": _poi_connected,
        "browser_clients": len(_browser_clients),
        "bridge_task_alive": bool(_bridge_task and not _bridge_task.done()),
        "bridge_exception": task_exception,
    }


# ── State ─────────────────────────────────────────────────────────────────

def _load_snapshot() -> dict:
    if not SNAPSHOT_PATH.exists():
        return {}
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def _first_fleet(snap: dict) -> list[dict]:
    fleets = snap.get("fleets", {})
    fleet1 = fleets.get("1") or fleets.get(1) or {}
    ship_ids = fleet1.get("api_ship", [])
    ships = snap.get("ships", {})
    result = []
    for sid in ship_ids:
        if sid == -1:
            continue
        s = ships.get(str(sid)) or ships.get(sid)
        if s:
            result.append(s)
    return result


@app.get("/api/state")
def get_state():
    snap = _load_snapshot()
    if not snap:
        return {"error": "snapshot not found", "hint": "launch poi with kckit-bridge plugin"}

    fleet1 = _first_fleet(snap)
    ships_out = []
    for s in fleet1:
        master = s.get("$master", {})
        now_hp = s.get("api_nowhp", 0)
        max_hp = s.get("api_maxhp", 1)
        hp_ratio = now_hp / max_hp
        ships_out.append({
            "id":        s.get("api_id"),
            "name":      master.get("api_name", "？"),
            "yomi":      master.get("api_yomi", ""),
            "stype":     master.get("api_stype", 0),
            "lv":        s.get("api_lv", 1),
            "now_hp":    now_hp,
            "max_hp":    max_hp,
            "hp_ratio":  round(hp_ratio, 3),
            "cond":      s.get("api_cond", 49),
            "fuel":      s.get("api_fuel", 0),
            "ammo":      s.get("api_bull", 0),
            "fuel_max":  master.get("api_fuel_max", 1),
            "ammo_max":  master.get("api_bull_max", 1),
            "locked":    bool(s.get("api_locked")),
            "taiha":     hp_ratio <= 0.25,
            "chuuha":    0.25 < hp_ratio <= 0.5,
        })

    res = snap.get("resources", {})
    fleets_out = {}
    for fid, f in snap.get("fleets", {}).items():
        mission = f.get("api_mission", [0, 0, 0, 0])
        fleets_out[str(fid)] = {
            "in_expedition":  mission[0] == 1,
            "expedition_id":  mission[1] if mission[0] == 1 else None,
            "return_time":    mission[2] if mission[0] == 1 else None,
        }

    return {
        "hq_level":  snap.get("hq_level", 120),
        "resources": {
            "fuel":    res.get("fuel", 0),
            "ammo":    res.get("ammo", 0),
            "steel":   res.get("steel", 0),
            "bauxite": res.get("bauxite", 0),
            "bucket":  res.get("bucket", 0),
        },
        "fleet1":    ships_out,
        "fleets":    fleets_out,
        "timestamp": snap.get("timestamp"),
        "last_event": snap.get("last_event", ""),
    }


# ── Strategies ────────────────────────────────────────────────────────────

def _find_strategy_yaml(map_id: str) -> Path | None:
    world = map_id.split("-")[0]
    for world_dir in STRATEGIES_DIR.iterdir():
        if world_dir.is_dir() and world_dir.name.startswith(f"world{world}_"):
            for f in world_dir.iterdir():
                if f.name.startswith(f"{map_id}_") and f.suffix == ".yaml":
                    return f
    return None


@app.get("/api/strategies/{map_id}")
def get_strategies(map_id: str):
    import yaml as _yaml
    path = _find_strategy_yaml(map_id)
    if not path:
        raise HTTPException(404, f"No strategy for {map_id}")
    with open(path) as f:
        data = _yaml.safe_load(f)
    presets = data.get("presets", [])
    map_raw = data.get("map") or {}
    map_name = map_raw.get("name", map_id) if isinstance(map_raw, dict) else map_id
    return {
        "map_id":   map_id,
        "map_name": map_name,
        "presets": [
            {
                "name":               p.get("name"),
                "formation":          (p.get("formation") or
                                       (p.get("requirements") or {}).get("formation") or 1),
                "routing_nodes":      ((p.get("requirements") or {}).get("routing_nodes") or []),
                "night_battle_nodes": ((p.get("requirements") or {}).get("night_battle_nodes") or []),
            }
            for p in presets
        ],
    }


# ── Plan ──────────────────────────────────────────────────────────────────

@app.get("/api/plan")
def get_plan(map_id: str, preset: str = ""):
    import yaml as _yaml
    path = _find_strategy_yaml(map_id)
    if not path:
        raise HTTPException(404, f"No strategy for {map_id}")
    with open(path) as f:
        data = _yaml.safe_load(f)
    presets = data.get("presets", [])
    if not presets:
        raise HTTPException(404, "No presets")

    chosen = next((p for p in presets if p.get("name") == preset), presets[0])
    snap = _load_snapshot()
    fleet1 = _first_fleet(snap)
    hq_level = snap.get("hq_level", 120)

    steps = generate_sortie_plan(map_id, chosen, fleet1, hq_level)
    return {
        "map_id": map_id,
        "preset": chosen.get("name"),
        "steps":  steps,
    }


# ── Layout ────────────────────────────────────────────────────────────────

@app.get("/api/layout")
def get_layout():
    import yaml as _yaml
    with open(LAYOUT_PATH) as f:
        return _yaml.safe_load(f)


# ── Screenshot (proxied from poi plugin) ──────────────────────────────────

@app.get("/api/screenshot")
async def get_screenshot():
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(POI_SCREENSHOT_URL, timeout=3.0)
        r.raise_for_status()
        return Response(
            content=r.content,
            media_type="image/jpeg",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(503, f"poi screenshot unavailable ({e.response.status_code})")
    except Exception as e:
        raise HTTPException(503, f"poi screenshot unavailable ({e})")


# ── Browser WebSocket ─────────────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()
    _browser_clients.add(websocket)
    await websocket.send_text(json.dumps({"type": "connected"}))
    # Send current poi connection status immediately so the UI dots are right
    await websocket.send_text(json.dumps({"type": "poi_status", "connected": _poi_connected}))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        _browser_clients.discard(websocket)


# ── Static files ──────────────────────────────────────────────────────────

app.mount("/", StaticFiles(
    directory=str(Path(__file__).parent / "static"), html=True), name="static")


if __name__ == "__main__":
    print("kckit Simulator → http://localhost:8765")
    uvicorn.run("tools.simulator.server:app", host="0.0.0.0", port=8765,
                reload=True, reload_dirs=[str(ROOT)])

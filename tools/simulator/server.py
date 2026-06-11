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
import time as _time
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import Response
import uvicorn

from tools.simulator.plan import generate_sortie_plan
from core.scene_perception import classify_screen, load_semantics

SNAPSHOT_PATH = Path.home() / ".kckit" / "box_snapshot.json"
UI_ATLAS_DIR = ROOT / "data" / "ui_atlas" / "raw"
STRATEGIES_DIR = ROOT / "strategies" / "maps"
LAYOUT_PATH = ROOT / "config" / "screen_layout.yaml"
POI_WS_URL = "ws://127.0.0.1:23456"
POI_SCREENSHOT_URL = "http://127.0.0.1:23457/screenshot"

# ── Browser client registry ────────────────────────────────────────────────

_browser_clients: set[WebSocket] = set()
_last_scene_screen: str | None = None  # last screen classified from scene_tree

# ── Recording ──────────────────────────────────────────────────────────────
_recording = False
_action_log_entries: list[dict] = []
_action_log_path = ROOT / "temp" / "action_log.jsonl"
_rec_clicks  = 0   # atomic counters — avoid O(n) scan on every status poll
_rec_apis    = 0
_rec_screens = 0

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
    # ── 編成 preset actions (real user actions within 編成 screen) ─────────
    "api_get_member/preset_deck":             "hensei",
    # Excluded: preset_dev_items, can_preset_slot_select (background loads,
    # fire unpredictably and cause factory→equipment misidentification)
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


# ── Screen state machine ───────────────────────────────────────────────────
# Maps (from_screen | '*', element_key_regex, to_screen).
# Applied when server receives a canvas_click event from the poi plugin.
# '*' matches any screen. First matching rule wins.

import re as _re

_SCREEN_TRANSITIONS: list[tuple[str, str, str]] = [
    # Common navigation (any screen)
    ('*', r'^back_port$',           'port'),
    ('*', r'^left_nav_home$',       'port'),
    ('*', r'^left_nav_hensei$',     'hensei'),
    ('*', r'^left_nav_supply$',     'supply'),
    ('*', r'^left_nav_equip$',      'equipment'),
    ('*', r'^left_nav_repair$',     'repair'),
    ('*', r'^left_nav_factory$',    'factory'),
    # Port nav wheel
    ('port', r'^sortie_nav$',       'sortie_type'),
    ('port', r'^hensei_nav$',       'hensei'),
    ('port', r'^supply_nav$',       'supply'),
    ('port', r'^equipment_nav$',    'equipment'),
    ('port', r'^repair_nav$',       'repair'),
    ('port', r'^factory_nav$',      'factory'),
    # 編成 chain
    ('hensei',              r'^(ship_slot|change)_',   'hensei_ship_select'),
    ('hensei_ship_select',  r'^close_area$',           'hensei'),
    ('hensei_ship_select',  r'^ship_item_',            'hensei_ship_confirm'),
    ('hensei_ship_confirm', r'^close_area$',           'hensei_ship_select'),
    ('hensei_ship_confirm', r'^confirm_btn$',          'hensei'),
    # 改装 chain
    ('equipment',           r'^fleet_tab_other$',      'equipment_other'),
    ('equipment',           r'^(equip_slot|slot_ex)$', 'equipment_select'),
    ('equipment_other',     r'^fleet_tab_[1234]$',     'equipment'),
    ('equipment_other',     r'^ship_item_',            'equipment'),
    ('equipment_select',    r'^close_area$',           'equipment'),
    ('equipment_select',    r'^equip_item_',           'equipment_confirm'),
    ('equipment_confirm',   r'^close_area$',           'equipment_select'),
    ('equipment_confirm',   r'^confirm_btn$',          'equipment'),
    # 入渠 chain
    ('repair',              r'^dock_[1234]$',          'repair_ship_select'),
    ('repair_ship_select',  r'^close_area$',           'repair'),
    ('repair_ship_select',  r'^ship_item_',            'repair_ship_confirm'),
    ('repair_ship_confirm', r'^close_area$',           'repair_ship_select'),
    ('repair_ship_confirm', r'^confirm_btn$',          'repair_confirm'),
    ('repair_confirm',      r'^(yes_btn|no_btn)$',     'repair'),
    # 出撃 chain
    ('sortie_type',         r'^sortie_btn$',           'sortie_world'),
    ('sortie_type',         r'^exercise_btn$',         'practice'),
    ('sortie_type',         r'^expedition_btn$',       'expedition_select'),
    ('sortie_world',        r'^world_',                'sortie_map'),
    ('sortie_map',          r'^sortie_btn$',           'formation_select'),
    ('practice',            r'^challenge_btn$',        'formation_select'),
    # 遠征
    ('expedition_select',   r'^go_btn$',               'port'),
    # 戦闘後
    ('battle_result',       r'^next$',                 'night_battle_select'),
    ('night_battle_select', r'^night_battle$',         'night_battle'),
    ('night_battle_select', r'^no_night$',             'post_battle'),
    ('post_battle',         r'^advance$',              'formation_select'),
    ('post_battle',         r'^retreat$',              'port'),
    # Result confirms
    ('expedition_result',   r'^confirm$',              'port'),
    ('modernize_result',    r'^confirm$',              'equipment'),
    ('construction_result', r'^confirm$',              'factory'),
]

_layout_cache: dict | None = None
_NO_COMMON_SM    = frozenset({'repair_confirm','expedition_result','battle','night_battle',
                              'battle_result','night_battle_select','sortie_routing',
                              'formation_select','post_battle'})
_NO_LEFT_NAV_SM  = frozenset({'port','quest_list','expedition_result','repair_confirm',
                              'battle','night_battle','battle_result','night_battle_select',
                              'sortie_routing','formation_select','post_battle'})
_HAS_LIST_DISMISS   = frozenset({'repair_ship_select','hensei_ship_select','equipment_select'})
_HAS_CONFIRM_DISMISS = frozenset({'repair_ship_confirm','hensei_ship_confirm','equipment_confirm'})


def _get_layout() -> dict:
    global _layout_cache
    if _layout_cache is None:
        import yaml as _yaml
        with open(LAYOUT_PATH) as f:
            _layout_cache = _yaml.safe_load(f)
    return _layout_cache


def _find_element(rx: float, ry: float, screen: str | None) -> str | None:
    """Return the element key hit by canvas-fraction click (rx, ry)."""
    layout = _get_layout()
    elements: dict = {}
    if screen not in _NO_COMMON_SM:
        elements.update(((layout.get("common") or {}).get("elements")) or {})
    if screen not in _NO_LEFT_NAV_SM:
        elements.update(((layout.get("left_nav") or {}).get("elements")) or {})
    popups = layout.get("popups") or {}
    if screen in _HAS_LIST_DISMISS:
        elements.update(((popups.get("list_dismiss") or {}).get("elements")) or {})
    if screen in _HAS_CONFIRM_DISMISS:
        elements.update(((popups.get("confirm_dismiss") or {}).get("elements")) or {})
    screen_els = ((layout.get("screens") or {}).get(screen or "") or {}).get("elements") or {}
    elements.update(screen_els)
    for key, el in elements.items():
        cx, cy = el["x"], el["y"]
        hw, hh = el["w"] / 2, el["h"] / 2
        if el.get("shape") == "circle":
            r = min(hw, hh)
            if (rx - cx) ** 2 + (ry - cy) ** 2 <= r * r:
                return key
        elif abs(rx - cx) <= hw and abs(ry - cy) <= hh:
            return key
    return None


def _apply_transition(from_screen: str | None, element_key: str) -> str | None:
    for from_pat, elem_pat, to_screen in _SCREEN_TRANSITIONS:
        if from_pat != '*' and from_pat != from_screen:
            continue
        if _re.search(elem_pat, element_key):
            return to_screen
    return None


# ── State helpers ──────────────────────────────────────────────────────────

_ship_names: dict[str, str] = {}  # ship instance id → display name, cached from poi state


def _enrich_fleets(fleets_raw: dict) -> dict:
    result: dict = {}
    for fid, fleet in (fleets_raw or {}).items():
        mission = fleet.get("api_mission") or [0, 0, 0, 0]
        result[str(fid)] = {
            "in_expedition": mission[0] == 1,
            "expedition_id": mission[1] if mission[0] == 1 else None,
            "return_time":   mission[2] if mission[0] == 1 else None,
        }
    return result


_QUEST_PERIOD = {1: 'daily', 2: 'weekly', 3: 'monthly', 4: 'once'}
# KC2 api_category: 1=建造 2=出撃 3=演習 4=遠征 5=改修 6=工廠
_QUEST_CAT    = {1: '建造', 2: '出撃', 3: '演習', 4: '遠征', 5: '改修', 6: '工廠'}
_QUEST_PROG   = {0: None, 1: '50%+', 2: '80%+', 3: '100%'}

_STYPE_NAME   = {
    2:'DD', 3:'CL', 4:'CLT', 5:'CA', 6:'CAV', 7:'CVL',
    8:'FBB', 9:'BB', 10:'CVB', 11:'CV', 13:'SS', 14:'SSV',
    16:'AV', 17:'LHA', 18:'LHA', 20:'AO', 21:'AO',
}


def _summarize_quests(quests_raw) -> dict:
    completable, active = [], []
    for _qid, q in (quests_raw or {}).items():
        if not isinstance(q, dict):
            continue
        state = q.get("api_state", 0)
        if state not in (2, 3):
            continue
        # api_type = period (1=daily,2=weekly,3=monthly,4=once)
        # api_category = quest type (2=battle,3=exercise,4=expedition,…)
        entry = {
            "no":     q.get("api_no", 0),
            "title":  (q.get("api_title") or "")[:28],
            "period": _QUEST_PERIOD.get(q.get("api_type", 0), "—"),
            "cat":    _QUEST_CAT.get(q.get("api_category", 0), "—"),
            "prog":   _QUEST_PROG.get(q.get("api_progress_flag", 0)),
        }
        (completable if state == 3 else active).append(entry)
    completable.sort(key=lambda q: q["no"])
    active.sort(key=lambda q: q["no"])
    return {"completable": completable, "active": active}


def _enrich_repairs(repairs_raw) -> dict:
    if isinstance(repairs_raw, list):
        docks = repairs_raw
    elif isinstance(repairs_raw, dict):
        docks = list(repairs_raw.values())
    else:
        return {}
    result: dict = {}
    for dock in docks:
        if not isinstance(dock, dict):
            continue
        did = str(dock.get("api_id", ""))
        if not did:
            continue
        ship_id = dock.get("api_ship_id", 0)
        result[did] = {
            "dock_id":       did,
            "ship_name":     _ship_names.get(str(ship_id), "") if ship_id else "",
            "complete_time": dock.get("api_complete_time", 0),
            "state":         dock.get("api_state", 0),
        }
    return result


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
                # App-level staleness watchdog: a zombie connection (e.g. the
                # plugin hot-reloaded without terminating clients) answers
                # protocol pings but never sends messages. Solicit a state
                # reply after 45s of silence; reconnect after two strikes.
                silent_strikes = 0
                while True:
                    try:
                        raw = await asyncio.wait_for(poi_ws.recv(), timeout=45.0)
                        silent_strikes = 0
                    except asyncio.TimeoutError:
                        silent_strikes += 1
                        if silent_strikes >= 2:
                            print("[bridge] poi WS silent >90s — reconnecting")
                            break
                        await poi_ws.send(json.dumps({"cmd": "get_state"}))
                        continue
                    except Exception:
                        break
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue
                    msg_type = msg.get("type")

                    # KC webview navigation — no API call, direct screen change
                    # source values: navigation, hashchange, spy_poll, kc2_xhr, spy_init
                    if msg_type == "screen_change":
                        screen = msg.get("screen")
                        source = msg.get("source", "navigation")
                        await _broadcast({
                            "type":   "screen_change",
                            "screen": screen,
                            "source": source,
                            "url":    msg.get("url", ""),
                            "hash":   msg.get("hash", ""),
                            "api":    msg.get("api", ""),   # set when source=kc2_xhr
                        })
                        if _recording and screen:
                            global _rec_screens
                            _rec_screens += 1
                            _action_log_entries.append({
                                "ts":     _time.time(),
                                "type":   "screen",
                                "screen": screen,
                                "source": source,
                            })
                        continue

                    # Canvas click → server-side state machine
                    if msg_type == "canvas_click":
                        rx = float(msg.get("rx") or 0)
                        ry = float(msg.get("ry") or 0)
                        screen = msg.get("screen")
                        elem = _find_element(rx, ry, screen)
                        next_screen = None
                        if elem:
                            next_screen = _apply_transition(screen, elem)
                            if next_screen and next_screen != screen:
                                await _broadcast({
                                    "type":   "screen_change",
                                    "screen": next_screen,
                                    "source": "click_nav",
                                    "api":    elem,
                                })
                        if _recording:
                            global _rec_clicks
                            _rec_clicks += 1
                            _action_log_entries.append({
                                "ts":          _time.time(),
                                "type":        "click",
                                "screen":      screen,
                                "rx":          round(rx, 4),
                                "ry":          round(ry, 4),
                                "element":     elem,        # None if not matched
                                "next_screen": next_screen,
                            })
                        continue

                    # Scene tree (perception v2): classify + forward.
                    # Nodes carry renderer-px bounds; payload has rw/rh dims.
                    if msg_type == "scene_tree":
                        payload = msg.get("payload")
                        if payload and payload.get("nodes"):
                            global _last_scene_screen
                            cls = classify_screen(payload["nodes"])
                            await _broadcast({
                                "type":     "scene_tree",
                                "payload":  payload,
                                "classify": cls,
                            })
                            if cls["screen"] and cls["screen"] != _last_scene_screen:
                                _last_scene_screen = cls["screen"]
                                await _broadcast({
                                    "type":   "screen_change",
                                    "screen": cls["screen"],
                                    "source": "scene_tree",
                                    "api":    "",
                                })
                                asyncio.create_task(
                                    _capture_screen_sample(cls["screen"], payload))
                            elif cls["screen"] is None and len(payload["nodes"]) >= 30:
                                # Unclassified screen with real content — a
                                # fingerprint gap (transient dialogs like 夜戦
                                # 突入 / 進撃撤退 land here). Archive under its
                                # dominant atlas prefix so it can be labeled.
                                top = max(cls["prefixes"], key=cls["prefixes"].get,
                                          default=None) if cls["prefixes"] else None
                                asyncio.create_task(_capture_screen_sample(
                                    f"unknown_{top or 'bare'}", payload))
                        continue

                    # Forward spy/probe/scene responses to browser
                    if msg_type in ("health", "spy_result", "spy_screen", "kc_globals", "resource_path",
                                    "screenshot_needed", "pixi_stage", "kc2_frame", "frame_list",
                                    "exec_kc2_result", "unknown_api"):
                        await _broadcast(msg)
                        continue

                    # Full game state — extract and forward curated subset (ships are too large)
                    if msg_type == "state":
                        global _ship_names
                        payload = msg.get("payload") or {}
                        for sid, ship in (payload.get("ships") or {}).items():
                            name = ((ship.get("$master") or {}).get("api_name") or "")
                            if name:
                                _ship_names[str(sid)] = name
                        await _broadcast({
                            "type":      "state_update",
                            "resources": payload.get("resources"),
                            "fleets":    _enrich_fleets(payload.get("fleets") or {}),
                            "repairs":   _enrich_repairs(payload.get("repairs")),
                            "quests":    _summarize_quests(payload.get("quests")),
                            "hq_level":  payload.get("hq_level"),
                        })
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
                    if _recording and screen:
                        global _rec_apis
                        _rec_apis += 1
                        _action_log_entries.append({
                            "ts":     _time.time(),
                            "type":   "api",
                            "event":  short,
                            "screen": screen,
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


# ── Action recording ───────────────────────────────────────────────────────

@app.post("/api/recording/start")
async def recording_start():
    global _recording, _action_log_entries, _rec_clicks, _rec_apis, _rec_screens
    _recording = True
    _action_log_entries = []
    _rec_clicks = _rec_apis = _rec_screens = 0
    return {"recording": True, "clicks": 0, "apis": 0, "screens": 0}


@app.post("/api/recording/stop")
def recording_stop():
    global _recording
    _recording = False
    _action_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(_action_log_path, "w", encoding="utf-8") as f:
        for entry in _action_log_entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return {
        "recording": False,
        "entries":   len(_action_log_entries),
        "clicks":    _rec_clicks,
        "apis":      _rec_apis,
        "screens":   _rec_screens,
        "path":      str(_action_log_path),
    }


@app.get("/api/recording/status")
def recording_status():
    return {"recording": _recording, "clicks": _rec_clicks, "apis": _rec_apis, "screens": _rec_screens}


# ── Virtual click (executor primitive) ────────────────────────────────────

@app.post("/api/click")
async def do_click(rx: float, ry: float, label: str = ""):
    """Send a virtual click to the KC2 canvas at (rx, ry) fractions [0-1].
    Routes through the poi plugin's wc.sendInputEvent() — normal rendering pipeline."""
    if not (0.0 <= rx <= 1.0 and 0.0 <= ry <= 1.0):
        raise HTTPException(400, f"rx/ry must be in [0,1], got ({rx}, {ry})")
    cmd = json.dumps({"cmd": "click_kc2", "rx": rx, "ry": ry, "label": label})
    result = await _poi_cmd(cmd, "click_result", timeout=5.0)
    if result.get("error") or result.get("err"):
        raise HTTPException(503, result.get("error") or result.get("err"))
    return result


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
    fleets_out = _enrich_fleets(snap.get("fleets", {}))

    # Populate ship name cache from snapshot if bridge hasn't connected yet
    if not _ship_names:
        for sid, ship in (snap.get("ships") or {}).items():
            name = ((ship.get("$master") or {}).get("api_name") or "")
            if name:
                _ship_names[str(sid)] = name
    repairs_out = _enrich_repairs(snap.get("repairs"))

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
        "repairs":   repairs_out,
        "quests":    _summarize_quests(snap.get("quests")),
        "timestamp": snap.get("timestamp"),
        "last_event": snap.get("last_event", ""),
    }


# ── List data endpoints ───────────────────────────────────────────────────
# Ship list sorted by api_sortno (KC default) with popup-list positions.
# 10 ships per page for repair_ship_select / hensei_ship_select.

@app.get("/api/ships")
def get_ships():
    snap = _load_snapshot()
    ships   = snap.get("ships") or {}
    fleets  = snap.get("fleets") or {}
    repairs = snap.get("repairs") or {}

    # Fleet membership: ship_id → (fleet_id, pos)
    in_fleet: dict[str, tuple[int, int]] = {}
    for fid, fleet in fleets.items():
        for pos, sid in enumerate(fleet.get("api_ship") or []):
            if sid != -1:
                in_fleet[str(sid)] = (int(fid), pos)

    # Dock occupancy: ship_id → dock_id
    in_dock: dict[str, str] = {}
    docks_raw = repairs if isinstance(repairs, list) else list(repairs.values())
    for dock in docks_raw:
        if isinstance(dock, dict) and dock.get("api_ship_id"):
            in_dock[str(dock["api_ship_id"])] = str(dock.get("api_id", ""))

    ship_list = []
    for _sid, ship in ships.items():
        master  = ship.get("$master") or {}
        now_hp  = ship.get("api_nowhp", 0)
        max_hp  = ship.get("api_maxhp", 1)
        hp_r    = now_hp / max_hp if max_hp else 1
        stype   = master.get("api_stype", 0)
        iid     = ship.get("api_id")
        fl      = in_fleet.get(str(iid))
        ship_list.append({
            "id":         iid,
            "master_id":  ship.get("api_ship_id"),
            "name":       master.get("api_name", "?"),
            "sortno":     master.get("api_sortno", 0),
            "stype":      stype,
            "stype_name": _STYPE_NAME.get(stype, "?"),
            "lv":         ship.get("api_lv", 1),
            "now_hp":     now_hp,
            "max_hp":     max_hp,
            "hp_ratio":   round(hp_r, 3),
            "taiha":      hp_r <= 0.25,
            "chuuha":     0.25 < hp_r <= 0.5,
            "cond":       ship.get("api_cond", 49),
            "locked":     bool(ship.get("api_locked")),
            "repair_ms":  ship.get("api_ndock_time", 0),
            "fleet":      fl[0] if fl else None,
            "fleet_pos":  fl[1] if fl else None,
            "in_dock":    in_dock.get(str(iid)),
        })

    ship_list.sort(key=lambda s: (s["sortno"], s["id"] or 0))
    for i, s in enumerate(ship_list):
        s["list_page"] = i // 10
        s["list_row"]  = i % 10
    return {"ships": ship_list, "count": len(ship_list)}


# Equipment list sorted by category → level desc → id.
# 10 equips per page for equipment_select.
# is_idle = not currently equipped on any ship (KC default filter).

@app.get("/api/equips")
def get_equips():
    snap    = snap = _load_snapshot()
    ships   = snap.get("ships") or {}
    equips  = snap.get("equips") or {}

    # Build equipped set: equip instance ids currently on ships
    equipped: set[int] = set()
    for ship in ships.values():
        for slot_list in [ship.get("api_slot") or [], [ship.get("api_slot_ex")]]:
            for eid in slot_list:
                if eid and eid != -1:
                    equipped.add(eid)

    equip_list = []
    for _eid, eq in equips.items():
        master   = eq.get("$master") or {}
        eq_type  = master.get("api_type") or []
        cat      = eq_type[2] if len(eq_type) > 2 else 0
        equip_list.append({
            "id":       eq.get("api_id"),
            "master_id": eq.get("api_slotitem_id"),
            "name":     master.get("api_name", "?"),
            "cat":      cat,
            "level":    eq.get("api_level", 0),
            "alv":      eq.get("api_alv", 0),
            "is_idle":  eq.get("api_id") not in equipped,
        })

    equip_list.sort(key=lambda e: (e["cat"], -(e["level"]), e["id"] or 0))
    for i, e in enumerate(equip_list):
        e["list_page"] = i // 10
        e["list_row"]  = i % 10
    return {"equips": equip_list, "count": len(equip_list)}


@app.get("/api/quests")
def get_quests():
    snap = _load_snapshot()
    return _summarize_quests(snap.get("quests"))


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


# ── Perception v2: atlases, semantics, on-demand scene tree ───────────────

@app.get("/api/atlas")
async def list_atlases():
    """Names of harvested UI atlases (json+png pairs in data/ui_atlas/raw)."""
    if not UI_ATLAS_DIR.exists():
        return {"atlases": []}
    return {"atlases": sorted(p.stem for p in UI_ATLAS_DIR.glob("*.json"))}


@app.get("/api/atlas/{name}")
async def get_atlas(name: str):
    """Serve a harvested atlas file, e.g. sally_top.json / sally_top.png."""
    if "/" in name or ".." in name:
        raise HTTPException(400, "bad atlas name")
    path = UI_ATLAS_DIR / name
    if not path.is_file():
        raise HTTPException(404, f"atlas {name} not harvested yet")
    media = "image/png" if name.endswith(".png") else "application/json"
    return Response(
        content=path.read_bytes(),
        media_type=media,
        headers={"Cache-Control": "max-age=3600"},
    )


@app.get("/api/semantics")
async def get_semantics():
    """Atlas frame → semantic element dictionary (data/ui_atlas/semantics.yaml)."""
    return load_semantics()


# ── Screen sample auto-capture ─────────────────────────────────────────────
# Archive scene tree + screenshot to temp/captures/ when a screen is first
# classified (re-captured after _RECAPTURE_S so dirty samples — e.g. a dialog
# covering the port wheel — get replaced eventually). A normal play session
# thereby collects labeling material for every screen passed through.
_CAPTURES_DIR = ROOT / "temp" / "captures"
_RECAPTURE_S = 6 * 3600
_captured_screens: dict[str, float] = {}


async def _capture_screen_sample(screen: str, payload: dict) -> None:
    last = _captured_screens.get(screen, 0)
    if _time.time() - last < _RECAPTURE_S:
        return
    _captured_screens[screen] = _time.time()
    try:
        _CAPTURES_DIR.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%Y%m%d_%H%M%S")
        (_CAPTURES_DIR / f"{stamp}_{screen}.json").write_text(
            json.dumps(payload, ensure_ascii=False))
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(POI_SCREENSHOT_URL, timeout=5.0)
        if r.status_code == 200:
            (_CAPTURES_DIR / f"{stamp}_{screen}.jpg").write_bytes(r.content)
        print(f"[capture] {screen} archived")
    except Exception as e:
        print(f"[capture] {screen} failed: {e}")


_KCRES_CACHE = ROOT / "temp" / "kcres_cache"
_kc_origin: str | None = None


async def _get_kc_origin() -> str | None:
    """Game server origin (e.g. https://w10b.kancolle-server.com), probed once
    from the KC2 frame via the plugin."""
    global _kc_origin
    if _kc_origin:
        return _kc_origin
    result = await _poi_cmd('{"cmd": "exec_kc2", "code": "location.origin"}',
                            "exec_kc2_result", timeout=6.0)
    origin = (result or {}).get("result")
    if isinstance(origin, str) and origin.startswith("http"):
        _kc_origin = origin
    return _kc_origin


@app.get("/api/kcres/{path:path}")
async def get_kc_resource(path: str):
    """Proxy+cache raw game resources (ship banners, map art …) so the recon
    view can render non-atlas textures. Whitelisted to /kcs2/ paths."""
    if not path.startswith("kcs2/") or ".." in path:
        raise HTTPException(400, "only kcs2/ resources are proxied")
    cached = _KCRES_CACHE / path
    suffix = path.rsplit(".", 1)[-1].lower()
    media = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "json": "application/json"}.get(suffix, "application/octet-stream")
    if cached.is_file():
        return Response(content=cached.read_bytes(), media_type=media,
                        headers={"Cache-Control": "max-age=86400"})
    origin = await _get_kc_origin()
    if not origin:
        raise HTTPException(503, "game origin unknown (KC2 not reachable)")
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{origin}/{path}", timeout=15.0,
                                 headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        raise HTTPException(502, f"resource fetch failed: {e}")
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(r.content)
    return Response(content=r.content, media_type=media,
                    headers={"Cache-Control": "max-age=86400"})


@app.post("/api/scene_tree")
async def pull_scene_tree():
    """On-demand scene tree walk (frontend initial load). Returns payload +
    classification; payload is null if the game/walker isn't ready."""
    result = await _poi_cmd('{"cmd": "get_scene_tree"}', "scene_tree", timeout=8.0)
    if not result or "error" in result or not result.get("nodes"):
        return {"payload": None, "classify": None}
    return {"payload": result, "classify": classify_screen(result["nodes"])}


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

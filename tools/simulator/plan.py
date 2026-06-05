"""
Generates a structured list of operation steps for a given map+preset.
Steps are screen-level actions: clicks, screen transitions, decisions.
Coordinates are 0-1 fractions of the 800×480 game canvas.
"""
from __future__ import annotations

import yaml
from pathlib import Path
from typing import Optional

_LAYOUT_PATH = Path(__file__).parent.parent.parent / "config" / "screen_layout.yaml"
_STRATEGIES_DIR = Path(__file__).parent.parent.parent / "strategies" / "maps"

STYPE_NAME = {
    2: "DD", 3: "CL", 4: "CLT", 5: "CA", 6: "CAV",
    8: "FBB", 9: "BB", 10: "BBV", 11: "BBV",
    13: "SS", 14: "SSV", 15: "SS", 16: "SSV",
    17: "CVL", 18: "CV", 19: "CVB",
    20: "AO", 21: "DE", 22: "AO",
}
STYPE_COLOR = {
    2: "#4a9eff", 3: "#22b8cf", 4: "#22b8cf", 5: "#f59f00", 6: "#f59f00",
    8: "#e03131", 9: "#e03131", 10: "#e03131", 11: "#e03131",
    13: "#40c057", 14: "#40c057", 15: "#40c057", 16: "#40c057",
    17: "#7048e8", 18: "#7048e8", 19: "#7048e8",
    20: "#f76707", 21: "#868e96", 22: "#f76707",
}

FORMATION_NAMES = {
    1: "単縦陣", 2: "複縦陣", 3: "輪形陣", 4: "梯形陣", 5: "単横陣",
}


def load_layout() -> dict:
    with open(_LAYOUT_PATH) as f:
        return yaml.safe_load(f)


def _elem(layout: dict, screen: str, element: str) -> Optional[tuple[float, float]]:
    s = layout["screens"].get(screen, {})
    e = s.get("elements", {}).get(element)
    if e:
        return (e["x"], e["y"])
    return None


def _world_elem(map_id: str) -> str:
    return f"world_{map_id.split('-')[0]}"


def _map_elem(map_id: str) -> str:
    w, m = map_id.split("-")
    return f"map_{w}_{m}"


def generate_sortie_plan(
    map_id: str,
    preset: dict,
    fleet_ships: list[dict],
    hq_level: int = 120,
) -> list[dict]:
    """
    Returns a list of step dicts for a full sortie cycle on map_id with given preset.
    fleet_ships: list of ship dicts from box_snapshot (first fleet).
    """
    layout = load_layout()
    steps = []
    _id = [0]

    def step(type_: str, screen: str, label: str, description: str = "",
             element: str = None, target: tuple = None,
             duration_ms: int = 600, wait_after_ms: int = 1000,
             wait_for: str = None,
             extra: dict = None):
        _id[0] += 1
        pos = target or (element and _elem(layout, screen, element))
        s = {
            "id": _id[0],
            "type": type_,
            "screen": screen,
            "label": label,
            "description": description or label,
            "element": element,
            "target": {"x": pos[0], "y": pos[1]} if pos else None,
            "duration_ms": duration_ms,
            "wait_after_ms": wait_after_ms,
        }
        if wait_for:
            s["wait_for"] = wait_for
        if extra:
            s.update(extra)
        steps.append(s)

    reqs = preset.get("requirements") or {}
    formation = preset.get("formation") or reqs.get("formation") or 1
    formation_name = FORMATION_NAMES.get(formation, f"陣形{formation}")
    routing = reqs.get("routing_nodes") or []
    night_nodes = set(preset.get("night_battle_nodes") or reqs.get("night_battle_nodes") or [])
    preset_name = preset.get("name", "？")

    # ── 1. 出撃前硬性安全门 ─────────────────────────────────────────
    # ABORT if any ship is 大破 (HP≤25%) — no exceptions
    step("check", "port", "大破检查（出撃前）",
         "确认第一舰队无大破（HP≤25%）。有大破 → 中止计划，先入渠修理",
         extra={"check_type": "no_taiha", "abort_on_fail": True}, wait_after_ms=300)
    # WARN if any ship is 中破 — soft gate, user decides
    step("check", "port", "中破检查（出撃前）",
         "确认第一舰队无中破（HP≤50%）。有中破 → 警告，请确认是否继续",
         extra={"check_type": "no_chuuha", "abort_on_fail": False}, wait_after_ms=300)

    # ── 2. 补给 ─────────────────────────────────────────────────────
    step("click", "port", "点击「補給」",
         "进入补给画面",
         element="supply_nav", duration_ms=400, wait_after_ms=1000)
    step("screen_change", "supply", "→ 补给画面", wait_after_ms=300)
    step("click", "supply", "一键补给",
         "点击「一括補給」为第一舰队全员补给燃料和弹药",
         element="supply_all", duration_ms=400, wait_after_ms=800,
         wait_for="api_req_hokyu/charge")
    step("click", "supply", "返回母港",
         element="back", duration_ms=400, wait_after_ms=1000,
         wait_for="api_port/port")
    step("screen_change", "port", "→ 母港", wait_after_ms=300)

    # ── 2b. 补给后再次大破检查（双保险） ────────────────────────────
    step("check", "port", "大破检查（补给后）",
         "补给操作完成后再次确认无大破（理论上不变，但作为双重保护）",
         extra={"check_type": "no_taiha", "abort_on_fail": True}, wait_after_ms=200)

    # ── 3. 出撃导航 ─────────────────────────────────────────────────
    step("click", "port", "点击「出撃」",
         "进入出撃/演習/遠征选择画面",
         element="sortie_nav", duration_ms=400, wait_after_ms=1000)
    step("screen_change", "sortie_type", "→ 出撃种别选択", wait_after_ms=400)
    step("click", "sortie_type", "点击「出撃」",
         "选择出撃模式",
         element="sortie_btn", duration_ms=400, wait_after_ms=1000)
    step("screen_change", "sortie_world", "→ 海域选択", wait_after_ms=500)

    world_num = map_id.split("-")[0]
    step("click", "sortie_world", f"选择 World {world_num}",
         f"点击「{layout['screens']['sortie_world']['elements'].get(_world_elem(map_id), {}).get('label', f'World {world_num}')}」海域",
         element=_world_elem(map_id), duration_ms=500, wait_after_ms=1000)
    step("screen_change", "sortie_map", f"→ {map_id} 海图", wait_after_ms=600)

    step("click", "sortie_map", f"选择节点 {map_id}",
         f"点击地图上的「{map_id}」节点",
         element=_map_elem(map_id), duration_ms=500, wait_after_ms=800)

    step("click", "sortie_map", "确认出撃",
         f"使用预案「{preset_name}」，点击「出撃」确认编成",
         element="sortie_btn", duration_ms=400, wait_after_ms=1000)
    step("screen_change", "sortie_fleet", "→ 编成确认", wait_after_ms=500)
    step("click", "sortie_fleet", "出撃！",
         "确认舰队编成正确，点击「出撃」",
         element="sortie_confirm", duration_ms=500, wait_after_ms=1500,
         wait_for="api_req_map/start")

    # ── 4. 节点循环 ─────────────────────────────────────────────────
    nodes = routing if routing else ["?"]
    for i, node in enumerate(nodes):
        is_last = i == len(nodes) - 1
        is_night = node in night_nodes
        # Node 0 arrives here because "出撃！" already consumed api_req_map/start.
        # Nodes 1+ wait for api_req_map/next (fires when user clicks 進撃 in-game).
        nav_event = None if i == 0 else "api_req_map/next"

        step("screen_change", "formation_select",
             f"→ 陣形選択 @ 节点{node}",
             description=f"进入节点{node}陣形选択" + (f"（等待 {nav_event}）" if nav_event else ""),
             wait_after_ms=400, wait_for=nav_event)
        step("click", "formation_select", f"选择陣形: {formation_name}",
             f"节点 {node}，选择{formation_name}（formation_{formation}）",
             element=f"formation_{formation}", duration_ms=400, wait_after_ms=800)

        step("info", "battle", f"节点 {node} 战斗",
             "等待战斗结算（由 poi WebSocket 事件驱动）",
             wait_after_ms=3000, wait_for="api_req_sortie/battleresult")
        step("screen_change", "battle_result", "→ 战斗结果", wait_after_ms=600)
        step("click", "battle_result", "确认战斗结果",
             element="next", duration_ms=400, wait_after_ms=800)

        if is_night:
            step("screen_change", "night_battle_select", "→ 夜戦突入？", wait_after_ms=400)
            step("click", "night_battle_select", "突入夜戦！",
                 f"节点 {node} 配置为夜战，点击「夜戦突入」",
                 element="night_battle", duration_ms=400, wait_after_ms=3500,
                 wait_for="api_req_battle_midnight/battle")
            step("info", "night_battle", f"节点 {node} 夜战",
                 "等待夜战结算",
                 wait_after_ms=3000, wait_for="api_req_sortie/battleresult")
            step("screen_change", "battle_result", "→ 夜战结果", wait_after_ms=600)
            step("click", "battle_result", "确认夜战结果",
                 element="next", duration_ms=400, wait_after_ms=800)

        # Hard gate: 大破 → 強制撤退（no exceptions, safety.py red line）
        step("check", "post_battle", "大破 → 強制撤退",
             "任何舰娘 HP≤25% → 强制撤退，绝不进撃",
             extra={"check_type": "no_taiha", "abort_on_fail": True,
                    "abort_action": "retreat", "critical": True}, wait_after_ms=200)

        if is_last:
            step("screen_change", "port", "→ 返回母港（完成）",
                 wait_after_ms=1000, wait_for="api_port/port")
        else:
            # Soft gate: 中破 警告（user confirms to continue）
            step("check", "post_battle", "中破确认",
                 "有中破舰（HP≤50%）建议撤退。请确认是否继续进撃",
                 extra={"check_type": "no_chuuha", "abort_on_fail": False}, wait_after_ms=200)
            step("click", "post_battle", "進撃！",
                 f"节点 {node} 通过，继续向 {nodes[i+1]} 进撃",
                 element="advance", duration_ms=400, wait_after_ms=1200)

    return steps

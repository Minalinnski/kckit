"""
Daily-routine advisor — the decision layer, runnable in SUGGEST (mock) mode.

Philosophy: reconcile, not playbook. There is no "position in a flow"; every
evaluation looks at the observed world (game state + current screen + scene
tree) and answers ONE question: "what is the single next click?"  Whoever
executes it — a human in mock mode, the executor later — the world changes,
and the next evaluation derives the next step. Restartable at any moment,
coexists with manual play, never depends on remembering its own actions.

v1 scope (low-risk daily domain): expedition collect → resupply → expedition
send → quest claim. Sortie intentionally absent.

Pure decision module: no I/O, no clicking. The simulator hosts the loop and
broadcasts suggestions to the panel.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

import yaml

from core.scene_perception import find_all, find_text, load_semantics

LAYOUT_PATH = Path(__file__).resolve().parent.parent / "config" / "screen_layout.yaml"

DEFAULT_CONFIG: dict[str, Any] = {
    # plan defaults mirror the commander's observed routine (2026-06-11:
    # fleets were running 9/11/37) — editable via POST /api/advisor
    "expedition": {"enabled": True, "plan": {"2": 9, "3": 11, "4": 37}},
    "supply": {"enabled": True},
    "quest_claim": {"enabled": True},
}

_layout_cache: Optional[dict] = None
_semantics_cache: Optional[dict] = None


def _layout() -> dict:
    global _layout_cache
    if _layout_cache is None:
        _layout_cache = yaml.safe_load(LAYOUT_PATH.read_text())
    return _layout_cache


def _sem() -> dict:
    global _semantics_cache
    if _semantics_cache is None:
        _semantics_cache = load_semantics()
    return _semantics_cache


# ── Target resolution ────────────────────────────────────────────────────────
# Semantic (live scene tree) first; hand-calibrated layout yaml as fallback.

def _node_target(n: dict, label: str) -> dict:
    return {"x": n["x"], "y": n["y"], "w": n["w"], "h": n["h"],
            "label": label, "source": "semantic"}


def _sem_target(tree: Optional[dict], screen: str, element: str,
                label: str, index: int = 0) -> Optional[dict]:
    if not tree:
        return None
    try:
        hits = find_all(tree["nodes"], screen, element, _sem())
    except KeyError:
        return None
    if index < len(hits):
        return _node_target(hits[index], label)
    return None


def _manual_target(screen: str, element: str, label: str,
                   rw: int = 1200, rh: int = 720) -> Optional[dict]:
    lay = _layout()
    sections = [lay.get("screens", {}).get(screen, {}).get("elements", {}),
                lay.get("common", {}).get("elements", {})]
    for elems in sections:
        e = elems.get(element)
        if e:
            w, h = e["w"] * rw, e["h"] * rh
            return {"x": round(e["x"] * rw - w / 2), "y": round(e["y"] * rh - h / 2),
                    "w": round(w), "h": round(h), "label": label, "source": "manual"}
    return None


def _suggest(instruction: str, reason: str, target: Optional[dict],
             goal: str) -> dict:
    return {"instruction": instruction, "reason": reason,
            "target": target, "goal": goal, "ts": time.time()}


def _goto_port(tree: Optional[dict], screen: Optional[str], reason: str) -> dict:
    target = (_sem_target(tree, "common", "nav_port", "母港")
              or _manual_target("common", "back_port", "母港"))
    return _suggest("回到母港（左侧『母港』按钮或左上徽章）", reason, target, "nav")


# ── State readers (raw plugin payload) ───────────────────────────────────────

def _fleet_expedition(state: dict, fid: int) -> tuple[int, int, float]:
    """(mission_state, expedition_id, return_ms) for a fleet."""
    f = (state.get("fleets") or {}).get(str(fid)) or {}
    m = f.get("api_mission") or [0, 0, 0, 0]
    return int(m[0] or 0), int(m[1] or 0), float(m[2] or 0)


def _fleet_needs_supply(state: dict, fid: int) -> bool:
    f = (state.get("fleets") or {}).get(str(fid)) or {}
    ships = state.get("ships") or {}
    for sid in f.get("api_ship") or []:
        if sid in (-1, None):
            continue
        s = ships.get(str(sid)) or {}
        master = s.get("$master") or {}
        if (s.get("api_fuel", 0) < master.get("api_fuel_max", 0)
                or s.get("api_bull", 0) < master.get("api_bull_max", 0)):
            return True
    return False


def _completable_quests(state: dict) -> list[dict]:
    out = []
    records = ((state.get("quests") or {}).get("records") or {})
    for qid, rec in records.items():
        if not isinstance(rec, dict):
            continue
        count = rec.get("count")
        required = rec.get("required")
        if count is None or required is None:
            # per-criterion records (e.g. battle_win) — completable only when
            # every sub-criterion is met
            subs = [v for v in rec.values()
                    if isinstance(v, dict) and "required" in v]
            if subs and all(v.get("count", 0) >= v["required"] for v in subs):
                out.append({"id": rec.get("id", qid)})
            continue
        if count >= required:
            out.append({"id": rec.get("id", qid)})
    return out


# ── The decision function ────────────────────────────────────────────────────

def next_step(state: Optional[dict], screen: Optional[str],
              tree: Optional[dict], config: dict) -> Optional[dict]:
    """The single next click toward the configured daily goals, or None."""
    if not state:
        return None
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    now_ms = time.time() * 1000
    exp_cfg = cfg.get("expedition") or {}
    plan = {int(k): int(v) for k, v in (exp_cfg.get("plan") or {}).items()}

    # 1. Collect returned expeditions (highest: fleets idle in limbo earn nothing)
    if exp_cfg.get("enabled"):
        for fid in sorted(plan):
            mstate, exp_id, ret_ms = _fleet_expedition(state, fid)
            if exp_id and ret_ms and now_ms >= ret_ms:
                if screen != "port":
                    return _goto_port(tree, screen,
                                      f"第{fid}舰队远征{exp_id}已到点，回港触发结算")
                return _suggest(
                    "点击画面（任意处）推进遠征結果结算",
                    f"第{fid}舰队远征{exp_id}已返回",
                    None, f"collect_exp_{fid}")

    # 2. Resupply fleets that are home and thirsty
    if (cfg.get("supply") or {}).get("enabled"):
        for fid in sorted(plan):
            mstate, exp_id, _ = _fleet_expedition(state, fid)
            if exp_id:        # still out
                continue
            if not _fleet_needs_supply(state, fid):
                continue
            if screen == "supply":
                target = (_sem_target(tree, "supply", "supply_all_btn", "まとめて補給")
                          or _sem_target(tree, "supply", "fleet_supply_all_btn", "艦隊全補給"))
                return _suggest(
                    f"切到第{fid}舰队页签，然后点『まとめて補給』",
                    f"第{fid}舰队油弹未满（远征前置）",
                    target, f"supply_{fid}")
            if screen == "port":
                return _suggest(
                    "点击母港转轮『補給』",
                    f"第{fid}舰队需要补给",
                    _manual_target("port", "supply_nav", "補給"),
                    f"supply_{fid}")
            target = _sem_target(tree, "common", "nav_supply", "補給")
            if target:
                return _suggest("点击左侧导航『補給』",
                                f"第{fid}舰队需要补给", target, f"supply_{fid}")
            return _goto_port(tree, screen, "需要补给，先回港")

    # 3. Send idle fleets on their planned expeditions
    if exp_cfg.get("enabled"):
        for fid in sorted(plan):
            mstate, exp_id, _ = _fleet_expedition(state, fid)
            if exp_id:
                continue
            if _fleet_needs_supply(state, fid):
                continue      # supply branch above will fire next round
            exp = plan[fid]
            world = (exp - 1) // 8 + 1
            if screen == "expedition_select":
                row = _expedition_row_target(tree, exp)
                if row:
                    return _suggest(
                        f"点击远征 {exp:02d} 行 → 右侧选第{fid}舰队 → 点『遠征開始』",
                        f"第{fid}舰队空闲，计划跑远征{exp}",
                        row, f"send_{fid}_{exp}")
                tab = _sem_target(tree, "expedition_select", "world_tabs",
                                  f"第{world}海域", index=world - 1)
                return _suggest(
                    f"切到第{world}海域页签（远征{exp}在那里）",
                    f"当前列表没有远征{exp:02d}",
                    tab, f"send_{fid}_{exp}")
            if screen == "sortie_type":
                return _suggest(
                    "点击『遠征』徽章",
                    f"去远征选择画面（第{fid}舰队→远征{exp}）",
                    _sem_target(tree, "sortie_type", "expedition_btn", "遠征")
                    or _manual_target("sortie_type", "expedition_btn", "遠征"),
                    f"send_{fid}_{exp}")
            if screen == "port":
                return _suggest(
                    "点击母港转轮『出撃』",
                    f"第{fid}舰队空闲 → 派远征{exp}",
                    _manual_target("port", "sortie_nav", "出撃"),
                    f"send_{fid}_{exp}")
            return _goto_port(tree, screen, f"要派远征{exp}，先回港")

    # 4. Claim completed quests
    if (cfg.get("quest_claim") or {}).get("enabled"):
        done = _completable_quests(state)
        if done:
            qid = done[0]["id"]
            if screen == "quest_list":
                return _suggest(
                    f"点击已达成的任务行（ID {qid}）领取，弹窗选『はい』",
                    f"{len(done)} 个任务可领取",
                    None, f"claim_{qid}")
            return _suggest(
                "点击顶部『任務』页签",
                f"{len(done)} 个任务可领取",
                _sem_target(tree, "common", "tab_quest", "任務"),
                f"claim_{qid}")

    # Nothing actionable — report the next wakeup so the panel shows liveness
    pending = []
    for fid in sorted(plan):
        _, exp_id, ret_ms = _fleet_expedition(state, fid)
        if exp_id and ret_ms > now_ms:
            pending.append((ret_ms, fid, exp_id))
    if pending:
        ret_ms, fid, exp_id = min(pending)
        mins = int((ret_ms - now_ms) / 60000)
        return _suggest(
            f"无事可做 — 等待第{fid}舰队远征{exp_id}返回（约{mins}分钟）",
            "所有目标已收敛", None, "wait")
    return None


def _expedition_row_target(tree: Optional[dict], exp: int) -> Optional[dict]:
    """Row containing the expedition id text ('05'), if visible."""
    if not tree:
        return None
    nodes = tree["nodes"]
    try:
        rows = find_all(nodes, "expedition_select", "expedition_rows", _sem())
    except KeyError:
        return None
    for txt_node in find_text(nodes, f"{exp:02d}"):
        for r in rows:
            if (r["x"] <= txt_node["x"] <= r["x"] + r["w"]
                    and r["y"] <= txt_node["y"] <= r["y"] + r["h"]):
                return _node_target(r, f"遠征{exp:02d}")
    return None

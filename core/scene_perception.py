"""
Perception layer v2 — screen classification & element lookup from the live
PIXI scene tree (see tools/scene_dump.py for how the tree is captured).

Two primitives:

  classify_screen(nodes)   → which screen/overlay is showing, from the
                             histogram of visible atlas texture prefixes.
                             KC2's atlases are named after their screens
                             (sally_top = 出撃選択, supply_main = 補給 …),
                             so the mapping is mostly mechanical.

  find_element(nodes, screen, name)
                           → live (rx, ry) click center for a semantic
                             element, resolved through the atlas semantic
                             dictionary (data/ui_atlas/semantics.yaml).
                             Replaces hand-measured screen_layout.yaml
                             coordinates with runtime ground truth.

PIXI.Text nodes carry their text content in the dump (`txt`), so screens can
also be confirmed by literal on-screen strings (e.g. 「出撃選択」) and live
values (HP "72/72") can be read without OCR.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import yaml

_SEMANTICS_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "ui_atlas" / "semantics.yaml"
)

# Visible atlas prefix → screen name. Prefixes come from texture cache ids in
# the scene dump ("sally_top_4" → "sally_top"). Order matters: more specific
# screens (overlays, sub-screens) are checked before generic ones, so e.g. a
# formation-select overlay during sortie wins over the map atlas behind it.
ATLAS_SCREEN_MAP: list[tuple[str, str]] = [
    # Decision dialogs before everything: they overlay battle/map screens.
    # The dialog TYPE (離脱判定/夜戦突入/進撃撤退…) is NOT in the fingerprint —
    # read it from the visible PIXI.Text nodes (find_text).
    ("map_decision", "map_decision"),
    # battle first: the battle HUD shows formation icons (sally_jin frames),
    # but formation_select never shows battle sprites
    ("battle_result", "battle_result"),
    ("battle_telop", "battle"),
    ("battle_main", "battle"),
    ("prac_main", "practice_battle"),
    ("sally_jin", "formation_select"),
    ("sally_top", "sortie_type"),
    ("sally_sortie", "sortie_world"),
    ("sally_map", "sortie_map"),
    ("sally_expedition", "expedition_select"),
    ("sally_practice", "practice"),
    ("map_main", "sortie_map"),
    ("sally_airbase", "airbase"),
    ("supply_main", "supply"),
    ("arsenal_main", "factory"),
    ("duty_main", "quest_list"),
    ("organize_main", "hensei"),
    ("remodel_main", "equipment"),
    ("repair_main", "repair"),
    ("port_ringmenu", "port"),
    ("port_main", "port"),
    ("title_main", "title"),
]

# Atlases that appear on many screens and carry no screen identity.
_NEUTRAL_PREFIXES = (
    "common_", "text_", "port_sidemenu", "port_skin", "sally_common",
)


def _atlas_prefix(tex: str) -> Optional[str]:
    """'sally_top_4' → 'sally_top'; non-atlas ids (URLs) → None."""
    if not tex or tex.startswith("@") or "/" in tex or "." in tex:
        return None  # URLs and raw image filenames are not atlas frames
    head, _, tail = tex.rpartition("_")
    return head if head and tail.isdigit() else tex


def classify_screen(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify the current screen from scene-dump nodes.

    Returns {screen, confidence, prefixes} where prefixes is the histogram of
    visible atlas prefixes (useful for diagnosing unknown screens).
    """
    histogram: dict[str, int] = {}
    for n in nodes:
        prefix = _atlas_prefix(n.get("t") or "")
        if prefix and not prefix.startswith(_NEUTRAL_PREFIXES):
            histogram[prefix] = histogram.get(prefix, 0) + 1

    for prefix, screen in ATLAS_SCREEN_MAP:
        hits = sum(c for p, c in histogram.items() if p.startswith(prefix))
        if hits:
            # 2+ distinct sprites from the screen's own atlas → confident
            confidence = 0.95 if hits >= 2 else 0.8
            return {"screen": screen, "confidence": confidence,
                    "prefixes": histogram}
    return {"screen": None, "confidence": 0.0, "prefixes": histogram}


def load_semantics() -> dict[str, Any]:
    return yaml.safe_load(_SEMANTICS_PATH.read_text()) or {}


def find_all(
    nodes: list[dict[str, Any]],
    screen: str,
    element: str,
    semantics: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """All live nodes for a semantic element, in top-to-bottom order.

    Multi-instance elements (e.g. repair.dock_slot ×4) come back sorted by y.
    Elements missing from the screen's section fall back to the 'common'
    section (left nav sidebar, top tab bar).
    """
    sem = semantics if semantics is not None else load_semantics()
    entry = (sem.get(screen) or {}).get(element) or (sem.get("common") or {}).get(element)
    if not entry:
        raise KeyError(f"no semantics for {screen}.{element} (nor common.{element})")
    frames = set(entry.get("frames") or [])
    hits = [n for n in nodes if n.get("t") in frames]
    # Geometry matcher for texture-less interactive containers (e.g. the
    # expedition list rows): geom: {w: [min,max], h: [min,max]} matches
    # interactive nodes by size. Combines with frames (union).
    geom = entry.get("geom")
    if geom:
        wlo, whi = geom.get("w", [0, 10**6])
        hlo, hhi = geom.get("h", [0, 10**6])
        hits += [n for n in nodes
                 if n.get("i") and n not in hits
                 and wlo <= n.get("w", 0) <= whi and hlo <= n.get("h", 0) <= hhi]
    hits.sort(key=lambda n: (n.get("y", 0), n.get("x", 0)))
    # Collapse co-located stacks: two-state buttons keep both state sprites
    # mounted at the same spot (e.g. factory 解体 normal+hover) — one click
    # target, not two.
    deduped: list[dict[str, Any]] = []
    for n in hits:
        if any(abs(n.get("x", 0) - m.get("x", 0)) <= 6
               and abs(n.get("y", 0) - m.get("y", 0)) <= 6 for m in deduped):
            continue
        deduped.append(n)
    return deduped


def find_element(
    nodes: list[dict[str, Any]],
    screen: str,
    element: str,
    semantics: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Resolve a semantic element to its live on-screen node.

    Returns the dump node (with rx/ry click center) for the first matching
    node, or None if the element is not currently on screen. Falls back to
    the 'common' section for shared elements (nav sidebar, tab bar).
    """
    hits = find_all(nodes, screen, element, semantics)
    return hits[0] if hits else None


def find_text(nodes: list[dict[str, Any]], needle: str) -> list[dict[str, Any]]:
    """All visible PIXI.Text nodes whose content contains `needle`."""
    return [n for n in nodes if needle in (n.get("txt") or "")]


def click_point(node: dict[str, Any]) -> tuple[float, float]:
    """Safe click coordinates (renderer px) for a scene node.

    Always the bounds center: for circular buttons (semantics `shape: circle`)
    the center is inside the disc, so rect-vs-circle never causes a miss —
    the risk only exists when clicking corners of the bounding box. Adjacent
    circular buttons with overlapping bounds (port wheel) also resolve
    correctly, because each disc's center lies in its own unique region.
    """
    return (node["x"] + node["w"] / 2, node["y"] + node["h"] / 2)

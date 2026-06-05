#!/usr/bin/env python3
"""
Find UI elements that appear consistently across ALL KC screenshots.
Strategy:
  1. Run Canny on every screenshot, keep only STRONG edges (high threshold).
  2. Bitwise-AND all edge maps → pixels that are edges in EVERY screen = shared chrome.
  3. Also produce zoomed crops of:
       - Top strip  (y=0..25%)  → header / React nav bar
       - Left strip (x=0..20%)  → left sidebar / 母港 button
  4. Run HoughLinesP on the common-edge map → exact boundary lines.

Output: temp/common_ui/
  common_edges.png        — edges present in all screens
  common_annotated.png    — hough lines drawn on first screenshot
  top_strip_<screen>.png  — zoomed top 25% per screen
  left_strip_<screen>.png — zoomed left 20% per screen
"""
from __future__ import annotations
from pathlib import Path
import cv2
import numpy as np

ROOT = Path(__file__).parent.parent
SRC  = ROOT / "temp" / "UI_screenshots"
OUT  = ROOT / "temp" / "common_ui"
OUT.mkdir(parents=True, exist_ok=True)

SCREEN_MAP = {
    "母港":                   "port",
    "编成（第一舰队）":       "hensei",
    "编成（选择其他舰）":     "hensei_ship_select",
    "入渠":                   "repair",
    "工厂":                   "factory",
    "改装（其他舰列表）":     "equipment",
    "改装（第一舰队第一只）": "equipment_detail",
    "任务":                   "quest_list",
    "出击":                   "sortie_type",
    "出击-出击（海域选择）":  "sortie_world",
    "出击-远征":             "expedition_select",
}


def strong_edges(img_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    filt = cv2.bilateralFilter(gray, 7, 60, 60)
    edges = cv2.Canny(filt, 80, 200)
    # dilate slightly so nearby edges connect
    k = np.ones((2, 2), np.uint8)
    return cv2.dilate(edges, k, iterations=1)


def main() -> None:
    paths = sorted(SRC.glob("*.png"))
    if not paths:
        print("[error] no screenshots found")
        return

    imgs: list[np.ndarray] = []
    edge_maps: list[np.ndarray] = []
    keys: list[str] = []

    for p in paths:
        stem = p.stem
        key = SCREEN_MAP.get(stem, stem)
        img = cv2.imread(str(p))
        if img is None:
            continue
        imgs.append(img)
        edge_maps.append(strong_edges(img))
        keys.append(key)
        print(f"  loaded {key}")

    if not edge_maps:
        print("[error] could not load any image")
        return

    h, w = edge_maps[0].shape

    # ── 1. Common edges: AND of all maps ─────────────────────────────────────
    common = edge_maps[0].copy()
    for em in edge_maps[1:]:
        common = cv2.bitwise_and(common, em)

    cv2.imwrite(str(OUT / "common_edges.png"), cv2.bitwise_not(common))

    # ── 2. Hough lines on common edges ────────────────────────────────────────
    lines = cv2.HoughLinesP(common, 1, np.pi / 180,
                            threshold=40, minLineLength=int(w * 0.08), maxLineGap=10)
    ref = imgs[0].copy()
    if lines is not None:
        for ln in lines:
            x1, y1, x2, y2 = ln[0]
            length = np.hypot(x2 - x1, y2 - y1)
            # color by orientation
            if abs(y2 - y1) < 8:
                color = (0, 200, 255)   # horizontal — cyan
            elif abs(x2 - x1) < 8:
                color = (80, 255, 80)   # vertical — green
            else:
                color = (200, 80, 255)  # diagonal — pink (skip?)
                continue
            cv2.line(ref, (x1, y1), (x2, y2), color, 2)
            # label with fraction coords
            mx, my = (x1 + x2) // 2, (y1 + y2) // 2
            lbl = f"({mx/w:.2f},{my/h:.2f})"
            cv2.putText(ref, lbl, (mx + 3, my - 3),
                        cv2.FONT_HERSHEY_PLAIN, 0.8, color, 1, cv2.LINE_AA)
    cv2.imwrite(str(OUT / "common_hough.png"), ref)
    print(f"  common hough lines: {len(lines) if lines is not None else 0}")

    # ── 3. Zoomed crops: top 25% and left 20% of EVERY screen ─────────────────
    TOP_FRAC  = 0.25
    LEFT_FRAC = 0.20
    top_h  = int(h * TOP_FRAC)
    left_w = int(w * LEFT_FRAC)

    for img, key in zip(imgs, keys):
        # Top strip — 3x zoom
        top_strip = img[:top_h, :]
        top_big   = cv2.resize(top_strip, (w, top_h * 3), interpolation=cv2.INTER_LINEAR)
        # Draw horizontal grid lines every 0.05 of original h
        for yf in np.arange(0.05, TOP_FRAC, 0.05):
            yp = int(yf / TOP_FRAC * top_h * 3)
            cv2.line(top_big, (0, yp), (w, yp), (0, 220, 220), 1)
            cv2.putText(top_big, f"y={yf:.2f}", (4, yp - 3),
                        cv2.FONT_HERSHEY_PLAIN, 0.9, (0, 220, 220), 1)
        # vertical grid every 0.05 of w
        for xf in np.arange(0.05, 1.0, 0.05):
            xp = int(xf * w)
            cv2.line(top_big, (xp, 0), (xp, top_h * 3), (200, 200, 0), 1)
            cv2.putText(top_big, f"{xf:.2f}", (xp + 2, 14),
                        cv2.FONT_HERSHEY_PLAIN, 0.7, (200, 200, 0), 1)
        cv2.imwrite(str(OUT / f"top_{key}.png"), top_big)

        # Left strip — 3x zoom on width
        left_strip = img[:, :left_w]
        left_big   = cv2.resize(left_strip, (left_w * 3, h), interpolation=cv2.INTER_LINEAR)
        for yf in np.arange(0.05, 1.0, 0.05):
            yp = int(yf * h)
            cv2.line(left_big, (0, yp), (left_w * 3, yp), (0, 220, 220), 1)
            cv2.putText(left_big, f"y={yf:.2f}", (4, yp - 3),
                        cv2.FONT_HERSHEY_PLAIN, 0.9, (0, 220, 220), 1)
        for xf in np.arange(0.02, LEFT_FRAC, 0.02):
            xp = int(xf / LEFT_FRAC * left_w * 3)
            cv2.line(left_big, (xp, 0), (xp, h), (200, 200, 0), 1)
            cv2.putText(left_big, f"x={xf:.2f}", (xp + 2, 20),
                        cv2.FONT_HERSHEY_PLAIN, 0.8, (200, 200, 0), 1)
        cv2.imwrite(str(OUT / f"left_{key}.png"), left_big)

    print(f"\nOutput in {OUT}")
    print("  common_edges.png   — edges in ALL screens")
    print("  common_hough.png   — hough lines on first screenshot")
    print("  top_<screen>.png   — top 25% zoomed 3x")
    print("  left_<screen>.png  — left 20% zoomed 3x")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Draw the calibrated chrome elements on all screenshots with precise labels.
Uses values derived from calibrate_chrome.py edge analysis:
  - Teal circle (母港 button): x=0..0.080, y=0..0.1456
  - Resource bar: y=0..0.0591
  - poi React nav bar: y=0.0591..0.1456
  - poi nav buttons: cy=0.1032, first at cx=0.200, second cx=0.400
  - Left sidebar right edge: x=0.0799
  - Content start y: 0.1456
Output: temp/chrome_overlay/<screen>_chrome.png
"""
from __future__ import annotations
from pathlib import Path
import cv2
import numpy as np

ROOT = Path(__file__).parent.parent
SRC  = ROOT / "temp" / "UI_screenshots"
OUT  = ROOT / "temp" / "chrome_overlay"
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
    "出击-远征":              "expedition_select",
}

# ── Calibrated chrome measurements ───────────────────────────────────────────
# All values are fractions of screenshot size (889×533)
# These ARE the same as KC canvas fractions since screenshot = canvas at ~1.11x

CHROME = {
    # Header horizontal bands
    "resource_bar_y1":  0.0591,   # bottom of KC resource bar
    "nav_bar_y1":       0.1456,   # bottom of poi React nav bar = KC content start

    # Left chrome: teal circular KC crest (母港 button)
    "left_chrome_x1":   0.0799,   # right edge of teal circle

    # poi React nav bar buttons (y_center = 0.1032)
    "nav_cy":           0.1032,
    "nav_btn_h":        0.030,
    "nav_btn_w":        0.074,
    "nav_btn_xs": [0.200, 0.399],  # confirmed from all-screen edge detection

    # 母港 button (teal circle)
    "back_port_cx":     0.040,
    "back_port_cy":     0.073,    # center of full header height
    "back_port_w":      0.080,
    "back_port_h":      0.146,
}


def draw_rect_frac(img, cx, cy, w, h, color, label="", thickness=2):
    H, W = img.shape[:2]
    x0 = int((cx - w/2) * W)
    y0 = int((cy - h/2) * H)
    x1 = int((cx + w/2) * W)
    y1 = int((cy + h/2) * H)
    cv2.rectangle(img, (x0, y0), (x1, y1), color, thickness)
    if label:
        cv2.putText(img, label, (x0 + 2, y0 + 13),
                    cv2.FONT_HERSHEY_PLAIN, 0.85, color, 1, cv2.LINE_AA)


def draw_hline(img, y_frac, color, label=""):
    H, W = img.shape[:2]
    y = int(y_frac * H)
    cv2.line(img, (0, y), (W, y), color, 1)
    if label:
        cv2.putText(img, label, (W - 160, y - 3),
                    cv2.FONT_HERSHEY_PLAIN, 0.85, color, 1, cv2.LINE_AA)


def draw_vline(img, x_frac, color, label=""):
    H, W = img.shape[:2]
    x = int(x_frac * W)
    cv2.line(img, (x, 0), (x, H), color, 1)
    if label:
        cv2.putText(img, label, (x + 2, 20),
                    cv2.FONT_HERSHEY_PLAIN, 0.85, color, 1, cv2.LINE_AA)


def annotate(img_path: Path, key: str) -> None:
    img = cv2.imread(str(img_path))
    if img is None:
        return
    H, W = img.shape[:2]
    vis = img.copy()

    # ── Header bands ─────────────────────────────────────────────────────────
    # Resource bar bottom (cyan)
    draw_hline(vis, CHROME["resource_bar_y1"], (0, 220, 220), f"y={CHROME['resource_bar_y1']:.4f} KC_resource_bar_end")

    # poi nav bar bottom = content start (bright green)
    draw_hline(vis, CHROME["nav_bar_y1"], (0, 255, 0), f"y={CHROME['nav_bar_y1']:.4f} content_start")

    # Shade resource bar (light blue tint)
    y0_r = 0
    y1_r = int(CHROME["resource_bar_y1"] * H)
    overlay = vis.copy()
    cv2.rectangle(overlay, (0, y0_r), (W, y1_r), (100, 200, 255), -1)
    cv2.addWeighted(overlay, 0.12, vis, 0.88, 0, vis)

    # Shade poi nav bar (light yellow tint)
    y0_n = int(CHROME["resource_bar_y1"] * H)
    y1_n = int(CHROME["nav_bar_y1"] * H)
    overlay = vis.copy()
    cv2.rectangle(overlay, (0, y0_n), (W, y1_n), (0, 200, 255), -1)
    cv2.addWeighted(overlay, 0.12, vis, 0.88, 0, vis)

    # ── Left chrome border ────────────────────────────────────────────────────
    draw_vline(vis, CHROME["left_chrome_x1"], (100, 255, 80), f"x={CHROME['left_chrome_x1']:.4f} chrome_right")

    # ── 母港 button (teal circle region) ──────────────────────────────────────
    draw_rect_frac(vis,
                   CHROME["back_port_cx"], CHROME["back_port_cy"],
                   CHROME["back_port_w"], CHROME["back_port_h"],
                   (0, 200, 255), "母港 back_port", thickness=3)

    # ── poi React nav bar detected buttons ────────────────────────────────────
    for i, cx in enumerate(CHROME["nav_btn_xs"]):
        draw_rect_frac(vis, cx, CHROME["nav_cy"],
                       CHROME["nav_btn_w"], CHROME["nav_btn_h"],
                       (255, 160, 0), f"poi_btn_{i+1}")

    # Content area start marker
    y_content = int(CHROME["nav_bar_y1"] * H)
    cv2.putText(vis, "KC CONTENT AREA START →", (int(CHROME["left_chrome_x1"] * W) + 5, y_content + 14),
                cv2.FONT_HERSHEY_PLAIN, 0.9, (0, 255, 0), 1, cv2.LINE_AA)

    out_path = OUT / f"{key}_chrome.png"
    cv2.imwrite(str(out_path), vis)
    print(f"  {key}: {out_path.name}")


def main():
    for p in sorted(SRC.glob("*.png")):
        key = SCREEN_MAP.get(p.stem, p.stem)
        annotate(p, key)
    print(f"\nOutput: {OUT}")


if __name__ == "__main__":
    main()

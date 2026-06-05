#!/usr/bin/env python3
"""
Overlay a coordinate grid + HoughLines row detection on KC screenshots.
Output: temp/grid/<screen>_grid.png  — 0.05-spaced grid with fraction labels
        temp/grid/<screen>_hlines.png — detected horizontal lines (row separators)

Usage:
    python tools/coord_grid.py [screen_name_or_stem]
    python tools/coord_grid.py 母港
    python tools/coord_grid.py          # all screens
"""
from __future__ import annotations
import sys
from pathlib import Path
import cv2
import numpy as np

ROOT = Path(__file__).parent.parent
SCREENSHOTS_DIR = ROOT / "temp" / "UI_screenshots"
OUT_DIR = ROOT / "temp" / "grid"

SCREEN_MAP: dict[str, str] = {
    "母港":                   "port",
    "编成（第一舰队）":       "hensei",
    "编成（选择其他舰）":     "hensei_ship_select",
    "补给":                  "supply",
    "入渠":                  "repair",
    "工厂":                  "factory",
    "改装（其他舰列表）":     "equipment",
    "改装（第一舰队第一只）": "equipment_detail",
    "任务":                  "quest_list",
    "出击":                  "sortie_type",
    "出击-出击（海域选择）":  "sortie_world",
    "出击-远征":             "expedition_select",
}

# Grid step — every STEP fraction a line + label is drawn
STEP = 0.05


def draw_grid(img: np.ndarray) -> np.ndarray:
    """Draw fractional coordinate grid on img copy."""
    h, w = img.shape[:2]
    vis = img.copy()

    # Minor grid (0.05 spacing) — semi-transparent
    overlay = vis.copy()
    x_pos = np.arange(STEP, 1.0, STEP)
    y_pos = np.arange(STEP, 1.0, STEP)

    # Draw minor grid in light gray
    for xf in x_pos:
        xi = int(xf * w)
        cv2.line(overlay, (xi, 0), (xi, h), (180, 180, 180), 1)
    for yf in y_pos:
        yi = int(yf * h)
        cv2.line(overlay, (0, yi), (w, yi), (180, 180, 180), 1)

    cv2.addWeighted(overlay, 0.4, vis, 0.6, 0, vis)

    # Major grid (0.1 spacing) — bright + labels
    for xf in np.arange(0.1, 1.0, 0.1):
        xi = int(xf * w)
        cv2.line(vis, (xi, 0), (xi, h), (0, 255, 255), 1)
        label = f"{xf:.1f}"
        cv2.putText(vis, label, (xi + 2, 14),
                    cv2.FONT_HERSHEY_PLAIN, 0.9, (0, 255, 255), 1, cv2.LINE_AA)

    for yf in np.arange(0.1, 1.0, 0.1):
        yi = int(yf * h)
        cv2.line(vis, (0, yi), (w, yi), (0, 255, 255), 1)
        label = f"{yf:.1f}"
        cv2.putText(vis, label, (2, yi - 2),
                    cv2.FONT_HERSHEY_PLAIN, 0.9, (0, 255, 255), 1, cv2.LINE_AA)

    return vis


def detect_hlines(img: np.ndarray, min_line_length_frac=0.25) -> list[int]:
    """Detect significant horizontal lines using HoughLinesP. Returns sorted y-pixel positions."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    filt = cv2.bilateralFilter(gray, 5, 40, 40)
    edges = cv2.Canny(filt, 40, 120)

    min_len = int(w * min_line_length_frac)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60,
                            minLineLength=min_len, maxLineGap=20)
    if lines is None:
        return []

    y_vals: list[int] = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if abs(y2 - y1) < 5:  # nearly horizontal
            y_vals.append((y1 + y2) // 2)

    # cluster nearby y values (within 10px → same line)
    y_vals.sort()
    clusters: list[int] = []
    for y in y_vals:
        if not clusters or y - clusters[-1] > 10:
            clusters.append(y)
        else:
            # update cluster to mean
            clusters[-1] = (clusters[-1] + y) // 2

    return clusters


def draw_hlines(img: np.ndarray, y_vals: list[int], h: int, w: int) -> np.ndarray:
    vis = img.copy()
    for y in y_vals:
        frac = y / h
        cv2.line(vis, (0, y), (w, y), (0, 128, 255), 2)
        cv2.putText(vis, f"y={frac:.3f} (px {y})", (5, y - 4),
                    cv2.FONT_HERSHEY_PLAIN, 0.9, (0, 200, 255), 1, cv2.LINE_AA)
    return vis


def detect_vlines(img: np.ndarray, min_line_length_frac=0.15) -> list[int]:
    """Detect significant vertical lines. Returns sorted x-pixel positions."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    filt = cv2.bilateralFilter(gray, 5, 40, 40)
    edges = cv2.Canny(filt, 40, 120)

    min_len = int(h * min_line_length_frac)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180, threshold=60,
                            minLineLength=min_len, maxLineGap=20)
    if lines is None:
        return []

    x_vals: list[int] = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        if abs(x2 - x1) < 5:  # nearly vertical
            x_vals.append((x1 + x2) // 2)

    x_vals.sort()
    clusters: list[int] = []
    for x in x_vals:
        if not clusters or x - clusters[-1] > 10:
            clusters.append(x)
        else:
            clusters[-1] = (clusters[-1] + x) // 2

    return clusters


def process(img_path: Path, screen_key: str) -> None:
    img = cv2.imread(str(img_path))
    if img is None:
        print(f"  [error] cannot read {img_path.name}")
        return
    h, w = img.shape[:2]

    # Grid overlay
    grid_img = draw_grid(img)
    cv2.imwrite(str(OUT_DIR / f"{screen_key}_grid.png"), grid_img)

    # Horizontal line detection
    h_ys = detect_hlines(img)
    v_xs = detect_vlines(img)

    hl_img = draw_grid(img)  # start from grid
    hl_img = draw_hlines(hl_img, h_ys, h, w)

    # Also mark vertical lines in green
    for x in v_xs:
        frac = x / w
        cv2.line(hl_img, (x, 0), (x, h), (0, 230, 80), 2)
        cv2.putText(hl_img, f"x={frac:.3f}", (x + 2, 30),
                    cv2.FONT_HERSHEY_PLAIN, 0.8, (0, 230, 80), 1, cv2.LINE_AA)

    cv2.imwrite(str(OUT_DIR / f"{screen_key}_lines.png"), hl_img)

    # Print detected positions
    print(f"  h-lines (y frac): {[round(y/h, 3) for y in h_ys]}")
    print(f"  v-lines (x frac): {[round(x/w, 3) for x in v_xs]}")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    filter_arg = sys.argv[1] if len(sys.argv) > 1 else None

    for img_path in sorted(SCREENSHOTS_DIR.glob("*.png")):
        stem = img_path.stem
        screen_key = SCREEN_MAP.get(stem)
        if not screen_key:
            print(f"[skip] no mapping for '{stem}'")
            continue
        if filter_arg and filter_arg not in (stem, screen_key):
            continue
        print(f"{img_path.name} → {screen_key}")
        process(img_path, screen_key)

    print("Done. Check temp/grid/")


if __name__ == "__main__":
    main()

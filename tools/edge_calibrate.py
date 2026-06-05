#!/usr/bin/env python3
"""
Edge-detection based UI element finder for KanColle screenshots.
Uses OpenCV Canny + contour detection to find precise element boundaries.

Usage:
    python tools/edge_calibrate.py [screen_name]
    python tools/edge_calibrate.py 母港
    python tools/edge_calibrate.py          # process all

Output: temp/edge/<screen>_edges.png   (raw Canny)
        temp/edge/<screen>_rects.png   (filtered rectangles, colored by size tier)
        temp/edge/<screen>_rects.json  (all detected rect coordinates as fractions)
"""
from __future__ import annotations
import sys
import json
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).parent.parent
SCREENSHOTS_DIR = ROOT / "temp" / "UI_screenshots"
OUT_DIR = ROOT / "temp" / "edge"

# filename stem → YAML screen key
SCREEN_MAP: dict[str, str] = {
    "母港":                 "port",
    "编成（第一舰队）":     "hensei",
    "编成（选择其他舰）":   "hensei_ship_select",
    "补给":                "supply",
    "入渠":                "repair",
    "工厂":                "factory",
    "改装（其他舰列表）":   "equipment",
    "改装（第一舰队第一只）": "equipment_detail",
    "任务":                "quest_list",
    "出击":                "sortie_type",
    "出击-出击（海域选择）": "sortie_world",
    "出击-远征":           "expedition_select",
}


def detect_rects(img_bgr: np.ndarray, min_area_frac=0.002, max_area_frac=0.55,
                 canny_lo=40, canny_hi=120) -> list[dict]:
    """Return list of {cx,cy,w,h} dicts as fractions of image size, sorted by area desc."""
    h, w = img_bgr.shape[:2]
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    # Bilateral filter to reduce noise while preserving edges
    filt = cv2.bilateralFilter(gray, 7, 50, 50)

    # Canny
    edges = cv2.Canny(filt, canny_lo, canny_hi)

    # Close small gaps so panel outlines form closed shapes
    k = np.ones((4, 4), np.uint8)
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    total = h * w
    results: list[dict] = []
    seen: set[tuple] = set()

    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < total * min_area_frac or area > total * max_area_frac:
            continue

        x, y, bw, bh = cv2.boundingRect(cnt)

        # deduplicate very similar rects (within 5px on each side)
        key = (x // 5, y // 5, bw // 5, bh // 5)
        if key in seen:
            continue
        seen.add(key)

        aspect = bw / bh if bh else 0
        if not (0.08 < aspect < 25):
            continue

        cx = (x + bw / 2) / w
        cy = (y + bh / 2) / h
        results.append({
            "cx": round(cx, 3),
            "cy": round(cy, 3),
            "w":  round(bw / w, 3),
            "h":  round(bh / h, 3),
            "area_frac": round(area / total, 4),
            "px": (x, y, bw, bh),
        })

    results.sort(key=lambda r: -r["area_frac"])
    return results


def _tier_color(area_frac: float) -> tuple[int, int, int]:
    """Color-code by element size tier (BGR)."""
    if area_frac > 0.08:
        return (50, 50, 220)    # large panels — red
    elif area_frac > 0.02:
        return (50, 180, 50)    # medium (ship rows, docks) — green
    elif area_frac > 0.006:
        return (200, 130, 0)    # small (buttons) — blue
    else:
        return (180, 180, 60)   # tiny — cyan


def process(img_path: Path, screen_key: str) -> None:
    img = cv2.imread(str(img_path))
    if img is None:
        print(f"  [error] could not read {img_path}")
        return

    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    filt = cv2.bilateralFilter(gray, 7, 50, 50)
    edges = cv2.Canny(filt, 40, 120)

    # Save raw edge image (white on black is confusing — invert)
    edges_inv = cv2.bitwise_not(edges)
    cv2.imwrite(str(OUT_DIR / f"{screen_key}_edges.png"), edges_inv)

    rects = detect_rects(img)

    # Draw all rects on a copy
    vis = img.copy()
    for r in rects:
        x, y, bw, bh = r["px"]
        color = _tier_color(r["area_frac"])
        cv2.rectangle(vis, (x, y), (x + bw, y + bh), color, 2)
        label = f"{r['cx']:.2f},{r['cy']:.2f} {r['w']:.2f}x{r['h']:.2f}"
        cv2.putText(vis, label, (x + 2, y + 12),
                    cv2.FONT_HERSHEY_PLAIN, 0.75, color, 1, cv2.LINE_AA)

    cv2.imwrite(str(OUT_DIR / f"{screen_key}_rects.png"), vis)

    # Save JSON (drop px)
    clean = [{k: v for k, v in r.items() if k != "px"} for r in rects]
    (OUT_DIR / f"{screen_key}_rects.json").write_text(
        json.dumps(clean, indent=2, ensure_ascii=False))

    print(f"  {screen_key}: {len(rects)} rects → {OUT_DIR / (screen_key + '_rects.png')}")


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    filter_name = sys.argv[1] if len(sys.argv) > 1 else None

    for img_path in sorted(SCREENSHOTS_DIR.glob("*.png")):
        stem = img_path.stem
        screen_key = SCREEN_MAP.get(stem)
        if not screen_key:
            print(f"[skip] no mapping for '{stem}'")
            continue
        if filter_name and filter_name not in (stem, screen_key):
            continue
        print(f"{img_path.name} → {screen_key}")
        process(img_path, screen_key)

    print("Done.")


if __name__ == "__main__":
    main()

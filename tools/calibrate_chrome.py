#!/usr/bin/env python3
"""
Precisely calibrate the common KC chrome: top header bands and left sidebar.

Strategy:
  1. Load all screenshots and AND-combine their Canny edges (same as find_common_ui).
  2. On the common edge map, scan for the strongest horizontal lines in the top 25%
     — these are header row separators.
  3. Scan for the strongest vertical lines in the left 20%
     — this gives the left sidebar right edge.
  4. For each screenshot, do template-matching style analysis within the top strip
     to find the 母港 button (large left-most button in nav bar).
  5. Print a summary table and emit an updated YAML chrome block.

Output: temp/chrome_cal/
  chrome_lines.png  — detected structure lines on first screenshot
  chrome_report.txt — precise fraction coords for each structural element
"""
from __future__ import annotations
import textwrap
from pathlib import Path
import cv2
import numpy as np

ROOT = Path(__file__).parent.parent
SRC  = ROOT / "temp" / "UI_screenshots"
OUT  = ROOT / "temp" / "chrome_cal"
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


# ── helpers ──────────────────────────────────────────────────────────────────

def strong_edges(img_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    filt = cv2.bilateralFilter(gray, 7, 60, 60)
    edges = cv2.Canny(filt, 80, 200)
    k = np.ones((2, 2), np.uint8)
    return cv2.dilate(edges, k, iterations=1)


def cluster(vals: list[float], gap: float = 0.008) -> list[float]:
    """Merge close values into cluster means."""
    vals = sorted(vals)
    groups: list[list[float]] = []
    for v in vals:
        if not groups or v - groups[-1][-1] > gap:
            groups.append([v])
        else:
            groups[-1].append(v)
    return [round(sum(g) / len(g), 4) for g in groups]


def hlines_in_region(common: np.ndarray, y0: float, y1: float,
                     w: int, h: int,
                     min_line_frac: float = 0.20) -> list[float]:
    """Find horizontal lines within y=[y0,y1] of the common edge map."""
    y0px, y1px = int(y0 * h), int(y1 * h)
    roi = common[y0px:y1px, :]
    min_len = int(w * min_line_frac)
    lines = cv2.HoughLinesP(roi, 1, np.pi / 180,
                            threshold=30, minLineLength=min_len, maxLineGap=15)
    if lines is None:
        return []
    ys = []
    for ln in lines:
        x1, y1r, x2, y2r = ln[0]
        if abs(y2r - y1r) < 4:  # horizontal
            ys.append((y0px + (y1r + y2r) / 2) / h)
    return cluster(ys)


def vlines_in_region(common: np.ndarray, x0: float, x1: float,
                     w: int, h: int,
                     min_line_frac: float = 0.10) -> list[float]:
    """Find vertical lines within x=[x0,x1] of the common edge map."""
    x0px, x1px = int(x0 * w), int(x1 * w)
    roi = common[:, x0px:x1px]
    min_len = int(h * min_line_frac)
    lines = cv2.HoughLinesP(roi, 1, np.pi / 180,
                            threshold=30, minLineLength=min_len, maxLineGap=15)
    if lines is None:
        return []
    xs = []
    for ln in lines:
        x1r, y1r, x2r, y2r = ln[0]
        if abs(x2r - x1r) < 4:  # vertical
            xs.append((x0px + (x1r + x2r) / 2) / w)
    return cluster(xs)


def find_strong_horizontal_bands(common: np.ndarray, h: int, w: int,
                                  y_max_frac: float = 0.25) -> list[float]:
    """
    Project edge map onto Y axis (sum per row) → find peaks = row separators.
    More reliable than HoughLinesP for short / broken lines.
    """
    y_max = int(y_max_frac * h)
    roi = common[:y_max, :]
    row_sum = roi.sum(axis=1).astype(float)

    # Normalize
    row_sum /= (w * 255)  # fraction of possible max

    # Find peaks above threshold
    threshold = 0.05
    peak_ys = [i for i in range(1, len(row_sum) - 1)
               if row_sum[i] > threshold
               and row_sum[i] >= row_sum[i - 1]
               and row_sum[i] >= row_sum[i + 1]]

    fracs = [round(y / h, 4) for y in peak_ys]
    return cluster(fracs, gap=0.006)


def find_strong_vertical_bands(common: np.ndarray, h: int, w: int,
                                x_max_frac: float = 0.25) -> list[float]:
    """Project onto X axis — find left-chrome vertical lines."""
    x_max = int(x_max_frac * w)
    roi = common[:, :x_max]
    col_sum = roi.sum(axis=0).astype(float)
    col_sum /= (h * 255)

    threshold = 0.04
    peak_xs = [i for i in range(1, len(col_sum) - 1)
               if col_sum[i] > threshold
               and col_sum[i] >= col_sum[i - 1]
               and col_sum[i] >= col_sum[i + 1]]

    fracs = [round(x / w, 4) for x in peak_xs]
    return cluster(fracs, gap=0.006)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    paths = sorted(SRC.glob("*.png"))
    if not paths:
        print("[error] no screenshots in", SRC)
        return

    imgs: list[np.ndarray] = []
    edge_maps: list[np.ndarray] = []
    keys: list[str] = []

    for p in paths:
        key = SCREEN_MAP.get(p.stem, p.stem)
        img = cv2.imread(str(p))
        if img is None:
            continue
        imgs.append(img)
        edge_maps.append(strong_edges(img))
        keys.append(key)

    h, w = edge_maps[0].shape

    # AND all edge maps → common chrome
    common = edge_maps[0].copy()
    for em in edge_maps[1:]:
        common = cv2.bitwise_and(common, em)

    # ── 1. Header horizontal bands ────────────────────────────────────────────
    h_peaks = find_strong_horizontal_bands(common, h, w, y_max_frac=0.25)
    h_hough = hlines_in_region(common, 0, 0.25, w, h, min_line_frac=0.15)

    # Merge both sources
    all_hy = cluster(h_peaks + h_hough, gap=0.008)
    print(f"\n── Header row separators (y fractions) ──────────────────────────────")
    for y in all_hy:
        print(f"  y = {y:.4f}  ({int(y*h)} px)")

    # ── 2. Left chrome vertical bands ─────────────────────────────────────────
    v_peaks = find_strong_vertical_bands(common, h, w, x_max_frac=0.20)
    v_hough = vlines_in_region(common, 0, 0.20, w, h, min_line_frac=0.10)

    all_vx = cluster(v_peaks + v_hough, gap=0.006)
    print(f"\n── Left chrome column separators (x fractions) ──────────────────────")
    for x in all_vx:
        print(f"  x = {x:.4f}  ({int(x*w)} px)")

    # ── 3. Nav-bar button scan (per screenshot, top 18%) ─────────────────────
    # Look for large rectangular blobs in the top 18% of each image
    print(f"\n── Per-screen nav-bar contour scan (y=0..0.18) ──────────────────────")
    button_hits: dict[str, list[dict]] = {}

    for img, key in zip(imgs, keys):
        roi_h = int(0.18 * h)
        roi = img[:roi_h, :]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        filt = cv2.bilateralFilter(gray, 5, 40, 40)
        edges = cv2.Canny(filt, 60, 160)
        k = np.ones((3, 3), np.uint8)
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k, iterations=2)

        cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        btns = []
        for cnt in cnts:
            area = cv2.contourArea(cnt)
            if area < 100 or area > roi_h * w * 0.5:
                continue
            x, y, bw, bh = cv2.boundingRect(cnt)
            aspect = bw / bh if bh else 0
            if not (0.3 < aspect < 20):
                continue
            btns.append({
                "cx": round((x + bw / 2) / w, 4),
                "cy": round((y + bh / 2) / h, 4),
                "w":  round(bw / w, 4),
                "h":  round(bh / h, 4),
            })
        btns.sort(key=lambda b: b["cx"])
        button_hits[key] = btns

        # Print the leftmost 3 buttons (most likely: 母港 icon + first 2 nav items)
        print(f"  {key}: {[b for b in btns[:4]]}")

    # ── 4. 母港 button — find largest blob in top-left quadrant (x<0.15, y<0.18) ──
    print(f"\n── 母港 top-left button position ────────────────────────────────────")
    matoba_candidates: list[dict] = []

    for img, key in zip(imgs, keys):
        roi_h = int(0.18 * h)
        roi_w = int(0.18 * w)
        roi = img[:roi_h, :roi_w]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        filt = cv2.bilateralFilter(gray, 5, 40, 40)
        edges = cv2.Canny(filt, 60, 160)
        k = np.ones((3, 3), np.uint8)
        closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, k, iterations=2)
        cnts, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        best = None
        for cnt in cnts:
            area = cv2.contourArea(cnt)
            if area < 200:
                continue
            x, y, bw, bh = cv2.boundingRect(cnt)
            if best is None or area > cv2.contourArea(best):
                best = cnt
        if best is not None:
            x, y, bw, bh = cv2.boundingRect(best)
            cx = (x + bw / 2) / w
            cy = (y + bh / 2) / h
            matoba_candidates.append({"key": key, "cx": round(cx, 4), "cy": round(cy, 4),
                                       "w": round(bw / w, 4), "h": round(bh / h, 4)})
            print(f"  {key}: cx={cx:.4f} cy={cy:.4f} w={bw/w:.4f} h={bh/h:.4f}")

    # ── 5. Per-individual-screenshot: full projection analysis ─────────────────
    print(f"\n── Individual screenshot: top-strip H projection + left-strip V projection ──")
    for img, key in zip(imgs[:3], keys[:3]):   # just first 3 for brevity
        gray_img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        e = strong_edges(img)

        # Top strip
        t_roi = e[:int(0.22 * h), :]
        row_s = t_roi.sum(axis=1) / (w * 255)
        top_peaks = [round(i / h, 4) for i in range(1, len(row_s) - 1)
                     if row_s[i] > 0.10 and row_s[i] >= row_s[i-1] and row_s[i] >= row_s[i+1]]
        top_peaks = cluster(top_peaks, gap=0.006)

        # Left strip
        l_roi = e[:, :int(0.18 * w)]
        col_s = l_roi.sum(axis=0) / (h * 255)
        left_peaks = [round(i / w, 4) for i in range(1, len(col_s) - 1)
                      if col_s[i] > 0.06 and col_s[i] >= col_s[i-1] and col_s[i] >= col_s[i+1]]
        left_peaks = cluster(left_peaks, gap=0.005)

        print(f"  {key}: top_h={top_peaks}  left_v={left_peaks}")

    # ── 6. Annotated output image ─────────────────────────────────────────────
    ref = imgs[0].copy()

    # Draw header h-lines in cyan
    for yf in all_hy:
        yp = int(yf * h)
        cv2.line(ref, (0, yp), (w, yp), (0, 220, 220), 2)
        cv2.putText(ref, f"y={yf:.4f}", (w - 110, yp - 3),
                    cv2.FONT_HERSHEY_PLAIN, 0.9, (0, 220, 220), 1, cv2.LINE_AA)

    # Draw left v-lines in green
    for xf in all_vx:
        xp = int(xf * w)
        cv2.line(ref, (xp, 0), (xp, h), (80, 255, 80), 2)
        cv2.putText(ref, f"x={xf:.4f}", (xp + 2, 25),
                    cv2.FONT_HERSHEY_PLAIN, 0.9, (80, 255, 80), 1, cv2.LINE_AA)

    cv2.imwrite(str(OUT / "chrome_lines.png"), ref)

    # Also save top-strip and left-strip zoomed with same annotations
    top_h = int(0.22 * h)
    top_strip = imgs[0][:top_h, :].copy()
    top_big   = cv2.resize(top_strip, (w, top_h * 4), interpolation=cv2.INTER_LINEAR)
    for yf in all_hy:
        if yf <= 0.22:
            yp = int(yf / 0.22 * top_h * 4)
            cv2.line(top_big, (0, yp), (w, yp), (0, 220, 220), 2)
            cv2.putText(top_big, f"y={yf:.4f}", (w - 120, yp - 3),
                        cv2.FONT_HERSHEY_PLAIN, 1.0, (0, 220, 220), 1, cv2.LINE_AA)
    # Per-pixel major grid
    for yf in np.arange(0.02, 0.22, 0.02):
        yp = int(yf / 0.22 * top_h * 4)
        cv2.line(top_big, (0, yp), (w, yp), (60, 60, 60), 1)
        cv2.putText(top_big, f"{yf:.2f}", (3, yp - 2),
                    cv2.FONT_HERSHEY_PLAIN, 0.8, (180, 180, 180), 1)
    cv2.imwrite(str(OUT / "chrome_top_strip.png"), top_big)

    left_w = int(0.18 * w)
    left_strip = imgs[0][:, :left_w].copy()
    left_big   = cv2.resize(left_strip, (left_w * 4, h), interpolation=cv2.INTER_LINEAR)
    for xf in all_vx:
        if xf <= 0.18:
            xp = int(xf / 0.18 * left_w * 4)
            cv2.line(left_big, (xp, 0), (xp, h), (80, 255, 80), 2)
            cv2.putText(left_big, f"x={xf:.4f}", (xp + 2, 20),
                        cv2.FONT_HERSHEY_PLAIN, 1.0, (80, 255, 80), 1, cv2.LINE_AA)
    for xf in np.arange(0.01, 0.18, 0.01):
        xp = int(xf / 0.18 * left_w * 4)
        cv2.line(left_big, (xp, 0), (xp, h), (60, 60, 60), 1)
        cv2.putText(left_big, f"{xf:.2f}", (xp + 1, 35),
                    cv2.FONT_HERSHEY_PLAIN, 0.7, (180, 180, 180), 1)
    cv2.imwrite(str(OUT / "chrome_left_strip.png"), left_big)

    print(f"\n── YAML chrome block (copy to screen_layout.yaml) ───────────────────")
    print(f"  # Detected header row separators:")
    for i, yf in enumerate(all_hy):
        print(f"  #   band_{i}: y_end = {yf:.4f}  ({int(yf*h)} px)")
    print(f"  # Detected left-chrome v-lines:")
    for i, xf in enumerate(all_vx):
        print(f"  #   v_{i}: x = {xf:.4f}  ({int(xf*w)} px)")

    print(f"\nOutput: {OUT}")
    print("  chrome_lines.png       — all detected structure lines on first screenshot")
    print("  chrome_top_strip.png   — top 22% zoomed 4x with detected h-lines")
    print("  chrome_left_strip.png  — left 18% zoomed 4x with detected v-lines")


if __name__ == "__main__":
    main()

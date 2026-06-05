#!/usr/bin/env python3
"""
tools/ui_detect.py  —  KC screen UI highlighter with PS-style hover overlays.

Loads screen_layout.yaml element positions (manually calibrated, correct),
then draws vivid semi-transparent PS-hover highlights over each element.
Also shows a pixel-crop thumbnail panel for each element so you can visually
confirm what the game is actually rendering there.

Usage:
  python tools/ui_detect.py                    # all UI_screenshots/
  python tools/ui_detect.py 母港.png           # single image (stem or path)
  python tools/ui_detect.py 入渠 --clean       # clean old dirs first
"""
from __future__ import annotations

import sys, shutil, math
from pathlib import Path
from typing import List, Tuple, Optional

import yaml
from PIL import Image, ImageDraw, ImageFont

ROOT      = Path(__file__).parent.parent
SRC       = ROOT / "temp" / "UI_screenshots"
OUT       = ROOT / "temp" / "detected"
YAML_PATH = ROOT / "config" / "screen_layout.yaml"

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

# ── Colour palette per element index ─────────────────────────────────────────
COLORS = [
    (255, 210,  30),  # gold
    ( 30, 200, 255),  # cyan
    ( 40, 220, 100),  # green
    (255,  70, 140),  # pink
    (190, 100, 255),  # purple
    ( 50, 130, 255),  # blue
    (255, 155,  40),  # orange
    (100, 255, 200),  # mint
]


def _fonts():
    try:
        sm = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 10)
        md = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 13)
        lg = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 15)
        return sm, md, lg
    except Exception:
        f = ImageFont.load_default()
        return f, f, f


def highlight_image(img_path: Path, screen_key: str, layout: dict) -> Optional[Image.Image]:
    screen = layout["screens"].get(screen_key)
    if not screen:
        print(f"    [skip] no YAML entry for '{screen_key}'")
        return None

    # Merge: common elements first, then screen-specific (screen overrides common)
    common_elems = layout.get("common", {}).get("elements", {})
    screen_elems = screen.get("elements", {})
    elements = {**common_elems, **screen_elems}

    if not elements:
        print(f"    [skip] no elements for '{screen_key}'")
        return None

    img    = Image.open(img_path).convert("RGBA")
    W, H   = img.size
    sm, md, lg = _fonts()

    # ── Layer 1: dim the background slightly so highlights pop ──────────────
    dim = Image.new("RGBA", img.size, (0, 0, 0, 40))
    base = Image.alpha_composite(img, dim)

    ov = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(ov)

    elements_list = list(elements.items())

    for idx, (key, e) in enumerate(elements_list):
        c = COLORS[idx % len(COLORS)]
        label = e.get("label", key)
        shape = e.get("shape", "rect")

        cx_px = e["x"] * W
        cy_px = e["y"] * H
        hw    = e.get("w", 0.08) * W / 2
        hh    = e.get("h", 0.06) * H / 2
        x0 = int(cx_px - hw);  y0 = int(cy_px - hh)
        x1 = int(cx_px + hw);  y1 = int(cy_px + hh)

        fill_a   = 120    # 47% fill opacity
        border_a = 250    # solid border
        glow_a1  = 30
        glow_a2  = 75

        if shape == "circle":
            rad = int(min(hw, hh))
            cx, cy = int(cx_px), int(cy_px)
            # Glow rings
            for exp, a in [(10, glow_a1), (5, glow_a2), (2, glow_a2*2)]:
                draw.ellipse([cx-rad-exp, cy-rad-exp, cx+rad+exp, cy+rad+exp],
                             outline=c+(a,), width=2)
            draw.ellipse([cx-rad, cy-rad, cx+rad, cy+rad],
                         fill=c+(fill_a,), outline=c+(border_a,), width=3)
        else:
            # Glow layers
            for exp, a in [(8, glow_a1), (4, glow_a2)]:
                draw.rectangle([x0-exp, y0-exp, x1+exp, y1+exp],
                               outline=c+(a,), width=1)
            draw.rectangle([x0, y0, x1, y1],
                           fill=c+(fill_a,), outline=c+(border_a,), width=3)

        # Crosshair at center
        cx_i, cy_i = int(cx_px), int(cy_px)
        draw.line([cx_i-10, cy_i, cx_i+10, cy_i], fill=c+(220,), width=2)
        draw.line([cx_i, cy_i-10, cx_i, cy_i+10], fill=c+(220,), width=2)

        # ── Label tag ────────────────────────────────────────────────────────
        # Show label, key, and fractional coords
        line1 = f"{label}  [{key}]"
        line2 = f"({e['x']:.3f}, {e['y']:.3f})  {int(hw*2)}×{int(hh*2)}px"

        bb1 = draw.textbbox((0, 0), line1, font=md)
        bb2 = draw.textbbox((0, 0), line2, font=sm)
        tw = max(bb1[2]-bb1[0], bb2[2]-bb2[0]) + 6
        th = (bb1[3]-bb1[1]) + (bb2[3]-bb2[1]) + 5

        # Position: try inside-top-left, clamp to screen
        lx = max(2, min(x0 + 4, W - tw - 4))
        ly = max(2, min(y0 + 3, H - th - 2))

        # Tag background (solid dark)
        draw.rectangle([lx-2, ly-2, lx+tw+2, ly+th+2], fill=(5, 5, 15, 210))
        # Accent bar on left
        draw.rectangle([lx-2, ly-2, lx, ly+th+2], fill=c+(230,))

        draw.text((lx+2, ly), line1, fill=(255, 255, 255, 255), font=md)
        draw.text((lx+2, ly + (bb1[3]-bb1[1]) + 2), line2, fill=(200, 210, 220, 230), font=sm)

    composite = Image.alpha_composite(base, ov).convert("RGB")

    # ── Thumbnail strip: crop of each element from original screenshot ────────
    thumb_h = 60
    strip   = Image.new("RGB", (W, thumb_h), (12, 14, 20))
    td      = ImageDraw.Draw(strip)

    n = len(elements_list)
    tw_each = max(20, W // max(n, 1))

    for idx, (key, e) in enumerate(elements_list):
        c = COLORS[idx % len(COLORS)]
        label = e.get("label", key)
        cx_px = e["x"] * W;  cy_px = e["y"] * H
        hw    = e.get("w", 0.08) * W / 2
        hh    = e.get("h", 0.06) * H / 2
        x0 = max(0, int(cx_px - hw));  y0 = max(0, int(cy_px - hh))
        x1 = min(W, int(cx_px + hw));  y1 = min(H, int(cy_px + hh))

        if x1 > x0 and y1 > y0:
            crop = composite.crop((x0, y0, x1, y1))
            scale = min((tw_each - 4) / max(crop.width, 1),
                        (thumb_h - 16) / max(crop.height, 1))
            nw = max(1, int(crop.width * scale))
            nh = max(1, int(crop.height * scale))
            thumb = crop.resize((nw, nh), Image.LANCZOS)
            tx = idx * tw_each + (tw_each - nw) // 2
            ty = (thumb_h - nh - 12) // 2
            strip.paste(thumb, (tx, ty))

        # Colour indicator + label below
        tx2 = idx * tw_each + 2
        td.rectangle([tx2, thumb_h-12, tx2+8, thumb_h-4], fill=c)
        td.text((tx2+10, thumb_h-13), label[:8], fill=(200, 210, 220), font=sm)

    # Attach strip below main image
    final = Image.new("RGB", (W, H + thumb_h + 2), (6, 8, 14))
    final.paste(composite, (0, 0))
    final.paste(strip, (0, H + 2))

    # ── Legend bar at top-right ───────────────────────────────────────────────
    ld = ImageDraw.Draw(final)
    lx, ly = W - 200, 6
    ld.rectangle([lx-4, ly-4, W-2, ly + n*16 + 4], fill=(8, 10, 18, 210))
    for i, (key, e) in enumerate(elements_list):
        c = COLORS[i % len(COLORS)]
        ld.rectangle([lx, ly+3, lx+10, ly+12], fill=c+(255,))
        lbl = e.get("label", key)
        ld.text((lx+14, ly), f"{lbl} ({key})", fill=(210, 215, 225), font=sm)
        ly += 16

    return final


def process(img_path: Path, layout: dict) -> None:
    stem = img_path.stem
    screen_key = SCREEN_MAP.get(stem)
    if not screen_key:
        print(f"  [skip] no screen mapping for '{stem}'")
        return

    result = highlight_image(img_path, screen_key, layout)
    if result is None:
        return

    out_path = OUT / f"detected_{stem}.png"
    result.save(out_path)

    H, W = result.size[1], result.size[0]
    n_common = len(layout.get("common", {}).get("elements", {}))
    n_screen = len(layout["screens"].get(screen_key, {}).get("elements", {}))
    print(f"  {img_path.name}  →  {n_common} common + {n_screen} screen = {n_common+n_screen} elements  →  {out_path.name}")


def clean_old() -> None:
    for name in ["annotated", "chrome_overlay", "common_ui", "edge", "grid", "chrome_cal"]:
        p = ROOT / "temp" / name
        if p.exists():
            shutil.rmtree(p)
            print(f"  removed temp/{name}/")


def main() -> None:
    args  = sys.argv[1:]
    clean = "--clean" in args
    args  = [a for a in args if not a.startswith("--")]

    OUT.mkdir(parents=True, exist_ok=True)

    if clean:
        clean_old()

    with open(YAML_PATH) as f:
        layout = yaml.safe_load(f)

    if args:
        p = Path(args[0])
        if not p.exists():
            p = SRC / args[0]
        if not p.exists():
            # try treating arg as screen stem
            candidates = list(SRC.glob(f"*{args[0]}*.png"))
            if candidates:
                p = candidates[0]
        paths = [p]
    else:
        paths = sorted(SRC.glob("*.png"))

    if not paths:
        print("[error] no images"); sys.exit(1)

    for p in paths:
        process(p, layout)

    print(f"\nOutput → {OUT}")


if __name__ == "__main__":
    main()

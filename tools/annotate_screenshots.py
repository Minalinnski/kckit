#!/usr/bin/env python3
"""
Draw YAML overlay boxes/circles onto UI screenshots → temp/annotated_*.png
Usage: python tools/annotate_screenshots.py [screenshot_dir] [screen_name]

If screen_name is given, only annotate matching screenshots.
Mapping of timestamp → screen name is hardcoded below.
"""
from __future__ import annotations

import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import yaml

SCREENSHOT_DIR = ROOT / "temp" / "UI_screenshots"
OUT_DIR = ROOT / "temp" / "annotated"
YAML_PATH = ROOT / "config" / "screen_layout.yaml"

# Map: screenshot filename stem → screen key in YAML
SCREEN_MAP = {
    # New Chinese-named screenshots (current)
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

COLORS = {
    "default": (74, 158, 255, 160),   # blue
    "target":  (255, 204, 0, 200),    # yellow
}


def draw_element(draw: ImageDraw.ImageDraw, e: dict, key: str,
                 w_px: int, h_px: int, color=(74, 158, 255, 160)) -> None:
    cx = e["x"] * w_px
    cy = e["y"] * h_px
    hw = e.get("w", 0.08) * w_px / 2
    hh = e.get("h", 0.06) * h_px / 2
    x0, y0 = cx - hw, cy - hh
    x1, y1 = cx + hw, cy + hh

    shape = e.get("shape", "rect")
    outline = color[:3] + (255,)
    fill = color[:3] + (40,)

    if shape == "circle":
        draw.ellipse([x0, y0, x1, y1], outline=outline, fill=fill, width=3)
    else:
        draw.rectangle([x0, y0, x1, y1], outline=outline, fill=fill, width=2)

    label = e.get("label", key)
    # Small label at top-left of box
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 11)
    except Exception:
        font = ImageFont.load_default()
    tx, ty = x0 + 2, y0 + 1
    draw.rectangle([tx - 1, ty - 1, tx + len(label) * 7, ty + 12],
                   fill=(0, 0, 0, 180))
    draw.text((tx, ty), label, fill=(200, 220, 255, 255), font=font)
    # Center cross
    draw.line([cx - 4, cy, cx + 4, cy], fill=outline, width=1)
    draw.line([cx, cy - 4, cx, cy + 4], fill=outline, width=1)


def annotate(img_path: Path, screen_key: str, layout: dict) -> Path:
    screen = layout["screens"].get(screen_key)
    if not screen:
        print(f"  [skip] screen '{screen_key}' not in YAML")
        return None

    img = Image.open(img_path).convert("RGBA")
    w_px, h_px = img.size

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    elements = screen.get("elements", {})
    for key, elem in elements.items():
        draw_element(draw, elem, key, w_px, h_px)

    # Composite
    out = Image.alpha_composite(img, overlay).convert("RGB")
    out_path = OUT_DIR / f"annotated_{screen_key}_{img_path.name}"
    out.save(out_path)
    print(f"  → {out_path.name}  ({len(elements)} elements, {w_px}×{h_px})")
    return out_path


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(YAML_PATH) as f:
        layout = yaml.safe_load(f)

    filter_screen = sys.argv[1] if len(sys.argv) > 1 else None

    for img_path in sorted(SCREENSHOT_DIR.glob("*.png")):
        screen_key = SCREEN_MAP.get(img_path.stem)
        if not screen_key:
            print(f"[skip] no mapping for {img_path.name}")
            continue
        if filter_screen and screen_key != filter_screen:
            continue
        print(f"{img_path.name} → {screen_key}")
        annotate(img_path, screen_key, layout)

    print("Done.")


if __name__ == "__main__":
    main()

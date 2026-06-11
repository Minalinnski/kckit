"""
Render a labeled contact sheet for a harvested UI atlas (see harvest_atlases.py).

Crops every frame out of data/ui_atlas/raw/<atlas>.png using <atlas>.json and
lays them out in a grid with the frame name printed under each crop. The sheet
is what a vision model (or a human) looks at to assign semantic labels — the
output of that labeling lives in data/ui_atlas/semantics.yaml.

Usage:
  python tools/atlas_sheet.py sally_top              # → temp/atlas_sheets/sally_top.png
  python tools/atlas_sheet.py sally_top sally_jin …  # multiple
  python tools/atlas_sheet.py --all                  # every harvested atlas
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "ui_atlas" / "raw"
OUT_DIR = Path(__file__).resolve().parent.parent / "temp" / "atlas_sheets"

CELL_PAD = 8
LABEL_H = 14
MAX_CELL_W = 220  # downscale very wide frames so the sheet stays compact
BG = (40, 40, 48)
FG = (230, 230, 230)


def build_sheet(atlas: str) -> Path:
    meta = json.loads((RAW_DIR / f"{atlas}.json").read_text())
    img = Image.open(RAW_DIR / f"{atlas}.png").convert("RGBA")
    frames = meta["frames"]

    crops = []
    for name, info in frames.items():
        f = info["frame"]
        crop = img.crop((f["x"], f["y"], f["x"] + f["w"], f["y"] + f["h"]))
        if crop.width > MAX_CELL_W:
            ratio = MAX_CELL_W / crop.width
            crop = crop.resize((MAX_CELL_W, max(1, int(crop.height * ratio))))
        # short label: drop the atlas prefix ("sally_top_12" → "12")
        short = name[len(atlas) + 1:] if name.startswith(atlas + "_") else name
        crops.append((short, crop))

    cols = max(1, int(math.sqrt(len(crops))))
    cell_w = max(c.width for _, c in crops) + CELL_PAD * 2
    cell_h = max(c.height for _, c in crops) + CELL_PAD * 2 + LABEL_H
    rows = math.ceil(len(crops) / cols)

    sheet = Image.new("RGBA", (cols * cell_w, rows * cell_h), BG)
    draw = ImageDraw.Draw(sheet)
    for idx, (short, crop) in enumerate(crops):
        cx = (idx % cols) * cell_w
        cy = (idx // cols) * cell_h
        sheet.paste(crop, (cx + CELL_PAD, cy + CELL_PAD), crop)
        draw.text((cx + CELL_PAD, cy + cell_h - LABEL_H - 2), short, fill=FG)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"{atlas}.png"
    sheet.convert("RGB").save(out)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("atlases", nargs="*", help="atlas names (e.g. sally_top)")
    ap.add_argument("--all", action="store_true", help="all harvested atlases")
    args = ap.parse_args()

    names = args.atlases
    if args.all:
        names = sorted(p.stem for p in RAW_DIR.glob("*.json"))
    if not names:
        ap.error("give atlas names or --all")

    for name in names:
        try:
            out = build_sheet(name)
            n = len(json.loads((RAW_DIR / f"{name}.json").read_text())["frames"])
            print(f"{name}: {n} frames -> {out}")
        except FileNotFoundError:
            print(f"{name}: not harvested yet, skipping", file=sys.stderr)


if __name__ == "__main__":
    main()

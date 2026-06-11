"""
Batch-download KC2 sea-map data straight from the game server — no need to
visit maps in-game.

Per map {world:03d}/{map:02d} the server exposes (no version param needed):
  _info.json   — spots[]: cell no, x/y screen coords, route line vectors;
                 bg sprite list; enemies[] positions
  _image.json  — TexturePacker atlas meta for the map's art
  _image.png   — atlas image (background pieces, cell markers)

Files land under data/maps/raw/<world>/<map>_*.{json,png} mirroring server
paths, plus a merged data/maps/spots.json {"1-1": [...], ...} for direct use
by routing/automation code.

Usage:
  python tools/harvest_maps.py              # worlds 1-7, maps 1-8 (404s skipped)
  python tools/harvest_maps.py --worlds 5 6 # subset
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

POI_BRIDGE_URL = "ws://127.0.0.1:23456"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "maps" / "raw"
SPOTS_PATH = Path(__file__).resolve().parent.parent / "data" / "maps" / "spots.json"
ORIGIN_CACHE = Path(__file__).resolve().parent.parent / "temp" / "kc_origin.txt"
THROTTLE_S = 0.5  # politeness delay between requests — static assets, no hurry


async def _kc_origin() -> str:
    async with websockets.connect(POI_BRIDGE_URL, max_size=4 * 1024 * 1024) as ws:
        await asyncio.wait_for(ws.recv(), timeout=5.0)
        await ws.send(json.dumps({"cmd": "exec_kc2", "code": "location.origin"}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
            except asyncio.TimeoutError:
                continue
            if msg.get("type") == "exec_kc2_result":
                origin = (msg.get("payload") or {}).get("result")
                if isinstance(origin, str) and origin.startswith("http"):
                    return origin
                raise RuntimeError(f"bad origin: {origin}")
    raise TimeoutError("no exec_kc2_result")


def _resolve_origin() -> str:
    """KC2 frame origin via plugin WS (2 tries), falling back to / refreshing
    a local cache file so the tool also works while poi is busy."""
    for _ in range(2):
        try:
            origin = asyncio.run(_kc_origin())
            ORIGIN_CACHE.parent.mkdir(parents=True, exist_ok=True)
            ORIGIN_CACHE.write_text(origin)
            return origin
        except Exception as e:
            print(f"  (origin probe failed: {e})", file=sys.stderr)
    if ORIGIN_CACHE.exists():
        cached = ORIGIN_CACHE.read_text().strip()
        if cached.startswith("http"):
            print(f"  using cached origin {cached}")
            return cached
    raise RuntimeError("cannot determine game origin (is poi running?)")


def _fetch(url: str) -> bytes | None:
    time.sleep(THROTTLE_S)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--worlds", nargs="*", type=int, default=list(range(1, 8)))
    ap.add_argument("--max-map", type=int, default=8)
    args = ap.parse_args()

    origin = _resolve_origin()
    print(f"game origin: {origin}")

    spots_merged: dict[str, list] = {}
    if SPOTS_PATH.exists():
        spots_merged = json.loads(SPOTS_PATH.read_text())

    got, skipped = 0, 0
    for world in args.worlds:
        for mp in range(1, args.max_map + 1):
            stem = f"{world:03d}/{mp:02d}"
            info_url = f"{origin}/kcs2/resources/map/{stem}_info.json"
            dest_dir = OUT_DIR / f"{world:03d}"
            info_dest = dest_dir / f"{mp:02d}_info.json"
            if info_dest.exists():
                skipped += 1
                data = info_dest.read_bytes()
            else:
                data = _fetch(info_url)
                if data is None:
                    continue  # map doesn't exist
                dest_dir.mkdir(parents=True, exist_ok=True)
                info_dest.write_bytes(data)
                for suffix in ("_image.json", "_image.png"):
                    extra = _fetch(f"{origin}/kcs2/resources/map/{stem}{suffix}")
                    if extra is not None:
                        (dest_dir / f"{mp:02d}{suffix}").write_bytes(extra)
                got += 1
                print(f"  + {world}-{mp}")
            try:
                spots_merged[f"{world}-{mp}"] = json.loads(data).get("spots") or []
            except Exception as e:
                print(f"  ! {world}-{mp} spots parse: {e}", file=sys.stderr)

    SPOTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SPOTS_PATH.write_text(json.dumps(spots_merged, ensure_ascii=False, indent=1))
    print(f"done: {got} maps fetched, {skipped} already present; "
          f"{len(spots_merged)} maps in {SPOTS_PATH}")


if __name__ == "__main__":
    main()

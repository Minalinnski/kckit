"""
Harvest KC2 UI sprite atlases from the live game session.

The game lazily loads TexturePacker atlases (json + png) per screen, e.g.
  /kcs2/img/sally/sally_main.json?version=…   (出撃 map select)
  /kcs2/img/battle/battle_main.json?version=…
Their exact versioned URLs sit in the KC2 frame's performance resource buffer.
This tool dumps those URLs and downloads each json+png pair into
data/ui_atlas/raw/, building the raw material for the UI semantic dictionary.

Run it after navigating around in the game — each screen you visit adds its
atlases to the buffer. Re-runs are incremental (skips already-downloaded files).

Usage:
  python tools/harvest_atlases.py            # harvest everything seen so far
  python tools/harvest_atlases.py --list     # just list URLs, no download
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

import websockets

POI_BRIDGE_URL = "ws://127.0.0.1:23456"
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "ui_atlas" / "raw"

_LIST_ATLAS_URLS = r"""
(function(){
  var out = [];
  try {
    var es = performance.getEntriesByType('resource');
    for (var i = 0; i < es.length; i++) {
      var u = es[i].name;
      if (u.indexOf('/kcs2/img/') >= 0 && u.indexOf('.json') >= 0) out.push(u);
    }
  } catch(e) { return {error: String(e)}; }
  return {urls: out};
})()
"""

# Atlases NOT reliably present in the performance buffer (loaded early, buffer
# rolls over). The game server serves them without a version param, so we can
# fetch by path directly. Extend this list when an unknown texture prefix
# shows up in scene dumps (orange boxes in the simulator overlay).
KNOWN_ATLAS_PATHS = [
    "/kcs2/img/remodel/remodel_main.json",
    "/kcs2/img/battle/battle_main.json",
    "/kcs2/img/duty/duty_main.json",
    "/kcs2/img/organize/organize_main.json",
]


async def _exec_kc2(code: str, timeout: float = 10.0) -> dict:
    async with websockets.connect(POI_BRIDGE_URL, max_size=16 * 1024 * 1024) as ws:
        await asyncio.wait_for(ws.recv(), timeout=5.0)  # initial state push
        await ws.send(json.dumps({"cmd": "exec_kc2", "code": code}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("type") == "exec_kc2_result":
                payload = msg.get("payload") or {}
                if "error" in payload:
                    raise RuntimeError(payload["error"])
                return payload.get("result") or {}
    raise TimeoutError("no exec_kc2_result")


def _download(url: str, dest: Path) -> bool:
    if dest.exists():
        return False
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": url.split("/kcs2/")[0] + "/",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        dest.write_bytes(r.read())
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="list URLs only")
    args = ap.parse_args()

    result = asyncio.run(_exec_kc2(_LIST_ATLAS_URLS))
    urls = sorted(set(result.get("urls") or []))
    if not urls:
        print("No atlas URLs in performance buffer "
              "(navigate around in the game first).", file=sys.stderr)
        sys.exit(1)

    # Add known atlases not in the buffer, using the buffer's host
    base = urls[0].split("/kcs2/")[0]
    have = {u.split("/")[-1].split("?")[0] for u in urls}
    for path in KNOWN_ATLAS_PATHS:
        if path.split("/")[-1] not in have:
            urls.append(base + path)

    if args.list:
        for u in urls:
            print(u)
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fetched, skipped, failed = 0, 0, 0
    for ju in urls:
        # /kcs2/img/sally/sally_main.json?version=x → sally_main.json
        name = ju.split("/")[-1].split("?")[0]
        pu = ju.replace(".json", ".png")
        pname = name.replace(".json", ".png")
        for url, fname in ((ju, name), (pu, pname)):
            try:
                if _download(url, OUT_DIR / fname):
                    fetched += 1
                    print(f"  + {fname}")
                else:
                    skipped += 1
            except Exception as e:
                failed += 1
                print(f"  ! {fname}: {e}", file=sys.stderr)
    print(f"done: {fetched} fetched, {skipped} already present, "
          f"{failed} failed -> {OUT_DIR}")


if __name__ == "__main__":
    main()

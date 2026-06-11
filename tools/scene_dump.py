"""
PIXI scene-tree dumper — the core of perception layer v2.

Walks KC2's live PIXI stage and dumps every visible node with:
  - tree path (r.0.1.3…), constructor name, texture cache id (atlas frame name)
  - global bounds in renderer pixels AND canvas fractions (same space as screen_layout.yaml)
  - interactivity flags (interactive/buttonMode), Text content if PIXI.Text

This replaces hand-measured Figma coordinates: element positions are read from
the scene graph at runtime, and screens/overlays are identified by which
texture frames are visible.

Usage:
  python tools/scene_dump.py                 # dump to stdout (summary)
  python tools/scene_dump.py --json out.json # full dump to file
  python tools/scene_dump.py --textures      # only nodes that have a texture id
  python tools/scene_dump.py --interactive   # only interactive nodes
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

POI_BRIDGE_URL = "ws://127.0.0.1:23456"

# Step 1: hook renderer.render() once to capture the stage root + renderer.
# Works for both WebGL and Canvas renderers (KC2 uses one of them depending on env).
_CAPTURE_ROOT = r"""
(function(){
  if (typeof PIXI === 'undefined') return {error: 'no PIXI'};
  function hook(cls){
    if (!cls || !cls.prototype || cls.prototype._kckit_rootcap) return false;
    var o = cls.prototype.render;
    cls.prototype.render = function(root, rt){
      if (!rt && root && !root.parent) {
        window._kckit_root = root;
        window._kckit_renderer = this;
      }
      return o.apply(this, arguments);
    };
    cls.prototype._kckit_rootcap = true;
    return true;
  }
  var w = hook(PIXI.WebGLRenderer), c = hook(PIXI.CanvasRenderer);
  return {ok: true, hookedWebGL: w, hookedCanvas: c,
          alreadyHadRoot: !!window._kckit_root};
})()
"""

# Step 2: walk the captured root.
_WALK_TREE = r"""
(function(){
  var root = window._kckit_root, rd = window._kckit_renderer;
  if (!root) return {error: 'no root captured yet'};
  var RW = (rd && rd.width) || 1200, RH = (rd && rd.height) || 720;
  var out = [], truncated = false;
  function texId(n){
    try {
      var t = n._texture || n.texture;
      if (!t) return null;
      var ids = t.textureCacheIds || [];
      for (var i = 0; i < ids.length; i++) {
        if (ids[i] && ids[i].length < 80) return ids[i];
      }
      if (t.baseTexture && t.baseTexture.imageUrl)
        return '@' + t.baseTexture.imageUrl.split('/').slice(-2).join('/');
      return null;
    } catch(e) { return null; }
  }
  function walk(n, path, depth){
    if (out.length >= 1500) { truncated = true; return; }
    if (!n.visible || n.alpha === 0 || n.renderable === false) return;
    var e = null;
    try {
      var b = n.getBounds();
      // skip zero-size and fully offscreen nodes
      if (b.width >= 1 && b.height >= 1 && b.x < RW && b.y < RH
          && b.x + b.width > 0 && b.y + b.height > 0) {
        e = {
          p: path,
          c: (n.constructor && n.constructor.name) || '?',
          t: texId(n),
          x: Math.round(b.x), y: Math.round(b.y),
          w: Math.round(b.width), h: Math.round(b.height),
          nc: (n.children || []).length
        };
        if (n.interactive) e.i = 1;
        if (n.buttonMode) e.btn = 1;
        if (typeof n.text === 'string' && n.text) e.txt = n.text.slice(0, 60);
        out.push(e);
      }
    } catch(err) {}
    if (depth < 12 && n.children) {
      for (var i = 0; i < n.children.length; i++)
        walk(n.children[i], path + '.' + i, depth + 1);
    }
  }
  walk(root, 'r', 0);
  return {renderer: {w: RW, h: RH}, count: out.length,
          truncated: truncated, nodes: out};
})()
"""


async def _ws_exec(ws, code: str, timeout: float = 10.0) -> dict:
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
                raise RuntimeError(f"exec_kc2 error: {payload['error']}")
            return payload.get("result") or {}
    raise TimeoutError("no exec_kc2_result within timeout")


async def dump_scene(url: str = POI_BRIDGE_URL) -> dict:
    """Capture stage root (if needed) and walk the tree. Returns walk result
    with bounds also normalized to canvas fractions (rx/ry/rw/rh)."""
    async with websockets.connect(url, max_size=16 * 1024 * 1024) as ws:
        await asyncio.wait_for(ws.recv(), timeout=5.0)  # initial state push
        cap = await _ws_exec(ws, _CAPTURE_ROOT)
        if cap.get("error"):
            raise RuntimeError(cap["error"])
        if not cap.get("alreadyHadRoot"):
            await asyncio.sleep(0.3)  # let at least one frame render
        result = await _ws_exec(ws, _WALK_TREE)
    if result.get("error"):
        raise RuntimeError(result["error"])
    rw = result["renderer"]["w"] or 1
    rh = result["renderer"]["h"] or 1
    for n in result["nodes"]:
        n["rx"] = round((n["x"] + n["w"] / 2) / rw, 4)  # center, fraction
        n["ry"] = round((n["y"] + n["h"] / 2) / rh, 4)
        n["rw"] = round(n["w"] / rw, 4)
        n["rh"] = round(n["h"] / rh, 4)
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", metavar="FILE", help="write full dump to FILE")
    ap.add_argument("--textures", action="store_true",
                    help="only show nodes with a texture cache id")
    ap.add_argument("--interactive", action="store_true",
                    help="only show interactive nodes")
    args = ap.parse_args()

    try:
        result = asyncio.run(dump_scene())
    except Exception as e:
        print(f"FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    nodes = result["nodes"]
    if args.textures:
        nodes = [n for n in nodes if n.get("t")]
    if args.interactive:
        nodes = [n for n in nodes if n.get("i")]

    if args.json:
        Path(args.json).write_text(
            json.dumps(result, ensure_ascii=False, indent=1))
        print(f"wrote {len(result['nodes'])} nodes "
              f"(renderer {result['renderer']['w']}x{result['renderer']['h']}, "
              f"truncated={result.get('truncated')}) -> {args.json}")
        return

    print(f"renderer {result['renderer']['w']}x{result['renderer']['h']}, "
          f"{len(nodes)} nodes shown / {result['count']} total")
    for n in nodes:
        flags = ("I" if n.get("i") else "") + ("B" if n.get("btn") else "")
        tex = n.get("t") or ""
        txt = f' "{n["txt"]}"' if n.get("txt") else ""
        print(f'{n["p"]:<28} {n["c"]:<14} {flags:<2} '
              f'({n["rx"]:.3f},{n["ry"]:.3f}) {n["w"]}x{n["h"]} {tex}{txt}')


if __name__ == "__main__":
    main()
